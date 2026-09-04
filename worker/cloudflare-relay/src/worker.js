const WEBHOOK_PATH = "/telegram/webhook";
const MAX_REQUEST_BYTES = 1_000_000;
const ORIGIN_TIMEOUT_MS = 25_000;

export default {
  async fetch(request, env) {
    const requestUrl = new URL(request.url);

    if (requestUrl.pathname !== WEBHOOK_PATH) {
      return new Response("Not found", { status: 404 });
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { allow: "POST" },
      });
    }
    if (!hasTelegramSecretToken(request.headers)) {
      return new Response("Forbidden", { status: 403 });
    }
    if (isRequestTooLarge(request.headers)) {
      return new Response("Payload too large", { status: 413 });
    }

    const originUrl = parseOriginWebhookUrl(env.ORIGIN_WEBHOOK_URL);
    if (originUrl === null) {
      return new Response("Invalid origin webhook URL", { status: 500 });
    }

    const controller = new AbortController();
    // Bound origin stalls while still giving Telegram a retryable response.
    const timeout = setTimeout(() => controller.abort(), ORIGIN_TIMEOUT_MS);

    try {
      const originResponse = await fetch(originUrl, {
        method: "POST",
        headers: forwardRequestHeaders(request.headers),
        body: request.body,
        signal: controller.signal,
      });

      return new Response(originResponse.body, {
        status: originResponse.status,
        statusText: originResponse.statusText,
        headers: forwardResponseHeaders(originResponse.headers),
      });
    } catch {
      return new Response("Origin unavailable", { status: 503 });
    } finally {
      clearTimeout(timeout);
    }
  },
};

function hasTelegramSecretToken(headers) {
  const value = headers.get("x-telegram-bot-api-secret-token");
  return typeof value === "string" && value.trim() !== "";
}

function isRequestTooLarge(headers) {
  const value = headers.get("content-length");
  if (value === null) {
    return false;
  }

  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > MAX_REQUEST_BYTES;
}

function parseOriginWebhookUrl(value) {
  if (typeof value !== "string" || value.trim() === "") {
    return null;
  }

  try {
    const url = new URL(value.trim());
    if (
      url.protocol !== "https:" ||
      url.pathname !== WEBHOOK_PATH ||
      url.search !== "" ||
      url.hash !== ""
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

function forwardRequestHeaders(source) {
  const headers = new Headers();
  copyHeader(source, headers, "content-type");
  copyHeader(source, headers, "x-telegram-bot-api-secret-token");
  return headers;
}

function forwardResponseHeaders(source) {
  const headers = new Headers();
  copyHeader(source, headers, "content-type");
  copyHeader(source, headers, "retry-after");
  return headers;
}

function copyHeader(source, destination, name) {
  const value = source.get(name);
  if (value !== null) {
    destination.set(name, value);
  }
}
