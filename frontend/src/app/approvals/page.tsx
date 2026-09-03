import type { Metadata } from "next";
import { listAllPendingApprovals } from "@/lib/api";
import { ApprovalInboxItem } from "./approval-inbox-item";

export const metadata: Metadata = { title: "Approvals" };

export default async function ApprovalsPage() {
  const items = await listAllPendingApprovals();

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="text-xl font-medium text-foreground">Approvals</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Every execution currently paused waiting on a human decision, across the whole
        deployment.
      </p>

      <div className="mt-6 space-y-3">
        {items.map((item) => (
          <ApprovalInboxItem key={item.id} item={item} />
        ))}
        {items.length === 0 && (
          <p className="rounded-md border border-border bg-surface p-6 text-center text-sm text-muted-foreground">
            Nothing needs your attention right now.
          </p>
        )}
      </div>
    </div>
  );
}
