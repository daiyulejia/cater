import { render } from '@testing-library/react'
import { expect, test } from 'vitest'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', role: 'team', kind: 'system' }
const events: TeamEvent[] = [
  { seq: 1, type: 'team.message_received', actor: { id: 'human', role: 'human', kind: 'human' }, payload: { kind: 'message', recipient: 'store_manager', text: '为什么这样修改？' } },
  { seq: 2, type: 'team.run_started', actor: host, payload: { message_seq: 1, role: 'store_manager', run_id: 'reply', revision: 0 } },
  { seq: 3, type: 'team.tool_started', actor: host, payload: { message_seq: 1, role: 'store_manager', run_id: 'reply', name: 'reply_to_customer' } },
]

test('顾客答复入口在折叠工具过程中显示中文，不伪称任务交付', () => {
  const view = render(<TeamToolProgress events={[...events, { seq: 5, type: 'team.tool_finished', actor: host,
    payload: { started_seq: 3, role: 'store_manager', run_id: 'reply', name: 'reply_to_customer', result: { status: 'ok', message_seq: 4, reply_to: 1 } } }]} names={{ store_manager: '店长' }} />)
  expect(view.container.textContent).toContain('答复顾客 · 工具已完成')
  expect(view.container.textContent).toContain('已投递消息 #4')
  expect(view.container.textContent).not.toMatch(/reply_to_customer|任务已完成|复核通过/)
  expect(view.container.querySelector('details')?.open).toBe(false)
})

test('处理中的答复动作使用中文名称', () => {
  const team: TeamState = { enabled: true, objective: '', status: 'running', revision: 0, pending_count: 0, checkpoint: '', runs: [], assignments: [] }
  const view = conversationActivity({ team, events, names: { store_manager: '店长' }, leaderId: 'store_manager', sending: false, executionState: 'running' })
  expect(JSON.stringify(view)).toContain('答复顾客')
  expect(JSON.stringify(view)).not.toContain('reply_to_customer')
})
