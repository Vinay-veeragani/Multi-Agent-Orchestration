import type { Metadata } from "next";
import Link from "next/link";
import { listAgents } from "@/lib/api";

export const metadata: Metadata = { title: "Agents" };

export default async function AgentsPage() {
  const agents = await listAgents();

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <h1 className="text-xl font-medium">Agents</h1>
      <p className="mt-1 text-sm text-neutral-500">
        The registry the supervisor delegates to and every workflow node references by id.
        Deny-by-default: an agent can only call a tool listed in its own allowlist -- see
        each agent&apos;s detail page.
      </p>

      <div className="mt-6 overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
        <table className="w-full text-sm">
          <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
            <tr>
              <th className="px-3 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Capabilities</th>
              <th className="px-3 py-2 font-medium">Tools</th>
              <th className="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-black/5 dark:divide-white/10">
            {agents.map((agent) => (
              <tr key={agent.id} className="hover:bg-black/[0.03] dark:hover:bg-white/[0.03]">
                <td className="px-3 py-2">
                  <Link href={`/agents/${agent.id}`} className="font-mono hover:underline">
                    {agent.id}
                  </Link>
                  <div className="text-xs text-neutral-500">{agent.name}</div>
                </td>
                <td className="px-3 py-2 font-mono text-xs text-neutral-500">{agent.kind}</td>
                <td className="px-3 py-2 text-neutral-500">{agent.capabilities.length}</td>
                <td className="px-3 py-2 text-neutral-500">{agent.allowed_tools.length}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      agent.enabled
                        ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-400"
                        : "bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-300"
                    }`}
                  >
                    {agent.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
              </tr>
            ))}
            {agents.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-neutral-500" colSpan={5}>
                  No agents registered.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
