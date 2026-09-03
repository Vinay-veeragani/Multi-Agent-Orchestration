"use client";

import { useActionState, useRef, useState } from "react";
import type { ProviderInfo } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { setActiveProviderAction } from "./actions";
import { providerFormInitialState } from "./form-state";

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

  // `activeProvider` is server-owned truth: this component stays mounted
  // across the server action's revalidation (only its props change), so a
  // plain `defaultValue` below would only ever reflect whatever was true on
  // first mount -- selecting a provider would submit correctly but the
  // dropdown itself would silently revert to "Automatic" on every render
  // after. Adjusting local state during render when the prop has changed
  // (React's own recommended pattern for this -- see "Adjusting state when a
  // prop changes" in the React docs) is what makes the select track the
  // confirmed server value, without the extra render pass an effect would add.
  const [value, setValue] = useState(activeProvider ?? "");
  const [syncedFor, setSyncedFor] = useState(activeProvider);
  if (activeProvider !== syncedFor) {
    setSyncedFor(activeProvider);
    setValue(activeProvider ?? "");
  }

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
            value={value}
            disabled={pending}
            onChange={(event) => {
              setValue(event.target.value);
              formRef.current?.requestSubmit();
            }}
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
