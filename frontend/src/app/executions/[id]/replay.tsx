"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ExecutionEvent } from "@/lib/api";

// node_id lives on the event itself for every node-lifecycle event type (see
// WorkflowExecutor's emit() calls), so status can be reconstructed purely
// from (event.type, event.node_id) -- no payload parsing needed.
const NODE_STATUS_FOR_EVENT: Record<string, string> = {
  node_started: "running",
  node_completed: "succeeded",
  node_failed: "failed",
  node_skipped: "skipped",
  retry_exhausted: "failed",
};

const STATUS_DOT: Record<string, string> = {
  pending: "bg-neutral-300 dark:bg-neutral-600",
  running: "bg-sky-500 animate-pulse",
  succeeded: "bg-emerald-500",
  failed: "bg-red-500",
  skipped: "bg-neutral-400",
};

const STEP_MS = 350;

export function Replay({ events, nodeIds }: { events: ExecutionEvent[]; nodeIds: string[] }) {
  const [cursor, setCursor] = useState(events.length);
  const [playing, setPlaying] = useState(false);
  const logRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    if (!playing || cursor >= events.length) return;
    const timer = setTimeout(() => setCursor((c) => Math.min(c + 1, events.length)), STEP_MS);
    return () => clearTimeout(timer);
  }, [playing, cursor, events.length]);

  useEffect(() => {
    const container = logRef.current;
    if (!container) return;
    container
      .querySelector<HTMLElement>(`[data-index="${cursor - 1}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const nodeStatus = useMemo(() => {
    const status: Record<string, string> = Object.fromEntries(
      nodeIds.map((id) => [id, "pending"]),
    );
    for (const event of events.slice(0, cursor)) {
      if (!event.node_id) continue;
      const mapped = NODE_STATUS_FOR_EVENT[event.type];
      if (mapped) status[event.node_id] = mapped;
    }
    return status;
  }, [events, cursor, nodeIds]);

  const atEnd = cursor >= events.length;

  return (
    <div className="rounded-lg border border-black/10 dark:border-white/15">
      <div className="flex flex-wrap items-center gap-3 border-b border-black/10 px-4 py-2 text-sm dark:border-white/15">
        <span className="font-medium">Replay ({events.length} events)</span>
        <button
          type="button"
          onClick={() => {
            if (atEnd) {
              setCursor(0);
              setPlaying(true);
            } else {
              setPlaying((p) => !p);
            }
          }}
          className="rounded bg-black/5 px-2 py-1 text-xs font-medium hover:bg-black/10 dark:bg-white/10 dark:hover:bg-white/15"
        >
          {playing && !atEnd ? "Pause" : atEnd ? "Replay" : "Play"}
        </button>
        <button
          type="button"
          onClick={() => {
            setPlaying(false);
            setCursor((c) => Math.max(c - 1, 0));
          }}
          disabled={cursor <= 0}
          className="rounded bg-black/5 px-2 py-1 text-xs disabled:opacity-40 dark:bg-white/10"
        >
          &larr; Step
        </button>
        <button
          type="button"
          onClick={() => {
            setPlaying(false);
            setCursor((c) => Math.min(c + 1, events.length));
          }}
          disabled={atEnd}
          className="rounded bg-black/5 px-2 py-1 text-xs disabled:opacity-40 dark:bg-white/10"
        >
          Step &rarr;
        </button>
        <input
          type="range"
          min={0}
          max={events.length}
          value={cursor}
          onChange={(event) => {
            setPlaying(false);
            setCursor(Number(event.target.value));
          }}
          className="min-w-[120px] flex-1"
        />
        <span className="font-mono text-xs text-neutral-500">
          {cursor}/{events.length}
        </span>
      </div>

      {nodeIds.length > 0 && (
        <div className="flex flex-wrap gap-2 border-b border-black/10 px-4 py-3 dark:border-white/15">
          {nodeIds.map((id) => (
            <span
              key={id}
              className="flex items-center gap-1.5 rounded-full bg-black/5 px-2.5 py-1 text-xs dark:bg-white/10"
            >
              <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[nodeStatus[id]]}`} />
              <span className="font-mono">{id}</span>
            </span>
          ))}
        </div>
      )}

      <ol
        ref={logRef}
        className="max-h-96 divide-y divide-black/5 overflow-y-auto text-sm dark:divide-white/10"
      >
        {events.map((event, index) => (
          <li
            key={event.id}
            data-index={index}
            className={`px-4 py-2 transition-opacity ${index >= cursor ? "opacity-30" : ""}`}
          >
            <span className="font-mono text-xs text-neutral-500">#{event.sequence}</span>{" "}
            <span className="font-medium">{event.type}</span>{" "}
            {event.message && (
              <span className="text-neutral-600 dark:text-neutral-400">{event.message}</span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
