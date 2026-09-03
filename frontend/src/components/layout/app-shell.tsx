import type { Health } from "@/lib/api";
import { Sidebar } from "./sidebar";
import { TopBar } from "./top-bar";

export function AppShell({
  health,
  children,
}: {
  health: Health | null;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col">
      <TopBar health={health} />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
