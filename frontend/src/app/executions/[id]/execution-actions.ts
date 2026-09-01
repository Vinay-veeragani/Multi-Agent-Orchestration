"use server";

import { revalidatePath } from "next/cache";
import { ApiError, cancelExecution } from "@/lib/api";

export interface CancelState {
  status: "idle" | "error";
  message?: string;
}

export async function cancelExecutionAction(
  executionId: string,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- useActionState's reducer shape
  _previous: CancelState,
): Promise<CancelState> {
  try {
    await cancelExecution(executionId);
  } catch (error) {
    return {
      status: "error",
      message: error instanceof ApiError ? error.message : "The execution could not be cancelled.",
    };
  }
  revalidatePath(`/executions/${executionId}`);
  return { status: "idle" };
}
