import Link from "next/link";
import { listBenchmarkRuns } from "@/lib/api";

export default async function BenchmarksPage() {
  const runs = await listBenchmarkRuns(50);

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <h1 className="text-xl font-medium">Benchmark runs</h1>
      <p className="mt-1 text-sm text-neutral-500">
        Evaluation reports produced by <code className="font-mono">orchestrator benchmark</code>{" "}
        -- see <code className="font-mono">docs/evaluation-benchmark.md</code> in the repo for
        how the four arms compare.
      </p>

      <div className="mt-6 overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
        <table className="w-full text-sm">
          <thead className="bg-black/5 text-left text-xs text-neutral-500 dark:bg-white/5">
            <tr>
              <th className="px-3 py-2 font-medium">Report</th>
              <th className="px-3 py-2 font-medium">Git SHA</th>
              <th className="px-3 py-2 font-medium">Scenarios</th>
              <th className="px-3 py-2 font-medium">Completed</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-black/5 dark:divide-white/10">
            {runs.map((run) => (
              <tr key={run.id} className="hover:bg-black/[0.03] dark:hover:bg-white/[0.03]">
                <td className="px-3 py-2">
                  <Link href={`/benchmarks/${run.id}`} className="font-mono hover:underline">
                    {run.id}
                  </Link>
                </td>
                <td className="px-3 py-2 font-mono text-xs text-neutral-500">
                  {run.git_sha ?? "—"}
                </td>
                <td className="px-3 py-2">{run.scenario_count}</td>
                <td className="px-3 py-2 text-neutral-500">
                  {new Date(run.completed_at).toLocaleString()}
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td className="px-3 py-6 text-center text-neutral-500" colSpan={4}>
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
