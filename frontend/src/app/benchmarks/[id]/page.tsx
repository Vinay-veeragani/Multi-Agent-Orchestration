import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiError, getBenchmarkRun } from "@/lib/api";

function pct(n: number | null): string {
  return n == null ? "—" : `${(n * 100).toFixed(1)}%`;
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  return { title: id };
}

export default async function BenchmarkDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let report;
  try {
    report = await getBenchmarkRun(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const categories = Array.from(new Set(report.results.map((r) => r.category))).sort();

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <Link href="/benchmarks" className="text-sm text-neutral-500 hover:underline">
        &larr; Benchmark runs
      </Link>

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-lg">{report.id}</h1>
        {report.git_sha && (
          <span className="rounded-full bg-black/5 px-2.5 py-0.5 font-mono text-xs dark:bg-white/10">
            {report.git_sha}
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-neutral-500">{report.provider_note}</p>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-neutral-500">
          Ablation -- {report.arms.length} arms, {report.scenario_count} scenarios
        </h2>
        <div className="overflow-x-auto rounded-lg border border-black/10 dark:border-white/15">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
              <tr>
                <th className="px-3 py-2 font-medium">Arm</th>
                <th className="px-3 py-2 font-medium">Passed</th>
                <th className="px-3 py-2 font-medium">Completion</th>
                <th className="px-3 py-2 font-medium">Routing accuracy</th>
                <th className="px-3 py-2 font-medium">Avg latency</th>
                <th className="px-3 py-2 font-medium">p95 latency</th>
                <th className="px-3 py-2 font-medium">Tokens</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-black/5 dark:divide-white/10">
              {report.arms.map((arm) => (
                <tr key={arm.arm}>
                  <td className="px-3 py-2 font-mono">{arm.arm}</td>
                  <td className="px-3 py-2">
                    {arm.scenarios_passed}/{arm.scenarios_run}
                  </td>
                  <td className="px-3 py-2">
                    {pct(arm.scenarios_run ? arm.scenarios_passed / arm.scenarios_run : null)}
                  </td>
                  <td className="px-3 py-2">{pct(arm.routing_accuracy)}</td>
                  <td className="px-3 py-2">{(arm.avg_latency_seconds * 1000).toFixed(1)}ms</td>
                  <td className="px-3 py-2">{(arm.p95_latency_seconds * 1000).toFixed(1)}ms</td>
                  <td className="px-3 py-2">{arm.total_tokens.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="mt-8">
        <h2 className="mb-2 text-sm font-medium text-neutral-500">
          Scenarios by category ({categories.length})
        </h2>
        <div className="space-y-4">
          {categories.map((category) => {
            const rows = report.results.filter((r) => r.category === category);
            const arms = Array.from(new Set(rows.map((r) => r.arm)));
            return (
              <div
                key={category}
                className="overflow-hidden rounded-lg border border-black/10 dark:border-white/15"
              >
                <div className="border-b border-black/10 bg-black/5 px-3 py-2 text-xs font-medium text-neutral-500 dark:border-white/15 dark:bg-white/5">
                  {category}
                </div>
                <div className="grid divide-y divide-black/5 text-sm dark:divide-white/10">
                  {Array.from(new Set(rows.map((r) => r.scenario_id))).map((scenarioId) => (
                    <div key={scenarioId} className="flex flex-wrap items-center gap-2 px-3 py-2">
                      <span className="font-mono">{scenarioId}</span>
                      <span className="flex flex-1 flex-wrap justify-end gap-1">
                        {arms.map((arm) => {
                          const result = rows.find(
                            (r) => r.scenario_id === scenarioId && r.arm === arm,
                          );
                          if (!result) return null;
                          return (
                            <span
                              key={arm}
                              title={`${arm}: ${result.passed ? "passed" : result.failures.join(", ") || "failed"}`}
                              className={`rounded px-1.5 py-0.5 font-mono text-xs ${
                                result.passed
                                  ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-400"
                                  : "bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-400"
                              }`}
                            >
                              {arm}
                            </span>
                          );
                        })}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
