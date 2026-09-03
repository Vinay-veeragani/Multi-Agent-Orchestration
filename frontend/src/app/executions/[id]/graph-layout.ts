// A small, dependency-free layered layout: BFS depth from every root
// (a node with no inbound edge) groups nodes into rows, which is exactly
// the supervisor -> parallel-fanout -> join shape this engine's graphs
// actually have. Not a general-purpose graph layout algorithm -- this
// project's workflows are shallow DAGs, not the kind of graph that needs
// a real force-directed/Sugiyama layout library.

export interface LayoutPosition {
  x: number;
  y: number;
}

const X_GAP = 220;
const Y_GAP = 130;

export function layeredLayout(
  nodeIds: string[],
  edges: { source: string; target: string }[],
): Map<string, LayoutPosition> {
  const incoming = new Map<string, string[]>();
  const outgoing = new Map<string, string[]>();
  for (const id of nodeIds) {
    incoming.set(id, []);
    outgoing.set(id, []);
  }
  for (const edge of edges) {
    incoming.get(edge.target)?.push(edge.source);
    outgoing.get(edge.source)?.push(edge.target);
  }

  const depth = new Map<string, number>();
  const roots = nodeIds.filter((id) => (incoming.get(id) ?? []).length === 0);
  const queue: Array<{ id: string; d: number }> = roots.map((id) => ({ id, d: 0 }));
  const guard = nodeIds.length * nodeIds.length + 8; // cycles should not exist, but never hang
  let steps = 0;

  while (queue.length && steps < guard) {
    steps += 1;
    const { id, d } = queue.shift()!;
    if ((depth.get(id) ?? -1) >= d) continue;
    depth.set(id, d);
    for (const next of outgoing.get(id) ?? []) {
      queue.push({ id: next, d: d + 1 });
    }
  }
  // Anything unreached (shouldn't happen for a valid graph) still gets a slot.
  for (const id of nodeIds) if (!depth.has(id)) depth.set(id, 0);

  const byDepth = new Map<number, string[]>();
  for (const id of nodeIds) {
    const d = depth.get(id) ?? 0;
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d)!.push(id);
  }

  const positions = new Map<string, LayoutPosition>();
  for (const [d, ids] of byDepth) {
    const width = (ids.length - 1) * X_GAP;
    ids.forEach((id, i) => {
      positions.set(id, { x: i * X_GAP - width / 2, y: d * Y_GAP });
    });
  }
  return positions;
}
