import type { Metadata } from "next";
import Link from "next/link";
import { listBenchmarkRuns } from "@/lib/api";

export const metadata: Metadata = { title: "Benchmarks" };

export default async function BenchmarksPage() {
  const runs = await listBenchmarkRuns(50);

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="text-xl font-medium text-foreground">Benchmark runs</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Evaluation reports produced by <code className="font-mono">orchestrator benchmark</code>{" "}
        -- see <code className="font-mono">docs/evaluation-benchmark.md</code> in the repo for
        how the four arms compare.
      </p>

      <div className="mt-6 overflow-hidden rounded-lg border border-border bg-surface">
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-elevated text-left text-xs text-subtle-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Report</th>
              <th className="px-3 py-2 font-medium">Git SHA</th>
              <th className="px-3 py-2 font-medium">Scenarios</th>
              <th className="px-3 py-2 font-medium">Completed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {runs.map((run) => (
              <tr key={run.id} className="hover:bg-white/[0.03]">
                <td className="px-3 py-2">
                  <Link
                    href={`/benchmarks/${run.id}`}
                    className="font-mono text-xs text-foreground hover:text-primary hover:underline"
                  >
                    {run.id}
                  </Link>
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                  {run.git_sha ?? "—"}
                </td>
                <td className="px-3 py-2 text-muted-foreground">{run.scenario_count}</td>
                <td className="px-3 py-2 text-subtle-foreground">
                  {new Date(run.completed_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-muted-foreground" colSpan={4}>
                  No benchmark runs yet -- run{" "}
                  <code className="font-mono">orchestrator benchmark --test-db</code>.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
