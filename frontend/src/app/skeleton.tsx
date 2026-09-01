// Shared loading-skeleton pieces for route-segment loading.tsx files. Not a
// component library -- just enough to avoid repeating the same pulse/table
// markup five times.

export function Bar({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-black/5 dark:bg-white/10 ${className}`} />;
}

export function TableSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="overflow-hidden rounded-lg border border-black/10 dark:border-white/15">
      <div className="border-b border-black/10 bg-black/5 px-3 py-2 dark:border-white/15 dark:bg-white/5">
        <Bar className="h-3 w-32" />
      </div>
      <div className="divide-y divide-black/5 dark:divide-white/10">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex items-center gap-4 px-3 py-3">
            <Bar className="h-3 w-24" />
            <Bar className="h-3 flex-1" />
            <Bar className="h-3 w-16" />
          </div>
        ))}
      </div>
    </div>
  );
}
