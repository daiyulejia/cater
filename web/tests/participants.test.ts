import { expect, test } from 'vitest'
import { conversationParticipants } from '../src/participants'
import type { WorkState } from '../src/WorkItems'

test('当前计划只展示店长、工作负责人和独立复核人，不沿用历史全员', () => {
  const work = { plan_seq: 4, stage_accepted: false, items: [
    { owner: 'dev', reviewer: 'test' }, { owner: 'dev', reviewer: null },
  ] } as WorkState
  expect(conversationParticipants({ work, assignments: [{ owner: 'product', status: 'assigned' }],
    messages: [{ actor: 'design' }] }, 'manager', ['manager', 'dev', 'test', 'product', 'design']))
    .toEqual(['manager', 'dev', 'test'])
})

test('旧会话按真实安排或发言展示，排除顾客和未知系统身份', () => {
  expect(conversationParticipants({ assignments: [{ owner: 'dev', status: 'assigned' }],
    messages: [{ actor: 'human' }, { actor: 'dev' }, { actor: 'product' }, { actor: 'scheduler' }] },
    'manager', ['manager', 'dev', 'product', 'design'])).toEqual(['manager', 'dev', 'product'])
})

test('只纳入当前工作尝试明确咨询的已知成员，不沿用旧计划或旧尝试', () => {
  const scope = { direction_seq: 1, planning_seq: null, rules: 'r1' }
  const work = { plan_seq: 4, items: [{ id: 'a', owner: 'dev', reviewer: 'test' }] } as WorkState
  const task = { work, assignments: [], messages: [] }
  const binding = { plan_seq: 4, work_item_id: 'a', attempt_id: 'current', scope, requester: 'dev' }
  const event = (seq: number, type: string, payload = {}) => ({ seq, type, payload: { ...binding, ...payload } })
  const events = [event(5, 'work.claimed'), event(6, 'collaboration.requested', { recipient: 'design' }),
    event(7, 'collaboration.requested', { recipient: 'product', attempt_id: 'old' }),
    event(8, 'collaboration.requested', { recipient: 'product', plan_seq: 1 }),
    event(9, 'collaboration.requested', { recipient: 'product', scope: { ...scope, rules: 'old' } }),
    event(10, 'collaboration.requested', { recipient: 'unknown' })]
  expect(conversationParticipants(task, 'manager', ['manager', 'dev', 'test', 'design', 'product'], [], events))
    .toEqual(['manager', 'dev', 'test', 'design'])
  expect(conversationParticipants(task, 'manager', ['manager', 'dev', 'test', 'design'], [],
    [...events, event(11, 'work.claimed', { attempt_id: 'new' })])).toEqual(['manager', 'dev', 'test'])
})
