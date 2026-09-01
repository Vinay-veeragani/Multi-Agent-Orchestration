"use client";

import { useEffect, useRef, useState } from "react";

interface StreamedEvent {
  id: string;
  sequence: number | null;
  type: string;
  severity: string;
  node_id: string | null;
  agent_id: string | null;
  message: string;
  created_at: string;
}

const TERMINAL_TYPES = new Set(["execution_completed", "execution_failed", "execution_cancelled"]);

const SEVERITY_DOT: Record<string, string> = {
  error: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-sky-500",
  debug: "bg-neutral-400",
};

export function LiveEvents({ executionId }: { executionId: string }) {
  const [events, setEvents] = useState<StreamedEvent[]>([]);
  const [connection, setConnection] = useState<"connecting" | "live" | "closed">("connecting");
  const seen = useRef(new Set<string>());

  useEffect(() => {
    const source = new EventSource(`/api/stream/${executionId}`);

    source.onopen = () => setConnection("live");

    // A named handler per event type would need one `addEventListener` call
    // per `EventType` (29 of them, see the architecture audit) and miss any
    // added later; the generic `onmessage` only catches the *unnamed*
    // default type, so every message here is instead read through this one
    // catch-all listener keyed off the browser's own `type` field.
    source.addEventListener("message", (raw) => onEvent(raw));
    for (const terminal of TERMINAL_TYPES) {
      source.addEventListener(terminal, (raw) => onEvent(raw));
    }
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) setConnection("closed");
    };

    function onEvent(raw: MessageEvent) {
      const data = JSON.parse(raw.data) as StreamedEvent;
      if (seen.current.has(data.id)) return;
      seen.current.add(data.id);
      setEvents((prev) => [...prev, data]);
      if (TERMINAL_TYPES.has(data.type)) {
        setConnection("closed");
        source.close();
      }
    }

    return () => source.close();
  }, [executionId]);

  return (
    <div className="rounded-lg border border-black/10 dark:border-white/15">
      <div className="flex items-center justify-between border-b border-black/10 px-4 py-2 text-sm dark:border-white/15">
        <span className="font-medium">Live events</span>
        <span className="flex items-center gap-1.5 text-xs text-neutral-500">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              connection === "live"
                ? "bg-emerald-500"
                : connection === "connecting"
                  ? "bg-amber-500"
                  : "bg-neutral-400"
            }`}
          />
          {connection}
        </span>
      </div>
      <ol className="max-h-96 overflow-y-auto divide-y divide-black/5 text-sm dark:divide-white/10">
        {events.length === 0 && (
          <li className="px-4 py-3 text-neutral-500">Waiting for events&hellip;</li>
        )}
        {events.map((event) => (
          <li key={event.id} className="flex items-start gap-2 px-4 py-2">
            <span
              className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                SEVERITY_DOT[event.severity] ?? "bg-neutral-400"
              }`}
            />
            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-mono text-xs text-neutral-500">
                  #{event.sequence ?? "?"}
                </span>
                <span className="font-medium">{event.type}</span>
                {event.node_id && (
                  <span className="text-xs text-neutral-500">node={event.node_id}</span>
                )}
                {event.agent_id && (
                  <span className="text-xs text-neutral-500">agent={event.agent_id}</span>
                )}
              </div>
              {event.message && <p className="text-neutral-600 dark:text-neutral-400">{event.message}</p>}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
