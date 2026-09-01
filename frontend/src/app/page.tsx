import Link from "next/link";
import { listAgents, listExecutions, listWorkflows } from "@/lib/api";

const STATUS_BADGE: Record<string, string> = {
  succeeded: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-400",
  failed: "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-400",
  cancelled: "bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-300",
  running: "bg-sky-100 text-sky-800 dark:bg-sky-500/15 dark:text-sky-400",
  pending: "bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-300",
  waiting_for_approval: "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-400",
};

export default async function DashboardPage() {
  const [executions, agents, workflows] = await Promise.all([
    listExecutions({ limit: 50 }),
    listAgents(),
    listWorkflows(),
  ]);

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <h1 className="text-xl font-medium">Executions</h1>
      <p className="mt-1 text-sm text-neutral-500">
        {agents.length} agents registered &middot; {workflows.length} workflows registered
      </p>

      <div className="mt-6 overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
        <table className="w-full text-sm">
          <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
            <tr>
              <th className="px-3 py-2 font-medium">Execution</th>
              <th className="px-3 py-2 font-medium">Task</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Cost</th>
              <th className="px-3 py-2 font-medium">Created</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-black/5 dark:divide-white/10">
            {executions.map((execution) => (
              <tr key={execution.id} className="hover:bg-black/[0.03] dark:hover:bg-white/[0.03]">
                <td className="px-3 py-2">
                  <Link href={`/executions/${execution.id}`} className="font-mono hover:underline">
                    {execution.id}
                  </Link>
                </td>
                <td className="max-w-xs truncate px-3 py-2 text-neutral-600 dark:text-neutral-400">
                  {execution.task}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      STATUS_BADGE[execution.status] ?? "bg-neutral-200 text-neutral-700"
                    }`}
                  >
                    {execution.status}
                  </span>
                </td>
                <td className="px-3 py-2 font-mono text-xs">
                  {execution.cost_usd != null ? `$${execution.cost_usd.toFixed(4)}` : "—"}
                </td>
                <td className="px-3 py-2 text-neutral-500">
                  {new Date(execution.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {executions.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-neutral-500" colSpan={5}>
                  No executions yet -- start one with{" "}
                  <code className="font-mono">POST /executions</code> or{" "}
                  <code className="font-mono">orchestrator run</code>.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
