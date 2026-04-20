/* ── API client ────────────────────────────────────────────────────── */

/**
 * JSON fetch wrapper with:
 *  - 30 s default timeout (uses the patched window.fetch from runtime-fixes)
 *  - auto Content-Type: application/json
 *  - response.ok guard with detail / message parsing
 *  - 204 → null
 */
export async function requestJson<T = any>(
  url: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const { timeoutMs, ...rest } = init ?? {};
  const effectiveTimeoutMs =
    typeof timeoutMs === "number" && timeoutMs > 0 ? timeoutMs : 30_000;
  const timeoutSeconds = Math.round(effectiveTimeoutMs / 1000);

  const controller = new AbortController();
  const upstreamSignal = rest.signal;
  let timedOut = false;

  const onAbort = () => controller.abort(upstreamSignal?.reason);
  if (upstreamSignal) {
    if (upstreamSignal.aborted) onAbort();
    else upstreamSignal.addEventListener("abort", onAbort, { once: true });
  }

  const timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, effectiveTimeoutMs);

  let response: Response;
  try {
    response = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(rest.headers as Record<string, string> ?? {}) },
      ...rest,
      signal: controller.signal,
    });
  } catch (err) {
    if (
      timedOut ||
      (err instanceof Error && err.name === "AbortError" && !upstreamSignal?.aborted)
    ) {
      throw new Error(`Request timed out after ${timeoutSeconds} seconds`);
    }
    throw err;
  } finally {
    window.clearTimeout(timeoutId);
    upstreamSignal?.removeEventListener("abort", onAbort);
  }

  if (!response.ok) {
    const payload = await response.text();
    let message = payload;
    if (payload) {
      try {
        const parsed = JSON.parse(payload);
        message = parsed?.detail ?? parsed?.message ?? payload;
      } catch {
        message = payload;
      }
    }
    throw new Error(message || `Request failed: ${response.status}`);
  }

  if (response.status === 204) return null as T;
  return response.json();
}

/** Build a URL with non-empty query params appended. */
export function appendQuery(url: string, params?: Record<string, string | undefined | null>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `${url}?${suffix}` : url;
}
