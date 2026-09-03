"use client";

import { useActionState, useRef } from "react";
import type { ProviderInfo } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { providerFormInitialState, setActiveProviderAction } from "./actions";

export function ActiveProviderSelector({
  providers,
  activeProvider,
}: {
  providers: ProviderInfo[];
  activeProvider: string | null;
}) {
  const [state, formAction, pending] = useActionState(
    setActiveProviderAction,
    providerFormInitialState,
  );
  const formRef = useRef<HTMLFormElement>(null);
  const connected = providers.filter((p) => p.configured);

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-foreground">Active provider</div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {activeProvider
              ? "Every agent and the supervisor call this provider only."
              : "Automatic -- the router picks per call across every connected provider."}
          </p>
        </div>
        <form
          ref={formRef}
          action={formAction}
          className="flex items-center gap-2"
        >
          <select
            name="active_provider"
            defaultValue={activeProvider ?? ""}
            disabled={pending}
            onChange={() => formRef.current?.requestSubmit()}
            className="h-8 rounded-md border border-border-strong bg-black/20 px-2 text-sm text-foreground outline-none focus:border-primary"
          >
            <option value="">Automatic</option>
            {connected.map((provider) => (
              <option key={provider.provider} value={provider.provider}>
                {provider.label}
              </option>
            ))}
          </select>
        </form>
      </div>
      {connected.length === 0 && (
        <p className="mt-2 text-xs text-subtle-foreground">
          Connect a provider below before choosing one here.
        </p>
      )}
      {state.status === "error" && <p className="mt-2 text-xs text-danger">{state.message}</p>}
    </Card>
  );
}
