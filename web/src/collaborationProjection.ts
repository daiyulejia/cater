type Event = { seq: number; type: string; payload: Record<string, unknown> }
type Assignment = { owner?: unknown; reviewer?: unknown; status?: unknown }

export function assignedMembers(event: Event) {
  const items: Assignment[] = event.type === 'work.plan_created'
    ? Array.isArray(event.payload.items) ? event.payload.items.filter((item): item is Assignment => Boolean(item) && typeof item === 'object') : []
    : event.type === 'subtask.assigned' && (!event.payload.status || event.payload.status === 'assigned') ? [event.payload] : []
  const ids = (values: unknown[]) => [...new Set(values.filter((id): id is string => typeof id === 'string' && Boolean(id.trim()) && id !== 'human' && id !== 'system'))]
  return { owners: ids(items.map((item) => item.owner)), reviewers: ids(items.map((item) => item.reviewer)) }
}

export function firstJoins(events: Event[], leader: string) {
  const seen = new Set([leader])
  const joins = new Map<number, string[]>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const { owners, reviewers } = assignedMembers(event)
    const added = [...new Set([...owners, ...reviewers])].filter((id) => !seen.has(id))
    added.forEach((id) => seen.add(id))
    if (added.length) joins.set(event.seq, added)
  }
  return joins
}

export function planningHandoff<T extends Event>(event: T, events: T[]) {
  if (event.payload.status !== 'installed') return undefined
  const plans = events.filter((entry) => entry.type === 'work.plan_created')
  // Prefer explicit provenance, regardless of whether the plan precedes the receipt.
  if (typeof event.payload.plan_seq === 'number' && event.payload.plan_seq > 0) return plans.find((entry) => entry.seq === event.payload.plan_seq)
  if (typeof event.payload.attempt_seq === 'number') return plans.find((entry) => (entry.payload.planning as { attempt_seq?: number } | null)?.attempt_seq === event.payload.attempt_seq)
  const next = [...events].sort((a, b) => a.seq - b.seq).find((entry) => entry.seq > event.seq && ['work.plan_created', 'planning.started', 'planning.finished', 'planning.requested', 'human.commanded'].includes(entry.type))
  return next?.type === 'work.plan_created' ? next : undefined
}

export function collaborationProjection(events: Event[], leader: string) {
  const ordered = [...events].sort((a, b) => a.seq - b.seq)
  const joins = firstJoins(ordered, leader)
  const records = new Map<number, { joined: string[]; handoff?: ReturnType<typeof assignedMembers> }>()
  const receipts = new Map(ordered.filter((event) => event.type === 'planning.finished').flatMap((event) => {
    const plan = planningHandoff(event, ordered)
    return plan ? [[plan.seq, event] as const] : []
  }))
  let arrangement: Event | undefined
  for (const event of ordered) {
    if (['task.decomposed', 'plan.amended', 'human.commanded', 'planning.started', 'planning.requested'].includes(event.type)) arrangement = event
    if (!['work.plan_created', 'subtask.assigned'].includes(event.type)) continue
    const members = assignedMembers(event)
    if (!members.owners.length && !members.reviewers.length) continue
    const subtasks = arrangement?.type === 'task.decomposed' && Array.isArray(arrangement.payload.subtasks) ? arrangement.payload.subtasks : []
    const decomposed = event.type === 'subtask.assigned' && typeof event.payload.id === 'string' && subtasks.some((item) => item && typeof item === 'object' && item.id === event.payload.id)
      ? arrangement : undefined
    const target = event.type === 'work.plan_created' ? receipts.get(event.seq) : decomposed
    const seq = target?.seq ?? event.seq
    const record = records.get(seq) ?? { joined: [] }
    record.joined.push(...joins.get(event.seq) ?? [])
    if (!decomposed) record.handoff = members
    records.set(seq, record)
  }
  return records
}
