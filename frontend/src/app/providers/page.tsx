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
        Bring your own key for any provider below -- it takes effect on the next execution this
        process starts, no restart required. A key entered here overrides the matching{" "}
        <code className="font-mono">ORCH_*_API_KEY</code> environment variable. Pick a model for a
        provider to force every routing decision (the supervisor&apos;s included) onto it instead
        of the router&apos;s usual cost-aware pick; leave it on &quot;Auto&quot; to let the router
        choose per call as usual.
      </p>

      <div className="mt-6 grid gap-4 sm:grid-cols-2">
        {providers.map((provider) => (
          <ProviderCard key={provider.provider} provider={provider} />
        ))}
      </div>
    </div>
  );
}
