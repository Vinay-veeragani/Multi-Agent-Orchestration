"use client";

import { useActionState } from "react";
import type { ProviderInfo } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  providerFormInitialState,
  removeProviderAction,
  saveProviderAction,
} from "./actions";

export function ProviderCard({ provider }: { provider: ProviderInfo }) {
  const [saveState, saveAction, saving] = useActionState(
    saveProviderAction.bind(null, provider.provider),
    providerFormInitialState,
  );
  const [removeState, removeAction, removing] = useActionState(
    removeProviderAction.bind(null, provider.provider),
    providerFormInitialState,
  );

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <span className="text-sm font-medium text-foreground">{provider.label}</span>
        <div className="flex items-center gap-1.5">
          {provider.configured ? (
            <Badge variant="success">
              {provider.source === "database" ? "configured · UI" : "configured · .env"}
            </Badge>
          ) : (
            <Badge variant="neutral">not configured</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
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
              placeholder={provider.masked_api_key ?? "Not set"}
              disabled={saving}
              className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary"
            />
          </div>

          <div>
            <label
              htmlFor={`${provider.provider}-base-url`}
              className="text-[10px] font-medium text-subtle-foreground"
            >
              Base URL <span className="text-subtle-foreground/70">(optional override)</span>
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
                Model agents should use
              </label>
              <select
                id={`${provider.provider}-model`}
                name="selected_model_key"
                defaultValue={provider.selected_model_key ?? ""}
                disabled={saving}
                className="mt-1 block w-full rounded-md border border-border-strong bg-black/20 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary"
              >
                <option value="">Auto (router chooses per call)</option>
                {provider.models.map((model) => (
                  <option key={model.key} value={model.key}>
                    {model.model}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button type="submit" variant="primary" size="sm" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
            {provider.source === "database" && (
              <Button
                type="button"
                variant="danger"
                size="sm"
                disabled={removing}
                onClick={() => removeAction()}
              >
                {removing ? "Removing…" : "Remove key"}
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
      </CardContent>
    </Card>
  );
}
