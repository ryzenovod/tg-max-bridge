import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const workerUrl = new URL("../src/worker.js", import.meta.url);
const validEnv = {
  ORIGIN_WEBHOOK_URL:
    "https://tg-max-bridge-5435cb52.containerapps.ru/telegram/webhook",
};

async function loadWorker() {
  await readFile(workerUrl);
  return import(`${workerUrl.href}?cache=${randomUUID()}`);
}

function telegramRequest({
  method = "POST",
  path = "/telegram/webhook",
  body = '{"message":{"text":"#max"}}',
  contentLength,
  contentType = "application/json",
  secret = "test-secret-token",
} = {}) {
  const headers = new Headers();
  if (contentLength !== undefined) {
    headers.set("content-length", contentLength);
  }
  if (contentType !== undefined) {
    headers.set("content-type", contentType);
  }
  if (secret !== null) {
    headers.set("x-telegram-bot-api-secret-token", secret);
  }
  return new Request(`https://relay.example${path}`, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : body,
  });
}

async function withFetchStub(stub, fn) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = stub;
  try {
    return await fn();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("forwards the exact body, content type, and Telegram secret header to the fixed origin", async () => {
  const { default: worker } = await loadWorker();
  const body = JSON.stringify({
    update_id: 123,
    message: { message_id: 456, text: "#max important" },
  });
  const calls = [];

  await withFetchStub(async (url, init = {}) => {
    calls.push({ url, init });
    assert.equal(url, validEnv.ORIGIN_WEBHOOK_URL);
    assert.equal(init.method, "POST");
    assert.equal(init.headers.get("content-type"), "application/json");
    assert.equal(
      init.headers.get("x-telegram-bot-api-secret-token"),
      "test-secret-token",
    );
    assert.equal(init.headers.get("host"), null);
    assert.equal(init.headers.get("cf-connecting-ip"), null);
    assert.equal(init.headers.get("x-forwarded-for"), null);
    assert.equal(await new Response(init.body).text(), body);
    return new Response("sent\n", { status: 200 });
  }, async () => {
    const response = await worker.fetch(telegramRequest({ body }), validEnv, {});
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "sent\n");
  });

  assert.equal(calls.length, 1);
});

test("does not require a Telegram secret Worker env var", async () => {
  const { default: worker } = await loadWorker();
  const envWithoutSecret = { ...validEnv };

  await withFetchStub(async (url, init = {}) => {
    assert.equal(url, validEnv.ORIGIN_WEBHOOK_URL);
    assert.equal(
      init.headers.get("x-telegram-bot-api-secret-token"),
      "test-secret-token",
    );
    return new Response("ok\n", { status: 200 });
  }, async () => {
    const response = await worker.fetch(
      telegramRequest(),
      envWithoutSecret,
      {},
    );
    assert.equal(response.status, 200);
  });
});

test("preserves a successful origin response status, body, and safe content type", async () => {
  const { default: worker } = await loadWorker();

  await withFetchStub(async () => {
    return new Response('{"ok":true}', {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }, async () => {
    const response = await worker.fetch(telegramRequest(), validEnv, {});
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "application/json");
    assert.equal(await response.text(), '{"ok":true}');
  });
});

test("preserves origin 503 so Telegram retries accepted but not-yet-sent updates", async () => {
  const { default: worker } = await loadWorker();

  await withFetchStub(async () => {
    return new Response("queued\n", { status: 503 });
  }, async () => {
    const response = await worker.fetch(telegramRequest(), validEnv, {});
    assert.equal(response.status, 503);
    assert.equal(await response.text(), "queued\n");
  });
});

test("preserves non-retryable origin errors instead of masking them", async () => {
  const { default: worker } = await loadWorker();

  for (const status of [400, 403, 413]) {
    await withFetchStub(async () => {
      return new Response(`origin ${status}\n`, { status });
    }, async () => {
      const response = await worker.fetch(telegramRequest(), validEnv, {});
      assert.equal(response.status, status);
      assert.equal(await response.text(), `origin ${status}\n`);
    });
  }
});

test("rejects wrong methods and paths without contacting the origin", async () => {
  const { default: worker } = await loadWorker();
  let originFetches = 0;

  await withFetchStub(async () => {
    originFetches += 1;
    return new Response("unexpected", { status: 500 });
  }, async () => {
    const wrongMethod = await worker.fetch(
      telegramRequest({ method: "GET" }),
      validEnv,
      {},
    );
    assert.equal(wrongMethod.status, 405);

    const wrongPath = await worker.fetch(
      telegramRequest({ path: "/not-telegram/webhook" }),
      validEnv,
      {},
    );
    assert.equal(wrongPath.status, 404);
  });

  assert.equal(originFetches, 0);
});

test("rejects missing or empty Telegram secret headers without contacting the origin", async () => {
  const { default: worker } = await loadWorker();
  let originFetches = 0;

  await withFetchStub(async () => {
    originFetches += 1;
    return new Response("unexpected", { status: 500 });
  }, async () => {
    for (const secret of [null, "", "   "]) {
      const response = await worker.fetch(
        telegramRequest({ secret }),
        validEnv,
        {},
      );
      assert.equal(response.status, 403);
    }
  });

  assert.equal(originFetches, 0);
});

test("rejects oversized requests by content length without contacting the origin", async () => {
  const { default: worker } = await loadWorker();
  let originFetches = 0;

  await withFetchStub(async () => {
    originFetches += 1;
    return new Response("unexpected", { status: 500 });
  }, async () => {
    const response = await worker.fetch(
      telegramRequest({ contentLength: "1000001" }),
      validEnv,
      {},
    );
    assert.equal(response.status, 413);
  });

  assert.equal(originFetches, 0);
});

test("rejects invalid origin URLs without contacting the origin", async () => {
  const { default: worker } = await loadWorker();
  const invalidEnvs = [
    {},
    { ORIGIN_WEBHOOK_URL: "not a url" },
    { ORIGIN_WEBHOOK_URL: "http://example.com/telegram/webhook" },
    { ORIGIN_WEBHOOK_URL: "https://example.com/wrong/path" },
    { ORIGIN_WEBHOOK_URL: "https://example.com/telegram/webhook/extra" },
  ];
  let originFetches = 0;

  await withFetchStub(async () => {
    originFetches += 1;
    return new Response("unexpected", { status: 500 });
  }, async () => {
    for (const env of invalidEnvs) {
      const response = await worker.fetch(telegramRequest(), env, {});
      assert.equal(response.status, 500);
      assert.match(await response.text(), /invalid origin/i);
    }
  });

  assert.equal(originFetches, 0);
});

test("maps origin network failures to retryable 503 responses", async () => {
  const { default: worker } = await loadWorker();

  await withFetchStub(async () => {
    throw new Error("connect ECONNRESET");
  }, async () => {
    const response = await worker.fetch(telegramRequest(), validEnv, {});
    assert.equal(response.status, 503);
    assert.match(await response.text(), /origin unavailable/i);
  });
});
