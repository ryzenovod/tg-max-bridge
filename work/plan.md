## Approach

Build a small standalone Python service in its own `tg-max-bridge` repository that runs a Telegram bot by long polling, accepts only an authorized `/max` or `/max@BotUsername` command that is sent as a reply to an existing group message, stores the selected Telegram message in a durable SQLite outbox, and delivers it to one configured MAX group/channel through one long-lived MCP stdio client process running a separately checked-out `max-mcp` with `uv run --frozen --directory /absolute/path/to/max-mcp max-mcp`. Keep the MVP text-only and deterministic: the source Telegram message id plus target MAX chat id is the semantic dedupe key, every MAX copy contains a stable marker for reconciliation, and ambiguous delivery is reconciled by searching/reading MAX before any resend. User-only setup remains limited to creating or logging into a MAX account on a phone, creating the Telegram bot token via BotFather, adding the MAX account to the existing MAX group/channel, adding the Telegram bot to the Telegram group, and choosing chat ids through the project's discovery CLIs.

## Change list

- `pyproject.toml` - define package `tg-max-bridge`, Python `>=3.12,<3.14`, console script `tg-max-bridge = tg_max_bridge.cli:app`, and minimal dependencies: `python-telegram-bot` for long polling, patched `mcp==1.28.1`, `aiosqlite` for the durable outbox, `pydantic-settings` for env config, `typer` for CLI, and pytest/dev tooling.
- `.gitignore` - ignore `.env`, `.venv/`, `data/`, SQLite files, logs, caches, and any local MAX/Telegram session artifacts; do not ignore `.env.example`.
- `.env.example` - document all required and optional env vars without secrets: Telegram token placeholders, allowed Telegram chat/user ids, MAX chat id, an absolute `max-mcp` directory, SQLite path, retry/backoff tuning, logging, and optional ack mode.
- `src/tg_max_bridge/__init__.py` - package marker and version export only.
- `src/tg_max_bridge/config.py` - load and validate settings from env, including CSV parsing for allowed ids and construction of default MAX MCP command args.
- `src/tg_max_bridge/models.py` - define the small set of dataclasses/enums shared between Telegram intake, outbox, transport, and dispatcher.
- `src/tg_max_bridge/formatting.py` - build the exact MAX text payload and deterministic marker from a Telegram source message; reject non-text/protected messages, annotate omitted attachments, and truncate safely to MAX's 4000-character limit.
- `src/tg_max_bridge/db.py` - open SQLite with WAL, foreign keys, busy timeout, schema initialization, and transaction helpers.
- `src/tg_max_bridge/outbox.py` - implement enqueue/dedupe, due-message leasing, success/failure/ambiguous state transitions, reconciliation scheduling, and status queries on top of SQLite.
- `src/tg_max_bridge/telegram_bot.py` - configure `python-telegram-bot` long polling, parse only reply commands, enforce allowed chat/user ids, enqueue bridge jobs, and avoid broad message access assumptions while Telegram Privacy Mode is enabled.
- `src/tg_max_bridge/max_transport.py` - own the single MCP stdio process/session lifecycle, call `send_message`, call `search_messages` for marker reconciliation, call `list_chats/get_chat` for discovery/doctor, parse both structured MCP results and JSON text content, and shut down gracefully.
- `src/tg_max_bridge/dispatcher.py` - run the delivery loop that leases outbox rows, sends due rows, handles retry/backoff, marks ambiguous cases conservatively, performs reconciliation before any resend, and exits cleanly on cancellation.
- `src/tg_max_bridge/service.py` - compose config, database, Telegram application, MCP transport, dispatcher task, signal handling, and startup/shutdown order for `run`.
- `src/tg_max_bridge/cli.py` - expose `run`, `init-db`, `doctor`, `discover-telegram`, `discover-max`, and `outbox` commands. `doctor` must validate env, SQLite access, local MAX session presence/permissions, MCP startup, and configured `MAX_CHAT_ID` without sending messages.
- `tests/conftest.py` - provide temp SQLite, fake Telegram updates, fake MCP transport, and deterministic time helpers.
- `tests/test_config.py` - cover env parsing, required fields, default MAX MCP args, and invalid id handling.
- `tests/test_formatting.py` - cover marker stability, source text/caption handling, message length behavior, and rejection of media-only messages.
- `tests/test_outbox.py` - cover schema init, semantic dedupe on `(tg_chat_id, tg_message_id, max_chat_id)`, leasing, success, retry, and ambiguous transitions.
- `tests/test_telegram_bot.py` - cover accepted `/max@BotUsername` reply commands, `/max` alias, missing reply rejection, unauthorized users/chats, non-text source rejection, and Privacy Mode-compatible assumptions.
- `tests/test_dispatcher.py` - cover successful send, transient retry/backoff, timeout-to-ambiguous, reconciliation-found-to-sent, and no blind resend before configured ambiguity grace.
- `tests/test_max_transport.py` - use fake MCP sessions to cover command construction, `send_message` result parsing, MCP tool errors, `search_messages` marker parsing, and graceful shutdown.
- `README.md` - document setup end to end: create a MAX account or use the existing account, log in to `max-mcp` by QR/SMS, create the Telegram bot in BotFather with Privacy Mode ON, add bot/account to groups, discover ids, configure `.env`, start service, operate outbox, and explain connectivity/media limitations.
- `Dockerfile` - optional non-root container image with the audited `max-mcp` commit pinned, no secrets baked into the image, and frozen dependency installs.
- `docker-compose.example.yml` - optional hardened deployment wiring using named writable volumes for SQLite and the MAX session.
- `systemd/tg-max-bridge.service` - hardened Linux template using `/opt` for code, `/etc/tg-max-bridge.env` for configuration, a dedicated user, and `/var/lib/tg-max-bridge` for writable state.

## Interfaces

`Settings` contract in `config.py`:

```python
class Settings(BaseSettings):
    telegram_bot_token: SecretStr
    telegram_allowed_chat_ids: set[int]
    telegram_allowed_user_ids: set[int]
    telegram_bot_username: str | None = None
    max_chat_id: int
    max_mcp_directory: Path
    max_mcp_command: str = "uv"
    sqlite_path: Path = Path("data/bridge.sqlite3")
    poll_timeout_seconds: int = 30
    delivery_tick_seconds: float = 1.0
    delivery_timeout_seconds: float = 30.0
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    ambiguous_reconcile_delay_seconds: float = 30.0
    ambiguous_resend_after_seconds: float = 600.0
    max_reconcile_scan_limit: int = 500
    telegram_ack_mode: Literal["never", "errors", "always"] = "errors"
    log_level: str = "INFO"

    def max_mcp_args(self) -> list[str]:
        return ["run", "--no-dev", "--frozen", "--directory", str(self.max_mcp_directory), "max-mcp"]
```

Shared models in `models.py`:

```python
class OutboxStatus(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"

@dataclass(frozen=True)
class TelegramSourceMessage:
    chat_id: int
    message_id: int
    trigger_message_id: int
    from_user_id: int
    from_display_name: str
    text: str
    date: datetime
    attachment_omitted: bool = False

@dataclass(frozen=True)
class MaxPayload:
    chat_id: int
    text: str
    marker: str

@dataclass(frozen=True)
class MaxSendResult:
    message_id: int
    raw: dict[str, Any]
```

Formatting contract in `formatting.py`:

```python
def build_marker(tg_chat_id: int, tg_message_id: int) -> str:
    # returns exactly: "[tg:<tg_chat_id>/<tg_message_id>]"

def build_max_payload(source: TelegramSourceMessage, max_chat_id: int) -> MaxPayload:
    # Includes marker once, source author/display time, and source text.
    # Raises UnsupportedMessageError for empty/media-only source text.
```

SQLite schema contract in `db.py`:

```sql
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_chat_id INTEGER NOT NULL,
  tg_message_id INTEGER NOT NULL,
  tg_trigger_message_id INTEGER NOT NULL,
  tg_from_user_id INTEGER NOT NULL,
  tg_from_display_name TEXT NOT NULL,
  source_text TEXT NOT NULL,
  max_chat_id INTEGER NOT NULL,
  max_text TEXT NOT NULL,
  marker TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL,
  locked_at INTEGER,
  last_error TEXT,
  max_message_id INTEGER,
  max_response_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  sent_at INTEGER,
  UNIQUE (tg_chat_id, tg_message_id, max_chat_id)
);
CREATE INDEX idx_outbox_due ON outbox(status, next_attempt_at, id);
```

Outbox repository contract in `outbox.py`:

```python
class OutboxRepository:
    async def enqueue(self, source: TelegramSourceMessage, payload: MaxPayload) -> EnqueueResult: ...
    async def lease_due(self, *, now_ms: int, limit: int) -> list[OutboxRecord]: ...
    async def mark_sent(self, outbox_id: int, result: MaxSendResult, *, now_ms: int) -> None: ...
    async def mark_retry(self, outbox_id: int, error: str, *, now_ms: int) -> None: ...
    async def mark_ambiguous(self, outbox_id: int, error: str, *, now_ms: int) -> None: ...
    async def release_after_reconciliation_miss(self, outbox_id: int, *, now_ms: int) -> None: ...
    async def get_by_source(self, tg_chat_id: int, tg_message_id: int, max_chat_id: int) -> OutboxRecord | None: ...
```

`enqueue` must be idempotent: an existing row for `(tg_chat_id, tg_message_id, max_chat_id)` is returned and not modified except for `updated_at` if needed for observability.

MAX transport contract in `max_transport.py`:

```python
class MaxTransport:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: int, text: str) -> MaxSendResult: ...
    async def find_marker(self, chat_id: int, marker: str, *, scan_limit: int) -> MaxSendResult | None: ...
    async def list_chats(self, *, limit: int = 100, marker: int | None = None) -> tuple[list[dict[str, Any]], int | None]: ...
    async def get_chat(self, chat_id: int) -> dict[str, Any]: ...
```

Implementation must use:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
```

and call:

```python
await session.initialize()
await session.call_tool("send_message", {"chat_id": chat_id, "text": text})
await session.call_tool("search_messages", {"chat_id": chat_id, "query": marker, "limit": 1, "scan_limit": scan_limit})
await session.call_tool("list_chats", {"limit": limit, "marker": marker})
await session.call_tool("get_chat", {"chat_id": chat_id})
```

Telegram intake contract in `telegram_bot.py`:

```python
def extract_trigger(update: Update, settings: Settings) -> TelegramSourceMessage | RejectReason:
    # Accept only a group/supergroup message whose text command is /max or /max@<bot username>,
    # whose sender and chat are allowed, and whose message.reply_to_message has text or caption.
```

Dispatcher contract in `dispatcher.py`:

```python
class Dispatcher:
    async def run_until_stopped(self) -> None: ...
    async def process_once(self, *, now_ms: int | None = None) -> int: ...
```

`process_once` returns the number of leased rows processed so tests can drive the worker without sleeping.

CLI contract in `cli.py`:

```text
tg-max-bridge init-db
tg-max-bridge doctor
tg-max-bridge discover-telegram [--limit 100]
tg-max-bridge discover-max [--query TEXT] [--limit 100]
tg-max-bridge outbox [--status pending|sending|sent|ambiguous|failed]
tg-max-bridge run
```

`discover-max` prints MAX chat ids, titles, and types only; it never sends messages. `doctor` prints missing setup steps but never prints token values or session contents.

## Risks

- A bridge cannot move messages while both Telegram and MAX are unreachable from the host. For Moscow outage resilience, the recommended runtime target is a stable always-on machine or VPS that can reach both Telegram Bot API and MAX; otherwise the bridge only helps for messages copied before the local outage.
- `max-mcp` uses an unofficial MAX client and a local user session in `~/.max-mcp`; MAX protocol changes or session expiry can stop delivery. `doctor` and systemd restart reduce detection/recovery time but cannot remove this risk.
- Ambiguous delivery is unavoidable when the MCP call times out or the process dies after MAX accepted the send but before the bridge received the response. The marker plus reconciliation-before-resend policy minimizes duplicates, but a rare duplicate with the same marker remains possible.
- Telegram Privacy Mode means the bot should not rely on seeing every group message. The reply command pattern is compatible because Telegram delivers bot commands and includes the replied-to message in the command update, but the tests must encode that assumption.
- MAX group vs channel choice affects permissions. A group is simpler if the reserve account is already a member and allowed to post; a channel is quieter for broadcast but the MAX account must have posting rights. The code should treat both as just `MAX_CHAT_ID`.
- MAX account creation/login cannot be automated by this project: it requires a supported phone/SMS or scanning a QR code from an existing logged-in mobile app. The README must keep this as an explicit user step.

## Acceptance criteria

- With valid `.env`, `uv run tg-max-bridge doctor` confirms SQLite access, MAX session presence/permissions, MCP startup, and `MAX_CHAT_ID` lookup without sending a message.
- With no MAX session, `doctor` fails clearly and points to the frozen `max-mcp-login login-qr` or SMS command for the configured `MAX_MCP_DIRECTORY`.
- `uv run tg-max-bridge discover-max --query <part-of-title>` lists the existing MAX group/channel id after the user has logged in with `max-mcp-login`.
- In a Telegram group with Privacy Mode ON, when an authorized user replies `/max@BotUsername` to a text message, exactly one outbox row is created for `(tg_chat_id, source_tg_message_id, max_chat_id)`.
- Repeating the same reply command against the same source Telegram message does not create a second delivery or a second MAX copy.
- A successful MCP `send_message` stores `status='sent'`, `max_message_id`, `sent_at`, and raw MAX response JSON.
- A transient MCP/tool/network failure schedules retry with capped exponential backoff and survives process restart because state is in SQLite.
- A timeout/process death during send marks the row `ambiguous`; the next processing pass searches MAX for the marker and marks sent if found.
- An ambiguous row is not resent before at least one reconciliation attempt and before `ambiguous_resend_after_seconds` has elapsed.
- Unsupported source messages for the MVP (media-only/no text) are rejected and never inserted into the outbox.
- `pytest` tests cover config, formatting, Telegram intake, outbox idempotency/state transitions, dispatcher success/retry/ambiguous reconciliation, and MCP transport parsing with fakes only; no test requires real Telegram, MAX, secrets, or `~/.max-mcp`.
- README, `.env.example`, Docker example, and systemd template contain no real tokens, phone numbers, MAX session files, or private chat ids.
