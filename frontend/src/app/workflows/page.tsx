import Link from "next/link";
import { listWorkflows } from "@/lib/api";

export default async function WorkflowsPage() {
  const workflows = await listWorkflows();

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-medium">Workflows</h1>
        <Link
          href="/workflows/new"
          className="rounded bg-black/5 px-3 py-1.5 text-sm font-medium hover:bg-black/10 dark:bg-white/10 dark:hover:bg-white/15"
        >
          New workflow
        </Link>
      </div>
      <p className="mt-1 text-sm text-neutral-500">
        Hand-authored workflows, registered via <code className="font-mono">POST /workflows</code>
        . Dynamic executions (no registered workflow) don&apos;t appear here -- see the{" "}
        <Link href="/" className="underline">
          executions dashboard
        </Link>
        .
      </p>

      <div className="mt-6 overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
        <table className="w-full text-sm">
          <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">ID</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-black/5 dark:divide-white/10">
            {workflows.map((workflow) => (
              <tr key={workflow.id}>
                <td className="px-3 py-2">{workflow.name}</td>
                <td className="px-3 py-2 font-mono text-xs text-neutral-500">{workflow.id}</td>
              </tr>
            ))}
            {workflows.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-neutral-500" colSpan={2}>
                  No workflows registered yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
