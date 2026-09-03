"use client";

import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Loader2,
  ShieldQuestion,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { useExecutionStore } from "@/lib/execution-store";
import { cn } from "@/lib/utils";

const ICONS: Record<string, { icon: typeof CheckCircle2; className: string }> = {
  execution_started: { icon: CircleDashed, className: "text-muted-foreground" },
  execution_completed: { icon: CheckCircle2, className: "text-success" },
  execution_failed: { icon: XCircle, className: "text-danger" },
  execution_cancelled: { icon: XCircle, className: "text-subtle-foreground" },
  node_started: { icon: Loader2, className: "text-running animate-spin" },
  node_completed: { icon: CheckCircle2, className: "text-success" },
  node_failed: { icon: XCircle, className: "text-danger" },
  node_skipped: { icon: CircleDashed, className: "text-subtle-foreground" },
  agent_invoked: { icon: Loader2, className: "text-running animate-spin" },
  agent_completed: { icon: CheckCircle2, className: "text-success" },
  agent_failed: { icon: XCircle, className: "text-danger" },
  tool_invoked: { icon: Loader2, className: "text-running animate-spin" },
  tool_completed: { icon: CheckCircle2, className: "text-success" },
  tool_failed: { icon: XCircle, className: "text-danger" },
  tool_denied: { icon: XCircle, className: "text-warning" },
  retry_started: { icon: AlertTriangle, className: "text-warning" },
  retry_exhausted: { icon: XCircle, className: "text-danger" },
  supervisor_decided: { icon: CircleDashed, className: "text-primary" },
  routing_degraded: { icon: AlertTriangle, className: "text-warning" },
  approval_requested: { icon: ShieldQuestion, className: "text-approval" },
  approval_granted: { icon: CheckCircle2, className: "text-approval" },
  approval_rejected: { icon: XCircle, className: "text-approval" },
  checkpoint_created: { icon: CircleDashed, className: "text-subtle-foreground" },
};

function elapsed(startedAt: string | null, at: string): string {
  if (!startedAt) return "";
  const seconds = Math.max(0, (new Date(at).getTime() - new Date(startedAt).getTime()) / 1000);
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function ExecutionTimeline() {
  const events = useExecutionStore((s) => s.events);
  const connection = useExecutionStore((s) => s.connection);
  const selectedNodeId = useExecutionStore((s) => s.selectedNodeId);
  const selectNode = useExecutionStore((s) => s.selectNode);
  const [expanded, setExpanded] = useState<string | null>(null);
  const startedAt = events[0]?.created_at ?? null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-xs font-medium text-muted-foreground">Timeline</span>
        <span className="flex items-center gap-1.5 text-[10px] text-subtle-foreground">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connection === "live" && "bg-running",
              connection === "connecting" && "bg-warning",
              connection === "reconnecting" && "bg-warning animate-pulse",
              connection === "closed" && "bg-subtle-foreground",
            )}
          />
          {connection}
        </span>
      </div>
      <ol className="flex-1 overflow-y-auto px-1 py-2">
        {events.length === 0 && (
          <li className="px-3 py-4 text-xs text-subtle-foreground">Waiting for events&hellip;</li>
        )}
        {events.map((event) => {
          const meta = ICONS[event.type] ?? { icon: CircleDashed, className: "text-muted-foreground" };
          const Icon = meta.icon;
          const isExpanded = expanded === event.id;
          const hasPayload = event.payload && Object.keys(event.payload).length > 0;
          const relatedToSelection = event.node_id && event.node_id === selectedNodeId;
          return (
            <li key={event.id}>
              <button
                type="button"
                onClick={() => {
                  setExpanded(isExpanded ? null : event.id);
                  if (event.node_id) selectNode(event.node_id);
                }}
                className={cn(
                  "flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-white/[0.04]",
                  relatedToSelection && "bg-primary-muted",
                )}
              >
                <span className="mt-0.5 font-mono text-[10px] text-subtle-foreground">
                  {elapsed(startedAt, event.created_at)}
                </span>
                <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", meta.className)} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs text-foreground">
                    {event.message || event.type}
                  </span>
                  {(event.node_id || event.agent_id || event.tool) && (
                    <span className="mt-0.5 flex gap-2 font-mono text-[10px] text-subtle-foreground">
                      {event.node_id && <span>{event.node_id}</span>}
                      {event.tool && <span>{event.tool}</span>}
                    </span>
                  )}
                  {isExpanded && hasPayload && (
                    <pre className="mt-1.5 overflow-x-auto rounded bg-black/40 p-2 text-[10px] text-muted-foreground">
                      {JSON.stringify(event.payload, null, 2)}
                    </pre>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
