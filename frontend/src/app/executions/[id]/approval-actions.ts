"use server";

import { revalidatePath } from "next/cache";
import { decideApproval } from "@/lib/api";

export interface DecisionState {
  status: "idle" | "error";
  message?: string;
}

// Bound with (executionId, approvalId, decision) from the client component
// before being handed to React as a plain form action -- the client only
// ever supplies the reviewer's name and note through the form itself, never
// which approval this decides (see the Next.js Server Actions security
// guide: "send a reference plus the change, not the whole object").
export async function decideApprovalAction(
  executionId: string,
  approvalId: string,
  decision: "approve" | "reject",
  _previous: DecisionState,
  formData: FormData,
): Promise<DecisionState> {
  const by = String(formData.get("by") ?? "").trim();
  const note = String(formData.get("note") ?? "").trim();
  if (!by) {
    return { status: "error", message: "Your name or email is required." };
  }

  try {
    await decideApproval(executionId, decision, {
      approvalId,
      by,
      note: note || undefined,
    });
  } catch (error) {
    return {
      status: "error",
      message: error instanceof Error ? error.message : "The decision could not be recorded.",
    };
  }

  revalidatePath(`/executions/${executionId}`);
  return { status: "idle" };
}
