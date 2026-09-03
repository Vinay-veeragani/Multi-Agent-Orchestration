import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { ApiError, getAgent } from "@/lib/api";
import { Badge } from "@/components/ui/badge";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  return { title: id };
}

export default async function AgentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let agent;
  try {
    agent = await getAgent(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link
        href="/agents"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Agents
      </Link>

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-lg text-foreground">{agent.id}</h1>
        <Badge variant="neutral" className="font-mono">
          {agent.kind}
        </Badge>
        <Badge variant={agent.enabled ? "success" : "neutral"}>
          {agent.enabled ? "enabled" : "disabled"}
        </Badge>
      </div>
      <p className="mt-1 text-muted-foreground">{agent.description}</p>

      <section className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Model" value={agent.model_key ?? "auto-routed"} />
        <Stat label="Max iterations" value={String(agent.max_iterations)} />
        <Stat label="Timeout" value={`${agent.timeout_seconds}s`} />
        <Stat label="Confidence floor" value={agent.confidence_floor.toFixed(2)} />
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-subtle-foreground">
          Capabilities ({agent.capabilities.length})
        </h2>
        {agent.capabilities.length > 0 ? (
          <div className="overflow-hidden rounded-lg border border-border bg-surface">
            <table className="w-full text-sm">
              <tbody className="divide-y divide-border">
                {agent.capabilities.map((capability) => (
                  <tr key={capability.name}>
                    <td className="px-3 py-2 font-mono text-foreground">{capability.name}</td>
                    <td className="px-3 py-2 text-muted-foreground">{capability.description}</td>
                    <td className="px-3 py-2 text-right font-mono text-xs text-subtle-foreground">
                      {capability.proficiency.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No declared capabilities.</p>
        )}
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-subtle-foreground">
          Allowed tools ({agent.allowed_tools.length})
        </h2>
        {agent.allowed_tools.length > 0 ? (
          <div className="overflow-hidden rounded-lg border border-border bg-surface">
            <table className="w-full text-sm">
              <thead className="border-b border-border bg-elevated text-left text-xs text-subtle-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Tool</th>
                  <th className="px-3 py-2 font-medium">Effect</th>
                  <th className="px-3 py-2 font-medium">Max calls</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {agent.allowed_tools.map((permission) => (
                  <tr key={permission.tool}>
                    <td className="px-3 py-2 font-mono text-foreground">{permission.tool}</td>
                    <td className="px-3 py-2 text-muted-foreground">{permission.effect}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {permission.max_calls ?? "unlimited"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            No tools allowed -- deny-by-default means this agent cannot call any tool.
          </p>
        )}
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-subtle-foreground">System prompt</h2>
        <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-lg border border-border bg-surface p-3 font-mono text-xs text-muted-foreground">
          {agent.system_prompt}
        </pre>
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="text-xs text-subtle-foreground">{label}</div>
      <div className="mt-1 font-mono text-sm text-foreground">{value}</div>
    </div>
  );
}
