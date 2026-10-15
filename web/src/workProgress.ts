import { consultationMatches, sameWorkScope } from './participants'

type Event = { seq: number; type: string; payload: Record<string, unknown> }
export type WorkProgress = { objective: string; state: string; steps: { seq: number; text: string }[] }

// Bind receipts to an actual claim; a plan alone never means someone started.
export function workProgress(events: Event[], now = Date.now() / 1000) {
  const records = new Map<number, WorkProgress>()
  const attempts = new Map<string, { attempt: string; claim: number; scope: unknown; submission?: number; failed?: boolean }>()
  const consultations = new Map<number, Event>()
  const finishedConsultations = new Set<number>()
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
      attempts.set(p.work_item_id, { attempt: p.attempt_id, claim: event.seq, scope: p.scope })
      if (typeof p.lease_until === 'number' && Number.isFinite(p.lease_until)) leases.set(event.seq, p.lease_until)
      records.set(event.seq, { objective: item.objective, state: '处理中', steps: [{ seq: event.seq, text: '已领取这项工作，正在准备回应。' }] })
      continue
    }
    const attempt = attempts.get(p.work_item_id)
    if (!attempt) continue
    const record = records.get(attempt.claim)!
    if (event.type === 'collaboration.requested' || event.type === 'collaboration.finished') {
      if (attempt.failed || attempt.submission !== undefined || p.attempt_id !== attempt.attempt || !sameWorkScope(p.scope, attempt.scope)) continue
      if (event.type === 'collaboration.requested' && typeof p.question === 'string' && p.question.trim()) {
        consultations.set(event.seq, event)
        record.state = '等待定向咨询'
        record.steps.push({ seq: event.seq, text: `已发起只读咨询：${p.question}` })
      } else if (event.type === 'collaboration.finished' && typeof p.request_seq === 'number'
        && !finishedConsultations.has(p.request_seq) && consultationMatches(consultations.get(p.request_seq), event)) {
        finishedConsultations.add(p.request_seq)
        const status = typeof p.status === 'string' ? p.status : ''
        const reply = (status === 'answered' || status === 'declined') && p.speaker_id === p.recipient && typeof p.reply === 'string' && p.reply.trim()
        record.state = reply ? '咨询已回应，等待负责人继续' : '咨询未完成，等待负责人处理'
        const labels: Record<string, string> = { answered: '咨询回复已记录', declined: '咨询成员已说明无法提供意见', invalid: '咨询回复未通过核对', unavailable: '咨询暂不可用', stale: '咨询依据已变化' }
        record.steps.push({ seq: event.seq, text: `${reply || !['answered', 'declined'].includes(status) ? labels[status] ?? '咨询回执待核对' : '咨询回执待核对'}；咨询不代表验收通过。` })
      }
      continue
    }
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
      if (event.type === 'msg.said' && !attempt.failed && p.phase !== 'collaboration_request') {
        if (record.state.includes('咨询')) record.state = '处理中'
        record.steps.push({ seq: event.seq, text: '已收到成员的回应；具体结果见下方消息。' })
      }
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
    if ((['处理中', '检查已发起，等待验收'].includes(record.state) || record.state.includes('咨询')) && (leases.get(claim) ?? Infinity) <= now) {
      record.state = '期限已过，结果待核对'
    }
  }
  return records
}
