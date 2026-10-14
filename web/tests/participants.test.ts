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
