"use client";

import { useEffect, useRef } from "react";
import type { ExecutionEvent } from "@/lib/api";
import { useExecutionStore } from "@/lib/execution-store";

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
 * Deliberately does not decide "this execution is over" from any event's
 * `type` -- a dynamic execution's supervisor can recover from what the
 * engine itself emits as EXECUTION_FAILED (a round draining with a blocking
 * failure, not the run ending; see the backend's `_looks_finished`), so an
 * `execution_failed` message arriving over the wire does not mean the stream
 * should close. The backend already withholds the *connection close* until
 * it has confirmed the execution is genuinely terminal; this hook mirrors
 * that by checking real status (`/api/executions/{id}/status`) whenever the
 * connection drops, rather than pattern-matching event types itself.
 *
 * Also reconnects itself instead of relying on the browser's built-in
 * EventSource retry: a native retry replays this execution's *entire*
 * Redis-backed backlog every time (the proxy's resume point is an `after_id`
 * query parameter, which a browser reconnect has no way to set), which is
 * wasteful for a long-running execution. Tracking the last-seen SSE id here
 * and reopening with `?after_id=` instead resumes exactly where the stream
 * left off.
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

    async function handleDrop() {
      source?.close();
      if (stopped) return;

      // Ambiguous by construction: a graceful server-side stream end (the
      // execution genuinely finished) and a real network drop both surface
      // as this same error. Ask directly rather than guess.
      try {
        const response = await fetch(`/api/executions/${executionId}/status`, {
          cache: "no-store",
        });
        if (response.ok) {
          const body = (await response.json()) as { isTerminal: boolean };
          if (body.isTerminal) {
            stopped = true;
            setConnection("closed");
            return;
          }
        }
      } catch {
        // Status check itself failed (e.g. this app's own server is down) --
        // fall through to the ordinary reconnect/backoff path below.
      }

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
        void handleDrop();
      };

      source.onmessage = (raw: MessageEvent) => {
        if (raw.lastEventId) lastEventId = raw.lastEventId;
        const event = JSON.parse(raw.data) as ExecutionEvent;
        push(event);
      };
    }

    connect();

    return () => {
      stopped = true;
      if (retryTimer) clearTimeout(retryTimer);
      source?.close();
    };
  }, [executionId, isTerminal, push, reset, seed, setConnection]);
}
