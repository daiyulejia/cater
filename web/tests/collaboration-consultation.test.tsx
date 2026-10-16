import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'

const members = [
  { id: 'dev', name: '米寇', role: '开发' },
  { id: 'design', name: '月亮', role: '设计' },
  { id: 'test', name: '麦芽', role: '复核' },
]
const leader = { id: 'store_manager', name: '店长', role: '店长' }
const scope = { direction_seq: 1, planning_seq: null, rules: 'r1' }
const binding = { work_item_id: 'board', attempt_id: 'first', plan_seq: 2, scope, requester: 'dev', recipient: 'design' }
const event = (seq: number, type: string, id: string, payload: Record<string, unknown>, kind = 'agent') => ({ seq, type, actor: { id, kind, role: id }, payload })
const request = event(5, 'collaboration.requested', 'dev', { ...binding, question: '棋盘怎样保持清晰？', message_seq: 4 })
const finish = (status: string, extra = {}) => event(6, 'collaboration.finished', 'collaboration', {
  ...binding, request_seq: 5, speaker_id: 'design', status,
  reply: '棋盘边缘保留足够对比。', reason: '只读咨询记录',
  evidence: [{ artifact_seq: 20, quote: '<canvas aria-label="棋盘">' }], ...extra,
}, 'system')

async function mount(extra: ReturnType<typeof event>[]) {
  const item = { id: 'board', owner: 'dev', reviewer: 'test', objective: '实现棋盘', status: 'running', reason: '', depends_on: [], criteria: [], checks: [], submission: null }
  const events = [event(1, 'task.created', 'human', { goal: '五子棋' }, 'human'),
    event(2, 'work.plan_created', 'work_scheduler', { items: [item] }, 'system'),
    event(3, 'work.claimed', 'work_scheduler', { ...binding, lease_until: Date.now() / 1000 + 600 }, 'system'),
    event(4, 'msg.said', 'dev', { ...binding, phase: 'collaboration_request', text: '重复发言不能显示' }), request, ...extra]
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'consult', goal: '五子棋', status: 'running', status_label: '执行中', conversation_count: events.length, latest_seq: events.at(-1)!.seq, version: events.length }] })
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, workers: members, cabinet: [] })
    if (url === '/api/tasks/consult') return json({ task: { id: 'consult', goal: '五子棋', status: 'running', acceptance: [], assignments: [], messages: [], deliverables: [],
      work: { plan_seq: 2, scope_current: true, stage_accepted: false, items: [item] } }, events, activity: { active: false, state: 'idle' }, review: {} })
    throw new Error(`Unexpected request ${url}`)
  }))
  await act(async () => { render(<App />) })
  return screen.getByLabelText('聊天记录')
}

function row(seq: number) {
  return within(screen.getByLabelText('聊天记录')).getByText(`事件 #${seq} · 审计记录`).closest('article')!
}

function visibleText(element: HTMLElement) {
  const clone = element.cloneNode(true) as HTMLElement
  clone.querySelectorAll('details:not([open])').forEach((details) => {
    Array.from(details.children).forEach((child) => { if (child.tagName !== 'SUMMARY') child.remove() })
  })
  return clone.textContent ?? ''
}

test('请求展示实际问题和@接收者，不增加收到回执或店长插话', async () => {
  const stream = await mount([])
  expect(visibleText(row(5))).toContain('棋盘怎样保持清晰？')
  expect(visibleText(row(5))).toContain('@月亮')
  expect(row(5).querySelector('[data-actor="dev"]')).toBeTruthy()
  expect(stream.textContent).not.toContain('重复发言不能显示')
  expect(stream.querySelector('[data-actor="store_manager"]')).toBeNull()
  expect(stream.querySelector('[data-actor="design"]')).toBeNull()
  expect(visibleText(row(5))).not.toContain('已收到')
})

test.each(['answered', 'declined'])('%s呈现实际角色回复，引用默认关闭且安全展示', async (status) => {
  await mount([finish(status)])
  const reply = row(6)
  expect(reply.querySelector('[data-actor="design"]')).toBeTruthy()
  expect(visibleText(reply)).toContain('月亮')
  expect(visibleText(reply)).toContain('棋盘边缘保留足够对比。')
  expect(visibleText(reply)).toContain('@米寇')
  const details = (await within(reply).findByText('查看判断与依据')).closest('details')!
  expect(details.open).toBe(false)
  expect(visibleText(reply)).not.toContain('成果 #20')
  fireEvent.click(details.querySelector('summary')!)
  expect(visibleText(reply)).toContain('成果 #20')
  expect(visibleText(reply)).toContain('<canvas aria-label="棋盘">')
  expect(visibleText(reply)).toContain('不代表工具执行或验收通过')
  expect(reply.querySelector('canvas')).toBeNull()
})

test.each(['invalid', 'unavailable', 'stale'])('%s只是系统回执，不用speaker_id冒充角色回复', async (status) => {
  await mount([finish(status, { reply: '这段不是有效角色回复', reason: '本次未取得有效咨询结果' })])
  const reply = row(6)
  expect(reply.querySelector('[data-actor="system"]')).toBeTruthy()
  expect(reply.querySelector('[data-actor="design"]')).toBeNull()
  expect(within(reply).queryByRole('img')).toBeNull()
  expect(visibleText(reply)).toContain('本次未取得有效咨询结果')
  expect(visibleText(reply)).not.toContain('这段不是有效角色回复')
})

test.each([
  { attempt_id: 'other' }, { plan_seq: 99 }, { scope: { ...scope, rules: 'other' } },
  { request_seq: 99 }, { speaker_id: 'test' }, { reply: '' },
])('错配或缺失真实回复不挪用角色身份 %j', async (extra) => {
  await mount([finish('answered', extra)])
  expect(row(6).querySelector('[data-actor="system"]')).toBeTruthy()
  expect(visibleText(row(6))).not.toContain('棋盘边缘保留足够对比。')
})

test('非咨询服务发出的完成事件不能冒充有效角色回复', async () => {
  const forged = finish('answered')
  forged.actor = { id: 'dev', kind: 'agent', role: 'dev' }
  await mount([forged])
  expect(row(6).querySelector('[data-actor="system"]')).toBeTruthy()
})
