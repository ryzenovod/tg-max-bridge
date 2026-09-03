# Cloud.ru webhook deployment plan

## Outcome

Keep the existing polling runtime for the Mac/systemd deployments and add a
request-driven Telegram webhook runtime for Cloud.ru Container Apps. The cloud
service runs at 0.1 vCPU / 256 MiB with `min_instances=0` and `max_instances=1`,
so low-volume `/max` forwarding fits the standard 25 vCPU-hour / 50 GB-hour
monthly free tier.

## Runtime design

- Add `TELEGRAM_MODE=polling|webhook` (default `polling`).
- In webhook mode require an HTTPS `TELEGRAM_WEBHOOK_URL`, a 1-256 character
  `TELEGRAM_WEBHOOK_SECRET` using Telegram's allowed character set, and listen
  on `0.0.0.0:$PORT`.
- Serve `GET /healthz` and the configured Telegram webhook path with `aiohttp`.
  Compare the `X-Telegram-Bot-Api-Secret-Token` header in constant time, limit
  request size, never log request bodies/headers, and deserialize with
  `Update.de_json`.
- Initialize the PTB `Application` without its updater, call `set_webhook` on
  startup, and leave the webhook installed during normal scale-to-zero shutdown.
- For an accepted `/max` update, enqueue through the existing PTB handler, run
  one serialized `Dispatcher.process_once()`, then read the semantic outbox row.
  Return 2xx only when it is `sent`; otherwise return 503 so Telegram retries and
  wakes the cold service again. Invalid/irrelevant updates return 2xx after normal
  handler processing. Existing markers and ambiguous reconciliation remain the
  delivery dedupe mechanism.
- Do not update `outbox.updated_at` when an already-enqueued source is seen;
  webhook retries must not postpone the ambiguous-resend age forever.

## Persistent state and secrets

- Mount one private Object Storage volume at `/state`, set
  `SQLITE_PATH=/state/bridge.sqlite3`, `HOME=/state/home`, and keep maximum
  instances at one so SQLite/MAX session files have a single writer.
- Seed the MAX session once from secret env `MAX_MCP_SESSION_TARB64`; validate the
  base64/gzip tar in Python, accept only the expected regular session files with
  a small size limit, write them with mode 0600, never overwrite an existing
  persistent session, and remove the environment variable before starting the
  bridge or its `max-mcp` child.
- Run the image as UID 1000 for Cloud.ru volume compatibility. No secret, `.env`,
  session, or real chat ID is stored in Git/GHCR/Artifact Registry.

## Files

- `src/tg_max_bridge/config.py`: conditional webhook settings and validation.
- `src/tg_max_bridge/webhook.py`: bounded/authenticated HTTP server and
  request-to-delivery flow.
- `src/tg_max_bridge/service.py`: select polling/webhook lifecycle and share
  startup/shutdown composition.
- `src/tg_max_bridge/outbox.py`: preserve state transition timestamps on dedupe.
- `src/tg_max_bridge/session_seed.py`: safe one-time persistent MAX-session seed.
- `scripts/docker-entrypoint.sh`: invoke seed helper, unset seed secret, exec.
- `Dockerfile`: UID 1000, cloud listener metadata, existing pinned dependencies.
- `pyproject.toml`, `uv.lock`, `.env.example`, `README.md`: dependency/config and
  staged Cloud.ru deployment/cutover/rollback documentation.
- Tests: settings validation, secret rejection, HTTP auth/body/error behavior,
  synchronous sent-vs-503 semantics, dedupe timestamp behavior, safe session
  extraction, and service cleanup.

## Deployment

1. Build and test linux/amd64 locally.
2. Create a private Cloud.ru Artifact Registry and a short-lived personal key;
   push the image, then revoke/delete the key after upload.
3. Create a private Object Storage bucket and mount it at `/state`.
4. Store Telegram token, webhook secret, and MAX seed archive in Secret
   Management; non-secret allow-list/chat IDs remain ordinary env variables.
5. Create the globally unique service name. Its public URL is
   `https://<service-name>.containerapps.ru`; configure the full webhook URL in
   the first revision, port 8080, min=0, max=1, short idle timeout, long request
   timeout, public endpoint without Cloud.ru browser authentication, and health
   probe `/healthz`.
6. Stop the Mac LaunchAgent immediately before the cloud revision installs the
   Telegram webhook. Verify `/healthz` and `getWebhookInfo`, then have the user
   send one real reply-command test. Roll back by deleting the webhook and
   restarting the LaunchAgent if delivery fails.

## Risks

- Cold MAX startup can be slow; use a request timeout comfortably above the
  configured 30-second MCP delivery timeout and let Telegram retry 503s.
- A mounted object-backed volume and SQLite are safe here only with one instance
  and no overlapping revisions. Stop the service before revisions that change
  state handling.
- The friend-account owner can access the cloud secrets/session. The deployment
  should proceed only with that owner's authorization, and Cloud.ru's promotion
  terms must not be treated as transferable quota.
- The current account shows the standard 25/50 free tier, not the 120/480 partner
  package. Its 4000-bonus grant expires 2026-11-02 and is rollout headroom, not the
  long-term cost model.

## Verification

- `pytest`, Ruff format/check, mypy, packaging and secret scans are green.
- A local container accepts `/healthz`; wrong/missing webhook secrets are 403;
  malformed/oversized payloads are rejected without logging secrets.
- Fake Telegram/MAX integration proves: accepted update + successful MAX send is
  2xx, pending/retry/ambiguous is 503, repeated Telegram delivery sends at most
  once, and restart recovery reconciles uncertain sends.
- Cloud health, Telegram `getWebhookInfo`, Cloud logs, outbox state, and one real
  Telegram-to-MAX test all pass before disabling the local fallback permanently.
