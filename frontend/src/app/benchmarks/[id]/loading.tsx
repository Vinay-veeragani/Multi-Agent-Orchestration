import { Bar } from "../../skeleton";

export default function Loading() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <Bar className="h-4 w-32" />
      <Bar className="mt-2 h-6 w-64" />
      <Bar className="mt-2 h-4 w-96" />

      <div className="mt-8">
        <Bar className="mb-2 h-3 w-48" />
        <Bar className="h-40 w-full rounded-lg" />
      </div>

      <div className="mt-8">
        <Bar className="mb-2 h-3 w-56" />
        <Bar className="h-64 w-full rounded-lg" />
      </div>
    </div>
  );
}
