"use client";

import { useActionState } from "react";
import { Button } from "@/components/ui/button";
import { cancelExecutionAction, type CancelState } from "./execution-actions";

const initialState: CancelState = { status: "idle" };

export function CancelButton({ executionId }: { executionId: string }) {
  const [state, formAction, pending] = useActionState(
    cancelExecutionAction.bind(null, executionId),
    initialState,
  );

  return (
    <form action={formAction} className="inline-flex items-center gap-2">
      <Button
        type="submit"
        size="sm"
        variant="danger"
        disabled={pending}
        onClick={(event) => {
          if (!confirm("Cancel this execution?")) event.preventDefault();
        }}
      >
        {pending ? "Cancelling…" : "Cancel"}
      </Button>
      {state.status === "error" && <span className="text-xs text-danger">{state.message}</span>}
    </form>
  );
}
