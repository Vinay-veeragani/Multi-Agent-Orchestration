import ReactMarkdown, { type Components } from "react-markdown";
import { cn } from "@/lib/utils";

// LLM output is Markdown, not plain text -- `**bold**`/bullet lists/code
// fences need to actually render, not show their literal syntax. This is
// the one place that mapping happens, so every "answer" surface looks the
// same regardless of which model produced the text.
const components: Components = {
  p: ({ className, ...props }) => (
    <p className={cn("mb-3 last:mb-0", className)} {...props} />
  ),
  strong: ({ className, ...props }) => (
    <strong className={cn("font-semibold text-foreground", className)} {...props} />
  ),
  ul: ({ className, ...props }) => (
    <ul className={cn("mb-3 list-disc space-y-1 pl-5 last:mb-0", className)} {...props} />
  ),
  ol: ({ className, ...props }) => (
    <ol className={cn("mb-3 list-decimal space-y-1 pl-5 last:mb-0", className)} {...props} />
  ),
  li: ({ className, ...props }) => <li className={cn("pl-0.5", className)} {...props} />,
  a: ({ className, ...props }) => (
    <a
      className={cn("text-primary underline underline-offset-2 hover:no-underline", className)}
      target="_blank"
      rel="noreferrer noopener"
      {...props}
    />
  ),
  code: ({ className, ...props }) => (
    <code
      className={cn("rounded bg-surface px-1 py-0.5 font-mono text-[0.85em]", className)}
      {...props}
    />
  ),
  pre: ({ className, ...props }) => (
    <pre
      className={cn(
        "mb-3 overflow-x-auto rounded border border-border bg-surface p-2.5 font-mono text-xs last:mb-0",
        className,
      )}
      {...props}
    />
  ),
  blockquote: ({ className, ...props }) => (
    <blockquote
      className={cn("mb-3 border-l-2 border-border-strong pl-3 text-muted-foreground last:mb-0", className)}
      {...props}
    />
  ),
  h1: ({ className, ...props }) => (
    <h1 className={cn("mb-2 text-base font-semibold text-foreground", className)} {...props} />
  ),
  h2: ({ className, ...props }) => (
    <h2 className={cn("mb-2 text-sm font-semibold text-foreground", className)} {...props} />
  ),
  h3: ({ className, ...props }) => (
    <h3 className={cn("mb-2 text-sm font-semibold text-foreground", className)} {...props} />
  ),
  hr: ({ className, ...props }) => (
    <hr className={cn("my-3 border-border", className)} {...props} />
  ),
};

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("text-sm leading-relaxed text-foreground", className)}>
      <ReactMarkdown components={components}>{children}</ReactMarkdown>
    </div>
  );
}
