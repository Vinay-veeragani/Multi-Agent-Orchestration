"use client";

import { useMemo } from "react";
import { useExecutionStore } from "@/lib/execution-store";

/**
 * The supervisor's own structured decision (action, targets, reason,
 * confidence) -- exactly the payload of the most recent `supervisor_decided`
 * event, nothing more. There is no hidden chain-of-thought to display: this
 * project's RoutingDecision schema only ever carries this much, by design
 * (see docs/supervisor-and-routing.md).
 */
export function SupervisorDecisionPanel() {
  const events = useExecutionStore((s) => s.events);

  const latest = useMemo(
    () => [...events].reverse().find((e) => e.type === "supervisor_decided"),
    [events],
  );
  const degraded = useMemo(
    () => events.some((e) => e.type === "routing_degraded"),
    [events],
  );

  if (!latest) return null;
  const payload = latest.payload as {
    action?: string;
    agents?: string[];
    confidence?: number;
    reason?: string;
  };

  return (
    <div className="rounded-md border border-border bg-surface p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">Supervisor decision</span>
        {degraded && (
          <span className="rounded-full bg-warning-muted px-2 py-0.5 text-[10px] text-warning">
            heuristic fallback
          </span>
        )}
      </div>
      <div className="font-mono text-sm text-primary uppercase">
        {payload.action ?? "unknown"}
      </div>
      {payload.reason && (
        <p className="mt-1.5 text-xs text-muted-foreground">{payload.reason}</p>
      )}
      {payload.agents && payload.agents.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {payload.agents.map((agent) => (
            <span
              key={agent}
              className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10px] text-foreground"
            >
              {agent}
            </span>
          ))}
        </div>
      )}
      {payload.confidence != null && (
        <div className="mt-2 text-[10px] text-subtle-foreground">
          confidence {payload.confidence.toFixed(2)}
        </div>
      )}
    </div>
  );
}
