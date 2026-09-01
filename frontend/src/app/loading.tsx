import { Bar, TableSkeleton } from "./skeleton";

export default function Loading() {
  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <div className="flex items-center justify-between">
        <Bar className="h-6 w-40" />
        <Bar className="h-8 w-32" />
      </div>
      <Bar className="mt-2 h-4 w-72" />
      <div className="mt-6">
        <TableSkeleton />
      </div>
    </div>
  );
}
