"use client";

import { useEffect, useRef } from "react";
import type { ExecutionEvent } from "@/lib/api";
import { useExecutionStore } from "@/lib/execution-store";

const TERMINAL_TYPES = new Set(["execution_completed", "execution_failed", "execution_cancelled"]);

// Exponential backoff, capped -- a dropped connection retries quickly at
// first (a network blip) and settles into a slow poll rather than hammering
// the API if the outage is long-lived.
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];
const MAX_RECONNECT_DELAY_MS = 15000;

/**
 * Seeds the shared execution store with the server-fetched event history,
 * then -- only for a non-terminal execution -- follows the real SSE
 * endpoint (via the same-origin proxy) so every subscriber (timeline,
 * graph, inspector) reacts to the same live event stream without each
 * opening its own connection.
 *
 * Reconnects itself on a dropped connection rather than relying on the
 * browser's built-in EventSource retry: a native retry replays this
 * execution's *entire* Redis-backed backlog every time (the proxy's resume
 * point is a `after_id` query parameter, which a browser reconnect has no way
 * to set), which is wasteful for a long-running execution and would still
 * leave the UI showing a stale "live" dot while it churns through history.
 * Tracking the last-seen SSE id here and reopening with `?after_id=` instead
 * resumes exactly where the stream left off.
 */
export function useLiveExecution(executionId: string, initialEvents: ExecutionEvent[], isTerminal: boolean) {
  const seed = useExecutionStore((s) => s.seed);
  const push = useExecutionStore((s) => s.push);
  const setConnection = useExecutionStore((s) => s.setConnection);
  const reset = useExecutionStore((s) => s.reset);

  // A ref, not an effect dependency: `initialEvents` is a fresh array
  // identity every render, but it should only be read once, at the moment
  // this hook (re)connects for a given execution.
  const initialEventsRef = useRef(initialEvents);
  useEffect(() => {
    initialEventsRef.current = initialEvents;
  });

  useEffect(() => {
    reset();
    seed(initialEventsRef.current, {});

    if (isTerminal) {
      setConnection("closed");
      return;
    }

    let stopped = false;
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let lastEventId = "";

    function scheduleReconnect() {
      if (stopped) return;
      setConnection("reconnecting");
      const delay = RECONNECT_DELAYS_MS[attempt] ?? MAX_RECONNECT_DELAY_MS;
      attempt += 1;
      retryTimer = setTimeout(connect, delay);
    }

    function connect() {
      if (stopped) return;
      setConnection(attempt === 0 ? "connecting" : "reconnecting");
      const url = lastEventId
        ? `/api/stream/${executionId}?after_id=${encodeURIComponent(lastEventId)}`
        : `/api/stream/${executionId}`;
      source = new EventSource(url);

      source.onopen = () => {
        attempt = 0;
        setConnection("live");
      };

      source.onerror = () => {
        source?.close();
        scheduleReconnect();
      };

      function onMessage(raw: MessageEvent) {
        if (raw.lastEventId) lastEventId = raw.lastEventId;
        const event = JSON.parse(raw.data) as ExecutionEvent;
        push(event);
        if (TERMINAL_TYPES.has(event.type)) {
          stopped = true;
          setConnection("closed");
          source?.close();
        }
      }
      source.addEventListener("message", onMessage);
      for (const terminal of TERMINAL_TYPES) source.addEventListener(terminal, onMessage);
    }

    connect();

    return () => {
      stopped = true;
      if (retryTimer) clearTimeout(retryTimer);
      source?.close();
    };
  }, [executionId, isTerminal, push, reset, seed, setConnection]);
}
