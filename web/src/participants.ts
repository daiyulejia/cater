import type { WorkState } from './WorkItems'

export function conversationParticipants(
  task: { work?: WorkState | null; assignments: { owner: string; status: string }[]; messages: { actor: string }[] },
  leaderId: string,
  memberIds: string[],
) {
  const participants = task.work?.items.length
    ? task.work.items.flatMap((item) => [item.owner, ...(item.reviewer ? [item.reviewer] : [])])
    : [...task.assignments.filter((item) => item.status === 'assigned').map((item) => item.owner),
      ...task.messages.map((item) => item.actor)]
  const known = new Set(memberIds)
  return [...new Set([leaderId, ...participants])].filter((id) => known.has(id))
}
