import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiError, getExecution, getExecutionEvents, listPendingApprovals } from "@/lib/api";
import { ApprovalPanel } from "./approval-panel";
import { LiveEvents } from "./live-events";

const STATUS_BADGE: Record<string, string> = {
  succeeded: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-400",
  failed: "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-400",
  cancelled: "bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-300",
  running: "bg-sky-100 text-sky-800 dark:bg-sky-500/15 dark:text-sky-400",
  pending: "bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-300",
  waiting_for_approval: "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-400",
};

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
  const events = await getExecutionEvents(id).catch(() => []);
  const isTerminal = ["succeeded", "failed", "cancelled"].includes(state.status);
  const pendingApprovals =
    state.status === "waiting_for_approval"
      ? await listPendingApprovals(id).catch(() => [])
      : [];

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <Link href="/" className="text-sm text-neutral-500 hover:underline">
        &larr; Executions
      </Link>

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-lg">{state.execution_id}</h1>
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
            STATUS_BADGE[state.status] ?? "bg-neutral-200 text-neutral-700"
          }`}
        >
          {state.status}
        </span>
      </div>
      <p className="mt-1 text-neutral-600 dark:text-neutral-400">{state.task.description}</p>
      {state.final_output && (
        <p className="mt-3 rounded-lg bg-black/5 p-3 text-sm dark:bg-white/5">
          {state.final_output}
        </p>
      )}

      {pendingApprovals.length > 0 && (
        <div className="mt-6 space-y-3">
          {pendingApprovals.map((approval) => (
            <ApprovalPanel key={approval.id} executionId={id} approval={approval} />
          ))}
        </div>
      )}

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-neutral-500">Nodes</h2>
        <div className="overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
          <table className="w-full text-sm">
            <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
              <tr>
                <th className="px-3 py-2 font-medium">Node</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Attempts</th>
                <th className="px-3 py-2 font-medium">Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-black/5 dark:divide-white/10">
              {Object.values(state.node_states).map((node) => (
                <tr key={node.node_id}>
                  <td className="px-3 py-2 font-mono">{node.node_id}</td>
                  <td className="px-3 py-2">{node.status}</td>
                  <td className="px-3 py-2">{node.attempts}</td>
                  <td className="px-3 py-2">
                    {node.duration_seconds != null ? `${node.duration_seconds.toFixed(2)}s` : "—"}
                  </td>
                </tr>
              ))}
              {Object.keys(state.node_states).length === 0 && (
                <tr>
                  <td className="px-3 py-3 text-neutral-500" colSpan={4}>
                    No nodes yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Cost" value={`$${state.budget_usage.cost_usd.toFixed(4)}`} />
        <Stat
          label="Tokens"
          value={String(state.budget_usage.input_tokens + state.budget_usage.output_tokens)}
        />
        <Stat label="Agent steps" value={String(state.budget_usage.agent_steps)} />
        <Stat label="Tool calls" value={String(state.budget_usage.tool_calls)} />
      </section>

      <section className="mt-8">
        {isTerminal ? (
          <RecordedEvents events={events} />
        ) : (
          <LiveEvents executionId={state.execution_id} />
        )}
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-black/10 p-3 dark:border-white/15">
      <div className="text-xs text-neutral-500">{label}</div>
      <div className="mt-1 font-mono text-sm">{value}</div>
    </div>
  );
}

function RecordedEvents({
  events,
}: {
  events: Awaited<ReturnType<typeof getExecutionEvents>>;
}) {
  return (
    <div className="rounded-lg border border-black/10 dark:border-white/15">
      <div className="border-b border-black/10 px-4 py-2 text-sm font-medium dark:border-white/15">
        Event log ({events.length})
      </div>
      <ol className="max-h-96 divide-y divide-black/5 overflow-y-auto text-sm dark:divide-white/10">
        {events.map((event) => (
          <li key={event.id} className="px-4 py-2">
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
