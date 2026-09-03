"use server";

import { redirect } from "next/navigation";
import { ApiError, createExecution } from "@/lib/api";

export interface NewExecutionState {
  status: "idle" | "error";
  message?: string;
}

export async function createExecutionAction(
  _previous: NewExecutionState,
  formData: FormData,
): Promise<NewExecutionState> {
  const task = String(formData.get("task") ?? "").trim();
  if (!task) {
    return { status: "error", message: "Describe the task." };
  }
  const successCriteria = String(formData.get("success_criteria") ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  let created;
  try {
    created = await createExecution({ task, successCriteria });
  } catch (error) {
    return {
      status: "error",
      message: error instanceof ApiError ? error.message : "The execution could not be started.",
    };
  }

  redirect(`/executions/${created.execution_id}`);
}
