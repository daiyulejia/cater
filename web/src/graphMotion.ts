import { isTeamHost, teamContinuationSource } from './team'
import type { TeamEvent, TeamState } from './team'
import type { TeamGraph } from './teamGraph'

export type GraphMotion = 'idle' | 'running' | 'handoff'

const running = (status: unknown) => status === 'running' || status === 'started'

export function teamGraphMotion(team: TeamState, input: readonly TeamEvent[], graph: TeamGraph, motionAllowed?: boolean) {
  const nodes = new Map<string, GraphMotion>()
  const edges = new Set<string>()
  // The caller must confirm both execution control and a live activity handle.
  if (!team.enabled || motionAllowed !== true) return { nodes, edges }

  const events = [...input].sort((a, b) => a.seq - b.seq)
  const bySeq = new Map(events.map(event => [event.seq, event]))
  const hosts = events.filter(isTeamHost)
  const objective = hosts.filter(event => event.type === 'team.objective_updated').at(-1)
  const completion = hosts.filter(event => event.type === 'team.objective_completed').at(-1)
  const revision = Math.max(team.revision, objective?.seq ?? 0)
  for (const entry of graph.nodes) {
    if (entry.historical || entry.withdrawal || !entry.recipient) continue
    const reported = graph.nodes.some(reply => reply.kind === 'report' && reply.sender === entry.recipient
      && reply.recipient === entry.sender && bySeq.get(reply.seq)?.payload.reply_to === entry.seq)
    if (reported) continue
    const source = bySeq.get(entry.seq)
    if (!source || source.type !== 'team.message_received' || source.payload.recipient !== entry.recipient
      || !['human', 'agent'].includes(source.actor.kind ?? '')
      || entry.kind === 'continuation' && !teamContinuationSource(source, bySeq)) continue

    const snapshots = team.runs.filter(run => run.message_seq === entry.seq && run.role === entry.recipient)
    const snapshot = snapshots[0]
    // A terminal or stale snapshot must not be revived by an older run_started event.
    if (snapshots.length > 1 || snapshot && !running(snapshot.status)) continue

    const start = hosts.filter(event => event.type === 'team.run_started' && event.seq > source.seq
      && event.payload.message_seq === entry.seq && event.payload.role === entry.recipient).at(-1)
    const adopting = start && objective && objective.seq === revision && start.seq < objective.seq
      && typeof start.payload.run_id === 'string' && start.payload.run_id === objective.payload.run_id
    if (snapshot?.revision !== undefined && snapshot.revision !== revision && !adopting) continue
    if (completion && completion.seq > (objective?.seq ?? 0) && (start?.seq ?? source.seq) < completion.seq) continue
    if (source.payload.revision !== undefined && source.payload.revision !== revision && !adopting) continue
    if (start && (typeof start.payload.run_id !== 'string' || !start.payload.run_id.trim()
      || start.payload.status !== undefined && !running(start.payload.status)
      || start.payload.revision !== undefined && start.payload.revision !== revision && !adopting)) continue
    if (!snapshot && !start) continue
    if (!snapshot && start && start.seq < revision && !adopting) continue
    if (hosts.some(event => event.type === 'team.run_finished' && event.seq > (start?.seq ?? source.seq)
      && (start && event.payload.run_id === start.payload.run_id
        || event.payload.message_seq === entry.seq && event.payload.role === entry.recipient
          && (!start || event.payload.run_id === undefined)))) continue
    if (start && hosts.some(event => event.type === 'team.questions_requested' && event.seq > start.seq
      && event.payload.run_id === start.payload.run_id && event.payload.role_id === entry.recipient
      && event.payload.message_seq === entry.seq)) continue

    const tool = start && hosts.filter(event => event.type === 'team.tool_started' && event.seq > start.seq
      && event.payload.run_id === start.payload.run_id && event.payload.role === entry.recipient
      && event.payload.message_seq === entry.seq).at(-1)
    if (tool?.payload.name === 'wait_for_reply') continue
    nodes.set(entry.id, entry.kind === 'continuation' ? 'handoff' : 'running')
  }
  const byId = new Map(graph.nodes.map(entry => [entry.id, entry]))
  for (const edge of graph.edges) {
    const source = byId.get(edge.source)
    if (nodes.has(edge.target) && source && !source.historical && !source.withdrawal) edges.add(edge.id)
  }
  return { nodes, edges }
}
