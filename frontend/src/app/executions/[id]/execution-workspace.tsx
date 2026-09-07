"use client";

import { useRouter } from "next/navigation";
import type {
  AgentInvocation,
  ExecutionEvent,
  ExecutionState,
  ToolInvocation,
  WorkflowDetail,
} from "@/lib/api";
import { Markdown } from "@/components/ui/markdown";
import { BudgetMeter } from "./budget-meter";
import { ExecutionGraph } from "./graph";
import { Inspector } from "./inspector";
import { SupervisorDecisionPanel } from "./supervisor-decision";
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
  const router = useRouter();
  // `state`/`agentInvocations`/`isTerminal` etc. are a one-time snapshot from
  // whenever this page first loaded -- see useLiveExecution's own docstring
  // for why the live event stream cannot fill in a completion's actual
  // result. Refreshing (a soft, data-only re-fetch of this same server
  // component, not a reload) the moment the client confirms the execution
  // genuinely finished is what makes the final answer/budget/status appear
  // on their own instead of needing a manual reload.
  useLiveExecution(executionId, initialEvents, isTerminal, () => router.refresh());

  const initialNodeStatus: Record<string, string> = {};
  for (const [nodeId, node] of Object.entries(state.node_states)) {
    initialNodeStatus[nodeId] = node.status;
  }

  // The supervisor can answer a task directly without ever delegating -- no
  // agent ran, nothing was ever added to the graph. The three-column
  // workspace exists to visualize a multi-agent run; forcing a one-line
  // answer through it buries the one thing worth seeing at the bottom of a
  // mostly-empty Inspector column. Once that outcome is certain (terminal,
  // and genuinely nothing ran), show the answer itself, prominently, instead.
  const hasGraphActivity = agentInvocations.length > 0 || workflow.edges.length > 0;
  if (isTerminal && !hasGraphActivity) {
    return (
      <div className="mx-auto h-full max-w-2xl overflow-y-auto px-6 py-8">
        <div className="rounded-md border border-border bg-surface p-5">
          <div className="mb-2 text-xs font-medium text-muted-foreground">Answer</div>
          {state.final_output ? (
            <Markdown className="text-base">{state.final_output}</Markdown>
          ) : (
            <p className="text-base leading-relaxed text-foreground">No output was produced.</p>
          )}
        </div>

        <div className="mt-4">
          <SupervisorDecisionPanel />
        </div>

        <div className="mt-4">
          <BudgetMeter budget={state.budget} usage={state.budget_usage} />
        </div>

        <details className="mt-4 overflow-hidden rounded-md border border-border">
          <summary className="cursor-pointer list-none px-3 py-2 text-xs font-medium text-muted-foreground select-none">
            Timeline
          </summary>
          <div className="h-64 border-t border-border">
            <ExecutionTimeline />
          </div>
        </details>
      </div>
    );
  }

  return (
    <div className="grid h-full grid-cols-1 overflow-hidden lg:grid-cols-[1fr_2fr_1fr]">
      <div className="min-h-0 min-w-0 border-b border-border lg:border-r lg:border-b-0">
        <ExecutionTimeline />
      </div>
      <div className="min-h-[360px] min-w-0 border-b border-border lg:border-r lg:border-b-0">
        <ExecutionGraph
          workflow={workflow}
          initialNodeStatus={initialNodeStatus}
          agentInvocations={agentInvocations}
          toolInvocations={toolInvocations}
        />
      </div>
      <div className="min-h-0 min-w-0">
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
