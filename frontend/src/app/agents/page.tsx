import type { Metadata } from "next";
import Link from "next/link";
import { listAgents } from "@/lib/api";
import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = { title: "Agents" };

export default async function AgentsPage() {
  const agents = await listAgents();

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="text-xl font-medium text-foreground">Agents</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        The registry the supervisor delegates to and every workflow node references by id.
        Deny-by-default: an agent can only call a tool listed in its own allowlist -- see
        each agent&apos;s detail page.
      </p>

      <div className="mt-6 overflow-hidden rounded-lg border border-border bg-surface">
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-elevated text-left text-xs text-subtle-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Capabilities</th>
              <th className="px-3 py-2 font-medium">Tools</th>
              <th className="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {agents.map((agent) => (
              <tr key={agent.id} className="hover:bg-white/[0.03]">
                <td className="px-3 py-2">
                  <Link
                    href={`/agents/${agent.id}`}
                    className="font-mono text-xs text-foreground hover:text-primary hover:underline"
                  >
                    {agent.id}
                  </Link>
                  <div className="text-xs text-subtle-foreground">{agent.name}</div>
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{agent.kind}</td>
                <td className="px-3 py-2 text-muted-foreground">{agent.capabilities.length}</td>
                <td className="px-3 py-2 text-muted-foreground">{agent.allowed_tools.length}</td>
                <td className="px-3 py-2">
                  <Badge variant={agent.enabled ? "success" : "neutral"}>
                    {agent.enabled ? "enabled" : "disabled"}
                  </Badge>
                </td>
              </tr>
            ))}
            {agents.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-muted-foreground" colSpan={5}>
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
