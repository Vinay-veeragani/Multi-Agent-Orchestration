"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, createWorkflow, type CreateWorkflowInput } from "@/lib/api";

export interface BuilderState {
  status: "idle" | "error";
  message?: string;
}

// The client component serialises its built node/edge rows into one hidden
// "workflow" form field (see builder.tsx) -- simpler than threading a dozen
// dynamic-array fields through FormData, and this action still validates the
// shape itself rather than trusting the client's JSON.
export async function createWorkflowAction(
  _previous: BuilderState,
  formData: FormData,
): Promise<BuilderState> {
  const raw = String(formData.get("workflow") ?? "");
  let input: CreateWorkflowInput;
  try {
    input = JSON.parse(raw) as CreateWorkflowInput;
  } catch {
    return { status: "error", message: "The workflow could not be read. Please try again." };
  }

  if (!input.name?.trim()) {
    return { status: "error", message: "Give the workflow a name." };
  }
  if (!Array.isArray(input.nodes) || input.nodes.length === 0) {
    return { status: "error", message: "Add at least one node." };
  }

  let created;
  try {
    created = await createWorkflow(input);
  } catch (error) {
    return {
      status: "error",
      message: error instanceof ApiError ? error.message : "The workflow could not be created.",
    };
  }

  revalidatePath("/workflows");
  redirect(`/workflows?created=${encodeURIComponent(created.id)}`);
}
