"use client";

import { ChevronRight, KeyRound } from "lucide-react";
import { useActionState, useState } from "react";
import type { ProviderInfo } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import {
  providerFormInitialState,
  removeProviderAction,
  saveProviderAction,
} from "./actions";

export function ProviderCard({
  provider,
  isActive,
}: {
  provider: ProviderInfo;
  isActive: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [saveState, saveAction, saving] = useActionState(
    saveProviderAction.bind(null, provider.provider),
    providerFormInitialState,
  );
  const [removeState, removeAction, removing] = useActionState(
    removeProviderAction.bind(null, provider.provider),
    providerFormInitialState,
  );

  return (
    <Card className="overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-white/[0.02]"
      >
        <KeyRound className="h-4 w-4 shrink-0 text-subtle-foreground" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-foreground">{provider.label}</div>
          {provider.configured && (
            <div className="truncate text-xs text-subtle-foreground">
              {provider.source === "database" ? "Connected via API key" : "Connected via .env"}
            </div>
          )}
        </div>
        {isActive && <Badge variant="primary">Active</Badge>}
        <Badge variant={provider.configured ? "success" : "neutral"}>
          {provider.configured ? "Connected" : "Not connected"}
        </Badge>
        <ChevronRight
          className={cn("h-4 w-4 shrink-0 text-subtle-foreground transition-transform", open && "rotate-90")}
        />
      </button>

      {open && (
        <div className="border-t border-border px-4 py-4">
          <form action={saveAction} className="space-y-3">
            <div>
              <label
                htmlFor={`${provider.provider}-api-key`}
                className="text-[10px] font-medium text-subtle-foreground"
              >
                API key
              </label>
              <input
                id={`${provider.provider}-api-key`}
                name="api_key"
                type="password"
                autoComplete="off"
                placeholder={provider.masked_api_key ?? "Paste your API key"}
                disabled={saving}
                className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary"
              />
            </div>

            <details className="group rounded-md border border-border">
              <summary className="cursor-pointer list-none px-3 py-2 text-xs font-medium text-muted-foreground select-none">
                Advanced settings
              </summary>
              <div className="space-y-3 border-t border-border px-3 py-3">
                <div>
                  <label
                    htmlFor={`${provider.provider}-base-url`}
                    className="text-[10px] font-medium text-subtle-foreground"
                  >
                    Base URL
                  </label>
                  <input
                    id={`${provider.provider}-base-url`}
                    name="base_url"
                    type="text"
                    placeholder={provider.base_url ?? ""}
                    disabled={saving}
                    className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 font-mono text-xs text-foreground outline-none focus:border-primary"
                  />
                </div>

                {provider.models.length > 0 && (
                  <div>
                    <label
                      htmlFor={`${provider.provider}-model`}
                      className="text-[10px] font-medium text-subtle-foreground"
                    >
                      Model
                    </label>
                    <select
                      id={`${provider.provider}-model`}
                      name="selected_model_key"
                      defaultValue={provider.selected_model_key ?? ""}
                      disabled={saving}
                      className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary"
                    >
                      <option value="">Auto</option>
                      {provider.models.map((model) => (
                        <option key={model.key} value={model.key}>
                          {model.model}
                        </option>
                      ))}
                    </select>
                    <p className="mt-1 text-[10px] text-subtle-foreground">
                      Used only while this is the active provider (set above the list). Auto lets
                      the router pick a model from this provider per call.
                    </p>
                  </div>
                )}
              </div>
            </details>

            <div className="flex items-center gap-2 pt-1">
              <Button type="submit" variant="primary" size="sm" disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
              {provider.source === "database" && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={removing}
                  onClick={() => removeAction()}
                >
                  {removing ? "Disconnecting…" : "Disconnect"}
                </Button>
              )}
            </div>

            {saveState.status === "error" && (
              <p className="text-xs text-danger">{saveState.message}</p>
            )}
            {saveState.status === "success" && (
              <p className="text-xs text-success">{saveState.message}</p>
            )}
            {removeState.status === "error" && (
              <p className="text-xs text-danger">{removeState.message}</p>
            )}
          </form>
        </div>
      )}
    </Card>
  );
}
