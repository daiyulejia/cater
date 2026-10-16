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

export type TeamState = {
  enabled: boolean
  objective: string
  status: string
  pending_count: number
  runs: TeamRun[]
  assignments: TeamAssignment[]
  checkpoint: string
  revision: number
}

export type TeamSummary = Pick<TeamState, 'enabled' | 'status' | 'pending_count' | 'revision'> & { runs?: TeamRun[] }

export type TeamEvent = {
  seq: number
  type: string
  actor: { id: string; kind?: string; role?: string }
  payload: Record<string, unknown>
}

export const isTeamHost = (event: TeamEvent) => event.actor.id === 'team' && event.actor.kind === 'system' && event.actor.role === 'team'

export const teamSpeaker = (event: TeamEvent): string => typeof event.payload.speaker === 'string' ? event.payload.speaker
  : typeof event.payload.speaker_id === 'string' ? event.payload.speaker_id : event.actor.id

export type TeamAudience = { audience?: 'team' | 'customer'; recipient?: string }

export function teamMessageAudience(event: TeamEvent, bySeq: ReadonlyMap<number, TeamEvent>): TeamAudience {
  const p = event.payload
  if (event.type === 'team.message_received') return event.actor.kind === 'agent'
    ? { audience: 'team', recipient: typeof p.recipient === 'string' ? p.recipient : undefined } : {}
  if (event.type !== 'msg.said' || p.phase !== 'team') return {}
  if (p.audience === 'team' || p.audience === 'customer') return {
    audience: p.audience, recipient: typeof p.recipient === 'string' ? p.recipient : undefined,
  }
  const source = typeof p.reply_to === 'number' ? bySeq.get(p.reply_to) : undefined
  if (!source || source.seq >= event.seq || source.type !== 'team.message_received' || source.payload.recipient !== teamSpeaker(event)) return {}
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

export const hasActiveTeamMessages = (team: TeamSummary) => team.pending_count > 0 || Boolean(team.runs?.some((run) =>
  (run.revision === undefined || run.revision === team.revision) && ['running', 'started'].includes(run.status ?? '')))

export function teamStatus(team: TeamSummary): string {
  if (team.status === 'done') return '店长确认可交付'
  if (team.status === 'failed' || team.status === 'blocked') return '任务需要跟进'
  const currentRuns = (team.runs ?? []).filter((run) => run.revision === undefined || run.revision === team.revision)
  if (hasActiveTeamMessages(team)) return '正在处理消息'
  const latest = currentRuns.at(-1)
  return ['done', 'finished', 'completed', 'succeeded'].includes(latest?.status ?? '') ? '消息处理完成，任务待交付' : '任务进行中'
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
      || message.actor.kind !== 'agent' || (message.payload.kind !== 'message'
        && !(message.payload.kind === 'report' && message.payload.visible === true)) || start.payload.role !== message.actor.id
      || p.role !== message.actor.id || result.status !== 'ok') return []
    return [{ run: start.payload.run_id, actor: message.actor.id, text: message.payload.text }]
  })
  return new Set(events.filter((event) => event.type === 'msg.said' && event.payload.phase === 'team' && sent.some((message) =>
    message.run === event.payload.run_id && message.actor === (event.payload.speaker ?? event.payload.speaker_id ?? event.actor.id)
    && typeof message.text === 'string' && message.text === event.payload.text)).map((event) => event.seq))
}
