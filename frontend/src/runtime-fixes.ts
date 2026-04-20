/**
 * Browser polyfills loaded before React.
 * - Wraps window.fetch with timeout support (30 s default).
 * - Patches EventSource to track/cleanup listeners on close.
 * - Renders a fallback UI on uncaught errors.
 */
(function () {
  function renderFallback() {
    const root = document.getElementById("root");
    if (!root) return;
    root.innerHTML =
      '<div class="flex min-h-screen items-center justify-center bg-background px-4">' +
      '<div class="max-w-md text-center"><h1 class="text-2xl font-bold text-foreground">Something went wrong</h1>' +
      '<p class="mt-2 text-sm text-muted-foreground">Reload the page to recover the live console.</p></div></div>';
  }

  let fallbackRendered = false;
  function showFallback() {
    if (fallbackRendered) return;
    fallbackRendered = true;
    try { renderFallback(); } catch { /* swallow */ }
  }

  window.addEventListener("error", (event) => { if (event?.error) showFallback(); });
  window.addEventListener("unhandledrejection", () => { showFallback(); });

  /* ---- fetch timeout wrapper ---- */
  if (typeof window.fetch === "function") {
    const nativeFetch = window.fetch.bind(window);
    (window as any).fetch = function (input: RequestInfo | URL, init?: RequestInit & { timeoutMs?: number }) {
      const options = init ?? {};
      const timeoutMs = typeof (options as any).timeoutMs === "number" && (options as any).timeoutMs > 0
        ? (options as any).timeoutMs
        : 30_000;
      const timeoutSeconds = Math.round(timeoutMs / 1000);
      const upstreamSignal = options.signal;
      const controller = new AbortController();
      let timedOut = false;

      const onAbort = () => controller.abort(upstreamSignal?.reason);
      if (upstreamSignal) {
        if (upstreamSignal.aborted) onAbort();
        else upstreamSignal.addEventListener("abort", onAbort, { once: true });
      }

      const timeoutId = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
      const fetchOptions = { ...options, signal: controller.signal };
      delete (fetchOptions as any).timeoutMs;

      return nativeFetch(input, fetchOptions)
        .catch((error: unknown) => {
          if (timedOut || (error instanceof Error && error.name === "AbortError" && !(upstreamSignal?.aborted))) {
            throw new Error("Request timed out after " + timeoutSeconds + " seconds");
          }
          throw error;
        })
        .finally(() => {
          window.clearTimeout(timeoutId);
          if (upstreamSignal) upstreamSignal.removeEventListener("abort", onAbort);
        });
    };
  }

  /* ---- EventSource listener tracking ---- */
  if (typeof window.EventSource === "function") {
    const NativeEventSource = window.EventSource;

    function ManagedEventSource(this: EventSource, url: string | URL, config?: EventSourceInit) {
      const source = new NativeEventSource(url, config);
      const listeners: [string, EventListener, any][] = [];
      const nativeAdd = source.addEventListener.bind(source);
      const nativeRemove = source.removeEventListener.bind(source);
      const nativeClose = source.close.bind(source);

      source.addEventListener = function (type: string, listener: any, options?: any) {
        listeners.push([type, listener, options]);
        return nativeAdd(type, listener, options);
      };
      source.removeEventListener = function (type: string, listener: any, options?: any) {
        for (let i = listeners.length - 1; i >= 0; i--) {
          if (listeners[i][0] === type && listeners[i][1] === listener) listeners.splice(i, 1);
        }
        return nativeRemove(type, listener, options);
      };
      source.close = function () {
        for (const [type, listener, opts] of listeners) {
          try { nativeRemove(type, listener, opts); } catch { /* swallow */ }
        }
        listeners.length = 0;
        source.onopen = null;
        source.onerror = null;
        source.onmessage = null;
        return nativeClose();
      };
      return source;
    }

    ManagedEventSource.prototype = NativeEventSource.prototype;
    (ManagedEventSource as any).CONNECTING = NativeEventSource.CONNECTING;
    (ManagedEventSource as any).OPEN = NativeEventSource.OPEN;
    (ManagedEventSource as any).CLOSED = NativeEventSource.CLOSED;
    (window as any).EventSource = ManagedEventSource;
  }
})();
