type Event = { seq: number; type: string; payload: Record<string, unknown> }
export type WorkProgress = { objective: string; state: string; steps: { seq: number; text: string }[] }

// Bind receipts to an actual claim; a plan alone never means someone started.
export function workProgress(events: Event[], now = Date.now() / 1000) {
  const records = new Map<number, WorkProgress>()
  const attempts = new Map<string, { attempt: string; claim: number; submission?: number; failed?: boolean }>()
  const leases = new Map<number, number>()
  let plan = 0
  let items = new Map<string, Record<string, unknown>>()
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const p = event.payload
    if (event.type === 'work.plan_created') {
      for (const entry of attempts.values()) {
        const record = records.get(entry.claim)!
        record.state = '安排已更新'
      }
      plan = event.seq
      items = new Map((Array.isArray(p.items) ? p.items : []).filter((item) => item && typeof item.id === 'string').map((item) => [item.id, item]))
      attempts.clear()
      continue
    }
    if (p.plan_seq !== plan || typeof p.work_item_id !== 'string') continue
    const item = items.get(p.work_item_id)
    if (!item || typeof item.objective !== 'string') continue
    if (event.type === 'work.claimed' && typeof p.attempt_id === 'string') {
      attempts.set(p.work_item_id, { attempt: p.attempt_id, claim: event.seq })
      if (typeof p.lease_until === 'number' && Number.isFinite(p.lease_until)) leases.set(event.seq, p.lease_until)
      records.set(event.seq, { objective: item.objective, state: '处理中', steps: [{ seq: event.seq, text: '已领取这项工作，正在准备回应。' }] })
      continue
    }
    const attempt = attempts.get(p.work_item_id)
    if (!attempt) continue
    const record = records.get(attempt.claim)!
    if (event.type === 'work.check_started' && p.submission_seq === attempt.submission && attempt.submission !== undefined) {
      record.state = '检查已发起，等待验收'
      if (typeof p.lease_until === 'number' && Number.isFinite(p.lease_until)) leases.set(attempt.claim, p.lease_until)
      record.steps.push({ seq: event.seq, text: '已发起本次成果的实际检查，旧验收结论不再作为当前通过依据。' })
      const affected = new Set([p.work_item_id])
      for (let i = 0; i < items.size; i++) {
        for (const [id, candidate] of items) {
          if (Array.isArray(candidate.depends_on) && candidate.depends_on.some((dependency) => affected.has(dependency))) affected.add(id)
        }
      }
      for (const id of affected) {
        const dependent = attempts.get(id)
        if (id !== p.work_item_id && dependent) {
          records.get(dependent.claim)!.state = '前置成果重新检查，需要核对'
          attempts.delete(id)
        }
      }
    } else if (event.type === 'work.reviewed' && !attempt.failed && p.submission_seq === attempt.submission && attempt.submission !== undefined) {
      record.state = p.status === 'accepted' ? '阶段验收通过' : p.status === 'rework' ? '需要返工' : '等待补齐依据'
      record.steps.push({ seq: event.seq, text: record.state + (typeof p.reason === 'string' ? `：${p.reason}` : '') })
    } else if (p.attempt_id === attempt.attempt) {
      if (event.type === 'msg.said' && !attempt.failed) record.steps.push({ seq: event.seq, text: '已收到成员的回应；具体结果见下方消息。' })
      if (event.type === 'work.submitted' && !attempt.failed) {
        attempt.submission = event.seq
        record.state = '已提交，等待验收'
        record.steps.push({ seq: event.seq, text: '成果已提交，尚不代表验证通过。' })
      }
      if (event.type === 'work.failed') {
        attempt.failed = true
        record.state = p.uncertain ? '结果待核对' : '本次已暂停'
        record.steps.push({ seq: event.seq, text: typeof p.reason === 'string' ? p.reason : record.state })
      }
    }
    if (event.type === 'work.invalidated' && Array.isArray(p.affected)) {
      for (const id of p.affected) {
        const stale = typeof id === 'string' ? attempts.get(id) : undefined
        if (stale) {
          const progress = records.get(stale.claim)!
          progress.state = '需要重新核对'
          progress.steps.push({ seq: event.seq, text: '工作依据发生变化，旧结果不再作为当前完成依据。' })
          attempts.delete(id as string)
        }
      }
    }
  }
  for (const [claim, record] of records) {
    if (['处理中', '检查已发起，等待验收'].includes(record.state) && (leases.get(claim) ?? Infinity) <= now) {
      record.state = '期限已过，结果待核对'
    }
  }
  return records
}
