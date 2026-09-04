"use client";

import "@xyflow/react/dist/style.css";
import {
  Background,
  BackgroundVariant,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { CircleDashed } from "lucide-react";
import { useMemo } from "react";
import type { AgentInvocation, ToolInvocation, WorkflowDetail } from "@/lib/api";
import { useExecutionStore } from "@/lib/execution-store";
import { layeredLayout } from "./graph-layout";
import { ExecutionGraphNode, type ExecutionNodeData } from "./graph-node";

const nodeTypes = { execution: ExecutionGraphNode };

export function ExecutionGraph({
  workflow,
  initialNodeStatus,
  agentInvocations,
  toolInvocations,
}: {
  workflow: WorkflowDetail;
  initialNodeStatus: Record<string, string>;
  agentInvocations: AgentInvocation[];
  toolInvocations: ToolInvocation[];
}) {
  const liveStatus = useExecutionStore((s) => s.nodeStatus);
  const selectedNodeId = useExecutionStore((s) => s.selectedNodeId);
  const selectNode = useExecutionStore((s) => s.selectNode);

  const positions = useMemo(
    () => layeredLayout(workflow.nodes.map((n) => n.id), workflow.edges),
    [workflow],
  );

  const invocationByNode = useMemo(() => {
    const map = new Map<string, AgentInvocation>();
    for (const inv of agentInvocations) {
      if (inv.node_id) map.set(inv.node_id, inv);
    }
    return map;
  }, [agentInvocations]);

  const toolCountByNode = useMemo(() => {
    const counts = new Map<string, number>();
    for (const inv of toolInvocations) {
      if (inv.node_id) counts.set(inv.node_id, (counts.get(inv.node_id) ?? 0) + 1);
    }
    return counts;
  }, [toolInvocations]);

  const nodes: Node[] = useMemo(
    () =>
      workflow.nodes.map((node) => {
        const pos = positions.get(node.id) ?? { x: 0, y: 0 };
        const invocation = invocationByNode.get(node.id);
        const status = liveStatus[node.id] ?? initialNodeStatus[node.id] ?? "pending";
        const data: ExecutionNodeData = {
          label: node.name || node.id,
          kind: node.kind,
          status,
          durationSeconds: invocation?.duration_seconds ?? undefined,
          tokens: invocation?.tokens ?? undefined,
          toolCalls: toolCountByNode.get(node.id) ?? undefined,
        };
        return {
          id: node.id,
          type: "execution",
          position: pos,
          data: data as unknown as Record<string, unknown>,
          selected: selectedNodeId === node.id,
        };
      }),
    [workflow.nodes, positions, invocationByNode, toolCountByNode, liveStatus, initialNodeStatus, selectedNodeId],
  );

  const edges: Edge[] = useMemo(
    () =>
      workflow.edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.label || undefined,
        style: { stroke: "var(--border-strong)" },
        animated:
          (liveStatus[edge.source] ?? initialNodeStatus[edge.source]) === "running" ||
          (liveStatus[edge.target] ?? initialNodeStatus[edge.target]) === "running",
      })),
    [workflow.edges, liveStatus, initialNodeStatus],
  );

  // ExecutionWorkspace intercepts the terminal-and-nothing-ran outcome
  // itself (see its own comment) and never renders this component for it,
  // so reaching here with no graph activity yet only ever means one thing:
  // the execution is still running and the supervisor has not made its
  // first move yet -- there is genuinely nothing to draw, but it is not
  // finished, either.
  const nothingYet = agentInvocations.length === 0 && workflow.edges.length === 0;
  if (nothingYet) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-center">
        <CircleDashed className="h-5 w-5 animate-pulse text-subtle-foreground" />
        <p className="max-w-[220px] text-xs text-subtle-foreground">
          Waiting for the supervisor&apos;s first decision…
        </p>
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_event, node) => selectNode(node.id)}
        onPaneClick={() => selectNode(null)}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        proOptions={{ hideAttribution: true }}
        colorMode="dark"
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--border)" />
      </ReactFlow>
    </div>
  );
}
