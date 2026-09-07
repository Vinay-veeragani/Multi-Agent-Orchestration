"use client";

import { ShieldQuestion } from "lucide-react";
import Link from "next/link";
import { useActionState } from "react";
import type { PendingApprovalItem } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  decideApprovalAction,
  type DecisionState,
} from "@/app/executions/[id]/approval-actions";

const initialState: DecisionState = { status: "idle" };

export function ApprovalInboxItem({ item }: { item: PendingApprovalItem }) {
  const [approveState, approveAction, approvePending] = useActionState(
    decideApprovalAction.bind(null, item.execution_id, item.id, "approve"),
    initialState,
  );
  const [rejectState, rejectAction, rejectPending] = useActionState(
    decideApprovalAction.bind(null, item.execution_id, item.id, "reject"),
    initialState,
  );
  const busy = approvePending || rejectPending;

  return (
    <div className="rounded-md border border-approval/30 bg-approval-muted p-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-medium text-approval">
          <ShieldQuestion className="h-4 w-4" />
          Human approval required
        </div>
        <Link
          href={`/executions/${item.execution_id}`}
          className="font-mono text-xs text-muted-foreground hover:text-foreground hover:underline"
        >
          {item.execution_id}
        </Link>
      </div>
      <p className="mt-2 truncate text-xs text-muted-foreground">{item.task_description}</p>
      <p className="mt-2 font-mono text-xs text-foreground">{item.action}</p>
      <p className="mt-1 text-xs text-muted-foreground">{item.risk_reason}</p>
      {Object.keys(item.parameters).length > 0 && (
        <pre className="mt-2 overflow-x-auto rounded bg-surface p-2 text-xs text-muted-foreground">
          {JSON.stringify(item.parameters, null, 2)}
        </pre>
      )}

      <form className="mt-3 flex flex-wrap items-end gap-2">
        <div className="flex flex-col">
          <label htmlFor={`by-${item.id}`} className="text-[10px] text-muted-foreground">
            Your name or email
          </label>
          <input
            id={`by-${item.id}`}
            name="by"
            required
            disabled={busy}
            className="h-8 rounded border border-border-strong bg-surface px-2 text-sm text-foreground outline-none focus:border-approval"
          />
        </div>
        <div className="flex flex-col">
          <label htmlFor={`note-${item.id}`} className="text-[10px] text-muted-foreground">
            Note (optional)
          </label>
          <input
            id={`note-${item.id}`}
            name="note"
            disabled={busy}
            className="h-8 rounded border border-border-strong bg-surface px-2 text-sm text-foreground outline-none focus:border-approval"
          />
        </div>
        <Button
          type="submit"
          formAction={approveAction}
          disabled={busy}
          className="bg-success/90 text-black hover:bg-success"
        >
          {approvePending ? "Approving…" : "Approve"}
        </Button>
        <Button type="submit" formAction={rejectAction} disabled={busy} variant="danger">
          {rejectPending ? "Rejecting…" : "Reject"}
        </Button>
      </form>

      {approveState.status === "error" && (
        <p className="mt-2 text-xs text-danger">{approveState.message}</p>
      )}
      {rejectState.status === "error" && (
        <p className="mt-2 text-xs text-danger">{rejectState.message}</p>
      )}
    </div>
  );
}
