import Link from "next/link";
import { listWorkflows } from "@/lib/api";
import { NewExecutionForm } from "./form";

export default async function NewExecutionPage() {
  const workflows = await listWorkflows();

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <Link href="/" className="text-sm text-neutral-500 hover:underline">
        &larr; Executions
      </Link>
      <h1 className="mt-2 text-xl font-medium">New execution</h1>
      <p className="mt-1 text-sm text-neutral-500">
        Starts through the same <code className="font-mono">POST /executions</code> the CLI
        uses. Without a real LLM key configured, routing falls back to the deterministic
        heuristic router -- see the root README&apos;s &quot;What this project is NOT&quot;.
      </p>

      <div className="mt-6">
        <NewExecutionForm workflows={workflows} />
      </div>
    </div>
  );
}
