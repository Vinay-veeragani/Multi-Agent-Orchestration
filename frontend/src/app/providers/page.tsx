import type { Metadata } from "next";
import { listProviders } from "@/lib/api";
import { ProviderCard } from "./provider-card";

export const metadata: Metadata = { title: "Providers" };

export default async function ProvidersPage() {
  const providers = await listProviders();

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <h1 className="text-xl font-medium text-foreground">Providers</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Connect a provider to let agents use it. Click one to add a key.
      </p>

      <div className="mt-6 space-y-2">
        {providers.map((provider) => (
          <ProviderCard key={provider.provider} provider={provider} />
        ))}
      </div>
    </div>
  );
}
