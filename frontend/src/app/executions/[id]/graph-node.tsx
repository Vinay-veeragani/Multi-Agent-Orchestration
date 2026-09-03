"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { cn } from "@/lib/utils";

export interface ExecutionNodeData {
  label: string;
  kind: string;
  status: string;
  durationSeconds?: number;
  tokens?: number;
  toolCalls?: number;
  [key: string]: unknown;
}

const STATUS_RING: Record<string, string> = {
  running: "border-running animate-pulse-ring",
  succeeded: "border-success",
  failed: "border-danger",
  waiting_for_approval: "border-approval",
  skipped: "border-border-strong opacity-60",
  cancelled: "border-border-strong opacity-60",
  pending: "border-border",
};

const STATUS_DOT: Record<string, string> = {
  running: "bg-running",
  succeeded: "bg-success",
  failed: "bg-danger",
  waiting_for_approval: "bg-approval",
  skipped: "bg-subtle-foreground",
  cancelled: "bg-subtle-foreground",
  pending: "bg-subtle-foreground",
};

export function ExecutionGraphNode({ data, selected }: NodeProps) {
  const nodeData = data as ExecutionNodeData;
  const status = nodeData.status || "pending";

  return (
    <div
      className={cn(
        "min-w-[168px] rounded-md border-2 bg-elevated px-3 py-2.5 shadow-lg transition-colors",
        STATUS_RING[status] ?? "border-border",
        selected && "ring-2 ring-primary/60",
      )}
    >
      <Handle type="target" position={Position.Top} className="!bg-border-strong" />
      <div className="flex items-center gap-1.5">
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", STATUS_DOT[status])} />
        <span className="truncate text-sm font-medium text-foreground">{nodeData.label}</span>
      </div>
      <div className="mt-0.5 font-mono text-[10px] text-subtle-foreground uppercase">
        {nodeData.kind}
      </div>
      {(nodeData.durationSeconds != null || nodeData.tokens != null) && (
        <div className="mt-1.5 flex flex-wrap gap-x-2 font-mono text-[10px] text-muted-foreground">
          {nodeData.durationSeconds != null && <span>{nodeData.durationSeconds.toFixed(2)}s</span>}
          {nodeData.toolCalls != null && nodeData.toolCalls > 0 && (
            <span>{nodeData.toolCalls} tools</span>
          )}
          {nodeData.tokens != null && <span>{nodeData.tokens.toLocaleString()} tok</span>}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-border-strong" />
    </div>
  );
}
