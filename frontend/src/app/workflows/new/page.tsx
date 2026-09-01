import type { Metadata } from "next";
import Link from "next/link";
import { listAgents } from "@/lib/api";
import { WorkflowBuilder } from "./builder";

export const metadata: Metadata = { title: "New Workflow" };

export default async function NewWorkflowPage() {
  const agents = await listAgents();

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <Link href="/workflows" className="text-sm text-neutral-500 hover:underline">
        &larr; Workflows
      </Link>
      <h1 className="mt-2 text-xl font-medium">New workflow</h1>
      <p className="mt-1 text-sm text-neutral-500">
        Limited scope by design: agent, join, and terminal nodes only -- no tool nodes,
        conditions, or approval nodes. Validated server-side against the live agent
        registry and graph rules before it&apos;s registered (the same check the CLI and
        every other caller of <code className="font-mono">POST /workflows</code> gets).
      </p>

      <div className="mt-6">
        <WorkflowBuilder agents={agents} />
      </div>
    </div>
  );
}
