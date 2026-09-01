import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiError, getAgent } from "@/lib/api";

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
      <Link href="/agents" className="text-sm text-neutral-500 hover:underline">
        &larr; Agents
      </Link>

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-lg">{agent.id}</h1>
        <span className="rounded-full bg-black/5 px-2.5 py-0.5 font-mono text-xs dark:bg-white/10">
          {agent.kind}
        </span>
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
            agent.enabled
              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-400"
              : "bg-neutral-200 text-neutral-700 dark:bg-neutral-500/20 dark:text-neutral-300"
          }`}
        >
          {agent.enabled ? "enabled" : "disabled"}
        </span>
      </div>
      <p className="mt-1 text-neutral-600 dark:text-neutral-400">{agent.description}</p>

      <section className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Model" value={agent.model_key ?? "auto-routed"} />
        <Stat label="Max iterations" value={String(agent.max_iterations)} />
        <Stat label="Timeout" value={`${agent.timeout_seconds}s`} />
        <Stat label="Confidence floor" value={agent.confidence_floor.toFixed(2)} />
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-neutral-500">
          Capabilities ({agent.capabilities.length})
        </h2>
        {agent.capabilities.length > 0 ? (
          <div className="overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
            <table className="w-full text-sm">
              <tbody className="divide-y divide-black/5 dark:divide-white/10">
                {agent.capabilities.map((capability) => (
                  <tr key={capability.name}>
                    <td className="px-3 py-2 font-mono">{capability.name}</td>
                    <td className="px-3 py-2 text-neutral-600 dark:text-neutral-400">
                      {capability.description}
                    </td>
                    <td className="px-3 py-2 text-right text-neutral-500">
                      {capability.proficiency.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-neutral-500">No declared capabilities.</p>
        )}
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-neutral-500">
          Allowed tools ({agent.allowed_tools.length})
        </h2>
        {agent.allowed_tools.length > 0 ? (
          <div className="overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
            <table className="w-full text-sm">
              <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
                <tr>
                  <th className="px-3 py-2 font-medium">Tool</th>
                  <th className="px-3 py-2 font-medium">Effect</th>
                  <th className="px-3 py-2 font-medium">Max calls</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-black/5 dark:divide-white/10">
                {agent.allowed_tools.map((permission) => (
                  <tr key={permission.tool}>
                    <td className="px-3 py-2 font-mono">{permission.tool}</td>
                    <td className="px-3 py-2">{permission.effect}</td>
                    <td className="px-3 py-2 text-neutral-500">
                      {permission.max_calls ?? "unlimited"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-neutral-500">
            No tools allowed -- deny-by-default means this agent cannot call any tool.
          </p>
        )}
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-neutral-500">System prompt</h2>
        <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-lg bg-black/5 p-3 text-xs dark:bg-white/5">
          {agent.system_prompt}
        </pre>
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
