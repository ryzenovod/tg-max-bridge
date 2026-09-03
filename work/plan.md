## Approach

Decouple Telegram webhook latency from MAX delivery, but keep Telegram as the scale-to-zero wake source until delivery is confirmed. A valid `/max` or `#max` webhook must authenticate, parse, validate, build the MAX payload, and durably enqueue/idempotently load the SQLite outbox row before returning. The handler must not call MAX or wait for `Dispatcher.process_once()`. Instead, `run_service()` must run the existing dispatcher loop in webhook mode as the in-process delivery worker. Return `200` only for irrelevant updates, invalid-but-non-retryable content, and accepted rows already marked `sent`; return a fast `503` for accepted rows in `pending`, `sending`, or `ambiguous` so Telegram retries later and wakes a scaled-to-zero app until the durable outbox reaches `sent`. This preserves at-least-once delivery, existing dedupe markers, and recovery from restarts without requiring `min_instances=1`.

## Change list

- `src/tg_max_bridge/service.py` — start `dispatcher.run_until_stopped()` in webhook mode as well as polling mode. Keep `max_instances=1` as the deployment assumption for SQLite safety. On shutdown, keep the current ordering: call `dispatcher.stop()`, set the shared stop event, cancel runtime tasks, gather them, stop the transport, remove signal handlers, and close SQLite. This makes webhook mode a web server plus local outbox worker instead of using Telegram request handlers as the worker.
- `src/tg_max_bridge/webhook.py` — remove synchronous dispatch from `TelegramWebhook.handle_update()`: no delivery lock, no `dispatcher.process_once(close_transport=True)`, and no MAX transport start/stop from the request path. After direct enqueue or PTB processing, read the outbox row by source and return `200` only if it is `sent`; return `503` for every other accepted durable state. Keep malformed/oversized/unauthorized request responses unchanged, and keep irrelevant updates `200`.
- `src/tg_max_bridge/dispatcher.py` — preserve existing `process_once()` and `run_until_stopped()` contracts. Do not add speculative queues or a second persistence layer. If implementation touches cancellation handling, keep cancellation delivery-safe: a row cancelled while `sending` may remain `sending`, because startup `recover_stale_sending()` and stale lease recovery convert it to `ambiguous` before retry/reconcile.
- `src/tg_max_bridge/outbox.py` — preserve the unique key `(tg_chat_id, tg_message_id, max_chat_id)`, marker storage, retry backoff, `ambiguous` reconciliation, stale `sending` lease recovery, and `recover_stale_sending()` startup behavior. No schema change is required.
- `README.md` — update the Cloud.ru webhook section: accepted updates are durable after SQLite enqueue, but the HTTP status remains `503` until MAX delivery is observed as `sent`; this is intentional so Telegram retries act as the external wake mechanism with `min_instances=0`. Remove wording that says the request synchronously performs delivery.
- `tests/test_webhook.py` — update webhook tests so accepted non-sent updates return quickly with `503` and do not call the dispatcher; already-sent duplicate rows return `200`; irrelevant updates return `200`; direct receiver without a Telegram application still enqueues and dedupes through the outbox.
- `tests/test_service.py` — add/update coverage proving webhook mode starts both `telegram-webhook` and `max-dispatcher`, skips Telegram application build when `TELEGRAM_WEBHOOK_AUTO_REGISTER=false`, and shuts down dispatcher/database when either runtime stops or fails.
- `tests/test_dispatcher.py` and `tests/test_outbox.py` — keep existing retry, ambiguous, marker reconciliation, stale `sending`, and close-transport tests. Add only narrow lifecycle coverage if needed to prove the webhook background dispatcher drains due rows without request-path dispatch.

## Interfaces

`TelegramWebhook.handle_update(request: object) -> web.Response`

- `403`: missing or wrong `X-Telegram-Bot-Api-Secret-Token`.
- `413`: request body exceeds `telegram_webhook_max_bytes`.
- `400`: body is not valid JSON object or cannot be parsed as a Telegram update.
- `200`: update is irrelevant to forwarding, unsupported for forwarding, non-retryable after validation, or the matching outbox row is already `OutboxStatus.SENT`.
- `503`: update is a valid forwarding trigger and the row was durably enqueued or already existed, but the matching outbox row status is not `sent`.
- Must not call `Dispatcher.process_once()` and must not start/stop `MaxTransport`.
- Must enqueue before returning `503` for a new accepted trigger. If enqueue raises before commit, let the request fail with a server error so Telegram retries and no accepted work is silently lost.

`run_webhook(application, dispatcher, outbox, settings, stop) -> None`

- Owns only the aiohttp server and optional Telegram webhook registration lifecycle.
- Does not own dispatcher scheduling; it receives the dispatcher only because `TelegramWebhook` currently keeps it in the constructor. That reference becomes inert in the request path and can be removed later if desired.
- `GET /healthz` must remain available without Telegram application startup when `TELEGRAM_WEBHOOK_AUTO_REGISTER=false`.

`run_service(settings: Settings) -> None`

- In polling mode: unchanged, starts polling plus `dispatcher.run_until_stopped()`.
- In webhook mode: starts `run_webhook(...)` plus `dispatcher.run_until_stopped()` concurrently.
- Watches both tasks with `asyncio.wait(..., FIRST_COMPLETED)`. If either exits unexpectedly, stop the whole service and surface the error.
- In `finally`, call `dispatcher.stop()` before cancelling tasks; then stop the transport and close SQLite.

`Dispatcher.run_until_stopped() -> None`

- Continues to loop: `process_once()`, then wait up to `settings.delivery_tick_seconds` or until stopped.
- The dispatcher is the only code path that sends to MAX in webhook mode.
- It may keep the MAX MCP session open while the container is warm; `transport.stop()` closes it on service shutdown.

`OutboxRepository.enqueue(source, payload) -> EnqueueResult`

- Remains idempotent by `(tg_chat_id, tg_message_id, max_chat_id)`.
- `created=True` means the handler durably accepted a new source row.
- `created=False` means a Telegram retry/duplicate loaded the existing row; handler status is based on that row's current `status`.

Outbox status contract:

- `pending`: safe to return fast `503`; dispatcher will lease and send.
- `sending`: safe to return fast `503`; if the container dies, startup recovery marks it `ambiguous`.
- `ambiguous`: safe to return fast `503`; dispatcher reconciles by marker before any resend and only resends after `ambiguous_resend_after_seconds`.
- `sent`: return `200` so Telegram stops retrying that update.
- `failed`: currently unused for terminal delivery; if introduced later, do not return `200` unless the product explicitly accepts dropping that Telegram update.

## Risks

- `200-on-enqueue` is not safe with `min_instances=0`: if MAX/network fails after Telegram receives `200`, there may be no future HTTP event to wake the container for retry.
- Fast `503-until-sent` intentionally makes Telegram retry delivered-but-not-yet-acknowledged updates. The outbox unique key and MAX marker reconciliation are therefore not optional; they are the dedupe boundary.
- Telegram retry windows are finite. This design is sufficient for cold starts, restarts, and ordinary transient failures, but a multi-day MAX outage still needs manual/admin intervention or an external scheduler. Avoid claiming infinite delivery without an always-on worker.
- SQLite on Object Storage remains a low-traffic, single-instance design. Keep `max_instances=1`; raising it risks lock contention and duplicate delivery pressure.
- If the platform kills the container during `send_text()`, the row can remain `sending` until the next startup or stale lease recovery. This is acceptable because it is not lost; it becomes `ambiguous` and is reconciled by marker before resend.
- `TELEGRAM_WEBHOOK_AUTO_REGISTER=true` still requires outbound Telegram Bot API access during startup. For Cloud.ru environments where Bot API is unreliable, keep the documented direct receiver mode: `TELEGRAM_WEBHOOK_AUTO_REGISTER=false` and `TELEGRAM_ACK_MODE=never`.

## Acceptance criteria

- A valid new `/max` or `#max` webhook returns within the request parsing/enqueue budget without starting MAX delivery in the handler.
- A valid new accepted row in `pending`, `sending`, or `ambiguous` returns `503`, causing Telegram to retry later.
- A duplicate webhook for a row already marked `sent` returns `200`, causing Telegram to stop retrying.
- Irrelevant Telegram updates return `200` and do not enqueue or dispatch.
- Webhook mode starts a background `max-dispatcher` task that drains due outbox rows while the container is warm.
- A network/MAX failure marks the row retryable or ambiguous through existing dispatcher semantics; later dispatcher ticks or Telegram retries eventually wake another attempt.
- If the process shuts down after enqueue but before send, the row remains durable and Telegram also still retries because the handler returned `503`.
- If the process shuts down during send, startup `recover_stale_sending()` or stale lease recovery prevents the row from being stranded permanently and forces marker reconciliation before resend.
- Existing dedupe semantics and marker-based reconciliation remain unchanged.
- Test-author can work in parallel with implementer because the return-status, service lifecycle, dispatcher ownership, and outbox contracts above are fixed.
