## Approach

Fix the production miss by treating Telegram edits as first-class forwarding triggers while keeping the existing durable outbox as the only delivery queue. The screenshot shows a `#max` message marked `edited`, and the current code registers/requests only `message` updates, so a marker added by editing an existing Telegram message can be invisible to the bridge. Add `edited_message` everywhere the bridge declares accepted Telegram update types, let the existing `Update.effective_message` extraction path parse it, and rely on the existing outbox uniqueness key `(tg_chat_id, tg_message_id, max_chat_id)` to avoid duplicate MAX posts from retries or repeated edits. Separately, make the deployment/runbook explicit that the old local macOS polling LaunchAgent must stay stopped/disabled for this bot token, because Telegram long polling disables/competes with the cloud webhook.

## Change list

- `src/tg_max_bridge/telegram_bot.py` - add a module-level public constant for forwarding update types, e.g. `FORWARD_ALLOWED_UPDATES: tuple[str, ...] = ("message", "edited_message")`, and use it from application-facing code/tests instead of scattering `Update.MESSAGE`. No change is needed to `extract_trigger()` or `extract_marker_trigger()` if `Update.effective_message` already resolves edited messages after `Update.de_json()`.
- `src/tg_max_bridge/webhook.py` - change `TelegramWebhook.register_webhook()` to pass `allowed_updates=list(FORWARD_ALLOWED_UPDATES)` so Telegram delivers both original messages and edits to the Cloudflare/Cloud.ru webhook.
- `src/tg_max_bridge/service.py` - change polling mode `_run_polling()` to use the same `FORWARD_ALLOWED_UPDATES` list. This keeps local/manual polling behavior equivalent to webhook mode, while documentation must still say not to run polling with the production token at the same time as cloud webhook.
- `src/tg_max_bridge/cli.py` - change `_discover_telegram()` to use the same allowed-update list, or deliberately leave discovery as `message` only and document that choice in the test. Prefer sharing the constant so future `allowed_updates` changes do not drift.
- `tests/test_webhook.py` - add an `edited_message` fixture/update body and direct receiver coverage proving that an edited group message containing `#max` is accepted, stripped, enqueued, and returns the same `200`/`503` behavior as a normal message. Update the webhook registration assertion to expect `["message", "edited_message"]`.
- `tests/test_service.py` - update polling startup assertions to expect both allowed update types.
- `tests/test_cli.py` - update Telegram discovery assertions if `_discover_telegram()` shares the new constant.
- `tests/test_outbox.py` or existing webhook/outbox dedupe tests - add or extend a test proving that a normal `message` update and a later `edited_message` update for the same `chat_id/message_id/max_chat_id` produce one persistent outbox row, not two MAX deliveries.
- `README.md` - update the Cloud.ru/Cloudflare webhook setup command from `allowed_updates=["message"]` to `allowed_updates=["message","edited_message"]`; add a short operational note: if `#max` is added by editing a Telegram message, it is supported only after this webhook setting is live, and the local `com.ryzenovod.tg-max-bridge` LaunchAgent must remain unloaded/disabled for the production bot token.

## Interfaces

Telegram update-type contract:

```python
FORWARD_ALLOWED_UPDATES: tuple[str, ...] = ("message", "edited_message")
```

- The constant is the single source of truth for webhook registration, polling startup, and optionally Telegram discovery.
- Values are Telegram Bot API update type strings, not enum objects, so `list(FORWARD_ALLOWED_UPDATES)` can be passed directly to `bot.set_webhook(..., allowed_updates=...)`, `Application.updater.start_polling(..., allowed_updates=...)`, and `Bot.get_updates(..., allowed_updates=...)`.
- Ordering is stable: `["message", "edited_message"]`. Tests should assert exact order to catch accidental drift in docs and setup commands.

Extraction contract for edited messages:

- An update with top-level key `edited_message` and an allowed group chat is processed through the same `_accepted_source() -> extract_trigger()/extract_marker_trigger()` path as top-level `message`.
- `extract_marker_trigger()` accepts edited text/caption containing the configured marker, strips the marker from the outbound MAX text, and sets both `TelegramSourceMessage.message_id` and `trigger_message_id` to the edited Telegram message id.
- If an edited message does not contain the marker or valid `/max` inline text, webhook returns `200` and does not enqueue.
- If a message was already queued/sent from the same Telegram `chat_id/message_id` to the same MAX chat, a later edit must not create a second outbox row. The existing DB uniqueness key is the dedupe boundary.

Webhook response contract to preserve:

- Accepted edited updates enqueue before response, exactly like accepted normal message updates.
- If the outbox row is already `sent`, return `200`.
- If the outbox row is `pending`, `sending`, or `ambiguous`, return `503` so Telegram keeps retrying and waking Cloud.ru until MAX delivery is confirmed.
- Invalid secret stays `403`; invalid JSON stays `400`; oversize body stays `413`.

Deployment/runbook contract:

```bash
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -F "url=https://tg-max-bridge-relay.belousov-carp.workers.dev/telegram/webhook" \
  -F "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -F 'allowed_updates=["message","edited_message"]' \
  -F "drop_pending_updates=false"
```

- After setting the webhook, verify `getWebhookInfo.result.url` is the Cloudflare Worker URL and `getWebhookInfo.result.allowed_updates` includes both `message` and `edited_message`.
- The macOS LaunchAgent using the same Telegram bot token must be stopped and disabled before/after webhook registration; otherwise polling can delete or consume cloud webhook updates. The runbook should name the known label/path: `com.ryzenovod.tg-max-bridge` / `~/Library/LaunchAgents/com.ryzenovod.tg-max-bridge.plist`.
- Redeploy Cloud.ru with the code change before setting the new webhook allowed updates, so Telegram does not send edited updates to an old container that lacks test coverage for the path.

## Risks

- Telegram only sends future update types according to the current webhook configuration; a message edited while `allowed_updates=["message"]` was active may not be recoverable from Telegram's pending queue. Acceptance must use a fresh edit/test after updating webhook config.
- If the local LaunchAgent is started again with polling mode and the production bot token, it can still disrupt the cloud webhook. Code changes cannot fully prevent this unless polling is made opt-in or the operator uses a separate dev bot token; for this fix, the minimal control is explicit disablement and verification.
- Immutable outbox rows mean a later edit to an already queued message will not update the MAX text or send a correction. That is intentional minimal behavior for exactly-once forwarding; supporting MAX edits/correction messages would be separate scope.
- Cloud.ru scale-to-zero reliability still depends on preserving the existing `503 until sent` behavior through Cloudflare. This plan must not convert retryable `503` responses to `200`.
- If python-telegram-bot's `MessageHandler` does not invoke handlers for edited messages in polling mode despite `allowed_updates`, implementation may need a separate edited-message handler with the same callback. Webhook direct parsing via `Update.effective_message` remains the primary production path.

## Acceptance criteria

- A fresh Telegram message sent originally as `text #max` is enqueued and delivered to MAX through the existing Cloudflare Worker and Cloud.ru webhook path.
- A Telegram message sent without `#max`, then edited to append `#max`, is enqueued and delivered to MAX.
- Repeated Telegram retries or repeated edits of the same `chat_id/message_id` do not produce duplicate outbox rows or duplicate MAX posts.
- `TelegramWebhook.register_webhook()` calls `set_webhook(..., allowed_updates=["message", "edited_message"], drop_pending_updates=False)`.
- Polling startup uses the same allowed update list.
- README setup commands show `allowed_updates=["message","edited_message"]`.
- Production `getWebhookInfo` after deployment shows the Cloudflare Worker URL and both allowed update types.
- The local LaunchAgent for the same production bot token is not loaded/enabled when the cloud webhook is active.
- Existing tests for normal `/max`, normal `#max`, webhook authorization, outbox retry/503 behavior, Cloudflare relay status preservation, and dispatcher delivery still pass.
