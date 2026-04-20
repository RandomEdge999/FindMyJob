import { useState, useEffect, useCallback } from "react";
import { requestJson } from "@/lib/api";
import { usePageVisible } from "./use-page-visible";
import type { OperatorSnapshot, ConnectionState } from "@/lib/types";

interface LiveConsoleReturn {
  snapshot: OperatorSnapshot;
  error: string;
  connection: ConnectionState;
  lastSnapshotAt: number;
  refresh: () => Promise<OperatorSnapshot>;
}

const EMPTY_SNAPSHOT: OperatorSnapshot = { state: null, events: [] };

/**
 * Live console hook: SSE primary channel with HTTP poll fallback.
 *
 * Events: `snapshot`, `update` → full payload; `heartbeat` → keep-alive.
 * Falls back to polling `/api/live/status?limit=60` on SSE error.
 */
export function useLiveConsole(): LiveConsoleReturn {
  const visible = usePageVisible();
  const [snapshot, setSnapshot] = useState<OperatorSnapshot>(EMPTY_SNAPSHOT);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [lastSnapshotAt, setLastSnapshotAt] = useState(Date.now());

  const refresh = useCallback(async () => {
    const payload = await requestJson<OperatorSnapshot>("/api/live/status?limit=60");
    setSnapshot(payload);
    setError("");
    setConnection("connected");
    setLastSnapshotAt(Date.now());
    return payload;
  }, []);

  useEffect(() => {
    let active = true;
    let fallbackTimer: ReturnType<typeof setInterval> | null = null;

    const pull = async () => {
      try {
        const payload = await requestJson<OperatorSnapshot>("/api/live/status?limit=60");
        if (!active) return;
        setSnapshot(payload);
        setError("");
        setConnection("connected");
        setLastSnapshotAt(Date.now());
      } catch (err) {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
        setConnection("reconnecting");
      }
    };

    // Initial HTTP pull
    pull();

    // Open SSE stream
    const stream = new EventSource("/api/live/events?limit=60");

    const handlePayload = (event: MessageEvent) => {
      try {
        const payload: OperatorSnapshot = JSON.parse(event.data);
        if (!active) return;
        setSnapshot(payload);
        setError("");
        setConnection("connected");
        setLastSnapshotAt(Date.now());
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : String(err));
      }
    };
    const handleHeartbeat = () => {
      if (!active) return;
      setConnection("connected");
      setLastSnapshotAt(Date.now());
    };
    const handleOpen = () => { if (active) setConnection("connected"); };
    const handleError = () => {
      if (!active) return;
      setConnection("reconnecting");
      if (!fallbackTimer) {
        fallbackTimer = setInterval(pull, visible ? 5000 : 15_000);
      }
    };

    stream.addEventListener("snapshot", handlePayload);
    stream.addEventListener("update", handlePayload);
    stream.addEventListener("heartbeat", handleHeartbeat);
    stream.onopen = handleOpen;
    stream.onerror = handleError;

    return () => {
      active = false;
      stream.removeEventListener("snapshot", handlePayload);
      stream.removeEventListener("update", handlePayload);
      stream.removeEventListener("heartbeat", handleHeartbeat);
      stream.onopen = null;
      stream.onerror = null;
      stream.close();
      if (fallbackTimer) clearInterval(fallbackTimer);
    };
  }, [visible]);

  return { snapshot, error, connection, lastSnapshotAt, refresh };
}
