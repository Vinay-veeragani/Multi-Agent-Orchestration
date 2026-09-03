import type { Budget, BudgetUsage } from "@/lib/api";

function Bar({ used, max, label, unit }: { used: number; max: number | null; label: string; unit?: string }) {
  const pct = max != null && max > 0 ? Math.min(100, (used / max) * 100) : null;
  return (
    <div>
      <div className="flex items-center justify-between text-[10px] text-subtle-foreground">
        <span>{label}</span>
        <span className="font-mono">
          {used.toLocaleString()}
          {max != null ? ` / ${max.toLocaleString()}` : ""}
          {unit}
        </span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-white/[0.06]">
        {pct != null && (
          <div
            className={`h-full rounded-full ${pct > 85 ? "bg-danger" : pct > 60 ? "bg-warning" : "bg-primary"}`}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
    </div>
  );
}

export function BudgetMeter({ budget, usage }: { budget: Budget; usage: BudgetUsage }) {
  return (
    <div className="rounded-md border border-border bg-surface p-3">
      <div className="mb-2 flex items-center justify-between text-xs font-medium text-muted-foreground">
        <span>Budget</span>
        <span className="font-mono text-foreground">
          ${usage.cost_usd.toFixed(4)}
          {budget.max_cost_usd != null && ` / $${budget.max_cost_usd.toFixed(2)}`}
        </span>
      </div>
      <div className="space-y-2.5">
        <Bar
          used={usage.input_tokens + usage.output_tokens}
          max={budget.max_tokens}
          label="Tokens"
        />
        <Bar used={usage.tool_calls} max={budget.max_tool_calls} label="Tool calls" />
        <Bar used={usage.agent_steps} max={budget.max_agent_steps} label="Agent steps" />
        <Bar used={usage.retries} max={null} label="Retries" />
      </div>
    </div>
  );
}
