"use client";

import { useActionState } from "react";
import type { WorkflowSummary } from "@/lib/api";
import { createExecutionAction, type NewExecutionState } from "./actions";

const initialState: NewExecutionState = { status: "idle" };

export function NewExecutionForm({ workflows }: { workflows: WorkflowSummary[] }) {
  const [state, formAction, pending] = useActionState(createExecutionAction, initialState);

  return (
    <form action={formAction} className="space-y-4">
      <div>
        <label htmlFor="task" className="text-xs font-medium text-neutral-500">
          Task
        </label>
        <textarea
          id="task"
          name="task"
          required
          rows={3}
          placeholder="e.g. compare CRM vendors on pricing"
          className="mt-1 block w-full rounded border border-black/10 bg-transparent px-2 py-1.5 text-sm dark:border-white/15"
        />
      </div>

      <div>
        <label htmlFor="success_criteria" className="text-xs font-medium text-neutral-500">
          Success criteria <span className="text-neutral-400">(optional, one per line)</span>
        </label>
        <textarea
          id="success_criteria"
          name="success_criteria"
          rows={2}
          className="mt-1 block w-full rounded border border-black/10 bg-transparent px-2 py-1.5 text-sm dark:border-white/15"
        />
      </div>

      <div>
        <label htmlFor="workflow_id" className="text-xs font-medium text-neutral-500">
          Workflow <span className="text-neutral-400">(optional -- omit for a dynamic, supervisor-driven run)</span>
        </label>
        <select
          id="workflow_id"
          name="workflow_id"
          defaultValue=""
          className="mt-1 block w-full rounded border border-black/10 bg-transparent px-2 py-1.5 text-sm dark:border-white/15"
        >
          <option value="">Dynamic (no workflow)</option>
          {workflows.map((workflow) => (
            <option key={workflow.id} value={workflow.id}>
              {workflow.name}
            </option>
          ))}
        </select>
      </div>

      {state.status === "error" && (
        <p className="text-sm text-red-700 dark:text-red-400">{state.message}</p>
      )}

      <button
        type="submit"
        disabled={pending}
        className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
      >
        {pending ? "Starting…" : "Start execution"}
      </button>
    </form>
  );
}
