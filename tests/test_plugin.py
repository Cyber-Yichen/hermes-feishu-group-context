from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path


SESSION_ENV: dict[str, str] = {}

gateway_module = types.ModuleType("gateway")
session_context_module = types.ModuleType("gateway.session_context")
session_context_module.get_session_env = lambda key, default="": SESSION_ENV.get(
    key, default
)
gateway_module.session_context = session_context_module
sys.modules["gateway"] = gateway_module
sys.modules["gateway.session_context"] = session_context_module

tools_module = types.ModuleType("tools")
registry_module = types.ModuleType("tools.registry")
registry_module.tool_error = lambda message: f"error:{message}"
registry_module.tool_result = lambda **payload: json.dumps(payload, ensure_ascii=False)
tools_module.registry = registry_module
sys.modules["tools"] = tools_module
sys.modules["tools.registry"] = registry_module

PLUGIN_PATH = Path(__file__).parents[1] / "plugin" / "__init__.py"
SPEC = importlib.util.spec_from_file_location("feishu_context_plugin_test", PLUGIN_PATH)
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class PluginTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self.temp_dir.name
        SESSION_ENV.clear()
        SESSION_ENV.update(
            {
                "HERMES_SESSION_PLATFORM": "feishu",
                "HERMES_SESSION_KEY": "feishu:group:oc_test",
                "HERMES_SESSION_CHAT_ID": "oc_test",
            }
        )
        plugin._LAST_RETENTION_CLEANUP_MS.clear()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def archive(
        self,
        message_id: str,
        text: str,
        *,
        minutes_ago: int = 0,
        mentions_bot: bool = False,
    ) -> None:
        timestamp_ms = int(time.time() * 1000) - minutes_ago * 60 * 1000
        plugin._archive_group_message(
            message_id=message_id,
            chat_id="oc_test",
            sender_id=f"sender-{message_id}",
            sender_type="user",
            msg_type="text",
            raw_content=json.dumps({"text": text}, ensure_ascii=False),
            create_time=timestamp_ms,
            mentions_bot=mentions_bot,
        )

    def test_legacy_settings_are_migrated_without_losing_disabled_state(self) -> None:
        path = plugin._settings_path()
        path.write_text(
            json.dumps(
                {
                    "default_count": 18,
                    "groups": {
                        "oc_test": {"enabled": False, "count": 7},
                        "oc_other": {"count": 9},
                    },
                }
            ),
            encoding="utf-8",
        )

        settings = plugin._load_settings()

        self.assertEqual(settings["defaults"]["pure_mention_count"], 18)
        self.assertEqual(settings["groups"]["oc_test"]["pure_mention_count"], 0)
        self.assertEqual(settings["groups"]["oc_other"]["pure_mention_count"], 9)
        self.assertEqual(
            settings["groups"]["oc_test"]["content_mention_count"],
            plugin.DEFAULT_CONTENT_MENTION_CONTEXT_COUNT,
        )
        self.assertEqual(
            settings["groups"]["oc_test"]["retention_days"],
            plugin.DEFAULT_RETENTION_DAYS,
        )

    def test_old_database_schema_gets_mentions_bot_column(self) -> None:
        db_path = plugin._db_path()
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT,
                    sender_id TEXT,
                    sender_type TEXT,
                    msg_type TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    create_time_ms INTEGER NOT NULL,
                    archived_at TEXT NOT NULL
                )
                """
            )

        with closing(plugin._connect()) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }

        self.assertIn("mentions_bot", columns)

    def test_content_mention_adds_related_mixed_context(self) -> None:
        for index, minutes_ago in enumerate(range(6, 0, -1)):
            self.archive(
                f"m{index}",
                f"Hermes 配置上下文 {index}",
                minutes_ago=minutes_ago,
                mentions_bot=index % 2 == 1,
            )
        self.archive("current", "@机器人 Hermes 配置还是报错", mentions_bot=True)

        result = plugin._handle_content_mention(
            chat_id="oc_test",
            message_id="current",
            text="Hermes 配置还是报错",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertNotIn("Hermes 配置上下文 0", result["text"])
        self.assertIn("Hermes 配置上下文 1", result["text"])
        self.assertIn("Hermes 配置上下文 5", result["text"])
        self.assertIn("[Current addressed message]", result["text"])
        self.assertIn("Hermes 配置还是报错", result["text"])

    def test_content_mention_ignores_unrelated_context(self) -> None:
        self.archive("m1", "午饭订两份米饭", minutes_ago=2)
        self.archive("current", "@机器人 检查 Python 环境", mentions_bot=True)

        result = plugin._handle_content_mention(
            chat_id="oc_test",
            message_id="current",
            text="检查 Python 环境",
        )

        self.assertIsNone(result)

    def test_content_mention_ignores_context_when_every_row_mentions_bot(self) -> None:
        for index in range(5, 0, -1):
            self.archive(
                f"m{index}",
                f"@机器人 Hermes 配置问题 {index}",
                minutes_ago=index,
                mentions_bot=True,
            )
        self.archive("current", "@机器人 Hermes 配置继续", mentions_bot=True)

        result = plugin._handle_content_mention(
            chat_id="oc_test",
            message_id="current",
            text="Hermes 配置继续",
        )

        self.assertIsNone(result)

    def test_zero_counts_disable_both_context_modes(self) -> None:
        plugin._command_context_settings("set 0 0 30")
        self.archive("m1", "Hermes 配置", minutes_ago=1)

        pure = plugin._handle_empty_mention(
            chat_id="oc_test",
            message_id="pure-current",
        )
        content = plugin._handle_content_mention(
            chat_id="oc_test",
            message_id="content-current",
            text="Hermes 配置",
        )

        self.assertIsNone(pure)
        self.assertIsNone(content)

    def test_retention_deletes_old_rows_and_zero_keeps_them(self) -> None:
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 31 * 24 * 60 * 60 * 1000
        with closing(plugin._connect()) as conn:
            for message_id, create_time_ms in (
                ("old", old_ms),
                ("new", now_ms),
            ):
                conn.execute(
                    """
                    INSERT INTO messages (
                        message_id, chat_id, thread_id, sender_id, sender_type,
                        msg_type, raw_content, create_time_ms, archived_at, mentions_bot
                    ) VALUES (?, 'oc_test', '', '', 'user', 'text', '{}', ?, '', 0)
                    """,
                    (message_id, create_time_ms),
                )
            conn.commit()

        self.assertEqual(
            plugin._prune_expired_messages("oc_test", 0, now_ms=now_ms),
            0,
        )
        self.assertEqual(
            plugin._prune_expired_messages("oc_test", 30, now_ms=now_ms),
            1,
        )
        with closing(plugin._connect()) as conn:
            remaining = {
                row[0] for row in conn.execute("SELECT message_id FROM messages")
            }
        self.assertEqual(remaining, {"new"})

    def test_slash_command_updates_all_settings(self) -> None:
        response = plugin._command_context_settings("set 12 4 0")
        settings = plugin._group_settings("oc_test")

        self.assertIn("设置已更新", response)
        self.assertEqual(settings["pure_mention_count"], 12)
        self.assertEqual(settings["content_mention_count"], 4)
        self.assertEqual(settings["retention_days"], 0)
        self.assertIn("永久保留", plugin._command_context_settings("status"))


if __name__ == "__main__":
    unittest.main()
