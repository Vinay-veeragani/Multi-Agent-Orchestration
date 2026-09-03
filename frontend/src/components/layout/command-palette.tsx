"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Command } from "cmdk";
import { Activity, Bot, FileSearch, Gauge, Plus, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { SearchResult } from "@/app/api/search/route";

interface Action {
  label: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  group: "Navigate" | "Actions";
}

const ACTIONS: Action[] = [
  { label: "Overview", href: "/", icon: Gauge, group: "Navigate" },
  { label: "Agents", href: "/agents", icon: Bot, group: "Navigate" },
  { label: "Benchmarks / Evaluations", href: "/benchmarks", icon: FileSearch, group: "Navigate" },
  { label: "New execution", href: "/executions/new", icon: Plus, group: "Actions" },
];

const GROUP_ICON: Record<SearchResult["group"], React.ComponentType<{ className?: string }>> = {
  Executions: Activity,
  Agents: Bot,
};

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const router = useRouter();

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // Debounced live search over real executions/agents (see
  // src/app/api/search/route.ts) -- the static ACTIONS list below is filtered
  // locally in the same render, so both appear together as you type.
  useEffect(() => {
    const trimmed = query.trim();
    const controller = new AbortController();
    const timer = setTimeout(() => {
      if (trimmed.length < 2) {
        setResults([]);
        setSearching(false);
        return;
      }
      setSearching(true);
      fetch(`/api/search?q=${encodeURIComponent(trimmed)}`, { signal: controller.signal })
        .then((res) => (res.ok ? (res.json() as Promise<SearchResult[]>) : []))
        .then(setResults)
        .catch(() => {})
        .finally(() => setSearching(false));
    }, 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  function go(href: string) {
    setOpen(false);
    router.push(href as Parameters<typeof router.push>[0]);
  }

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (!next) {
      setQuery("");
      setResults([]);
    }
  }

  const filteredActions = ACTIONS.filter((a) =>
    a.label.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const executionResults = results.filter((r) => r.group === "Executions");
  const agentResults = results.filter((r) => r.group === "Agents");

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex h-8 w-full max-w-xs items-center gap-2 rounded-md border border-border bg-white/[0.03] px-3 text-xs text-muted-foreground transition-colors hover:border-border-strong hover:text-foreground"
      >
        <Search className="h-3.5 w-3.5" />
        <span className="flex-1 text-left">Search anything...</span>
      </button>

      <Dialog.Root open={open} onOpenChange={onOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-[2px]" />
          <Dialog.Content className="fixed top-[20%] left-1/2 z-50 w-full max-w-lg -translate-x-1/2 overflow-hidden rounded-lg border border-border-strong bg-elevated shadow-2xl">
            <Dialog.Title className="sr-only">Command palette</Dialog.Title>
            <Command className="flex flex-col" loop shouldFilter={false}>
              <div className="flex items-center gap-2 border-b border-border px-3">
                <Search className="h-4 w-4 text-muted-foreground" />
                <Command.Input
                  autoFocus
                  value={query}
                  onValueChange={setQuery}
                  placeholder="Search executions, agents, pages..."
                  className="h-11 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-subtle-foreground"
                />
                {searching && (
                  <span className="text-[10px] text-subtle-foreground">Searching…</span>
                )}
              </div>
              <Command.List className="max-h-80 overflow-y-auto p-2">
                <Command.Empty className="px-2 py-6 text-center text-sm text-muted-foreground">
                  No results.
                </Command.Empty>

                {executionResults.length > 0 && (
                  <Command.Group
                    heading="Executions"
                    className="px-2 pt-2 pb-1 text-[10px] font-medium tracking-wide text-subtle-foreground uppercase [&_[cmdk-group-items]]:mt-1"
                  >
                    {executionResults.map((result) => {
                      const Icon = GROUP_ICON[result.group];
                      return (
                        <Command.Item
                          key={`execution-${result.id}`}
                          value={`execution-${result.id}`}
                          onSelect={() => go(result.href)}
                          className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-2 text-sm text-foreground data-[selected=true]:bg-white/[0.06]"
                        >
                          <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate font-mono text-xs">{result.label}</span>
                            <span className="block truncate text-xs text-subtle-foreground">
                              {result.sublabel}
                            </span>
                          </span>
                        </Command.Item>
                      );
                    })}
                  </Command.Group>
                )}

                {agentResults.length > 0 && (
                  <Command.Group
                    heading="Agents"
                    className="px-2 pt-2 pb-1 text-[10px] font-medium tracking-wide text-subtle-foreground uppercase [&_[cmdk-group-items]]:mt-1"
                  >
                    {agentResults.map((result) => {
                      const Icon = GROUP_ICON[result.group];
                      return (
                        <Command.Item
                          key={`agent-${result.id}`}
                          value={`agent-${result.id}`}
                          onSelect={() => go(result.href)}
                          className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-2 text-sm text-foreground data-[selected=true]:bg-white/[0.06]"
                        >
                          <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate font-mono text-xs">{result.label}</span>
                            <span className="block truncate text-xs text-subtle-foreground">
                              {result.sublabel}
                            </span>
                          </span>
                        </Command.Item>
                      );
                    })}
                  </Command.Group>
                )}

                {(["Navigate", "Actions"] as const).map((group) => {
                  const actions = filteredActions.filter((a) => a.group === group);
                  if (actions.length === 0) return null;
                  return (
                    <Command.Group
                      key={group}
                      heading={group}
                      className="px-2 pt-2 pb-1 text-[10px] font-medium tracking-wide text-subtle-foreground uppercase [&_[cmdk-group-items]]:mt-1"
                    >
                      {actions.map((action) => (
                        <Command.Item
                          key={action.href}
                          value={action.label}
                          onSelect={() => go(action.href)}
                          className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-2 text-sm text-foreground data-[selected=true]:bg-white/[0.06]"
                        >
                          <action.icon className="h-4 w-4 text-muted-foreground" />
                          {action.label}
                        </Command.Item>
                      ))}
                    </Command.Group>
                  );
                })}
              </Command.List>
            </Command>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
