## Approach

Put a very small Cloudflare Worker in front of the existing Cloud.ru webhook and move Telegram's configured webhook URL from `https://*.containerapps.ru/telegram/webhook` to the Worker URL. The Worker must not parse, persist, log, or authenticate Telegram messages; it only accepts `POST /telegram/webhook`, streams the original body to one fixed Cloud.ru origin URL, forwards `X-Telegram-Bot-Api-Secret-Token` unchanged, and returns the Cloud.ru HTTP status/body/selected safe headers unchanged so Telegram keeps its normal retry behavior. Cloud.ru remains the only stateful component: it still authenticates the secret header, writes SQLite outbox rows, returns `503` until MAX delivery is marked `sent`, and returns `200` afterward. This directly addresses the observed Telegram-to-Cloud.ru reachability problem without introducing a second queue, secret copy, or message store.

## Change list

- `worker/cloudflare-relay/src/worker.js` — add a module Worker exporting `default.fetch(request, env, ctx)`. It handles only `POST /telegram/webhook`, rejects other methods/paths locally, validates `env.ORIGIN_WEBHOOK_URL` as an HTTPS URL ending in `/telegram/webhook`, creates a new fixed-origin request with the original method/body and pass-through headers, calls `fetch()` with a short origin timeout, and returns Cloud.ru's status/body. It must not call `request.json()`, must not log request bodies or secret headers, and must not store anything in KV/D1/Cache.
- `worker/cloudflare-relay/test/worker.test.js` — add native `node:test` coverage with a stubbed `fetch`: pass-through secret header and body, Cloud.ru `200` preserved, Cloud.ru `503` preserved, wrong method/path rejected before origin fetch, origin URL validation rejects non-HTTPS/wrong path, and origin network failure maps to a retryable `503`.
- `worker/cloudflare-relay/package.json` — add minimal ESM metadata and scripts: `test` for `node --test test/*.test.js`, and optional `deploy`/`dev` wrappers around `wrangler`. Do not add runtime dependencies.
- `worker/cloudflare-relay/wrangler.toml.example` — add a checked-in example, not a live deployment config with secrets. Include `main = "src/worker.js"`, `compatibility_date`, `workers_dev = true` or route placeholders, and `[vars] ORIGIN_WEBHOOK_URL = "https://tg-max-bridge-5435cb52.containerapps.ru/telegram/webhook"`. No Telegram token or webhook secret belongs here.
- `.gitignore` — if implementation uses local Wrangler state in this repo, ignore only local Worker artifacts such as `worker/cloudflare-relay/.wrangler/` and `worker/cloudflare-relay/node_modules/`. Do not ignore broad directories.
- `README.md` — add a short "Cloudflare Worker relay" subsection under Cloud.ru deployment. Document that Telegram webhook URL should point at the Worker, the Worker origin points at Cloud.ru `/telegram/webhook`, the Worker does not know the secret, and `503` is intentional because it preserves Telegram retries until Cloud.ru reports delivery.
- `src/tg_max_bridge/webhook.py` and `tests/test_webhook.py` — keep the current uncommitted hardening direction: webhook request path enqueues directly even with `TELEGRAM_WEBHOOK_AUTO_REGISTER=true`, does not call `process_update()`, and returns `503` for accepted non-`sent` rows. The Worker plan depends on this contract and must not revert it.

## Interfaces

`worker/cloudflare-relay/src/worker.js`

```js
export default {
  async fetch(request, env, ctx) { ... }
}
```

Worker request contract:

- Accept only `POST` to path `/telegram/webhook`.
- Return `405` for other methods on `/telegram/webhook`.
- Return `404` for other paths.
- Do not parse the Telegram update body. Forward the body stream/bytes exactly once to Cloud.ru.
- Forward `X-Telegram-Bot-Api-Secret-Token` unchanged if present. Do not read it into logs, variables used for diagnostics, or error responses.
- Forward safe request headers needed by origin: at minimum `content-type` and `x-telegram-bot-api-secret-token`. Do not forward client-controlled `host`, `cf-*`, `x-forwarded-*`, or arbitrary hop-by-hop headers.
- Use only a fixed `env.ORIGIN_WEBHOOK_URL`; never derive the upstream host/path from the inbound request.

Worker origin response contract:

- If Cloud.ru returns `200`, Worker returns `200` to Telegram.
- If Cloud.ru returns `503`, Worker returns `503` to Telegram.
- If Cloud.ru returns `400`, `403`, or `413`, Worker preserves that status so bad Telegram deliveries are not masked.
- If the origin fetch times out or throws, Worker returns a retryable `503` with a small plain-text body such as `origin unavailable\n`.
- Do not cache origin responses.

`env.ORIGIN_WEBHOOK_URL`

- Required.
- Must parse as `https:`.
- Must end exactly with `/telegram/webhook`.
- Should be the Cloud.ru endpoint: `https://tg-max-bridge-5435cb52.containerapps.ru/telegram/webhook`.
- Is not a secret; it may live in `wrangler.toml` vars.

Existing Cloud.ru webhook contract to preserve:

- `X-Telegram-Bot-Api-Secret-Token` remains validated only by `TelegramWebhook._authorized()`.
- Accepted `/max` or `#max` messages are durably enqueued in SQLite before returning.
- Accepted rows in `pending`, `sending`, or `ambiguous` return `503`.
- Already `sent` rows return `200`.
- MAX delivery is done only by the background dispatcher, not inside the HTTP request path.

Terminal deployment steps for implementer:

```bash
cd /Users/ryzenovod/tg-max-bridge/worker/cloudflare-relay
npm test
npx wrangler login
npx wrangler deploy --var ORIGIN_WEBHOOK_URL:https://tg-max-bridge-5435cb52.containerapps.ru/telegram/webhook
```

After deploy, set Telegram webhook from a machine where Bot API is reachable:

```bash
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -F "url=https://<worker-name>.<account>.workers.dev/telegram/webhook" \
  -F "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -F 'allowed_updates=["message"]' \
  -F "drop_pending_updates=false"
```

Then verify without printing secrets:

```bash
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" \
  | jq '{ok, url: .result.url, pending_update_count: .result.pending_update_count, last_error_date: .result.last_error_date, last_error_message: .result.last_error_message}'
```

## Risks

- Cloudflare Worker fixes the inbound Telegram-to-Cloud.ru path, but it does not fix Cloud.ru outbound access to Telegram Bot API. That is acceptable only while `TELEGRAM_WEBHOOK_AUTO_REGISTER=false` and `TELEGRAM_ACK_MODE=never`; webhook registration continues to be done externally.
- Worker must preserve Cloud.ru `503`. Converting origin `503` to `200` would silently break the at-least-once wake/retry design.
- Worker must not persist or inspect Telegram bodies. Adding KV/D1/Queues would create a second delivery system and a new privacy/security surface.
- A Worker origin timeout may cause Telegram retries even if Cloud.ru eventually processes the request after the Worker gives up. Existing outbox idempotency by `(tg_chat_id, tg_message_id, max_chat_id)` and MAX marker reconciliation are the required dedupe boundary.
- Cloud.ru SQLite on Object Storage remains single-instance only. Keep Cloud.ru `max_instances=1`; do not scale horizontally to compensate for retry traffic.
- Wrangler deployment may require the user to complete browser login or provide Cloudflare account selection. That is an operator step, not a code design issue.

## Acceptance criteria

- Telegram webhook URL points to the Cloudflare Worker, not directly to `*.containerapps.ru`.
- A test `#max` update reaches Cloud.ru through Worker and appears in MAX after dispatcher delivery.
- Worker test proves an origin `503` is returned to Telegram as `503`.
- Worker test proves an origin `200` is returned to Telegram as `200`.
- Worker test proves `X-Telegram-Bot-Api-Secret-Token` is forwarded unchanged and not required as a Worker env var.
- Worker test proves invalid paths/methods do not contact Cloud.ru.
- Worker test proves origin network failure returns retryable `503`.
- `getWebhookInfo` shows the Worker URL and no continuing `last_error_message` after a successful sent duplicate gets `200`.
- No Telegram bot token, MAX credentials/session, or webhook secret is added to Worker files, Wrangler vars, tests, logs, or README examples.
- Test-author can work in parallel with implementer because the Worker HTTP contract, origin status mapping, env var contract, and existing Cloud.ru webhook contract are fixed.
