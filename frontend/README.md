# agent-orchestration-engine -- web UI

Next.js 16 (App Router) + TypeScript + Tailwind v4. A read-mostly view over
the orchestrator's HTTP API, plus a human-in-the-loop approve/reject panel.
There is no separate write path or database of its own -- every mutation
goes through the same API the CLI uses (see [`../docs/interfaces.md`](../docs/interfaces.md)).

## Pages

| Route | Shows |
|---|---|
| `/` | Recent executions -- status, cost, created time |
| `/executions/new` | Start an execution (task, optional success criteria, optional workflow) |
| `/executions/[id]` | Nodes, budget usage, agent/tool invocations, a Cancel button while in flight, and either a live SSE event stream (in-flight) or a play/step/scrub replay (finished) |
| `/benchmarks` | Recent `orchestrator benchmark` reports |
| `/benchmarks/[id]` | One report's ablation comparison and per-scenario pass/fail grid |
| `/workflows` | Registered (hand-authored) workflows |
| `/workflows/new` | A limited-scope builder: agent/join/terminal nodes and edges, not a graph editor |

## Running it

Requires the orchestrator API already running (see the root
[`README.md`](../README.md#quickstart)).

```bash
cp .env.local.example .env.local   # set ORCHESTRATOR_API_URL / _API_KEY
npm install
npm run dev
```

## How it talks to the API

`src/lib/api.ts` is a **server-only** client -- imported only from Server
Components and Route Handlers, never from a `"use client"` file. That is
what keeps `ORCHESTRATOR_API_KEY` out of the browser bundle: every request
the browser itself makes goes to one of this app's own same-origin routes,
which hold the key server-side:

- **`src/app/api/stream/[id]/route.ts`** proxies `GET /executions/{id}/stream`
  (Server-Sent Events) byte-for-byte, attaching the key server-side -- a
  plain browser `EventSource` cannot set custom headers, so this is what
  lets the live-events view (`executions/[id]/live-events.tsx`) use one
  directly.
- **`src/app/executions/[id]/approval-actions.ts`** is a Next.js Server
  Action -- the approve/reject mutation runs server-side and calls the
  existing `/approve`/`/reject` routes; the browser never holds the key.

## UI details

- Per-page `<title>` (via `export const metadata` / `generateMetadata`),
  active-route highlighting in the header nav (`nav-links.tsx`), a custom
  `not-found.tsx`, a custom `error.tsx` (a client-side error boundary --
  its actual rendering only happens during browser hydration, which is
  the one thing about this app that genuinely can't be checked with
  `curl`), and `loading.tsx` skeletons on the list/detail routes.
- A route that calls `notFound()` returns HTTP `200` with the custom
  not-found content and a `<meta name="robots" content="noindex">` tag,
  not a real `404` status -- confirmed as an inherent trade-off of this
  Next.js version's streaming model (present with or without a
  `loading.tsx` on that route), not something introduced here. Harmless
  for an internal dashboard with no public search-indexing concern; see
  Next's own `notFound()` docs ("Status codes") if this ever needs a real
  404 status, which requires checking existence before the response
  starts streaming.

## Development

```bash
npm run lint     # eslint
npm run build    # next build, includes a full TypeScript check
```

No test suite of its own yet -- correctness of the data it displays is
covered by the backend's integration tests
(`tests/integration/test_api.py`), which exercise the same routes against
real PostgreSQL and Redis.
