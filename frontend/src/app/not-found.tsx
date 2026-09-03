import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl px-6 py-20 text-center">
      <p className="text-sm font-medium text-subtle-foreground">404</p>
      <h1 className="mt-2 text-xl font-medium text-foreground">Not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        There&apos;s nothing at this address -- it may have been a typo, or the
        execution/agent/benchmark id doesn&apos;t exist.
      </p>
      <Link
        href="/"
        className="mt-6 inline-block rounded-md bg-white/[0.06] px-3 py-1.5 text-sm font-medium text-foreground hover:bg-white/[0.1]"
      >
        &larr; Back to executions
      </Link>
    </div>
  );
}
