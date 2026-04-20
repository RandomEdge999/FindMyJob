import { useState, useEffect, useCallback } from "react";
import { requestJson } from "@/lib/api";
import { usePageVisible } from "./use-page-visible";

interface PolledJsonReturn<T> {
  data: T | null;
  error: string;
  loading: boolean;
  refresh: () => Promise<T>;
}

/**
 * Poll a JSON endpoint. Slows to max(interval*3, 15 s) when the tab is hidden.
 */
export function usePolledJson<T = any>(url: string, intervalMs = 5000): PolledJsonReturn<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const visible = usePageVisible();

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const load = async () => {
      try {
        const payload = await requestJson<T>(url);
        if (!active) return;
        setData(payload);
        setError("");
      } catch (err) {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (active) {
          setLoading(false);
          timer = setTimeout(load, visible ? intervalMs : Math.max(intervalMs * 3, 15_000));
        }
      }
    };
    load();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [intervalMs, url, visible]);

  const refresh = useCallback(async () => {
    const payload = await requestJson<T>(url);
    setData(payload);
    setError("");
    setLoading(false);
    return payload;
  }, [url]);

  return { data, error, loading, refresh };
}
