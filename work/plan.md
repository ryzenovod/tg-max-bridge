## Approach

Keep the deployed bridge architecture unchanged: Telegram webhook/polling accepts explicit forwarding triggers, writes the source message to the existing SQLite outbox, and lets the existing dispatcher deliver to MAX with marker-based idempotency. The required user behavior is: reply `/max` still forwards the replied message; `/max <text>` forwards the command message itself; a standalone case-insensitive `#max` token in normal text or a media caption forwards that same message with only the marker removed. For the class-group context, marker forwarding is authorized by configured Telegram chat only, not by `TELEGRAM_ALLOWED_USER_IDS`; the reply command remains user-restricted because it can copy someone else's message.

## Change list

- `src/tg_max_bridge/config.py` — keep `telegram_forward_marker: str | None = "#max"`; normalize empty/whitespace env values to `None` so marker forwarding can be disabled with `TELEGRAM_FORWARD_MARKER=`.
- `src/tg_max_bridge/telegram_bot.py` — retain the current `49e5b9a` split between `extract_trigger()` and `extract_marker_trigger()`, but fix marker stripping to use a standalone-token parser rather than `split()`, preserving internal whitespace/newlines. Keep `/max` reply compatibility, `/max <text>`, and `/max@BotUsername <text>`. Add loop protection for marker messages from bot accounts when there is no `sender_chat`, while still allowing anonymous admin/channel-style messages with `sender_chat`. Keep the marker handler chat-filtered and not user-filtered.
- `src/tg_max_bridge/webhook.py` — keep `_accepted_source()` trying command extraction first and marker extraction second so polling and direct webhook mode accept the same messages.
- `tests/test_telegram_bot.py` — cover reply command compatibility, inline command text, addressed command text, marker in text/caption, case-insensitive marker, multiline preservation, non-standalone marker rejection, empty-after-marker rejection, protected content, unconfigured/private chat rejection, marker from any human user in allowed chat, and bot-without-`sender_chat` rejection.
- `tests/test_webhook.py` — cover direct webhook receiver accepting both `/max <text>` and `#max`, preserving existing outbox/dedupe and 2xx/503 semantics.
- `tests/conftest.py` — extend fakes only as needed for `is_bot`, `sender_chat`, protected content, text, and caption.
- `README.md` and `.env.example` — document `TELEGRAM_FORWARD_MARKER=#max`, empty value disables marker forwarding, `/max <text>` works, marker forwarding is chat-authorized, and MAX receives the body without the marker. Preserve unrelated local edits in `.env.example`.

## Interfaces

`Settings.telegram_forward_marker: str | None`

- Default is `"#max"`.
- Env var is `TELEGRAM_FORWARD_MARKER`.
- Validator strips surrounding whitespace and returns `None` for empty values.

`extract_trigger(update: Update, settings: Settings) -> TelegramSourceMessage | RejectReason`

- `/max` or `/max@<configured_bot>` with no extra text forwards `message.reply_to_message`.
- `/max <body>` or `/max@<configured_bot> <body>` forwards the command message itself with `message_id == trigger_message_id`; addressed forms are rejected when `telegram_bot_username` is unknown or does not match.
- Chat must be in `telegram_allowed_chat_ids`. Inline `/max <body>` is allowed
  for any human member of that chat because it forwards only the sender's own
  explicit text. Bare reply `/max` remains restricted to
  `telegram_allowed_user_ids` because it can copy another member's message.

`extract_marker_trigger(update: Update, settings: Settings) -> TelegramSourceMessage | RejectReason`

- Accept only `group`/`supergroup` messages from `telegram_allowed_chat_ids`.
- Do not require `telegram_allowed_user_ids`.
- Use `message.text` first, then `message.caption`.
- Match marker as a standalone whitespace-delimited token, case-insensitively. Must match `#max text`, `text #MAX`, and `text\n#max\nmore`; must not match `foo#max`, `#maximum`, `#max.`, or `/max`.
- Remove all standalone marker tokens, trim only outer whitespace, and preserve internal newlines/spaces.
- Reject protected content and blank body after marker removal.
- Reject `from_user.is_bot is True` when `sender_chat is None`; allow `sender_chat` so anonymous admin posts still work.

`build_application(settings: Settings, outbox: OutboxRepository) -> TelegramApplication`

- Keep `concurrent_updates(False)`.
- Keep the command handler chat-filtered; `extract_trigger()` enforces the
  narrower user allow-list for bare reply `/max`.
- Keep marker handler as `chat_filter` plus text/caption filters, no user filter, silently ignoring rejected marker candidates.

`_accepted_source(update: Update, settings: Settings) -> TelegramSourceMessage | None`

- Return the source polling would enqueue: command first, marker second.
- Return `None` for irrelevant/unsupported updates.
- Build payload only as validation; caller owns enqueue/dispatch.

## Risks

- `49e5b9a` currently strips marker with `split()`, which flattens multiline announcements. This should be fixed before deployment.
- Allowing `#max` from any member of the configured Telegram group can create noise. This is acceptable for a class reserve channel where the Telegram group is the boundary; if MAX is broader or sensitive, add a separate marker allow-list later.
- `#max.` intentionally should not trigger. Users need to put the marker separated by spaces or on its own line.
- Bot-loop protection must not block anonymous admins; reject bot users only when there is no `sender_chat`.
- Cloud.ru Container Apps require deployable images in Artifact Registry in the same project, so GHCR is not a drop-in runtime migration for this Cloud.ru service. It can help CI/build flow, but the final image still needs Cloud.ru Artifact Registry unless hosting changes.
- After 2026-11-02 the 4000-bonus grant expires; the bridge should remain within Cloud.ru Free Tier for Container Apps/Object Storage at low traffic, but Artifact Registry storage remains a tiny pay-as-you-go cost unless old artifacts are pruned aggressively.

## Acceptance criteria

- Existing reply `/max` behavior and authorization remain unchanged.
- `/max Meet at entrance B` from an allowed user in the configured Telegram group forwards `Meet at entrance B`.
- `Meet at entrance B #MAX` from any human member in the configured Telegram group forwards `Meet at entrance B`, with no marker in the MAX payload.
- `Line 1\n#max\nLine 2` forwards without flattening all line breaks.
- `foo#max`, `#maximum`, and `#max.` do not forward.
- Private chats, unconfigured chats, protected messages, blank-after-marker messages, and bot-authored messages without `sender_chat` do not forward.
- Marker forwarding works in both polling and direct Cloud.ru webhook mode.
- Existing webhook contract remains: accepted update returns 2xx only after outbox status is `sent`; retryable/ambiguous delivery returns 503; irrelevant updates return 2xx.
- Test-author can work in parallel with implementer because the public function contracts above are fixed.
