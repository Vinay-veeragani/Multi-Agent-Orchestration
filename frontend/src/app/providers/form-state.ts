// Not a "use server" module: a server actions file may only export async
// functions (see https://nextjs.org/docs/messages/invalid-use-server-value),
// so the shared state type/initial value used by both the actions and the
// client components consuming them lives here instead.

export interface ProviderFormState {
  status: "idle" | "error" | "success";
  message?: string;
}

export const providerFormInitialState: ProviderFormState = { status: "idle" };
