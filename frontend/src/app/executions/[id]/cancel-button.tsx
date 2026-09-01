"use client";

import { useActionState } from "react";
import { cancelExecutionAction, type CancelState } from "./execution-actions";

const initialState: CancelState = { status: "idle" };

export function CancelButton({ executionId }: { executionId: string }) {
  const [state, formAction, pending] = useActionState(
    cancelExecutionAction.bind(null, executionId),
    initialState,
  );

  return (
    <form action={formAction} className="inline-flex items-center gap-2">
      <button
        type="submit"
        disabled={pending}
        onClick={(event) => {
          if (!confirm("Cancel this execution?")) event.preventDefault();
        }}
        className="rounded bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
      >
        {pending ? "Cancelling…" : "Cancel"}
      </button>
      {state.status === "error" && (
        <span className="text-xs text-red-700 dark:text-red-400">{state.message}</span>
      )}
    </form>
  );
}
