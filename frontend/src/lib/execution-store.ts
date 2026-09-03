import { create } from "zustand";
import type { ExecutionEvent } from "@/lib/api";

// node_id lives on every node-lifecycle event as a top-level field (see
// WorkflowExecutor's emit() calls), so status is reconstructed purely from
// (event.type, event.node_id) -- the same approach replay.tsx already uses.
const NODE_STATUS_FOR_EVENT: Record<string, string> = {
  node_started: "running",
  node_completed: "succeeded",
  node_failed: "failed",
  node_skipped: "skipped",
  retry_exhausted: "failed",
};

interface ExecutionStoreState {
  events: ExecutionEvent[];
  nodeStatus: Record<string, string>;
  seenIds: Set<string>;
  selectedNodeId: string | null;
  connection: "idle" | "connecting" | "live" | "reconnecting" | "closed";
  seed: (events: ExecutionEvent[], initialStatus: Record<string, string>) => void;
  push: (event: ExecutionEvent) => void;
  setConnection: (connection: ExecutionStoreState["connection"]) => void;
  selectNode: (nodeId: string | null) => void;
  reset: () => void;
}

export const useExecutionStore = create<ExecutionStoreState>((set, get) => ({
  events: [],
  nodeStatus: {},
  seenIds: new Set(),
  selectedNodeId: null,
  connection: "idle",

  seed: (events, initialStatus) => {
    const seenIds = new Set(events.map((e) => e.id));
    const nodeStatus = { ...initialStatus };
    for (const event of events) {
      const mapped = event.node_id && NODE_STATUS_FOR_EVENT[event.type];
      if (mapped && event.node_id) nodeStatus[event.node_id] = mapped;
    }
    set({ events, nodeStatus, seenIds });
  },

  push: (event) => {
    const { seenIds } = get();
    if (seenIds.has(event.id)) return;
    seenIds.add(event.id);
    set((s) => {
      const nodeStatus = { ...s.nodeStatus };
      const mapped = event.node_id && NODE_STATUS_FOR_EVENT[event.type];
      if (mapped && event.node_id) nodeStatus[event.node_id] = mapped;
      return { events: [...s.events, event], nodeStatus, seenIds };
    });
  },

  setConnection: (connection) => set({ connection }),
  selectNode: (nodeId) => set({ selectedNodeId: nodeId }),
  reset: () =>
    set({
      events: [],
      nodeStatus: {},
      seenIds: new Set(),
      selectedNodeId: null,
      connection: "idle",
    }),
}));
