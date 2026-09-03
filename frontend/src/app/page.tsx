import type { Metadata } from "next";
import Link from "next/link";
import { Plus } from "lucide-react";
import { listAgents, listExecutions } from "@/lib/api";
import { Badge, statusVariant } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";

export const metadata: Metadata = { title: "Executions" };

export default async function DashboardPage() {
  const [executions, agents] = await Promise.all([
    listExecutions({ limit: 50 }),
    listAgents(),
  ]);

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-medium text-foreground">Executions</h1>
          <p className="mt-1 text-sm text-muted-foreground">{agents.length} agents registered</p>
        </div>
        <Link href="/executions/new" className={buttonVariants({ variant: "primary" })}>
          <Plus className="h-4 w-4" />
          New execution
        </Link>
      </div>

      <div className="mt-6 overflow-hidden rounded-lg border border-border bg-surface">
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-elevated text-left text-xs text-subtle-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Execution</th>
              <th className="px-3 py-2 font-medium">Task</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Cost</th>
              <th className="px-3 py-2 font-medium">Created</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {executions.map((execution) => (
              <tr key={execution.id} className="hover:bg-white/[0.03]">
                <td className="px-3 py-2">
                  <Link
                    href={`/executions/${execution.id}`}
                    className="font-mono text-xs text-foreground hover:text-primary hover:underline"
                  >
                    {execution.id}
                  </Link>
                </td>
                <td className="max-w-xs truncate px-3 py-2 text-muted-foreground">
                  {execution.task}
                </td>
                <td className="px-3 py-2">
                  <Badge variant={statusVariant(execution.status)}>{execution.status}</Badge>
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {execution.cost_usd != null ? `$${execution.cost_usd.toFixed(4)}` : "—"}
                </td>
                <td className="px-3 py-2 text-subtle-foreground">
                  {new Date(execution.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {executions.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-muted-foreground" colSpan={5}>
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
