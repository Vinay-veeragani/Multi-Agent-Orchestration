"use client";

import { useActionState } from "react";
import { Button } from "@/components/ui/button";
import { createExecutionAction, type NewExecutionState } from "./actions";

const initialState: NewExecutionState = { status: "idle" };

export function NewExecutionForm() {
  const [state, formAction, pending] = useActionState(createExecutionAction, initialState);

  return (
    <form action={formAction} className="space-y-4">
      <div>
        <label htmlFor="task" className="text-xs font-medium text-subtle-foreground">
          Task
        </label>
        <textarea
          id="task"
          name="task"
          required
          rows={3}
          placeholder="e.g. compare CRM vendors on pricing"
          className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary"
        />
      </div>

      <div>
        <label htmlFor="success_criteria" className="text-xs font-medium text-subtle-foreground">
          Success criteria <span className="text-subtle-foreground/70">(optional, one per line)</span>
        </label>
        <textarea
          id="success_criteria"
          name="success_criteria"
          rows={2}
          className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary"
        />
      </div>

      {state.status === "error" && <p className="text-sm text-danger">{state.message}</p>}

      <Button type="submit" variant="primary" disabled={pending}>
        {pending ? "Starting…" : "Start execution"}
      </Button>
    </form>
  );
}
