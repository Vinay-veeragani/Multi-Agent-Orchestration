import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import {
  ApiError,
  getExecution,
  getExecutionEvents,
  getExecutionWorkflow,
  isTerminalStatus,
  listAgentInvocations,
  listPendingApprovals,
  listToolInvocations,
} from "@/lib/api";
import { Badge, statusVariant } from "@/components/ui/badge";
import { ApprovalPanel } from "./approval-panel";
import { CancelButton } from "./cancel-button";
import { ExecutionWorkspace } from "./execution-workspace";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  return { title: id };
}

export default async function ExecutionDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let state;
  try {
    state = await getExecution(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }
  const isTerminal = isTerminalStatus(state.status);

  const [events, workflow, agentInvocations, toolInvocations] = await Promise.all([
    getExecutionEvents(id).catch(() => []),
    getExecutionWorkflow(id),
    listAgentInvocations(id).catch(() => []),
    listToolInvocations(id).catch(() => []),
  ]);
  const pendingApprovals =
    state.status === "waiting_for_approval" ? await listPendingApprovals(id).catch(() => []) : [];

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-border bg-surface px-6 py-3">
        <Link
          href="/"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Executions
        </Link>
        <span className="text-border-strong">/</span>
        <h1 className="truncate font-mono text-sm text-foreground">{state.execution_id}</h1>
        <Badge variant={statusVariant(state.status)}>{state.status}</Badge>
        <span className="truncate text-xs text-muted-foreground">{state.task.description}</span>
        <div className="ml-auto flex items-center gap-2">
          {!isTerminal && <CancelButton executionId={state.execution_id} />}
        </div>
      </div>

      {pendingApprovals.length > 0 && (
        <div className="space-y-3 border-b border-border bg-surface px-6 py-4">
          {pendingApprovals.map((approval) => (
            <ApprovalPanel key={approval.id} executionId={id} approval={approval} />
          ))}
        </div>
      )}

      <div className="min-h-0 flex-1">
        <ExecutionWorkspace
          executionId={id}
          state={state}
          workflow={workflow}
          initialEvents={events}
          isTerminal={isTerminal}
          agentInvocations={agentInvocations}
          toolInvocations={toolInvocations}
        />
      </div>
    </div>
  );
}
