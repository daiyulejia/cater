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
