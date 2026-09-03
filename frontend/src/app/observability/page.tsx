import type { Metadata } from "next";
import Link from "next/link";
import { getObservabilitySummary } from "@/lib/api";
import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = { title: "Observability" };

export default async function ObservabilityPage() {
  const summary = await getObservabilitySummary();
  const { totals, agents, tools, recent_issues } = summary;
  const succeeded = totals.by_status["succeeded"] ?? 0;
  const successRate = totals.total_executions > 0 ? succeeded / totals.total_executions : null;

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="text-xl font-medium text-foreground">Observability</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Durable history across every execution -- not the live Prometheus counters at{" "}
        <code className="font-mono">/metrics</code>, which reset on every process restart.
      </p>

      <section className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Executions" value={totals.total_executions.toLocaleString()} />
        <Stat
          label="Success rate"
          value={successRate === null ? "—" : `${(successRate * 100).toFixed(0)}%`}
        />
        <Stat label="Total cost" value={`$${totals.total_cost_usd.toFixed(4)}`} />
        <Stat label="Total tokens" value={totals.total_tokens.toLocaleString()} />
      </section>

      {Object.keys(totals.by_status).length > 0 && (
        <section className="mt-4 flex flex-wrap gap-2">
          {Object.entries(totals.by_status).map(([status, count]) => (
            <Badge key={status} variant={statusBadgeVariant(status)}>
              {status}: {count}
            </Badge>
          ))}
        </section>
      )}

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-subtle-foreground">
          Agents ({agents.length})
        </h2>
        <div className="overflow-x-auto rounded-lg border border-border bg-surface">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="border-b border-border bg-elevated text-left text-xs text-subtle-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Invocations</th>
                <th className="px-3 py-2 font-medium">Success rate</th>
                <th className="px-3 py-2 font-medium">Avg duration</th>
                <th className="px-3 py-2 font-medium">Tokens</th>
                <th className="px-3 py-2 font-medium">Cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {agents.map((agent) => (
                <tr key={agent.agent_id}>
                  <td className="px-3 py-2">
                    <Link
                      href={`/agents/${agent.agent_id}`}
                      className="font-mono text-xs text-foreground hover:text-primary hover:underline"
                    >
                      {agent.agent_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{agent.invocations}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {((agent.succeeded / agent.invocations) * 100).toFixed(0)}%
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {agent.avg_duration_seconds.toFixed(2)}s
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {agent.total_tokens.toLocaleString()}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                    ${agent.total_cost_usd.toFixed(4)}
                  </td>
                </tr>
              ))}
              {agents.length === 0 && (
                <tr>
                  <td className="px-3 py-6 text-center text-muted-foreground" colSpan={6}>
                    No agent invocations yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-subtle-foreground">
          Tools ({tools.length})
        </h2>
        <div className="overflow-x-auto rounded-lg border border-border bg-surface">
          <table className="w-full min-w-[480px] text-sm">
            <thead className="border-b border-border bg-elevated text-left text-xs text-subtle-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Tool</th>
                <th className="px-3 py-2 font-medium">Calls</th>
                <th className="px-3 py-2 font-medium">Succeeded</th>
                <th className="px-3 py-2 font-medium">Denied</th>
                <th className="px-3 py-2 font-medium">Needed approval</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {tools.map((tool) => (
                <tr key={tool.tool}>
                  <td className="px-3 py-2 font-mono text-xs text-foreground">{tool.tool}</td>
                  <td className="px-3 py-2 text-muted-foreground">{tool.invocations}</td>
                  <td className="px-3 py-2 text-muted-foreground">{tool.succeeded}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {tool.denied > 0 ? (
                      <Badge variant="danger">{tool.denied}</Badge>
                    ) : (
                      tool.denied
                    )}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{tool.required_approval}</td>
                </tr>
              ))}
              {tools.length === 0 && (
                <tr>
                  <td className="px-3 py-6 text-center text-muted-foreground" colSpan={5}>
                    No tool calls yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-subtle-foreground">
          Recent issues ({recent_issues.length})
        </h2>
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <div className="divide-y divide-border">
            {recent_issues.map((issue) => (
              <Link
                key={issue.id}
                href={`/executions/${issue.execution_id}`}
                className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-white/[0.03]"
              >
                <Badge variant={issue.severity === "error" ? "danger" : "warning"}>
                  {issue.severity}
                </Badge>
                <span className="font-mono text-xs text-subtle-foreground">{issue.type}</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">
                  {issue.message}
                </span>
                <span className="shrink-0 font-mono text-xs text-subtle-foreground">
                  {new Date(issue.created_at).toLocaleString()}
                </span>
              </Link>
            ))}
            {recent_issues.length === 0 && (
              <p className="px-3 py-6 text-center text-sm text-muted-foreground">
                No warnings or errors recorded.
              </p>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function statusBadgeVariant(status: string): "success" | "danger" | "neutral" | "running" {
  switch (status) {
    case "succeeded":
      return "success";
    case "failed":
    case "budget_exceeded":
    case "timed_out":
      return "danger";
    case "running":
      return "running";
    default:
      return "neutral";
  }
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="text-xs text-subtle-foreground">{label}</div>
      <div className="mt-1 font-mono text-sm text-foreground">{value}</div>
    </div>
  );
}
