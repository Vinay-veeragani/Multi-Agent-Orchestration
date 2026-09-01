// Same-origin SSE proxy for GET /executions/{id}/stream.
//
// A browser EventSource cannot set custom headers, and the orchestrator API
// requires X-API-Key on every route (see docs/interfaces.md's "Live event
// streaming" section). This route handler holds the key server-side and
// forwards the orchestrator's byte stream straight through, so the browser
// only ever talks to this app's own origin.

import { orchestratorConfig } from "@/lib/api";

export async function GET(request: Request, ctx: RouteContext<"/api/stream/[id]">) {
  const { id } = await ctx.params;
  const { baseUrl, apiKey } = orchestratorConfig();
  const afterId = new URL(request.url).searchParams.get("after_id");

  const upstreamUrl = new URL(`${baseUrl}/executions/${id}/stream`);
  if (afterId) upstreamUrl.searchParams.set("after_id", afterId);

  const upstream = await fetch(upstreamUrl, {
    headers: apiKey ? { "X-API-Key": apiKey } : {},
    // The whole point is a long-lived, unbuffered response -- never let this
    // pass through Next.js's own fetch cache.
    cache: "no-store",
  });

  if (!upstream.ok || !upstream.body) {
    const body = await upstream.text().catch(() => "");
    return new Response(body || upstream.statusText, { status: upstream.status });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
