# Agent Guide: Hermes Feishu Group Context

Read this file before changing code. `README.md` is the human-facing product and operations guide. This file defines the implementation contracts that agents must preserve.

## Project Purpose

This repository extends Hermes Agent's Feishu/Lark platform with three capabilities:

1. Archive every group message locally before Hermes applies mention gating.
2. Inject archived context only when explicitly needed:
   - the user sends a pure bot mention with no other content; or
   - an addressed message has content that is obviously related to recent chat; or
   - the model calls `feishu_group_history` for a requested recap or time range.
3. Delete expired rows using a bounded, per-group retention policy.

The central optimization is that ordinary unmentioned group chatter must not call the model or consume model tokens.

## Source of Truth

- The repository root is the source of truth.
- `plugin/` is copied into the user's Hermes home during installation.
- The installed copy under `HERMES_HOME/plugins/feishu-context-archive` is generated output. Do not make source-only changes there.
- The Hermes repository itself is an external dependency. Do not vendor it into this repository.

## File Map

| Path | Responsibility |
| --- | --- |
| `plugin/__init__.py` | SQLite archive, settings, hooks, history tool, slash command |
| `plugin/plugin.yaml` | Hermes plugin manifest and version |
| `scripts/patch_adapter.py` | Idempotently inserts three Hook calls into the Hermes Feishu adapter |
| `tests/test_plugin.py` | Unit tests for migration, filtering, commands, and retention |
| `scripts/backfill_history.py` | Imports existing Feishu history through the official API |
| `install.ps1` | Validates paths, backs up files, installs plugin, patches adapter, enables plugin, restarts gateway |
| `README.md` | Human installation, usage, security, recovery, and troubleshooting guide |
| `.env.example` | Placeholder-only environment reference; never add real values |
| `LICENSE` | MIT license selected by the repository owner |

## Technology Stack

Direct project dependencies:

- Python 3.10 or newer.
- Python standard library only:
  - `sqlite3` for local persistence;
  - `urllib` for Feishu history backfill;
  - `json` for settings and API payloads;
  - `zoneinfo` and `datetime` for local range boundaries, with a standard-library UTC+8 fallback when Windows has no IANA timezone database;
  - `pathlib`, `shutil`, and `argparse` for installation helpers.
- SQLite 3 in WAL mode.
- Windows PowerShell 5.1 or PowerShell 7+ for `install.ps1`.
- Hermes Plugin API for hooks, tools, and slash commands.

Runtime capabilities supplied by external systems:

- Hermes Agent supplies the Gateway, session context, tool registry, plugin loader, Feishu adapter, and Python virtual environment.
- Hermes's Feishu platform dependency supplies `lark-oapi` and WebSocket event delivery.
- Feishu Open Platform supplies tenant tokens and message-history APIs.

There is intentionally no:

- Node.js or frontend build;
- `requirements.txt` or additional PyPI dependency;
- ORM;
- external database server;
- Docker requirement;
- standalone daemon besides Hermes Gateway.

## Runtime Data Flow

### Every Feishu group message

1. Feishu emits `im.message.receive_v1`.
2. Hermes Feishu adapter validates event shape and deduplicates `message_id`.
3. Patched adapter invokes `feishu_group_message_received`.
4. Plugin writes the raw message to SQLite using `message_id` as the primary key.
5. Hermes applies its normal group policy and mention gate.
6. If the message is unmentioned, processing stops before the model.

### Pure bot mention

1. The regular archive Hook stores the message.
2. Hermes confirms the bot was mentioned.
3. Mention stripping leaves empty text.
4. Before Hermes drops the empty message, the patched adapter invokes `feishu_group_empty_mention`.
5. Plugin reads the configured number of preceding messages, excluding the current pure mention.
6. Plugin returns synthetic context text.
7. Hermes processes that text as the current addressed turn and replies.

### Addressed message with content

1. The regular archive Hook stores the message and whether it mentioned the bot.
2. After self-mention stripping, the adapter invokes `feishu_group_content_mention`.
3. A count of `0` skips context lookup.
4. The plugin reads the configured number of preceding messages, excluding the current message.
5. If every candidate row mentioned the bot, the plugin returns `None`.
6. If lightweight keyword and follow-up-marker matching finds no obvious relationship, the plugin returns `None`.
7. Otherwise, the plugin prepends optional context while preserving the current addressed text.

### Retention cleanup

1. Retention defaults to 30 days per group.
2. A value of `0` disables automatic deletion.
3. Archive writes opportunistically clean at most once per group every six hours.
4. Changing retention through `/group-context` triggers an immediate cleanup attempt.

### Explicit recap request

1. A normal addressed message enters Hermes.
2. The model sees `feishu_group_history`.
3. The tool may be called only when the user explicitly asks to inspect or summarize group history.
4. The tool reads the selected range from local SQLite.
5. Returned rows are marked as context, not pending requests.

## Behavior Invariants

Do not break these invariants:

1. Unmentioned group messages are archived but never dispatched to the model.
2. Normal addressed messages receive at most their configured small context window.
3. Pure mentions use only messages preceding the mention.
4. Current messages are excluded from both context modes by `message_id`.
5. Archive writes are idempotent by `message_id`.
6. Archive failures must never crash inbound message handling.
7. A context count of zero disables only that context mode.
8. Settings are scoped per Feishu `chat_id`.
9. No real credentials, tokens, chat IDs, user IDs, logs, backups, or databases may be committed.
10. Installer patching must remain idempotent.
11. Existing unrelated Hermes adapter modifications must be preserved.
12. Patch mismatch must fail with an actionable error. Never force a blind replacement.
13. Retention zero must never delete archived rows.
14. Old settings and databases must migrate in place without dropping messages.

## Plugin Contracts

### Hook: `feishu_group_message_received`

Called before Hermes admission filtering.

Expected keyword arguments:

```text
message_id: str
chat_id: str
thread_id: str
sender_id: str
sender_type: "user" | "bot"
msg_type: str
raw_content: str
create_time: int | str | None
mentions_bot: bool
```

The callback returns `None`.

### Hook: `feishu_group_empty_mention`

Called after self-mention stripping produces empty text and before the adapter's empty-text guard.

Expected keyword arguments:

```text
message_id: str
chat_id: str
thread_id: str
```

The callback returns one of:

```python
{"text": "synthetic context and response instruction"}
```

or `None` to preserve Hermes's normal empty-message drop behavior.

### Hook: `feishu_group_content_mention`

Called for a group text message that explicitly mentions the bot and still has
non-command content after self-mention stripping.

Expected keyword arguments:

```text
message_id: str
chat_id: str
thread_id: str
text: str
```

The callback returns replacement text containing optional history and the
unaltered current message, or `None` to process only the current message.

### Tool: `feishu_group_history`

Only valid in a Feishu group session. It uses `gateway.session_context.get_session_env` and must reject other platforms or DM sessions.

Supported ranges:

- `today`
- `yesterday`
- `last_hours`
- `recent`
- `custom`

Bounds:

- `hours`: 1 to 168
- `limit`: 1 to 1000
- `max_chars`: 1000 to 60000

Keep the schema description strict: the model should call the tool only after an explicit request to use group history.

### Slash command: `/group-context`

Supported forms:

```text
/group-context
/group-context status
/group-context pure <0-100>
/group-context content <0-100>
/group-context retention <0-3650>
/group-context set <pure> <content> <days>
/group-context <0-100>
/group-context on
/group-context off
/group-context reset
```

The command is valid only in a Feishu group session.

## Persistent Files

All runtime state is under `HERMES_HOME/archives`.

### Archive database

```text
feishu_group_messages.sqlite3
```

Schema:

```sql
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    sender_id TEXT,
    sender_type TEXT,
    msg_type TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    create_time_ms INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    mentions_bot INTEGER NOT NULL DEFAULT 0
);
```

Index:

```sql
CREATE INDEX idx_messages_chat_time
ON messages(chat_id, create_time_ms);
```

SQLite uses WAL mode and a busy timeout. Preserve both unless a migration justifies changing them.

### Settings

```text
feishu_context_settings.json
```

Shape:

```json
{
  "defaults": {
    "pure_mention_count": 20,
    "content_mention_count": 5,
    "retention_days": 30
  },
  "groups": {
    "oc_example": {
      "pure_mention_count": 30,
      "content_mention_count": 5,
      "retention_days": 30
    }
  }
}
```

The example ID is synthetic. Never replace it with a real ID.

Settings writes use a temporary file followed by replacement. Preserve atomic writes.

## Adapter Patch Rules

`scripts/patch_adapter.py` inserts three independent blocks:

1. Archive Hook before `self._admit(sender, message)`.
2. Pure-mention Hook before the empty-text guard in `_process_inbound_message`.
3. Addressed-content Hook before the same empty-text guard.

Each block has a unique marker:

```text
"feishu_group_message_received"
"feishu_group_empty_mention"
"feishu_group_content_mention"
```

The patcher must:

- detect each feature independently;
- add only missing features;
- require exactly one insertion point for every missing feature;
- back up the adapter before writing;
- preserve all unrelated content;
- remain valid when run repeatedly;
- write UTF-8;
- return nonzero on an incompatible upstream adapter.

Do not replace the entire Hermes adapter with a repository copy.

## Backfill Rules

`scripts/backfill_history.py`:

- reads credentials from `HERMES_HOME/.env`;
- never prints credentials or access tokens;
- obtains a tenant access token at runtime;
- requests messages in ascending creation order;
- follows pagination;
- uses page size 50;
- supports multiple `--chat-id` values;
- supports optional Feishu second-based `start_time` and `end_time`;
- deduplicates through SQLite `message_id`.

Do not add credentials, tokens, or chat IDs to defaults.

## Security Requirements

- Treat the repository as public.
- Never read or commit a user's real `.env`.
- Never commit `*.sqlite3`, WAL/SHM files, logs, screenshots, backups, message exports, or API responses.
- Do not log message bodies in new diagnostic output.
- Do not expose full sender IDs when a suffix is sufficient for model attribution.
- The archive is plaintext. Do not claim encryption.
- Do not claim compliance with any privacy regulation.
- Mention retention and consent implications in human documentation.

## Validation

Use Hermes's Python interpreter when available.

### Syntax

```powershell
<HERMES_HOME>\hermes-agent\venv\Scripts\python.exe -m py_compile `
  .\plugin\__init__.py `
  .\scripts\patch_adapter.py `
  .\scripts\backfill_history.py
```

### Patcher compatibility

```powershell
<HERMES_HOME>\hermes-agent\venv\Scripts\python.exe `
  .\scripts\patch_adapter.py `
  <HERMES_HOME>\hermes-agent\plugins\platforms\feishu\adapter.py `
  --check
```

Expected output is one of:

```text
adapter_status=patchable missing=...
adapter_status=already_patched features=archive,archive_mention_flag,empty_mention,content_mention
```

### Functional checks

At minimum, validate:

1. Legacy settings migrate disabled pure mentions to a count of zero.
2. Legacy databases gain `mentions_bot` without dropping rows.
3. Related addressed messages receive the configured context window.
4. Unrelated context and all-mentioned context are ignored.
5. Both context modes return `None` when configured to zero.
6. Retention 30 deletes rows older than 30 days.
7. Retention zero deletes nothing.
8. `/group-context set 20 5 30` updates all three values.
9. Patched adapter parses as valid Python and each marker appears once.
10. A real unmentioned group message increases the archive count without creating an Agent turn.
11. Real pure and content mentions produce the expected Feishu responses.

Use temporary `HERMES_HOME` paths for synthetic tests. Never modify a user's live archive during unit tests.

## Installation and Release

Before release:

1. Update `plugin/plugin.yaml` version.
2. Update the version shown in `README.md`.
3. Run syntax, patcher, and synthetic functional checks.
4. Scan for secrets and real Feishu identifiers.
5. Confirm no `__pycache__`, database, logs, or backups are tracked.
6. Re-run `install.ps1` against a test or known local Hermes installation.
7. Confirm plugin is enabled and Gateway reconnects to Feishu.
8. Confirm pure mention behavior through Feishu history metadata without publishing message content.

## Change Guidance

- Prefer small changes in `plugin/__init__.py` over broad Hermes core edits.
- Keep adapter modifications limited to Hook invocation.
- Add settings only when they have clear operational value.
- Bound every user-controlled count and output size.
- Keep archive queries indexed by `chat_id` and `create_time_ms`.
- Preserve chronological order in context returned to the model.
- Preserve the distinction between context and user instructions.
- Update both human and agent documentation when behavior changes.

## Known Limitations

- Installer is Windows PowerShell oriented.
- SQLite archive is unencrypted; retention defaults to 30 days and is configurable.
- Sender display uses ID suffixes, not resolved names.
- Pure mentions cannot include future messages.
- Major upstream Hermes Feishu adapter changes may require patch-point updates.
- There is no GUI for archive management.
