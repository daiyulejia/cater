import type { WorkState } from './WorkItems'
import type { TeamState } from './team'

type WorkEvent = { seq: number; type: string; actor?: { id: string }; payload: Record<string, unknown> }

export function sameWorkScope(left: unknown, right: unknown): boolean {
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object' || Array.isArray(left) || Array.isArray(right)) return false
  const a = left as Record<string, unknown>, b = right as Record<string, unknown>
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every((key) => Object.hasOwn(b, key) && a[key] === b[key])
}

export function consultationMatches(request: WorkEvent | undefined, reply: WorkEvent): boolean {
  if (!request || request.type !== 'collaboration.requested' || reply.type !== 'collaboration.finished'
    || reply.payload.request_seq !== request.seq || reply.seq <= request.seq) return false
  return ['work_item_id', 'attempt_id', 'plan_seq', 'requester', 'recipient'].every((key) => request.payload[key] !== undefined && request.payload[key] === reply.payload[key])
    && sameWorkScope(request.payload.scope, reply.payload.scope)
}

export function conversationParticipants(
  task: { team?: TeamState | null; work?: WorkState | null; assignments: { owner: string; status: string }[]; messages: { actor: string }[] },
  leaderId: string,
  memberIds: string[],
  questionRoleIds: string[] = [],
  events: WorkEvent[] = [],
) {
  if (task.team?.enabled) {
    const known = new Set(memberIds)
    const involved = events.flatMap((event) => {
      const p = event.payload
      if (event.type.startsWith('team.')) return [event.actor?.id, p.role, p.recipient]
      if (event.type === 'msg.said' && p.phase === 'team') return [event.actor?.id, p.speaker, p.speaker_id]
      return []
    })
    return [...new Set([leaderId, ...(task.team.runs ?? []).map((run) => run.role),
      ...(task.team.assignments ?? []).flatMap((assignment) => [assignment.sender, assignment.owner, assignment.recipient, assignment.role]), ...involved, ...questionRoleIds])]
      .filter((id): id is string => typeof id === 'string' && known.has(id))
  }
  const participants = task.work?.items.length
    ? task.work.items.flatMap((item) => [item.owner, ...(item.reviewer ? [item.reviewer] : [])])
    : [...task.assignments.filter((item) => item.status === 'assigned').map((item) => item.owner),
      ...task.messages.map((item) => item.actor)]
  const known = new Set(memberIds)
  const claims = new Map<string, typeof events[number]>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (event.type === 'work.claimed' && event.payload.plan_seq === task.work?.plan_seq && typeof event.payload.work_item_id === 'string') claims.set(event.payload.work_item_id, event)
  }
  const consulted = task.work?.scope_current === false ? [] : events.flatMap((event) => {
    const p = event.payload
    const claim = typeof p.work_item_id === 'string' ? claims.get(p.work_item_id) : undefined
    if (event.type !== 'collaboration.requested' || !claim || claim.seq >= event.seq || p.plan_seq !== task.work?.plan_seq
      || p.attempt_id !== claim.payload.attempt_id || !sameWorkScope(p.scope, claim.payload.scope)
      || !task.work?.items.some((item) => item.id === p.work_item_id && item.owner === p.requester)) return []
    return [p.requester, p.recipient].filter((id): id is string => typeof id === 'string')
  })
  return [...new Set([leaderId, ...participants, ...questionRoleIds, ...consulted])].filter((id) => known.has(id))
}
