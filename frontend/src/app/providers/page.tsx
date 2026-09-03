import type { Metadata } from "next";
import { listProviders } from "@/lib/api";
import { ActiveProviderSelector } from "./active-provider-selector";
import { ProviderCard } from "./provider-card";

export const metadata: Metadata = { title: "Providers" };

export default async function ProvidersPage() {
  const { active_provider, providers } = await listProviders();

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <h1 className="text-xl font-medium text-foreground">Providers</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Connect a provider to let agents use it. Click one to add a key.
      </p>

      <div className="mt-6">
        <ActiveProviderSelector providers={providers} activeProvider={active_provider} />
      </div>

      <div className="mt-4 space-y-2">
        {providers.map((provider) => (
          <ProviderCard
            key={provider.provider}
            provider={provider}
            isActive={provider.provider === active_provider}
          />
        ))}
      </div>
    </div>
  );
}
