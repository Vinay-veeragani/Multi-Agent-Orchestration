"use client";

import {
  Bot,
  ChevronsLeft,
  ChevronsRight,
  FileSearch,
  FlaskConical,
  Gauge,
  Key,
  Library,
  Server,
  Settings,
  Shield,
  Wrench,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { cn } from "@/lib/utils";

interface NavItem {
  label: string;
  href?: string;
  icon: React.ComponentType<{ className?: string }>;
}

interface NavSection {
  label: string;
  items: NavItem[];
}

// Every item without an `href` is on the roadmap but not wired to a real
// page/route yet -- shown for the product's intended shape (see the
// frontend spec), rendered disabled rather than linked, so nothing here
// points at fabricated data.
const SECTIONS: NavSection[] = [
  {
    label: "Workspace",
    items: [
      { label: "Executions", href: "/", icon: Gauge },
      { label: "Agents", href: "/agents", icon: Bot },
      { label: "Tools", icon: Wrench },
      { label: "Knowledge", icon: Library },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { label: "Evaluations", href: "/benchmarks", icon: FileSearch },
      { label: "Experiments", icon: FlaskConical },
    ],
  },
  {
    label: "Operations",
    items: [
      { label: "Providers", icon: Key },
      { label: "Policies", icon: Shield },
      { label: "Observability", icon: Server },
    ],
  },
  {
    label: "System",
    items: [{ label: "Settings", icon: Settings }],
  },
];

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);
  const pathname = usePathname();

  return (
    <aside
      className={cn(
        "flex h-full shrink-0 flex-col border-r border-border bg-surface transition-[width] duration-150",
        collapsed ? "w-[68px]" : "w-[248px]",
      )}
    >
      <nav className="flex-1 space-y-5 overflow-y-auto px-2.5 py-4">
        {SECTIONS.map((section) => (
          <div key={section.label}>
            {!collapsed && (
              <div className="px-2 pb-1.5 text-[10px] font-medium tracking-wide text-subtle-foreground uppercase">
                {section.label}
              </div>
            )}
            <div className="space-y-0.5">
              {section.items.map((item) => {
                const active = item.href != null && (pathname === item.href || (item.href !== "/" && pathname.startsWith(`${item.href}/`)));
                const content = (
                  <span
                    className={cn(
                      "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors",
                      collapsed && "justify-center",
                      item.href == null && "cursor-default text-subtle-foreground",
                      item.href != null && !active && "text-muted-foreground hover:bg-white/[0.05] hover:text-foreground",
                      active && "bg-primary-muted text-primary",
                    )}
                    title={collapsed ? item.label : undefined}
                  >
                    <item.icon className="h-4 w-4 shrink-0" />
                    {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
                    {!collapsed && item.href == null && (
                      <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[9px] text-subtle-foreground">
                        soon
                      </span>
                    )}
                  </span>
                );
                return item.href ? (
                  <Link key={item.label} href={item.href}>
                    {content}
                  </Link>
                ) : (
                  <div key={item.label}>{content}</div>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="border-t border-border p-2.5">
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className={cn(
            "flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-white/[0.05] hover:text-foreground",
            collapsed && "justify-center",
          )}
        >
          {collapsed ? <ChevronsRight className="h-4 w-4" /> : <ChevronsLeft className="h-4 w-4" />}
          {!collapsed && "Collapse"}
        </button>
      </div>
    </aside>
  );
}
