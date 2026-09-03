"use client";

import type {
  AgentInvocation,
  ExecutionEvent,
  ExecutionState,
  ToolInvocation,
  WorkflowDetail,
} from "@/lib/api";
import { ExecutionGraph } from "./graph";
import { Inspector } from "./inspector";
import { ExecutionTimeline } from "./timeline";
import { useLiveExecution } from "./use-live-execution";

export function ExecutionWorkspace({
  executionId,
  state,
  workflow,
  initialEvents,
  isTerminal,
  agentInvocations,
  toolInvocations,
}: {
  executionId: string;
  state: ExecutionState;
  workflow: WorkflowDetail;
  initialEvents: ExecutionEvent[];
  isTerminal: boolean;
  agentInvocations: AgentInvocation[];
  toolInvocations: ToolInvocation[];
}) {
  useLiveExecution(executionId, initialEvents, isTerminal);

  const initialNodeStatus: Record<string, string> = {};
  for (const [nodeId, node] of Object.entries(state.node_states)) {
    initialNodeStatus[nodeId] = node.status;
  }

  return (
    <div className="grid h-full grid-cols-1 lg:grid-cols-[1fr_2fr_1fr]">
      <div className="min-h-0 border-b border-border lg:border-r lg:border-b-0">
        <ExecutionTimeline />
      </div>
      <div className="min-h-[360px] border-b border-border lg:border-r lg:border-b-0">
        <ExecutionGraph
          workflow={workflow}
          initialNodeStatus={initialNodeStatus}
          agentInvocations={agentInvocations}
          toolInvocations={toolInvocations}
        />
      </div>
      <div className="min-h-0">
        <Inspector
          state={state}
          workflow={workflow}
          agentInvocations={agentInvocations}
          toolInvocations={toolInvocations}
        />
      </div>
    </div>
  );
}
