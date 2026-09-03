// Same-origin status check for the live-execution reconnect logic.
//
// `lib/api.ts` is server-only (holds ORCHESTRATOR_API_KEY), so the client-side
// reconnect hook can't call getExecution() directly. When its SSE connection
// drops, it needs to know whether that's a real network blip (reconnect) or
// the execution genuinely finished (stop) -- an EventSource can't tell those
// apart on its own, since a graceful server-side stream end looks identical
// to a dropped connection. This is the one cheap REST call that answers it.

import { getExecution, isTerminalStatus } from "@/lib/api";

export async function GET(
  _request: Request,
  ctx: RouteContext<"/api/executions/[id]/status">,
): Promise<Response> {
  const { id } = await ctx.params;
  const state = await getExecution(id);
  return Response.json({ status: state.status, isTerminal: isTerminalStatus(state.status) });
}
