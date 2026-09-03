"use client";

import { useMemo } from "react";
import type {
  AgentInvocation,
  ExecutionState,
  ToolInvocation,
  WorkflowDetail,
} from "@/lib/api";
import { useExecutionStore } from "@/lib/execution-store";
import { Badge, statusVariant } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BudgetMeter } from "./budget-meter";
import { SupervisorDecisionPanel } from "./supervisor-decision";

export function Inspector({
  state,
  workflow,
  agentInvocations,
  toolInvocations,
}: {
  state: ExecutionState;
  workflow: WorkflowDetail;
  agentInvocations: AgentInvocation[];
  toolInvocations: ToolInvocation[];
}) {
  const selectedNodeId = useExecutionStore((s) => s.selectedNodeId);
  const liveStatus = useExecutionStore((s) => s.nodeStatus);
  const events = useExecutionStore((s) => s.events);

  const node = workflow.nodes.find((n) => n.id === selectedNodeId);
  const invocation = agentInvocations.find((i) => i.node_id === selectedNodeId);
  const nodeTools = useMemo(
    () => toolInvocations.filter((t) => t.node_id === selectedNodeId),
    [toolInvocations, selectedNodeId],
  );
  const nodeEvents = useMemo(
    () => events.filter((e) => e.node_id === selectedNodeId),
    [events, selectedNodeId],
  );
  const output = selectedNodeId ? state.agent_outputs[selectedNodeId] : undefined;
  const nodeStatus = selectedNodeId
    ? (liveStatus[selectedNodeId] ?? state.node_states[selectedNodeId]?.status ?? "pending")
    : null;

  if (!node) {
    // Nothing selected: the execution-level view -- supervisor's latest
    // decision and the budget, the two things worth seeing by default.
    return (
      <div className="h-full space-y-4 overflow-y-auto p-3">
        <SupervisorDecisionPanel />
        <BudgetMeter budget={state.budget} usage={state.budget_usage} />
        {state.final_output && (
          <div className="rounded-md border border-border bg-surface p-3">
            <div className="mb-1.5 text-xs font-medium text-muted-foreground">Final result</div>
            <p className="text-sm whitespace-pre-wrap text-foreground">{state.final_output}</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <div className="mb-3 flex items-center gap-2">
        <span className="font-mono text-sm text-foreground">{node.name || node.id}</span>
        {nodeStatus && <Badge variant={statusVariant(nodeStatus)}>{nodeStatus}</Badge>}
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="output">Output</TabsTrigger>
          <TabsTrigger value="tools">Tools ({nodeTools.length})</TabsTrigger>
          <TabsTrigger value="events">Events ({nodeEvents.length})</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <Field label="Kind" value={node.kind} mono />
            <Field label="Agent" value={node.agent_id ?? "—"} mono />
            <Field
              label="Duration"
              value={invocation?.duration_seconds != null ? `${invocation.duration_seconds.toFixed(2)}s` : "—"}
            />
            <Field label="Model" value={invocation?.model_key ?? "—"} mono />
            <Field label="Tokens" value={invocation?.tokens?.toLocaleString() ?? "—"} />
            <Field label="Cost" value={invocation ? `$${invocation.cost_usd.toFixed(4)}` : "—"} />
            <Field label="Attempt" value={String(invocation?.attempt ?? "—")} />
            <Field
              label="Confidence"
              value={invocation?.confidence != null ? invocation.confidence.toFixed(2) : "—"}
            />
          </dl>
        </TabsContent>

        <TabsContent value="output">
          {output ? (
            <pre className="overflow-x-auto rounded bg-black/40 p-2.5 text-xs text-muted-foreground">
              {JSON.stringify(output, null, 2)}
            </pre>
          ) : (
            <p className="text-xs text-subtle-foreground">No output recorded yet.</p>
          )}
        </TabsContent>

        <TabsContent value="tools">
          {nodeTools.length > 0 ? (
            <ul className="space-y-2">
              {nodeTools.map((tool) => (
                <li key={tool.id} className="rounded-md border border-border bg-surface p-2.5">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-foreground">{tool.tool}</span>
                    <Badge variant={statusVariant(tool.status)}>{tool.status}</Badge>
                  </div>
                  <div className="mt-1 flex gap-3 text-[10px] text-subtle-foreground">
                    <span>policy: {tool.policy_effect}</span>
                    {tool.duration_seconds != null && <span>{tool.duration_seconds.toFixed(2)}s</span>}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-subtle-foreground">No tool calls for this node.</p>
          )}
        </TabsContent>

        <TabsContent value="events">
          <ul className="space-y-1.5">
            {nodeEvents.map((event) => (
              <li key={event.id} className="text-xs text-muted-foreground">
                <span className="font-mono text-subtle-foreground">#{event.sequence}</span>{" "}
                {event.type}
              </li>
            ))}
            {nodeEvents.length === 0 && (
              <p className="text-xs text-subtle-foreground">No events yet.</p>
            )}
          </ul>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-[10px] text-subtle-foreground uppercase">{label}</dt>
      <dd className={mono ? "font-mono text-xs text-foreground" : "text-xs text-foreground"}>
        {value}
      </dd>
    </div>
  );
}
