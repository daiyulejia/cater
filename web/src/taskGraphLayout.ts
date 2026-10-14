import { graphlib, layout } from '@dagrejs/dagre'
import type { WorkState } from './WorkItems'

type TopologyItem = Pick<WorkState['items'][number], 'id' | 'depends_on'>
const NODE_WIDTH = 248
const NODE_HEIGHT = 160

// Layout uses topology alone, so status polling cannot shift nodes or reset the viewport.
export function layoutTaskGraph(items: readonly TopologyItem[]) {
  const ordered = [...items].sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
  const ids = new Set(ordered.map(item => item.id))
  const graph = new graphlib.Graph().setGraph({ rankdir: 'LR', nodesep: 36, ranksep: 72, marginx: 24, marginy: 24 })
    .setDefaultEdgeLabel(() => ({}))
  ordered.forEach(item => graph.setNode(item.id, { width: NODE_WIDTH, height: NODE_HEIGHT }))
  const edges = ordered.flatMap(item => [...new Set(item.depends_on)].filter(id => ids.has(id)).sort().map(source => ({
    id: JSON.stringify([source, item.id]), source, target: item.id,
  })))
  edges.forEach(edge => graph.setEdge(edge.source, edge.target))
  if (ordered.length) layout(graph)
  return {
    nodes: ordered.map(item => {
      const position = graph.node(item.id)
      return { id: item.id, position: { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 }, width: NODE_WIDTH, height: NODE_HEIGHT }
    }),
    edges,
  }
}
