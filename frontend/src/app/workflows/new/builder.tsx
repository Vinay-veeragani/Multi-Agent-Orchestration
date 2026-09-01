"use client";

import { useActionState, useMemo, useState } from "react";
import type { AgentSummary, WorkflowEdgeInput, WorkflowNodeInput } from "@/lib/api";
import { createWorkflowAction, type BuilderState } from "./actions";

const initialState: BuilderState = { status: "idle" };

let nextRowId = 0;
function rowId(): number {
  nextRowId += 1;
  return nextRowId;
}

interface NodeRow extends WorkflowNodeInput {
  rowId: number;
}
interface EdgeRow extends WorkflowEdgeInput {
  rowId: number;
}

function stripRowId<T extends { rowId: number }>(row: T): Omit<T, "rowId"> {
  const rest: Partial<T> = { ...row };
  delete rest.rowId;
  return rest as Omit<T, "rowId">;
}

export function WorkflowBuilder({ agents }: { agents: AgentSummary[] }) {
  const [name, setName] = useState("");
  const [nodes, setNodes] = useState<NodeRow[]>([
    { rowId: rowId(), id: "a", kind: "agent", agent_id: agents[0]?.id },
    { rowId: rowId(), id: "final", kind: "terminal" },
  ]);
  const [edges, setEdges] = useState<EdgeRow[]>([{ rowId: rowId(), source: "a", target: "final" }]);

  const workflowJson = useMemo(() => {
    return JSON.stringify({
      name,
      nodes: nodes.map(stripRowId),
      edges: edges.map(stripRowId),
    });
  }, [name, nodes, edges]);

  const [state, formAction, pending] = useActionState(createWorkflowAction, initialState);

  return (
    <form action={formAction} className="space-y-6">
      <input type="hidden" name="workflow" value={workflowJson} />

      <div>
        <label htmlFor="wf-name" className="text-xs font-medium text-neutral-500">
          Name
        </label>
        <input
          id="wf-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          className="mt-1 block w-full rounded border border-black/10 bg-transparent px-2 py-1.5 text-sm dark:border-white/15"
        />
      </div>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-medium text-neutral-500">Nodes</h2>
          <button
            type="button"
            onClick={() =>
              setNodes((rows) => [
                ...rows,
                { rowId: rowId(), id: `node_${rows.length + 1}`, kind: "agent" },
              ])
            }
            className="rounded bg-black/5 px-2 py-1 text-xs font-medium hover:bg-black/10 dark:bg-white/10"
          >
            + Add node
          </button>
        </div>
        <div className="space-y-2">
          {nodes.map((node) => (
            <div key={node.rowId} className="flex flex-wrap items-center gap-2 text-sm">
              <input
                value={node.id}
                onChange={(event) =>
                  setNodes((rows) =>
                    rows.map((r) => (r.rowId === node.rowId ? { ...r, id: event.target.value } : r)),
                  )
                }
                placeholder="node id"
                className="w-32 rounded border border-black/10 bg-transparent px-2 py-1 font-mono dark:border-white/15"
              />
              <select
                value={node.kind}
                onChange={(event) =>
                  setNodes((rows) =>
                    rows.map((r) =>
                      r.rowId === node.rowId
                        ? { ...r, kind: event.target.value as WorkflowNodeInput["kind"] }
                        : r,
                    ),
                  )
                }
                className="rounded border border-black/10 bg-transparent px-2 py-1 dark:border-white/15"
              >
                <option value="agent">agent</option>
                <option value="join">join</option>
                <option value="terminal">terminal</option>
              </select>
              {node.kind === "agent" && (
                <select
                  value={node.agent_id ?? ""}
                  onChange={(event) =>
                    setNodes((rows) =>
                      rows.map((r) =>
                        r.rowId === node.rowId ? { ...r, agent_id: event.target.value } : r,
                      ),
                    )
                  }
                  className="rounded border border-black/10 bg-transparent px-2 py-1 dark:border-white/15"
                >
                  <option value="">select agent&hellip;</option>
                  {agents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.id}
                    </option>
                  ))}
                </select>
              )}
              {node.kind === "join" && (
                <select
                  value={node.join_policy ?? "all"}
                  onChange={(event) =>
                    setNodes((rows) =>
                      rows.map((r) =>
                        r.rowId === node.rowId
                          ? {
                              ...r,
                              join_policy: event.target.value as WorkflowNodeInput["join_policy"],
                            }
                          : r,
                      ),
                    )
                  }
                  className="rounded border border-black/10 bg-transparent px-2 py-1 dark:border-white/15"
                >
                  <option value="all">all</option>
                  <option value="any">any</option>
                  <option value="quorum">quorum</option>
                </select>
              )}
              <button
                type="button"
                onClick={() => setNodes((rows) => rows.filter((r) => r.rowId !== node.rowId))}
                className="text-xs text-red-600 hover:underline dark:text-red-400"
              >
                remove
              </button>
            </div>
          ))}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-medium text-neutral-500">Edges</h2>
          <button
            type="button"
            onClick={() =>
              setEdges((rows) => [...rows, { rowId: rowId(), source: "", target: "" }])
            }
            className="rounded bg-black/5 px-2 py-1 text-xs font-medium hover:bg-black/10 dark:bg-white/10"
          >
            + Add edge
          </button>
        </div>
        <div className="space-y-2">
          {edges.map((edge) => (
            <div key={edge.rowId} className="flex flex-wrap items-center gap-2 text-sm">
              <select
                value={edge.source}
                onChange={(event) =>
                  setEdges((rows) =>
                    rows.map((r) =>
                      r.rowId === edge.rowId ? { ...r, source: event.target.value } : r,
                    ),
                  )
                }
                className="rounded border border-black/10 bg-transparent px-2 py-1 font-mono dark:border-white/15"
              >
                <option value="">source&hellip;</option>
                {nodes.map((n) => (
                  <option key={n.rowId} value={n.id}>
                    {n.id}
                  </option>
                ))}
              </select>
              <span className="text-neutral-400">&rarr;</span>
              <select
                value={edge.target}
                onChange={(event) =>
                  setEdges((rows) =>
                    rows.map((r) =>
                      r.rowId === edge.rowId ? { ...r, target: event.target.value } : r,
                    ),
                  )
                }
                className="rounded border border-black/10 bg-transparent px-2 py-1 font-mono dark:border-white/15"
              >
                <option value="">target&hellip;</option>
                {nodes.map((n) => (
                  <option key={n.rowId} value={n.id}>
                    {n.id}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => setEdges((rows) => rows.filter((r) => r.rowId !== edge.rowId))}
                className="text-xs text-red-600 hover:underline dark:text-red-400"
              >
                remove
              </button>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium text-neutral-500">Preview</h2>
        <pre className="overflow-x-auto rounded-lg bg-black/5 p-3 text-xs dark:bg-white/5">
          {JSON.stringify(JSON.parse(workflowJson || "{}"), null, 2)}
        </pre>
      </section>

      {state.status === "error" && (
        <p className="text-sm text-red-700 dark:text-red-400">{state.message}</p>
      )}

      <button
        type="submit"
        disabled={pending}
        className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
      >
        {pending ? "Creating…" : "Create workflow"}
      </button>
    </form>
  );
}
