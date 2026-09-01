import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl px-6 py-20 text-center">
      <p className="text-sm font-medium text-neutral-500">404</p>
      <h1 className="mt-2 text-xl font-medium">Not found</h1>
      <p className="mt-2 text-sm text-neutral-500">
        There&apos;s nothing at this address -- it may have been a typo, or the
        execution/workflow/benchmark id doesn&apos;t exist.
      </p>
      <Link
        href="/"
        className="mt-6 inline-block rounded bg-black/5 px-3 py-1.5 text-sm font-medium hover:bg-black/10 dark:bg-white/10 dark:hover:bg-white/15"
      >
        &larr; Back to executions
      </Link>
    </div>
  );
}
