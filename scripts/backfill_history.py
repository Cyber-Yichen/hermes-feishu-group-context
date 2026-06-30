"""Backfill existing Feishu group history into the local SQLite archive."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def api_base(env: dict[str, str]) -> str:
    return (
        "https://open.larksuite.com"
        if env.get("FEISHU_DOMAIN", "feishu").strip().lower() == "lark"
        else "https://open.feishu.cn"
    )


def tenant_token(base_url: str, env: dict[str, str]) -> str:
    response = request_json(
        f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
        method="POST",
        payload={
            "app_id": env.get("FEISHU_APP_ID", ""),
            "app_secret": env.get("FEISHU_APP_SECRET", ""),
        },
    )
    if response.get("code") != 0:
        raise RuntimeError(
            f"token request failed: code={response.get('code')} msg={response.get('msg')}"
        )
    token = str(response.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError("token response did not include tenant_access_token")
    return token


def connect_archive(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_chat_time "
        "ON messages(chat_id, create_time_ms)"
    )
    return conn


def archive_items(
    conn: sqlite3.Connection,
    chat_id: str,
    items: list[dict[str, Any]],
) -> int:
    before = conn.total_changes
    for item in items:
        message_id = str(item.get("message_id") or "")
        if not message_id:
            continue
        sender = item.get("sender") or {}
        body = item.get("body") or {}
        conn.execute(
            """
            INSERT INTO messages (
                message_id, chat_id, thread_id, sender_id, sender_type,
                msg_type, raw_content, create_time_ms, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                thread_id=excluded.thread_id,
                sender_id=excluded.sender_id,
                sender_type=excluded.sender_type,
                msg_type=excluded.msg_type,
                raw_content=excluded.raw_content,
                create_time_ms=excluded.create_time_ms
            """,
            (
                message_id,
                chat_id,
                str(item.get("thread_id") or item.get("root_id") or ""),
                str(sender.get("id") or ""),
                str(sender.get("sender_type") or "user"),
                str(item.get("msg_type") or "unknown"),
                str(body.get("content") or ""),
                int(str(item.get("create_time") or "0")),
                datetime.now().astimezone().isoformat(),
            ),
        )
    conn.commit()
    return conn.total_changes - before


def backfill_chat(
    conn: sqlite3.Connection,
    base_url: str,
    token: str,
    chat_id: str,
    *,
    start_time: str = "",
    end_time: str = "",
) -> tuple[int, int]:
    page_token = ""
    pages = 0
    fetched = 0
    while True:
        query: dict[str, Any] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if page_token:
            query["page_token"] = page_token
        if start_time:
            query["start_time"] = start_time
        if end_time:
            query["end_time"] = end_time

        response = request_json(
            f"{base_url}/open-apis/im/v1/messages?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.get("code") != 0:
            raise RuntimeError(
                f"history request failed for {chat_id}: "
                f"code={response.get('code')} msg={response.get('msg')}"
            )

        data = response.get("data") or {}
        items = list(data.get("items") or [])
        archive_items(conn, chat_id, items)
        fetched += len(items)
        pages += 1

        page_token = str(data.get("page_token") or "")
        if not data.get("has_more") or not page_token:
            break
        time.sleep(0.08)
    return pages, fetched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--chat-id", action="append", required=True)
    parser.add_argument("--start-time", default="")
    parser.add_argument("--end-time", default="")
    args = parser.parse_args()

    hermes_home = args.hermes_home.resolve()
    env = load_env(hermes_home / ".env")
    base_url = api_base(env)
    token = tenant_token(base_url, env)
    db_path = hermes_home / "archives" / "feishu_group_messages.sqlite3"

    with closing(connect_archive(db_path)) as conn:
        for chat_id in args.chat_id:
            pages, fetched = backfill_chat(
                conn,
                base_url,
                token,
                chat_id,
                start_time=args.start_time,
                end_time=args.end_time,
            )
            print(f"chat={chat_id} pages={pages} fetched={fetched}")
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"archive={db_path} total_messages={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
