import { Bell, CircleDot } from "lucide-react";
import Link from "next/link";
import type { Health } from "@/lib/api";
import { CommandPalette } from "./command-palette";

export function TopBar({ health }: { health: Health | null }) {
  const healthy = health != null && health.status === "ok";

  return (
    <header className="flex h-12 shrink-0 items-center gap-4 border-b border-border bg-background px-4">
      <Link href="/" className="flex items-center gap-2 text-sm font-medium">
        <OrchestraMark />
        <span className="hidden sm:inline">Agent Orchestration Engine</span>
      </Link>

      <div className="flex flex-1 justify-center">
        <CommandPalette />
      </div>

      <div className="flex items-center gap-3">
        {health && (
          <span className="rounded-full bg-white/[0.06] px-2 py-0.5 font-mono text-[10px] tracking-wide text-muted-foreground uppercase">
            {health.demo_mode ? "Demo" : "Live"}
          </span>
        )}
        <span
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
          title={health ? JSON.stringify(health) : "API unreachable"}
        >
          <CircleDot
            className={`h-3.5 w-3.5 ${healthy ? "text-success" : "text-danger"}`}
            strokeWidth={2.5}
          />
          {healthy ? "System healthy" : "System unreachable"}
        </span>
        <button
          type="button"
          className="rounded-md p-1.5 text-muted-foreground hover:bg-white/[0.06] hover:text-foreground"
          aria-label="Notifications"
        >
          <Bell className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}

function OrchestraMark() {
  // Multiple paths converging into one -- infrastructure, not a robot head.
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="5" cy="5" r="2.2" className="fill-running" />
      <circle cx="12" cy="5" r="2.2" className="fill-primary" />
      <circle cx="19" cy="5" r="2.2" className="fill-approval" />
      <path
        d="M5 7.2V12M12 7.2V12M19 7.2V12M5 12C5 15 8 15 12 15C16 15 19 15 19 12"
        stroke="currentColor"
        strokeOpacity="0.4"
        strokeWidth="1.4"
        fill="none"
      />
      <circle cx="12" cy="18.5" r="2.4" className="fill-success" />
      <path d="M12 15V16.2" stroke="currentColor" strokeOpacity="0.4" strokeWidth="1.4" />
    </svg>
  );
}
