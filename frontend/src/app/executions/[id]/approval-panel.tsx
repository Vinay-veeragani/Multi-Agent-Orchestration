"use client";

import { useActionState } from "react";
import type { ApprovalRequest } from "@/lib/api";
import { decideApprovalAction, type DecisionState } from "./approval-actions";

const initialState: DecisionState = { status: "idle" };

export function ApprovalPanel({
  executionId,
  approval,
}: {
  executionId: string;
  approval: ApprovalRequest;
}) {
  const [approveState, approveAction, approvePending] = useActionState(
    decideApprovalAction.bind(null, executionId, approval.id, "approve"),
    initialState,
  );
  const [rejectState, rejectAction, rejectPending] = useActionState(
    decideApprovalAction.bind(null, executionId, approval.id, "reject"),
    initialState,
  );
  const busy = approvePending || rejectPending;

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-500/30 dark:bg-amber-500/10">
      <div className="font-medium text-amber-900 dark:text-amber-300">Approval required</div>
      <p className="mt-1 text-amber-800 dark:text-amber-200">{approval.action}</p>
      <p className="mt-1 text-amber-700 dark:text-amber-300/80">{approval.risk_reason}</p>
      {Object.keys(approval.parameters).length > 0 && (
        <pre className="mt-2 overflow-x-auto rounded bg-black/5 p-2 text-xs dark:bg-white/5">
          {JSON.stringify(approval.parameters, null, 2)}
        </pre>
      )}

      <form className="mt-3 flex flex-wrap items-end gap-2">
        <div className="flex flex-col">
          <label htmlFor="by" className="text-xs text-amber-800 dark:text-amber-300">
            Your name or email
          </label>
          <input
            id="by"
            name="by"
            required
            disabled={busy}
            className="rounded border border-amber-300 bg-white px-2 py-1 text-sm dark:border-amber-500/40 dark:bg-black/20"
          />
        </div>
        <div className="flex flex-col">
          <label htmlFor="note" className="text-xs text-amber-800 dark:text-amber-300">
            Note (optional)
          </label>
          <input
            id="note"
            name="note"
            disabled={busy}
            className="rounded border border-amber-300 bg-white px-2 py-1 text-sm dark:border-amber-500/40 dark:bg-black/20"
          />
        </div>
        <button
          type="submit"
          formAction={approveAction}
          disabled={busy}
          className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {approvePending ? "Approving…" : "Approve"}
        </button>
        <button
          type="submit"
          formAction={rejectAction}
          disabled={busy}
          className="rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
        >
          {rejectPending ? "Rejecting…" : "Reject"}
        </button>
      </form>

      {approveState.status === "error" && (
        <p className="mt-2 text-red-700 dark:text-red-400">{approveState.message}</p>
      )}
      {rejectState.status === "error" && (
        <p className="mt-2 text-red-700 dark:text-red-400">{rejectState.message}</p>
      )}
    </div>
  );
}
