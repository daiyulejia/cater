import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import { collaborationProjection, workTransfers } from '../src/collaborationProjection'

type Event = { seq: number; type: string; actor: { id: string; kind: string }; payload: Record<string, unknown> }
const event = (seq: number, type: string, payload: Record<string, unknown>, id = 'work_scheduler', kind = 'system'): Event => ({ seq, type, payload, actor: { id, kind } })
const plan = (seq: number, items = [{ owner: 'dev', reviewer: 'test' }], extra = {}) => event(seq, 'work.plan_created', { items, ...extra })
const finished = (seq: number, extra = {}) => event(seq, 'planning.finished', { status: 'installed', reason: '先完成入口，再核对验收。', ...extra }, 'store_manager')
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const members = [{ id: 'dev', name: '米寇', role: 'dev' }, { id: 'test', name: '三角', role: 'test' }, { id: 'design', name: '罐头', role: 'design' }]
const leader = { id: 'store_manager', name: '店长', role: 'leader' }

async function mount(initial: Event[], questions: Record<string, unknown>[] = []) {
  let events = [event(1, 'task.created', { goal: '安排接力' }, 'human', 'human'), ...initial]
  const makeDetail = () => ({ task: { id: 'notice', goal: '安排接力', acceptance: [], assignments: [], messages: [], deliverables: [], status: 'pending' }, events, questions, activity: { active: false, state: 'idle' }, review: {} })
  const fetchMock = vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'notice', goal: '安排接力', status: 'pending', status_label: '待开始', version: 1, latest_seq: 1, conversation_count: 1 }] })
    if (url === '/api/tasks/notice') return json(makeDetail())
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  await screen.findByRole('heading', { name: '安排接力', level: 1 })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').querySelectorAll('article').length).toBeGreaterThan(0))
  await waitFor(() => expect(screen.getByRole('button', { name: '今天在店里' }).textContent).toBeTruthy())
  return { fetchMock, update: async (next = initial) => {
    events = [events[0], ...next]
    await act(async () => fireEvent(document, new Event('visibilitychange')))
  } }
}
const stream = () => screen.getByLabelText('聊天记录')
const article = (seq: number) => within(stream()).getByText(`事件 #${seq} · 审计记录`).closest('article')!
const count = (value: number) => expect(stream().querySelectorAll('article')).toHaveLength(value)

test('自然聊天先显示原话，理解后的执行指令不重复显示成顾客再次改需求', async () => {
  await mount([
    event(2, 'conversation.received', { instruction: '@米寇 继需了吗？' }, 'human', 'human'),
    event(3, 'conversation.started', { conversation_seq: 2 }, 'conversation'),
    event(4, 'conversation.finished', { conversation_seq: 2, status: 'finished', action: 'discuss', reply: '米寇还在处理，尚未有新的提交。', speaker_id: 'store_manager' }, 'conversation'),
    event(5, 'conversation.received', { instruction: '那接着做吧' }, 'human', 'human'),
    event(6, 'human.commanded', { conversation_seq: 5, intent: 'resume', instruction: '那接着做吧' }, 'human', 'human'),
    event(7, 'conversation.finished', { conversation_seq: 5, status: 'applied', action: 'resume_task', reply: '按原来的安排继续。', speaker_id: 'store_manager' }, 'conversation'),
  ])
  count(5)
  expect(within(stream()).getAllByText('那接着做吧')).toHaveLength(1)
  expect(within(stream()).getByText('@米寇 继需了吗？')).toBeTruthy()
  expect(stream().textContent).not.toContain('方向更新')
  expect(article(4).querySelector('[data-actor="store_manager"].is-agent')).toBeTruthy()
})

test('理解后转讲解不把同一句话再显示一次，也不显示内部理解过程', async () => {
  await mount([
    event(2, 'conversation.received', { instruction: '这个咋打开呀' }, 'human', 'human'),
    event(3, 'question.asked', { question: '这个咋打开呀', role_id: 'dev' }, 'human', 'human'),
    event(4, 'conversation.finished', { conversation_seq: 2, question_seq: 3, reply: '我核对一下现有成果的打开方式。', speaker_id: 'store_manager' }, 'conversation'),
  ], [{ seq: 3, status: 'answering', role_id: 'store_manager' }])
  count(3)
  expect(within(stream()).getAllByText('这个咋打开呀')).toHaveLength(1)
  expect(within(article(4)).getByText('正在核对成果并回答')).toBeTruthy()
})

test('查询进度只展示真实记录，执行分流不把模型的提前成功描述当作事实', async () => {
  await mount([
    event(2, 'conversation.received', { instruction: '继需了吗' }, 'human', 'human'),
    event(3, 'task.progress_reported', { text: '任务尚未进入分项执行。' }, 'store_manager'),
    event(4, 'conversation.finished', { action: 'status_task', status: 'finished', progress_seq: 3, reply: '已经全部做好' }, 'conversation'),
    event(5, 'conversation.finished', { action: 'update_task', status: 'finished', reply: '正在开发，马上部署完成', speaker_id: 'store_manager' }, 'conversation'),
  ])
  expect(within(stream()).getByText('任务尚未进入分项执行。')).toBeTruthy()
  expect(within(stream()).getByText('补充要求已记入当前任务。')).toBeTruthy()
  expect(stream().textContent).not.toContain('已经全部做好')
  expect(stream().textContent).not.toContain('马上部署完成')
})

test.each(['before', 'after'])('计划在店长回执 %s 时仅在原安排气泡显示交接与首次加入，不重复名单', async (order) => {
  const events = order === 'before' ? [plan(3, undefined, { planning: { attempt_seq: 2 } }), finished(4, { plan_seq: 3, attempt_seq: 2 })]
    : [finished(3, { attempt_seq: 2 }), plan(4, undefined, { planning: { attempt_seq: 2 } })]
  const app = await mount(events)
  const receipt = order === 'before' ? 4 : 3
  await waitFor(() => expect(within(article(receipt)).getByLabelText('工作交接').textContent).toContain('接手：@米寇'))
  count(2)
  const row = article(receipt)
  expect(row.querySelector('[data-actor="store_manager"].is-agent')).toBeTruthy()
  expect(within(row).getByText('先完成入口，再核对验收。').textContent).toBe('先完成入口，再核对验收。')
  expect(within(row).getByLabelText('工作交接').textContent).toContain('复核：@三角')
  expect(within(row).getByLabelText('协作通知').textContent).toBe('米寇、三角 加入协作')
  expect(stream().textContent).not.toContain('罐头 加入协作')
  await app.update(); count(2)
  expect(within(stream()).getAllByLabelText('协作通知')).toHaveLength(1)
  const selected = within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /安排接力.*待开始/ })
  expect(selected.getAttribute('aria-current')).toBe('page')
  expect(selected.textContent).not.toMatch(/条对话|对话待同步/)
})

test('重规划只增量提示新 owner/reviewer，暂缓、离开后再加入及重复 owner 不重复加入', async () => {
  const events = [plan(2), finished(3, { plan_seq: 2 }),
    plan(4, [{ owner: 'dev', reviewer: 'test' }, { owner: 'dev', reviewer: 'design' }]), finished(5, { plan_seq: 4 }),
    plan(6, [{ owner: 'test', reviewer: 'dev' }]), finished(7, { plan_seq: 6 }),
    plan(8, [{ owner: 'design', reviewer: 'test' }]), finished(9, { plan_seq: 8 }),
    event(10, 'subtask.assigned', { owner: 'unassigned', status: 'deferred' }),
  ]
  await mount(events)
  await waitFor(() => expect(within(article(5)).getByLabelText('协作通知').textContent).toBe('罐头 加入协作'))
  count(5)
  expect(within(stream()).getAllByLabelText('协作通知')).toHaveLength(2)
  expect(within(article(7)).queryByLabelText('协作通知')).toBeNull()
  expect(within(article(9)).queryByLabelText('协作通知')).toBeNull()
  expect(stream().textContent).not.toContain('unassigned')
})

test('task.decomposed 的实际后继 assigned 只把首次增量附在已有分工上', async () => {
  const subtasks = [
    { id: 'a', owner: 'dev', lane: '实现', objective: '实现入口', status: 'assigned' },
    { id: 'b', owner: 'test', lane: '验收', objective: '验收入口', status: 'assigned' },
  ]
  await mount([event(2, 'task.decomposed', { subtasks }, 'store_manager'),
    event(3, 'subtask.assigned', subtasks[0], 'store_manager'), event(4, 'subtask.assigned', subtasks[1], 'store_manager')])
  await waitFor(() => expect(within(article(2)).getByLabelText('协作通知').textContent).toBe('米寇、三角 加入协作'))
  count(2)
  expect(within(article(2)).queryByLabelText('工作交接')).toBeNull()
  expect(within(article(2)).getByText(/实现入口，交给 @米寇/)).toBeTruthy()
})

test('没有店长消息的手动 plan 与独立 assigned 保留一条必要中性通知，只有首次增量加入', async () => {
  await mount([plan(2), event(3, 'subtask.assigned', { id: 'manual', owner: 'design', reviewer: 'dev', status: 'assigned' }, 'human', 'human'),
    event(4, 'subtask.assigned', { id: 'next', owner: 'dev', status: 'assigned' }, 'store_manager')])
  await waitFor(() => expect(within(article(2)).getByLabelText('工作交接').textContent).toContain('接手：@米寇'))
  count(4)
  const selected = within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /安排接力.*待开始/ })
  expect(selected.getAttribute('aria-current')).toBe('page')
  expect(selected.textContent).not.toMatch(/条对话|对话待同步/)
  expect(within(article(3)).getByLabelText('工作交接').textContent).toContain('接手：@罐头')
  expect(within(article(3)).getByLabelText('协作通知').textContent).toBe('罐头 加入协作')
  expect(within(article(4)).queryByLabelText('协作通知')).toBeNull()
  for (const seq of [2, 3, 4]) {
    expect(within(article(seq)).queryByRole('img')).toBeNull()
    expect(article(seq).querySelector('.message-bubble')).toBeNull()
  }
})

test('旧安排无可关联计划保留原话，失败规划不编造交接；名单必须来自事件而非当前 work', async () => {
  await mount([finished(2), event(3, 'planning.finished', { status: 'stale', reason: '方向已变化。', plan_seq: 999 }, 'store_manager')])
  count(3)
  expect(within(stream()).queryByLabelText('工作交接')).toBeNull()
  expect(within(stream()).queryByLabelText('协作通知')).toBeNull()
  expect(within(article(2)).getByText('先完成入口，再核对验收。')).toBeTruthy()
})

test('关联按 plan_seq 优先，不跨新方向或下一次规划回执猜接手人', () => {
  const events = [finished(2, { plan_seq: 8, attempt_seq: 7 }),
    plan(3, [{ owner: 'wrong', reviewer: 'wrong' }], { planning: { attempt_seq: 7 } }),
    finished(4), event(5, 'human.commanded', { instruction: '换方向' }, 'human', 'human'), plan(6),
    plan(8, [{ owner: 'actual', reviewer: 'check' }]), finished(9, { status: 'invalid', plan_seq: 6 })]
  const projection = collaborationProjection(events, 'store_manager')
  expect(projection.get(2)?.handoff).toEqual({ owners: ['actual'], reviewers: ['check'] })
  expect(projection.get(4)).toBeUndefined()
  expect(projection.get(9)).toBeUndefined()
  expect(projection.get(3)?.handoff?.owners).toEqual(['wrong'])
  expect(projection.get(6)?.handoff?.owners).toEqual(['dev'])
})

test('旧回执只有后继计划时可关联；显式关联失配、无真实 owner、deferred 不产生假加入', () => {
  const events = [finished(2), plan(3), finished(4, { attempt_seq: 77 }), plan(5, [], { planning: { attempt_seq: 88 } }),
    event(6, 'subtask.assigned', { owner: 'design', status: 'deferred' }), plan(7, [{ owner: 'store_manager', reviewer: 'store_manager' }])]
  const projection = collaborationProjection(events, 'store_manager')
  expect(projection.get(2)?.handoff?.owners).toEqual(['dev'])
  expect(projection.get(3)).toBeUndefined()
  expect(projection.get(4)).toBeUndefined()
  expect(projection.get(5)).toBeUndefined()
  expect(projection.get(6)).toBeUndefined()
  expect(projection.get(7)?.joined).toEqual([])
})

const workPlan = () => event(2, 'work.plan_created', { items: [
  { id: 'a', owner: 'dev', reviewer: 'test', objective: '实现棋盘', depends_on: [] },
  { id: 'b', owner: 'design', reviewer: 'test', objective: '完善界面', depends_on: ['a'] },
] })
const workEvent = (seq: number, type: string, id = 'a', attempt = 'run-a', extra = {}) => event(seq, type, { plan_seq: 2, work_item_id: id, attempt_id: attempt, ...extra })

test('实际提交才通知复核人；依赖验收不伪装已开工，调度领取后才显示接力', async () => {
  const events = [workPlan(), workEvent(3, 'work.claimed'), workEvent(4, 'work.submitted'),
    workEvent(5, 'work.reviewed', 'a', 'run-a', { status: 'accepted', submission_seq: 4 }),
    workEvent(6, 'work.claimed', 'b', 'run-b', { inputs: { a: 'accepted-digest' } })]
  const projection = workTransfers(events)
  expect([...projection.keys()]).toEqual([3, 4, 6])
  await mount(events)
  expect(within(article(4)).getByText('@米寇 已提交成果，交给 @三角 复核。')).toBeTruthy()
  expect(within(article(6)).getByText('@罐头 已接手，接着 @米寇 的成果继续做。')).toBeTruthy()
  expect(within(article(4)).queryByRole('img')).toBeNull()
  expect(await within(article(6)).findByText('完善界面')).toBeTruthy()
  expect(within(article(3)).getByText('@米寇 已接手这项工作。')).toBeTruthy()
  expect(within(article(3)).getByText('工作过程 · 阶段验收通过')).toBeTruthy()
  expect(within(article(6)).getByText('工作过程 · 处理中').closest('details')!.open).toBe(true)
})

test('多项依赖尚无领取记录时不编造下一位；旧计划、旧尝试和失效提交不形成交接', () => {
  const events = [workPlan(), workEvent(3, 'work.submitted'), workEvent(4, 'work.claimed'),
    workEvent(5, 'work.submitted', 'a', 'old-attempt'),
    workEvent(6, 'work.invalidated', 'a', 'run-a', { affected: ['a', 'b'] }),
    workEvent(7, 'work.submitted'),
    workEvent(8, 'work.reviewed', 'a', 'run-a', { status: 'accepted' }),
    plan(9), workEvent(10, 'work.claimed', 'b', 'run-b', { inputs: { a: 'digest' } })]
  expect([...workTransfers(events).keys()]).toEqual([4])
})

test('分工正文展示工作到成员的对应关系，常规阶段播报不让店长重复插话', async () => {
  await mount([workPlan(), finished(3, { plan_seq: 2 }),
    event(4, 'brief.generated', { reason: 'work_checkpoint', brief: { checkpoint: { state: 'ready' }, dispute: '例行阶段播报' } }, 'store_manager'),
    event(5, 'brief.generated', { reason: 'work_checkpoint', brief: { checkpoint: { state: 'uncertain' }, dispute: '执行结果需要核对' } }, 'store_manager'),
    event(6, 'task.progress_reported', { text: '已完成一项，下一项尚未开始。' }, 'store_manager')])
  expect(within(article(3)).getByLabelText('工作交接').textContent).toContain('实现棋盘，交给 @米寇')
  expect(within(article(3)).getByLabelText('工作交接').textContent).toContain('完善界面，交给 @罐头')
  expect(within(stream()).queryByText('例行阶段播报')).toBeNull()
  expect(within(article(5)).getByText('执行结果需要核对')).toBeTruthy()
  expect(within(article(6)).getByText('已完成一项，下一项尚未开始。')).toBeTruthy()
})
