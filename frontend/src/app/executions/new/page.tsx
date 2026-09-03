import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { NewExecutionForm } from "./form";

export const metadata: Metadata = { title: "New Execution" };

export default function NewExecutionPage() {
  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <Link
        href="/"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Executions
      </Link>
      <h1 className="mt-2 text-xl font-medium text-foreground">New execution</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Starts through the same <code className="font-mono">POST /executions</code> the CLI
        uses -- a supervisor agent decides which agents to delegate to as it runs. Without a
        real LLM key configured, routing falls back to the deterministic heuristic router --
        see the root README&apos;s &quot;What this project is NOT&quot;.
      </p>

      <div className="mt-6">
        <NewExecutionForm />
      </div>
    </div>
  );
}
