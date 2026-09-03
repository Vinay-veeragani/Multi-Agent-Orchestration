"use server";

import { revalidatePath } from "next/cache";
import { ApiError, deleteProviderCredential, updateProvider } from "@/lib/api";

export interface ProviderFormState {
  status: "idle" | "error" | "success";
  message?: string;
}

const initialState: ProviderFormState = { status: "idle" };

export { initialState as providerFormInitialState };

export async function saveProviderAction(
  provider: string,
  _previous: ProviderFormState,
  formData: FormData,
): Promise<ProviderFormState> {
  const apiKey = String(formData.get("api_key") ?? "").trim();
  const baseUrl = String(formData.get("base_url") ?? "").trim();
  const selectedModelKey = String(formData.get("selected_model_key") ?? "").trim();

  try {
    await updateProvider(provider, {
      apiKey: apiKey || undefined,
      baseUrl: baseUrl || undefined,
      selectedModelKey: selectedModelKey || null,
    });
  } catch (error) {
    return {
      status: "error",
      message: error instanceof ApiError ? error.message : "Could not save this provider.",
    };
  }

  revalidatePath("/providers");
  return { status: "success", message: "Saved." };
}

export async function removeProviderAction(provider: string): Promise<ProviderFormState> {
  try {
    await deleteProviderCredential(provider);
  } catch (error) {
    return {
      status: "error",
      message: error instanceof ApiError ? error.message : "Could not remove this key.",
    };
  }

  revalidatePath("/providers");
  return { status: "success", message: "Removed." };
}
