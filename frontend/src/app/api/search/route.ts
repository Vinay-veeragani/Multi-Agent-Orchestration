// Same-origin search proxy for the command palette.
//
// `lib/api.ts` is server-only (it holds ORCHESTRATOR_API_KEY), so a "use
// client" component like the command palette cannot call it directly. This
// route runs the real listExecutions/listAgents calls server-side and
// returns a small, already-filtered result set -- there is no backend search
// endpoint, so this does the substring match itself over what those two
// routes return.

import { listAgents, listExecutions } from "@/lib/api";

export interface SearchResult {
  group: "Executions" | "Agents";
  id: string;
  label: string;
  sublabel: string;
  href: string;
}

const MAX_PER_GROUP = 8;

export async function GET(request: Request): Promise<Response> {
  const q = new URL(request.url).searchParams.get("q")?.trim().toLowerCase() ?? "";
  if (q.length < 2) return Response.json([]);

  const [executions, agents] = await Promise.all([
    listExecutions({ limit: 100 }).catch(() => []),
    listAgents().catch(() => []),
  ]);

  const results: SearchResult[] = [
    ...executions
      .filter((e) => e.id.toLowerCase().includes(q) || e.task.toLowerCase().includes(q))
      .slice(0, MAX_PER_GROUP)
      .map((e) => ({
        group: "Executions" as const,
        id: e.id,
        label: e.id,
        sublabel: e.task,
        href: `/executions/${e.id}`,
      })),
    ...agents
      .filter((a) => a.id.toLowerCase().includes(q) || a.name.toLowerCase().includes(q))
      .slice(0, MAX_PER_GROUP)
      .map((a) => ({
        group: "Agents" as const,
        id: a.id,
        label: a.id,
        sublabel: a.name,
        href: `/agents/${a.id}`,
      })),
  ];

  return Response.json(results);
}
