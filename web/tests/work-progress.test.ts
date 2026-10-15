import { expect, test } from 'vitest'
import { workProgress } from '../src/workProgress'

const plan = { seq: 1, type: 'work.plan_created', payload: { items: [{ id: 'a', objective: '实现棋盘' }, { id: 'b', objective: '检查棋盘' }] } }
const event = (seq: number, type: string, extra = {}) => ({ seq, type, payload: { plan_seq: 1, work_item_id: 'a', attempt_id: 'first', ...extra } })

test('只有真实领取才有工作过程，阶段验收必须关联本次提交', () => {
  expect(workProgress([plan]).size).toBe(0)
  const progress = workProgress([plan, event(2, 'work.claimed'), event(3, 'msg.said'), event(4, 'work.submitted'),
    event(5, 'work.reviewed', { submission_seq: 99, status: 'accepted' })])
  expect(progress.get(2)?.state).toBe('已提交，等待验收')
  expect(progress.get(2)?.steps).toHaveLength(3)
})

test('旧尝试与其他成员事件不混入，失败不冒充仍在运行', () => {
  const progress = workProgress([plan, event(2, 'work.claimed'), event(3, 'msg.said', { attempt_id: 'old' }),
    event(4, 'msg.said', { work_item_id: 'b' }), event(5, 'work.failed', { uncertain: true, reason: '调用超时，结果需核对' })])
  expect(progress.get(2)?.state).toBe('结果待核对')
  expect(progress.get(2)?.steps).toHaveLength(2)
})

test('重规划终止旧进度，不把新安排的结果追加到旧过程', () => {
  const progress = workProgress([plan, event(2, 'work.claimed'), { ...plan, seq: 3 }, event(4, 'work.submitted')])
  expect(progress.get(2)?.state).toBe('安排已更新')
  expect(progress.get(2)?.steps).toHaveLength(1)
})

test('失效工作撤回完成状态，后续旧回执不能重新验收', () => {
  const progress = workProgress([plan, event(2, 'work.claimed'), event(3, 'work.submitted'),
    event(4, 'work.invalidated', { affected: ['a'] }), event(5, 'work.reviewed', { status: 'accepted', submission_seq: 3 })])
  expect(progress.get(2)?.state).toBe('需要重新核对')
})

test('重新检查撤回当前通过状态，也撤回依赖成员的旧进展', () => {
  const linked = { ...plan, payload: { items: [{ id: 'a', objective: '实现棋盘' }, { id: 'b', objective: '检查棋盘', depends_on: ['a'] }] } }
  const progress = workProgress([linked, event(2, 'work.claimed'), event(3, 'work.submitted'),
    event(4, 'work.reviewed', { submission_seq: 3, status: 'accepted' }), event(5, 'work.claimed', { work_item_id: 'b' }),
    event(6, 'work.check_started', { submission_seq: 3, lease_until: 200 })], 100)
  expect(progress.get(2)?.state).toBe('检查已发起，等待验收')
  expect(progress.get(5)?.state).toBe('前置成果重新检查，需要核对')
})

test('过期领取不一直宣称处理中，已结束工作不受旧租期影响', () => {
  expect(workProgress([plan, event(2, 'work.claimed', { lease_until: 99 })], 100).get(2)?.state).toBe('期限已过，结果待核对')
  expect(workProgress([plan, event(2, 'work.claimed', { lease_until: 99 }), event(3, 'work.submitted')], 100).get(2)?.state).toBe('已提交，等待验收')
})

test('重规划后的旧提交不再显示待验收，失败后的迟到提交也不推进', () => {
  expect(workProgress([plan, event(2, 'work.claimed'), event(3, 'work.submitted'), { ...plan, seq: 4 }]).get(2)?.state).toBe('安排已更新')
  expect(workProgress([plan, event(2, 'work.claimed'), event(3, 'work.failed', { uncertain: true }), event(4, 'work.submitted')]).get(2)?.state).toBe('结果待核对')
})

const scope = { direction_seq: 1, planning_seq: null, rules: 'rules-1' }
const consulting = (seq: number, type: string, extra = {}) => event(seq, type, { scope, requester: 'dev', recipient: 'design', ...extra })
const request = consulting(4, 'collaboration.requested', { question: '棋盘如何保持可读？' })
const answer = consulting(5, 'collaboration.finished', { request_seq: 4, speaker_id: 'design', status: 'answered', reply: '保留棋子边缘对比。' })

test('只读咨询关联领取与原请求，回复不冒充执行或验收，隐藏重复请求发言', () => {
  const events = [plan, consulting(2, 'work.claimed'), consulting(3, 'msg.said', { phase: 'collaboration_request' }), request, answer]
  const progress = workProgress(events).get(2)!
  expect(progress.state).toBe('咨询已回应，等待负责人继续')
  expect(progress.steps.map((step) => step.seq)).toEqual([2, 4, 5])
  expect(progress.steps[1].text).toContain('棋盘如何保持可读？')
  expect(progress.steps[2].text).toContain('咨询不代表验收通过')
  expect(workProgress([...events, consulting(6, 'msg.said')]).get(2)?.state).toBe('处理中')
})

test.each([
  { attempt_id: 'old' }, { plan_seq: 99 }, { work_item_id: 'b' }, { request_seq: 999 },
  { scope: { ...scope, direction_seq: 99 } }, { recipient: 'test' }, { requester: 'product' },
])('错配咨询回复不推进当前工作 %j', (extra) => {
  const wrong = { ...answer, payload: { ...answer.payload, ...extra } }
  const progress = workProgress([plan, consulting(2, 'work.claimed'), request, wrong]).get(2)!
  expect(progress.state).toBe('等待定向咨询')
  expect(progress.steps.map((step) => step.seq)).toEqual([2, 4])
})

test('旧咨询不能污染新尝试，失败或提交后的迟到回复不覆盖状态', () => {
  const retried = workProgress([plan, consulting(2, 'work.claimed'), request,
    consulting(5, 'work.claimed', { attempt_id: 'second' }), { ...answer, seq: 6 }])
  expect(retried.get(5)?.steps).toHaveLength(1)
  for (const type of ['work.failed', 'work.submitted']) {
    const progress = workProgress([plan, consulting(2, 'work.claimed'), request, consulting(5, type), { ...answer, seq: 6 }]).get(2)!
    expect(progress.steps.some((step) => step.seq === 6)).toBe(false)
  }
  const replan = workProgress([plan, consulting(2, 'work.claimed'), request, { ...plan, seq: 5 }, { ...answer, seq: 6 }])
  expect(replan.get(2)?.state).toBe('安排已更新')
  expect(replan.get(2)?.steps.some((step) => step.seq === 6)).toBe(false)
})

test.each(['invalid', 'unavailable', 'stale'])('咨询%s不冒充角色回应，仍不代表负责人已继续', (status) => {
  const progress = workProgress([plan, consulting(2, 'work.claimed'), request,
    { ...answer, payload: { ...answer.payload, status, reply: '不可信的回复' } }]).get(2)!
  expect(progress.state).toBe('咨询未完成，等待负责人处理')
  expect(progress.steps.at(-1)?.text).not.toContain('不可信的回复')
  expect(progress.steps.at(-1)?.text).not.toContain('已继续')
})
