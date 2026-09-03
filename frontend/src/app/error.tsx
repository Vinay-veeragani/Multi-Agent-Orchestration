"use client";

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // The one place this app deliberately logs to the browser console: a
    // client-side error boundary has no other destination for the failure
    // it just caught.
    console.error(error);
  }, [error]);

  return (
    <div className="mx-auto max-w-2xl px-6 py-20 text-center">
      <p className="text-sm font-medium text-danger">Something went wrong</p>
      <h1 className="mt-2 text-xl font-medium text-foreground">
        {error.message || "An unexpected error occurred"}
      </h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Often this means the orchestrator API isn&apos;t reachable, or
        <code className="mx-1 font-mono">ORCHESTRATOR_API_KEY</code>
        doesn&apos;t match what it expects.
      </p>
      <button
        type="button"
        onClick={() => reset()}
        className="mt-6 rounded-md bg-white/[0.06] px-3 py-1.5 text-sm font-medium text-foreground hover:bg-white/[0.1]"
      >
        Try again
      </button>
    </div>
  );
}
