export function DemoModeBanner() {
  return (
    <div className="border-b border-amber-200 bg-amber-50 px-6 py-2 text-center text-xs text-amber-900 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300">
      Demo mode -- no real LLM provider is configured, so every routing
      decision comes from the deterministic mock/heuristic engine, not a real
      model. See the root README&apos;s &quot;What this project is NOT&quot;.
    </div>
  );
}
