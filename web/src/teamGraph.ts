import { isTeamHost, teamContinuationSource, teamRuns, teamRunStatus } from './team'
import type { TeamEvent, TeamState } from './team'

export type TeamGraphEntry = {
  id: string; seq: number; kind: string; sender: string; recipient: string
  text: string; status: string; processing?: string; reason?: string; historical: boolean
  artifacts?: { seq: number; filename: string }[]
}
export type TeamGraphEdge = { id: string; source: string; target: string; label: string }

const text = (value: unknown) => typeof value === 'string' ? value : ''
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const id = (seq: number) => `event-${seq}`
export const teamGraphKinds: Record<string, string> = {
  message: '消息', assignment: '委托', report: '回报', progress: '进展', notification: '知会',
  review: '复核请求', decision: '复核结论', continuation: '接续交接',
}
const reviewStatuses: Record<string, string> = {
  pending: '待复核', changes_requested: '待修复', approved: '该版本复核通过', outdated: '版本已变化，待重新复核', stale: '历史复核',
}

// Edges describe recorded communication, never task prerequisites or inferred role hierarchy.
export function projectTeamGraph(team: TeamState, input: readonly TeamEvent[]) {
  const events = [...input].sort((a, b) => a.seq - b.seq)
  const bySeq = new Map(events.map(event => [event.seq, event]))
  const messages = events.filter(event => event.type === 'team.message_received'
    && ['human', 'agent'].includes(event.actor.kind ?? '')
    && (['message', 'assignment', 'report', 'progress', 'notification'].includes(text(event.payload.kind))
      || event.actor.kind === 'agent' && event.payload.kind === 'continuation' && event.payload.visible === true))
  const requests = events.filter(event => isTeamHost(event) && event.type === 'team.review_requested'
    && text(event.payload.author) && text(event.payload.reviewer))
  const requestBySeq = new Map(requests.map(event => [event.seq, event]))
  const root = (event: TeamEvent) => typeof event.payload.root_seq === 'number' ? event.payload.root_seq : event.seq
  const decisions = events.filter(event => {
    const request = requestBySeq.get(Number(event.payload.review_seq))
    return isTeamHost(event) && event.type === 'team.review_recorded' && request && request.seq < event.seq
      && event.payload.root_seq === root(request) && event.payload.reviewer === request.payload.reviewer
      && ['approved', 'changes_requested'].includes(text(event.payload.verdict))
  })
  const latestObjective = events.filter(event => isTeamHost(event) && event.type === 'team.objective_updated').at(-1)
  const runs = teamRuns(team, events)
  const edges: TeamGraphEdge[] = []
  const connect = (source: number, target: number, label: string) => {
    if (source >= target) return
    const edge = { id: `${source}:${target}:${label}`, source: id(source), target: id(target), label }
    if (!edges.some(entry => entry.id === edge.id)) edges.push(edge)
  }
  const sourceMessage = (event: TeamEvent, key: string) => {
    const seq = event.payload[key]
    return typeof seq === 'number' ? messages.find(message => message.seq === seq && message.seq < event.seq
      && ['assignment', 'message'].includes(text(message.payload.kind))) : undefined
  }
  const replies = new Set<number>()
  for (const message of messages) {
    const p = message.payload
    const continued = teamContinuationSource(message, bySeq)
    if (continued) connect(continued.seq, message.seq, '接续原委托')
    const source = sourceMessage(message, 'reply_to')
    if (source && source.payload.recipient === message.actor.id && source.actor.id === p.recipient
      && ['report', 'progress'].includes(text(p.kind))) {
      connect(source.seq, message.seq, p.kind === 'report' ? '回报' : '进展')
      if (p.kind === 'report') replies.add(source.seq)
    }
    const parent = sourceMessage(message, 'parent_assignment_seq')
    if (parent && parent.payload.recipient === message.actor.id && message.actor.kind === 'agent'
      && ['assignment', 'message', 'notification', 'continuation'].includes(text(p.kind))) {
      connect(parent.seq, message.seq, p.kind === 'assignment' ? '子委托' : p.kind === 'notification' ? '进展知会' : p.kind === 'continuation' ? '接续交接' : '协作消息')
    }
    const request = requestBySeq.get(Number(p.review_seq))
    if (request && request.seq < message.seq && p.kind === 'assignment' && message.actor.id === request.payload.author
      && p.recipient === request.payload.reviewer) connect(request.seq, message.seq, '投递复核')
  }
  // Receipts preserve their actual source even when another relation already enters the target.
  for (const finish of events) {
    if (!isTeamHost(finish) || finish.type !== 'team.tool_finished') continue
    const p = finish.payload, result = record(p.result)
    const start = bySeq.get(Number(p.started_seq)), sent = messages.find(message => message.seq === result.message_seq)
    if (!start || !isTeamHost(start) || start.type !== 'team.tool_started' || !sent || result.status !== 'ok'
      || !['delegate', 'send_message', 'request_review'].includes(text(start.payload.name))
      || start.seq >= sent.seq || sent.seq >= finish.seq || start.payload.role !== sent.actor.id
      || p.role !== sent.actor.id || p.run_id !== start.payload.run_id
      || (p.name !== undefined && p.name !== start.payload.name)) continue
    const source = messages.find(message => message.seq === start.payload.message_seq && message.seq < start.seq
      && message.payload.recipient === sent.actor.id)
    if (!source) continue
    const request = requestBySeq.get(Number(result.review_seq))
    if (start.payload.name === 'request_review' && request && sent.payload.kind === 'assignment'
      && sent.payload.review_seq === request.seq && start.seq < request.seq && request.seq < sent.seq
      && request.payload.author === sent.actor.id && request.payload.reviewer === sent.payload.recipient
      && request.payload.run_id === start.payload.run_id) {
      connect(source.seq, request.seq, '请求复核')
      connect(request.seq, sent.seq, '投递复核')
      continue
    }
    const reply = sent.payload.reply_to === source.seq && sent.payload.recipient === source.actor.id
    const parent = sent.payload.parent_assignment_seq === source.seq
    const label = sent.payload.kind === 'assignment' ? parent ? '子委托' : '委托'
      : reply && sent.payload.kind === 'report' ? '回报' : reply && sent.payload.kind === 'progress' ? '进展'
      : parent && sent.payload.kind === 'notification' ? '进展知会' : sent.payload.kind === 'continuation' ? '接续交接' : '协作消息'
    connect(source.seq, sent.seq, label)
  }
  for (const request of requests) {
    const previous = requests.filter(event => event.seq < request.seq && root(event) === root(request)
      && event.payload.author === request.payload.author && event.payload.reviewer === request.payload.reviewer).at(-1)
    if (previous) connect(previous.seq, request.seq, '再次复核')
  }
  for (const decision of decisions) connect(Number(decision.payload.review_seq), decision.seq, '复核结论')
  const nodes: TeamGraphEntry[] = [...messages, ...requests, ...decisions].sort((a, b) => a.seq - b.seq).map(event => {
    const p = event.payload
    const review = event.type === 'team.review_requested', decision = event.type === 'team.review_recorded'
    const request = decision ? requestBySeq.get(Number(p.review_seq))! : event
    const snapshot = (team.reviews ?? []).find(entry => entry.request_seq === request.seq)
    const superseded = (review || decision) && requests.some(entry => entry.seq > request.seq && root(entry) === root(request))
    const historical = Boolean(latestObjective && event.seq < latestObjective.seq
      && (event.actor.kind === 'agent' || review || decision))
    const run = runs.find(entry => entry.message_seq === event.seq && entry.role === p.recipient)
    const verdict = decisions.filter(entry => entry.payload.review_seq === request.seq).at(-1)
    const reviewStatus = snapshot?.status ?? (historical ? 'stale' : text(verdict?.payload.verdict) || 'pending')
    const kind = review ? 'review' : decision ? 'decision' : text(p.kind)
    const messageStatus = p.kind === 'report' ? '回报已投递' : p.kind === 'progress' ? '进展已投递'
      : p.kind === 'notification' ? '知会已投递'
      : p.kind === 'continuation' ? teamContinuationSource(event, bySeq) ? '接续指令已投递' : '原委托关联待核对'
      : run && ['failed', 'blocked', 'interrupted', 'uncertain', 'cancelled'].includes(run.status ?? '') ? teamRunStatus(run.status)
      : replies.has(event.seq) ? '已收到回报' : run ? teamRunStatus(run.status) : '消息等待处理'
    const artifacts = review || decision ? request.payload.artifacts : undefined
    return {
      id: id(event.seq), seq: event.seq, kind,
      sender: review ? text(p.author) : decision ? text(p.reviewer) : event.actor.id,
      recipient: review ? text(p.reviewer) : decision ? text(request.payload.author) : text(p.recipient),
      text: review ? text(p.instruction) : decision ? text(p.feedback) : text(p.text),
      status: review || decision ? superseded ? '已被再次复核替代' : reviewStatuses[reviewStatus] ?? '复核状态待确认' : messageStatus,
      historical, processing: run ? teamRunStatus(run.status) : undefined, reason: run?.reason,
      artifacts: Array.isArray(artifacts) ? artifacts.flatMap(value => {
        const artifact = record(value)
        return typeof artifact.seq === 'number' && typeof artifact.filename === 'string' ? [{ seq: artifact.seq, filename: artifact.filename }] : []
      }) : undefined,
    }
  })
  return { nodes, edges }
}
