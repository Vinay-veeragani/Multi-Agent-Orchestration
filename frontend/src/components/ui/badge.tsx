import { type VariantProps, cva } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap",
  {
    variants: {
      variant: {
        neutral: "bg-white/[0.06] text-muted-foreground",
        success: "bg-success-muted text-success",
        warning: "bg-warning-muted text-warning",
        danger: "bg-danger-muted text-danger",
        running: "bg-running-muted text-running",
        approval: "bg-approval-muted text-approval",
        primary: "bg-primary-muted text-primary",
      },
    },
    defaultVariants: {
      variant: "neutral",
    },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/** Maps this engine's execution/node status strings to a Badge variant. */
export function statusVariant(status: string): BadgeProps["variant"] {
  switch (status) {
    case "succeeded":
      return "success";
    case "failed":
    case "budget_exceeded":
    case "timed_out":
      return "danger";
    case "running":
      return "running";
    case "waiting_for_approval":
      return "approval";
    case "cancelled":
    case "skipped":
      return "neutral";
    default:
      return "neutral";
  }
}
