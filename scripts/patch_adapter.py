"""Patch the Hermes Feishu adapter to archive group messages before filtering."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


ARCHIVE_MARKER = '"feishu_group_message_received"'
ARCHIVE_NEEDLE = """        reason = self._admit(sender, message)
        if reason is not None:
"""
ARCHIVE_BLOCK = """        archive_chat_type = getattr(message, "chat_type", "p2p")
        if archive_chat_type != "p2p":
            try:
                from hermes_cli.plugins import invoke_hook

                archive_sender_id = getattr(sender, "sender_id", None)
                archive_sender_primary = (
                    getattr(archive_sender_id, "open_id", None)
                    or getattr(archive_sender_id, "user_id", None)
                    or getattr(archive_sender_id, "union_id", None)
                    or ""
                )
                invoke_hook(
                    "feishu_group_message_received",
                    message_id=str(message_id),
                    chat_id=str(getattr(message, "chat_id", "") or ""),
                    thread_id=str(
                        getattr(message, "thread_id", None)
                        or getattr(message, "root_id", None)
                        or ""
                    ),
                    sender_id=str(archive_sender_primary),
                    sender_type="bot" if _is_bot_sender(sender) else "user",
                    msg_type=str(getattr(message, "message_type", "") or "unknown"),
                    raw_content=str(getattr(message, "content", "") or ""),
                    mentions_bot=bool(self._mentions_self(message)),
                    create_time=getattr(message, "create_time", None),
                )
            except Exception:
                logger.warning(
                    "[Feishu] Failed to archive group message %s",
                    message_id,
                    exc_info=True,
                )

"""
ARCHIVE_MENTION_MARKER = "mentions_bot=bool(self._mentions_self(message))"
ARCHIVE_MENTION_NEEDLE = """                    create_time=getattr(message, "create_time", None),
"""
ARCHIVE_MENTION_BLOCK = """                    mentions_bot=bool(self._mentions_self(message)),
"""
EMPTY_MENTION_MARKER = '"feishu_group_empty_mention"'
EMPTY_MENTION_NEEDLE = """        # Guard runs post-strip so a pure "@Bot" message (stripped to "") is dropped.
        if inbound_type == MessageType.TEXT and not text and not media_urls:
"""
CONTENT_MENTION_MARKER = '"feishu_group_content_mention"'
CONTENT_MENTION_NEEDLE = """        # Guard runs post-strip so a pure "@Bot" message (stripped to "") is dropped.
"""
CONTENT_MENTION_BLOCK = """        if (
            chat_type != "p2p"
            and inbound_type == MessageType.TEXT
            and text
            and not text.startswith("/")
            and self._post_mentions_bot(mentions)
        ):
            try:
                from hermes_cli.plugins import invoke_hook

                content_mention_results = invoke_hook(
                    "feishu_group_content_mention",
                    message_id=str(message_id),
                    chat_id=str(getattr(message, "chat_id", "") or ""),
                    thread_id=str(
                        getattr(message, "thread_id", None)
                        or getattr(message, "root_id", None)
                        or ""
                    ),
                    text=str(text),
                )
                for content_mention_result in content_mention_results:
                    if not isinstance(content_mention_result, dict):
                        continue
                    replacement_text = str(
                        content_mention_result.get("text") or ""
                    ).strip()
                    if replacement_text:
                        text = replacement_text
                        logger.info(
                            "[Feishu] Addressed group message expanded with archived context: id=%s",
                            message_id,
                        )
                        break
            except Exception:
                logger.warning(
                    "[Feishu] Failed to expand addressed group message %s",
                    message_id,
                    exc_info=True,
                )

"""
EMPTY_MENTION_BLOCK = """        if (
            chat_type != "p2p"
            and inbound_type == MessageType.TEXT
            and not text
            and not media_urls
        ):
            try:
                from hermes_cli.plugins import invoke_hook

                empty_mention_results = invoke_hook(
                    "feishu_group_empty_mention",
                    message_id=str(message_id),
                    chat_id=str(getattr(message, "chat_id", "") or ""),
                    thread_id=str(
                        getattr(message, "thread_id", None)
                        or getattr(message, "root_id", None)
                        or ""
                    ),
                )
                for empty_mention_result in empty_mention_results:
                    if not isinstance(empty_mention_result, dict):
                        continue
                    replacement_text = str(empty_mention_result.get("text") or "").strip()
                    if replacement_text:
                        text = replacement_text
                        logger.info(
                            "[Feishu] Pure group mention expanded with archived context: id=%s",
                            message_id,
                        )
                        break
            except Exception:
                logger.warning(
                    "[Feishu] Failed to expand pure group mention %s",
                    message_id,
                    exc_info=True,
                )

"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("adapter", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()

    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")

    adapter = args.adapter.resolve()
    if not adapter.is_file():
        raise SystemExit(f"adapter not found: {adapter}")

    text = adapter.read_text(encoding="utf-8-sig")
    changes: list[tuple[str, str, str]] = []
    if ARCHIVE_MARKER not in text:
        changes.append(("archive", ARCHIVE_NEEDLE, ARCHIVE_BLOCK))
    elif ARCHIVE_MENTION_MARKER not in text:
        changes.append(
            ("archive_mention_flag", ARCHIVE_MENTION_NEEDLE, ARCHIVE_MENTION_BLOCK)
        )
    if EMPTY_MENTION_MARKER not in text:
        changes.append(("empty_mention", EMPTY_MENTION_NEEDLE, EMPTY_MENTION_BLOCK))
    if CONTENT_MENTION_MARKER not in text:
        changes.append(
            ("content_mention", CONTENT_MENTION_NEEDLE, CONTENT_MENTION_BLOCK)
        )

    if not changes:
        print(
            "adapter_status=already_patched "
            "features=archive,archive_mention_flag,empty_mention,content_mention"
        )
        return 0

    for name, needle, _ in changes:
        occurrences = text.count(needle)
        if occurrences != 1:
            raise SystemExit(
                f"{name} patch point mismatch: expected 1 occurrence, found {occurrences}"
            )

    if args.check:
        print("adapter_status=patchable missing=" + ",".join(item[0] for item in changes))
        return 0

    backup_dir = (args.backup_dir or adapter.parent / "archive-backups").resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{adapter.name}.before-context-archive-{stamp}.bak"
    shutil.copy2(adapter, backup)

    patched = text
    for _, needle, block in changes:
        patched = patched.replace(needle, block + needle, 1)
    adapter.write_text(patched, encoding="utf-8")
    print(
        "adapter_status=patched features="
        + ",".join(item[0] for item in changes)
        + f" backup={backup}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
