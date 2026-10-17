export type TeamRun = {
  message_seq?: number
  role?: string
  status?: string
  reason?: string
  revision?: number
}

export type TeamAssignment = {
  seq?: number
  sender?: string
  recipient?: string
  role?: string
  owner?: string
  text?: string
  status?: string
}

export type TeamReview = {
  root_seq: number
  request_seq: number
  author: string
  reviewer: string
  artifacts: { seq: number; filename: string; content_sha256: string }[]
  status: 'pending' | 'changes_requested' | 'approved' | 'outdated' | 'stale'
  feedback?: string
  revision: number
}

export type TeamState = {
  enabled: boolean
  objective: string
  status: string
  pending_count: number
  runs: TeamRun[]
  assignments: TeamAssignment[]
  reviews?: TeamReview[]
  checkpoint: string
  revision: number
}

export type TeamSummary = Pick<TeamState, 'enabled' | 'status' | 'pending_count' | 'revision'> & { runs?: TeamRun[] }

export type TeamActivity = { state: string; active: boolean; revision?: number; follow_up?: { action?: string } }

export function resolveTeamActivity<T extends TeamActivity>(detail: T | undefined, summary: T | undefined, newObjective: boolean): T | undefined {
  return newObjective || (summary?.revision ?? -1) > (detail?.revision ?? -1) ? summary : detail
}

export function hasNewTeamObjective(detail?: TeamSummary | null, summary?: TeamSummary | null): boolean {
  return Boolean(detail?.enabled && summary?.enabled && summary.revision > detail.revision)
}

export function resolveTeamTask<T extends { goal: string; status: string; team?: TeamState | null }>(
  detail: T | undefined, summary: { goal: string; status: string; version: number; team?: TeamSummary | null } | undefined,
  detailVersion: number,
): T | undefined {
  if (!detail?.team?.enabled || !summary?.team?.enabled
    || (!hasNewTeamObjective(detail.team, summary.team) && summary.version <= detailVersion)) return detail
  const newObjective = hasNewTeamObjective(detail.team, summary.team)
  // A compact snapshot cannot repair every old run; do not merge it into stale live facts.
  const runs = summary.team.runs ?? []
  return { ...detail, goal: summary.goal, status: summary.status,
    team: { ...detail.team, ...summary.team, objective: summary.goal, runs,
      reviews: newObjective ? [] : detail.team.reviews,
      checkpoint: newObjective ? '新目标已收到，详细记录正在同步。' : '详细记录正在同步。' } }
}

export type TeamEvent = {
  seq: number
  type: string
  actor: { id: string; kind?: string; role?: string }
  payload: Record<string, unknown>
}

export const isTeamHost = (event: TeamEvent) => event.actor.id === 'team' && event.actor.kind === 'system' && event.actor.role === 'team'

export const isTeamRecovery = (event: TeamEvent) => event.type === 'team.message_received' && event.payload.kind === 'recovery' && isTeamHost(event)

export function teamContinuationSource(event: TeamEvent, bySeq: ReadonlyMap<number, TeamEvent>): TeamEvent | undefined {
  if (event.type !== 'team.message_received' || event.actor.kind !== 'agent' || event.payload.kind !== 'continuation') return undefined
  const seq = event.payload.continue_assignment_seq
  const source = typeof seq === 'number' ? bySeq.get(seq) : undefined
  return source && source.seq < event.seq && source.type === 'team.message_received' && source.actor.kind === 'agent'
    && source.actor.id === event.actor.id && source.actor.role === event.actor.role
    && source.payload.recipient === event.payload.recipient && typeof source.payload.recipient === 'string'
    && ['assignment', 'message'].includes(String(source.payload.kind)) ? source : undefined
}

export const teamSpeaker = (event: TeamEvent): string => typeof event.payload.speaker === 'string' ? event.payload.speaker
  : typeof event.payload.speaker_id === 'string' ? event.payload.speaker_id : event.actor.id

export type TeamAudience = { audience?: 'team' | 'customer'; recipient?: string }

export function teamMessageAudience(event: TeamEvent, bySeq: ReadonlyMap<number, TeamEvent>): TeamAudience {
  const p = event.payload
  if (event.type === 'team.message_received') return (isTeamRecovery(event) || event.actor.kind === 'agent' && p.kind !== 'recovery')
    ? { audience: 'team', recipient: typeof p.recipient === 'string' ? p.recipient : undefined } : {}
  if (event.type !== 'msg.said' || p.phase !== 'team') return {}
  const source = typeof p.reply_to === 'number' ? bySeq.get(p.reply_to) : undefined
  const delivery = typeof p.delivery_seq === 'number' ? bySeq.get(p.delivery_seq) : undefined
  const delivered = delivery && isTeamHost(delivery) && delivery.type === 'team.objective_completed'
    && delivery.seq < event.seq && delivery.payload.run_id === p.run_id
  if (source && source.seq < event.seq && source.payload.recipient === teamSpeaker(event)
    && !delivered
    && (isTeamRecovery(source) || source.type === 'team.message_received' && source.actor.kind === 'agent' && source.payload.kind === 'continuation'))
    return { audience: 'team', recipient: source.actor.id }
  if (p.audience === 'team' || p.audience === 'customer') return {
    audience: p.audience, recipient: typeof p.recipient === 'string' ? p.recipient : undefined,
  }
  if (!source || source.seq >= event.seq || source.type !== 'team.message_received' || source.payload.recipient !== teamSpeaker(event)) return {}
  if (source.payload.kind === 'recovery' || source.payload.kind === 'continuation' && source.actor.kind !== 'agent') return {}
  if (source.actor.kind === 'human') return { audience: 'customer', recipient: source.actor.id }
  if (source.actor.kind === 'agent') return { audience: 'team', recipient: source.actor.id }
  return {}
}

export function teamDeliveries<T extends TeamEvent>(events: T[], leader: string): { messages: Map<number, T>; consumed: Set<number> } {
  const bySeq = new Map(events.map((event) => [event.seq, event]))
  const messages = new Map<number, T>(), consumed = new Set<number>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (event.type !== 'msg.said' || event.payload.phase !== 'team' || teamSpeaker(event) !== leader
      || !((event.actor.kind === 'agent' && event.actor.id === leader) || isTeamHost(event))
      || typeof event.payload.text !== 'string' || !event.payload.text.trim()) continue
    const completion = typeof event.payload.delivery_seq === 'number' ? bySeq.get(event.payload.delivery_seq) : undefined
    if (!completion || !isTeamHost(completion) || completion.type !== 'team.objective_completed' || completion.seq >= event.seq) continue
    const run = event.payload.run_id
    if (typeof run !== 'string' || !run.trim()) continue
    const start = events.find((entry) => isTeamHost(entry) && entry.type === 'team.run_started'
      && entry.payload.run_id === run)
    if (Object.hasOwn(completion.payload, 'run_id')) {
      if (completion.payload.run_id !== run) continue
    } else {
      // Legacy completions need an unambiguous, still-open leader run as evidence.
      if (!start || start.payload.role !== leader || start.seq >= completion.seq
        || events.some((entry) => isTeamHost(entry) && entry.seq < completion.seq
          && ((entry.type === 'team.run_finished' && entry.payload.run_id === run)
            || (entry.type === 'team.run_started' && entry.payload.role === leader && entry.seq > start.seq && entry.payload.run_id !== run)))) continue
    }
    if ((start && (start.payload.role !== leader || start.seq >= completion.seq))
      || events.some((entry) => isTeamHost(entry) && entry.type === 'team.objective_updated' && entry.seq > completion.seq && entry.seq < event.seq)) continue
    messages.set(completion.seq, event)
    consumed.add(event.seq)
  }
  return { messages, consumed }
}

export function groupTeamConversation<T extends { internal?: boolean }>(items: T[]): { internal: boolean; items: T[] }[] {
  const groups: { internal: boolean; items: T[] }[] = []
  for (const item of items) {
    const last = groups.at(-1)
    if (item.internal && last?.internal) last.items.push(item)
    else groups.push({ internal: Boolean(item.internal), items: [item] })
  }
  return groups
}

export function teamProgressMessages(events: TeamEvent[]): Set<number> {
  const progress = new Set<number>(), pending = new Map<string, TeamEvent>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const p = event.payload
    if (event.type === 'msg.said' && p.phase === 'team' && (event.actor.kind === 'agent' || isTeamHost(event))) {
      if (typeof p.run_id === 'string') pending.delete(p.run_id)
      if (typeof p.delivery_seq === 'number') continue
      if (p.response_kind === 'progress') progress.add(event.seq)
      else if (p.response_kind === undefined && typeof p.run_id === 'string') pending.set(p.run_id, event)
    } else if (isTeamHost(event) && typeof p.run_id === 'string') {
      const said = pending.get(p.run_id)
      if (said && p.role === teamSpeaker(said)) {
        if (event.type === 'team.tool_started' && p.name !== 'wait_for_reply') progress.add(said.seq)
        if (event.type === 'team.run_finished') pending.delete(p.run_id)
      }
    }
  }
  return progress
}

const currentTeamRuns = (team: TeamSummary) => (team.runs ?? []).filter((run) => run.revision === undefined || run.revision === team.revision)
const isRunningTeamRun = (run: TeamRun) => ['running', 'started'].includes(run.status ?? '')
export const hasActiveTeamMessages = (team: TeamSummary) => team.pending_count > 0 || currentTeamRuns(team).some(isRunningTeamRun)

export function teamStatus(team: TeamSummary, activity?: TeamActivity): string {
  const followUp = team.status === 'done'
  const processing = followUp ? '正在处理后续消息' : '正在处理消息'
  if (activity?.active) {
    if (activity.state === 'queued') return followUp ? '后续消息已排队' : '消息处理已排队'
    if (activity.state === 'running') return processing
  }
  if (activity?.state === 'failed') return '执行失败，需跟进'
  const currentRuns = currentTeamRuns(team)
  if (currentRuns.some(isRunningTeamRun)) {
    return activity?.active === false ? '执行状态待核对' : processing
  }
  const latest = currentRuns.at(-1)
  if (latest?.status === 'failed') return team.pending_count > 0 ? '处理失败，仍有待办' : teamRunStatus('failed')
  if (team.pending_count > 0) return followUp ? '后续消息等待处理' : '消息等待处理'
  if (team.status === 'done') return '店长确认可交付'
  if (team.status === 'failed' || team.status === 'blocked') return '任务需要跟进'
  if (latest && ['interrupted', 'cancelled'].includes(latest.status ?? '')) return `本轮${teamRunStatus(latest.status)}`
  if (latest?.status === 'waiting') return '本轮结束，等待消息'
  if (latest && ['blocked', 'uncertain'].includes(latest.status ?? '')) return '处理结果待核对'
  return ['done', 'finished', 'completed', 'succeeded'].includes(latest?.status ?? '') ? '消息处理完成，任务待交付' : '任务待推进'
}

export const teamRunStatus = (status?: string) => ({
  running: '正在处理消息', started: '正在处理消息', pending: '消息等待处理', queued: '消息等待处理',
  done: '消息处理完成', finished: '消息处理完成', completed: '消息处理完成', succeeded: '消息处理完成',
  failed: '消息处理失败', blocked: '消息处理受阻', cancelled: '消息处理已取消', uncertain: '消息处理结果待确认',
  interrupted: '消息处理中断',
  waiting: '等待伙伴回复',
}[status ?? ''] ?? '消息处理结果待确认')

export function teamRuns(team: TeamState, events: TeamEvent[]): TeamRun[] {
  const runs = [...(team.runs ?? [])]
  for (const event of events) {
    if (!isTeamHost(event) || !['team.run_started', 'team.run_finished'].includes(event.type)) continue
    const p = event.payload
    if (typeof p.message_seq !== 'number' || typeof p.role !== 'string') continue
    const index = runs.findIndex((item) => item.message_seq === p.message_seq && item.role === p.role)
    const run = { message_seq: p.message_seq, role: p.role,
      status: typeof p.status === 'string' ? p.status : event.type === 'team.run_started' ? 'running' : 'uncertain',
      reason: typeof p.reason === 'string' ? p.reason : undefined,
      revision: typeof p.revision === 'number' ? p.revision : runs[index]?.revision }
    if (index < 0) runs.push(run)
    else runs[index] = run
  }
  return runs
}

export function resolveTeam(team: TeamState | null | undefined, events: TeamEvent[]): TeamState | undefined {
  if (!team?.enabled) return team ?? undefined
  const objective = events.filter((event) => isTeamHost(event) && ['team.objective_updated', 'team.objective_completed'].includes(event.type)).at(-1)
  return { ...team, runs: teamRuns(team, events),
    ...(objective?.type === 'team.objective_updated' && typeof objective.payload.goal === 'string' ? { objective: objective.payload.goal, status: team.status === 'done' ? 'running' : team.status } : {}),
    ...(objective?.type === 'team.objective_completed' ? { status: 'done' } : {}) }
}

export function teamMessageEchoes(events: TeamEvent[]): Set<number> {
  // A tool receipt ties the delivered message to a specific model turn.
  const bySeq = new Map(events.map((event) => [event.seq, event]))
  const sent = events.flatMap((event) => {
    if (!isTeamHost(event) || event.type !== 'team.tool_finished') return []
    const p = event.payload
    const result = p.result !== null && typeof p.result === 'object' ? p.result as Record<string, unknown> : {}
    const start = typeof p.started_seq === 'number' ? bySeq.get(p.started_seq) : undefined
    const message = typeof result.message_seq === 'number' ? bySeq.get(result.message_seq) : undefined
    if (!start || !isTeamHost(start) || start.type !== 'team.tool_started' || start.payload.name !== 'send_message'
      || typeof start.payload.run_id !== 'string' || !message || message.type !== 'team.message_received'
      || message.actor.kind !== 'agent' || (!['message', 'notification'].includes(String(message.payload.kind))
        && !(['report', 'continuation'].includes(String(message.payload.kind)) && message.payload.visible === true)) || start.payload.role !== message.actor.id
      || p.role !== message.actor.id || result.status !== 'ok') return []
    return [{ run: start.payload.run_id, actor: message.actor.id, text: message.payload.text,
      recipient: teamMessageAudience(message, bySeq).recipient }]
  })
  return new Set(events.filter((event) => {
    if (event.type !== 'msg.said' || event.payload.phase !== 'team') return false
    const audience = teamMessageAudience(event, bySeq)
    if (audience.audience === 'customer') return false
    const recipient = audience.recipient ?? (typeof event.payload.recipient === 'string' ? event.payload.recipient : undefined)
    return sent.some((message) => message.run === event.payload.run_id && message.actor === teamSpeaker(event)
      && (recipient === undefined || recipient === message.recipient)
      && typeof message.text === 'string' && message.text === event.payload.text)
  }).map((event) => event.seq))
}
