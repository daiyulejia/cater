type Event = { seq: number; type: string; payload: Record<string, unknown> }
type Assignment = { owner?: unknown; reviewer?: unknown; status?: unknown }
export type WorkTransfer = { kind: 'submitted' | 'started'; owner: string; reviewer?: string; from: string[]; objective: string }
export type WorkAssignment = { owner: string; reviewer?: string; objective: string }

// Announce scheduler receipts, never infer that an accepted dependency started work.
export function workTransfers(events: Event[]) {
  const records = new Map<number, WorkTransfer>()
  let planSeq: number | undefined
  let items = new Map<string, Record<string, unknown>>()
  const claims = new Map<string, string>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const p = event.payload
    if (event.type === 'work.plan_created') {
      planSeq = event.seq
      items = new Map((Array.isArray(p.items) ? p.items : []).filter((i) => i && typeof i.id === 'string').map((i) => [i.id, i]))
      claims.clear()
      continue
    }
    if (planSeq === undefined || p.plan_seq !== planSeq || typeof p.work_item_id !== 'string') continue
    const item = items.get(p.work_item_id)
    if (!item || typeof item.owner !== 'string' || typeof item.objective !== 'string') continue
    if (event.type === 'work.claimed' && typeof p.attempt_id === 'string') {
      claims.set(p.work_item_id, p.attempt_id)
      const inputs = p.inputs && typeof p.inputs === 'object' ? Object.keys(p.inputs) : []
      const from = [...new Set(inputs.map((id) => items.get(id)?.owner).filter((id): id is string => typeof id === 'string'))]
      records.set(event.seq, { kind: 'started', owner: item.owner, from, objective: item.objective })
    } else if (event.type === 'work.submitted' && typeof p.attempt_id === 'string' && claims.get(p.work_item_id) === p.attempt_id) {
      records.set(event.seq, { kind: 'submitted', owner: item.owner, reviewer: typeof item.reviewer === 'string' ? item.reviewer : undefined, from: [], objective: item.objective })
      claims.delete(p.work_item_id)
    } else if (event.type === 'work.failed') {
      if (claims.get(p.work_item_id) === p.attempt_id) claims.delete(p.work_item_id)
    } else if (event.type === 'work.invalidated' && Array.isArray(p.affected)) {
      for (const id of p.affected) if (typeof id === 'string') claims.delete(id)
    }
  }
  return records
}

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
  const records = new Map<number, { joined: string[]; handoff?: ReturnType<typeof assignedMembers>; assignments?: WorkAssignment[] }>()
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
    if (event.type === 'work.plan_created' && Array.isArray(event.payload.items)) {
      record.assignments = event.payload.items.filter((item): item is WorkAssignment => Boolean(item) && typeof item.owner === 'string'
        && typeof item.objective === 'string' && (item.reviewer === undefined || item.reviewer === null || typeof item.reviewer === 'string'))
    }
    records.set(seq, record)
  }
  return records
}
