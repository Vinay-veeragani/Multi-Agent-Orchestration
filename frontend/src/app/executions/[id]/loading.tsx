import { Bar } from "../../skeleton";

export default function Loading() {
  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <Bar className="h-4 w-24" />
      <Bar className="mt-2 h-6 w-64" />
      <Bar className="mt-2 h-4 w-96" />

      <div className="mt-8">
        <Bar className="mb-2 h-3 w-16" />
        <Bar className="h-32 w-full rounded-lg" />
      </div>

      <div className="mt-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Bar key={i} className="h-16 rounded-lg" />
        ))}
      </div>

      <div className="mt-8">
        <Bar className="h-48 w-full rounded-lg" />
      </div>
    </div>
  );
}
