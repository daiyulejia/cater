import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import type { TeamState } from '../src/team'
import { teamDeliveries, teamMessageAudience, teamMessageEchoes, teamProgressMessages, teamStatus } from '../src/team'

type EventRecord = TaskDetail['events'][number]
const event = (seq: number, type: string, payload: Record<string, unknown> = {}, id = 'team', kind = 'system'): EventRecord => ({
  seq, type, payload, actor: { id, kind, role: id }, category: 'collaboration',
})
const teamState = (extra: Partial<TeamState> = {}): TeamState => ({ enabled: true, objective: '团队接力', status: 'running', pending_count: 0,
  runs: [], assignments: [], checkpoint: '继续完善入口与交付说明', revision: 1, ...extra })
const oldWork = { plan_seq: 4, stage_accepted: false, scope_current: false, items: [{ id: 'old', owner: 'test', reviewer: 'design',
  objective: '旧命令行进度', status: 'submitted', reason: '旧工作等待验收', depends_on: [], criteria: [{ id: 'check', kind: 'local_check', text: '旧检查门闩' }], checks: [], submission: { seq: 5, artifact_seqs: [] } }] }
const detail = (events: EventRecord[], team: TeamState | null = teamState()): TaskDetail => ({ task: { id: 'team-task', goal: '团队接力', acceptance: [],
  status: 'done', messages: [], assignments: [], deliverables: [], team, work: oldWork, brief: { dispute: '旧进度卡住' } },
  events: [event(1, 'task.created', { goal: '团队接力' }, 'human', 'human'), ...events],
  activity: { active: false, state: 'completed' }, review: {},
})
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const members = [{ id: 'dev', name: '米寇', role: 'dev' }, { id: 'test', name: '三角', role: 'test' }, { id: 'design', name: '罐头', role: 'design' }]
const leader = { id: 'store_manager', name: '店长', role: 'leader' }
const stream = () => within(screen.getByLabelText('聊天记录'))
const board = () => within(screen.getByRole('complementary', { name: '成果与过程' }))
beforeEach(() => window.sessionStorage.clear())

test.each(['paused', 'cancelled'] as const)('App将%s执行控制传到有待处理消息的团队工作板', async (state) => {
  const current = detail([event(2, 'task.execution_controlled', { action: state === 'paused' ? 'pause' : 'cancel' }, 'human', 'human')],
    teamState({ pending_count: 2 }))
  current.execution_control = { state, seq: 2, reason: 'Execution is not running' }
  await mount(current)
  expect(board().getByLabelText('团队执行状态').textContent).toBe(state === 'paused' ? '已暂停后续执行' : '已取消后续执行')
  expect(board().getByText('待处理消息：2')).toBeTruthy()
  expect(board().queryByText('正在处理消息')).toBeNull()
})

test.each([
  ['queued', true, 'failed', '消息处理已排队'],
  ['running', true, 'running', '正在处理消息'],
  ['completed', false, 'failed', '消息处理失败'],
  ['completed', false, 'waiting', '本轮结束，等待消息'],
  ['completed', false, 'running', '执行状态待核对'],
] as const)('App统一列表、顶部、过程状态：消费者%s，角色%s', async (state, active, status, label) => {
  const current = detail([], teamState({ runs: [{ message_seq: 1, role: 'dev', revision: 1, status }] }))
  current.activity = { state, active }
  const { fetchMock } = await mount(current)
  expect(board().getByLabelText('团队执行状态').textContent).toBe(label)
  expect(document.querySelector('.chat-heading .status-pill')?.textContent).toBe(label)
  expect(screen.getByRole('button', { name: /团队接力/ }).textContent).toContain(label)
  expect(Boolean(document.querySelector('.chat-heading .status-pill.is-running'))).toBe(active && state === 'running')
  expect(fetchMock.mock.calls.some(([, init]) => init.method === 'POST')).toBe(false)
})

const recoveryMessage = () => event(4, 'team.message_received', { kind: 'recovery', recipient: 'store_manager',
  recovery_of: 2, recovery_hash: 'original-start-hash', interrupted_seq: 3, revision: 1, scope: { revision: 1 }, text: '先核对持久事实，再决定如何继续' })

test('恢复轮的正式店长交付仍向顾客显示，不能被内部消息折叠规则吞掉', async () => {
  const recovery = recoveryMessage()
  const completed = event(6, 'team.objective_completed', { summary: '已核对并完成说明', verification: '正文已核对', run_id: 'fresh-recovery' })
  const reply = event(7, 'msg.said', { phase: 'team', reply_to: 4, audience: 'customer', recipient: 'human',
    delivery_seq: 6, run_id: 'fresh-recovery', text: '说明已准备好，包含本机打开步骤。' }, 'store_manager', 'agent')
  expect(teamMessageAudience(reply, new Map([[4, recovery], [6, completed]]))).toEqual({ audience: 'customer', recipient: 'human' })
  await mount(detail([recovery, completed, reply], teamState({ status: 'done', revision: 6 })))
  expect(stream().getByText('说明已准备好，包含本机打开步骤。').closest('details')).toBeNull()
})

test('受信系统恢复通知只进入内部折叠过程，不冒充顾客或成员，不宣称完成', async () => {
  const recovery = recoveryMessage()
  expect(teamMessageAudience(recovery, new Map())).toEqual({ audience: 'team', recipient: 'store_manager' })
  await mount(detail([event(2, 'team.run_started', { message_seq: 1, role: 'store_manager', run_id: 'old' }),
    event(3, 'team.run_finished', { message_seq: 1, role: 'store_manager', run_id: 'old', status: 'interrupted' }), recovery,
    event(5, 'msg.said', { phase: 'team', reply_to: 4, text: '正在核对原工作记录' }, 'store_manager', 'agent')]))
  const group = stream().getByText('团队协作 · 2 条').closest('details')!
  expect(group.open).toBe(false)
  const notice = within(group).getByText('先核对持久事实，再决定如何继续').closest('[data-actor]')!
  expect(notice.getAttribute('data-actor')).toBe('system')
  expect(notice.textContent).toContain('系统 · 恢复通知')
  expect(within(group).getByText('正在核对原工作记录')).toBeTruthy()
  expect(stream().queryByText(/店长最终交付|恢复成功|已验收/)).toBeNull()
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('伪造恢复来源 %j 不进入聊天、不归为内部受信消息', async (actor) => {
  const recovery = { ...recoveryMessage(), actor }
  expect(teamMessageAudience(recovery, new Map())).toEqual({})
  const reply = event(5, 'msg.said', { phase: 'team', reply_to: recovery.seq }, 'store_manager', 'agent')
  expect(teamMessageAudience(reply, new Map([[recovery.seq, recovery]]))).toEqual({})
  await mount(detail([recovery]))
  expect(stream().queryByText('先核对持久事实，再决定如何继续')).toBeNull()
  expect(stream().queryByText('恢复通知')).toBeNull()
})

test('接续按原工作交接折叠展示，关联回应也留在内部', async () => {
  const continuation = event(3, 'team.message_received', { kind: 'continuation', recipient: 'dev', text: '保留原实现，补核对回执',
    continue_assignment_seq: 2, revision: 1, visible: true }, 'store_manager', 'agent')
  const reply = event(4, 'msg.said', { phase: 'team', reply_to: 3, audience: 'customer', text: '正在补核对回执' }, 'dev', 'agent')
  expect(teamMessageAudience(reply, new Map([[3, continuation]]))).toEqual({ audience: 'team', recipient: 'store_manager' })
  await mount(detail([event(2, 'team.message_received', { kind: 'assignment', recipient: 'dev', text: '原实现委托' }, 'store_manager', 'agent'), continuation, reply]))
  const group = stream().getByText('团队协作 · 3 条').closest('details')!
  expect(group.open).toBe(false)
  expect(within(group).getByText('@店长 → @米寇 · 接续工作交接').closest('article')?.className).toBe('collaboration-notice')
  expect(within(group).getByText('保留原实现，补核对回执').closest('details')?.open).toBe(false)
  expect(within(group).getByText('正在补核对回执')).toBeTruthy()
  expect(stream().queryByText('店长最终交付')).toBeNull()
})

test.each(['human', 'system', 'hidden'])('来源或可见性不符合契约的接续不展示：%s', async (kind) => {
  await mount(detail([event(2, 'team.message_received', { kind: 'continuation', recipient: 'dev', text: '不可信接续指令',
    visible: kind !== 'hidden', continue_assignment_seq: 1 }, 'store_manager', kind === 'hidden' ? 'agent' : kind)]))
  expect(stream().queryByText('不可信接续指令')).toBeNull()
})

test('现有协作图与列表呈现接续原委托关系和补充指令', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  await mount(detail([
    event(2, 'team.message_received', { kind: 'assignment', text: '保留的原委托', recipient: 'dev' }, 'store_manager', 'agent'),
    event(3, 'team.message_received', { kind: 'continuation', text: '补查现有成果', recipient: 'dev', continue_assignment_seq: 2, visible: true }, 'store_manager', 'agent'),
  ]))
  fireEvent.click(screen.getByRole('button', { name: '任务脉络' }))
  const dialog = await screen.findByRole('dialog', { name: '任务脉络' })
  expect(await within(dialog).findByRole('region', { name: '当前团队协作图' })).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: '协作列表' }))
  const list = within(dialog).getByRole('list', { name: '当前团队协作列表' })
  fireEvent.click(within(list).getByRole('button', { name: /接续交接 #3/ }))
  const details = within(within(dialog).getByRole('complementary', { name: '协作记录详情' }))
  expect(details.getByText('补查现有成果')).toBeTruthy()
  expect(details.getByText('接续指令已投递')).toBeTruthy()
  expect(details.getByRole('button', { name: '接续原委托 · 委托 #2' })).toBeTruthy()
})

test('接续投递的真实工具回执可去除同轮重复口述', () => {
  const events = [event(2, 'team.tool_started', { name: 'send_message', role: 'store_manager', run_id: 'resume' }),
    event(3, 'team.message_received', { kind: 'continuation', recipient: 'dev', visible: true, text: '请补核对回执', continue_assignment_seq: 1 }, 'store_manager', 'agent'),
    event(4, 'team.tool_finished', { started_seq: 2, role: 'store_manager', result: { status: 'ok', message_seq: 3 } }),
    event(5, 'msg.said', { phase: 'team', run_id: 'resume', recipient: 'dev', text: '请补核对回执' }, 'store_manager', 'agent')]
  expect(teamMessageEchoes(events)).toEqual(new Set([5]))
  events[1].payload.visible = false
  expect(teamMessageEchoes(events)).toEqual(new Set())
})

test('原生聊天复用脉络入口且不创建旧计划，打开关闭后保留草稿并恢复焦点', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  const { fetchMock } = await mount(detail([
    event(2, 'team.message_received', { text: '检查实际入口', recipient: 'dev', kind: 'assignment' }, 'store_manager', 'agent'),
    event(3, 'team.message_received', { text: '入口已检查', recipient: 'store_manager', kind: 'report', reply_to: 2 }, 'dev', 'agent'),
  ]))
  const input = screen.getByRole('textbox', { name: '向团队发送消息' })
  fireEvent.change(input, { target: { value: '保留当前草稿' } })
  const trigger = screen.getByRole('button', { name: '任务脉络' })
  trigger.focus()
  fireEvent.click(trigger)
  const dialog = await screen.findByRole('dialog', { name: '任务脉络' })
  expect(within(dialog).getByRole('region', { name: '当前团队协作图' })).toBeTruthy()
  expect(dialog.textContent).not.toContain('旧命令行进度')
  fireEvent.click(within(dialog).getByRole('button', { name: '协作列表' }))
  expect(within(dialog).getByRole('list', { name: '当前团队协作列表' })).toBeTruthy()
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '任务脉络' })).toBeNull())
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect((input as HTMLTextAreaElement).value).toBe('保留当前草稿')
  expect(fetchMock.mock.calls.some(([, init]) => init.method === 'POST')).toBe(false)
})

test('仅知会的进展留在可展开协作记录，不冒充待执行工作或再次公开答复', async () => {
  await mount(detail([event(2, 'team.objective_completed', { summary: '已经交付' }),
    event(3, 'team.message_received', { text: '资料已归档，仅供知悉', recipient: 'store_manager', kind: 'notification', visible: true }, 'dev', 'agent')],
    teamState({ revision: 2, status: 'done' })))
  const note = stream().getByText('@米寇 → @店长 · 进展知会')
  expect(note.closest('details.team-collaboration')?.open).toBe(false)
  expect(stream().getByText('资料已归档，仅供知悉').closest('details')?.open).toBe(false)
  expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull()
  expect(stream().queryByText(/后台有.*等待处理/)).toBeNull()
})

test('已交付后的追问仍显示消息在处理，不把旧交付当成本轮答复', () => {
  expect(teamStatus(teamState({ status: 'done', pending_count: 1 }))).toBe('后续消息等待处理')
  expect(teamStatus(teamState({ status: 'done', runs: [{ role: 'dev', revision: 1, status: 'running' }] }))).toBe('正在处理后续消息')
  expect(teamStatus(teamState({ status: 'done', runs: [{ role: 'dev', revision: 0, status: 'running' }] }))).toBe('店长确认可交付')
})

test('工具调用阶段的对客过程发言归入过程，实际答复保持主对话可见', async () => {
  const acknowledgement = event(3, 'msg.said', { text: '收到，我先核对文件。', phase: 'team', run_id: 'read', reply_to: 2,
    audience: 'customer', recipient: 'human', response_kind: 'progress' }, 'store_manager', 'agent')
  const answer = event(5, 'msg.said', { text: '双击网页文件就能打开。', phase: 'team', run_id: 'read', reply_to: 2,
    audience: 'customer', recipient: 'human', response_kind: 'reply' }, 'store_manager', 'agent')
  await mount(detail([event(2, 'team.message_received', { text: '怎么打开？', recipient: 'store_manager', kind: 'message' }, 'human', 'human'),
    acknowledgement, event(4, 'team.tool_started', { run_id: 'read', role: 'store_manager', name: 'workspace_read' }), answer]))
  expect(stream().getByText('收到，我先核对文件。').closest('details.team-collaboration')?.open).toBe(false)
  expect(stream().getByText('双击网页文件就能打开。').closest('details.team-collaboration')).toBeNull()
})

test.each([true, false])('等待信息的对客发言不自动收起，避免隐藏真实澄清（明确类型=%s）', async (explicit) => {
  const said = event(3, 'msg.said', { text: '请补充你要处理的文件路径。', phase: 'team', run_id: 'waiting', reply_to: 2,
    audience: 'customer', recipient: 'human', ...(explicit ? { response_kind: 'reply' } : {}) }, 'store_manager', 'agent')
  const waiting = event(4, 'team.tool_started', { run_id: 'waiting', role: 'store_manager', name: 'wait_for_reply' })
  expect(teamProgressMessages([said, waiting])).toEqual(new Set())
  await mount(detail([event(2, 'team.message_received', { text: '请处理一下', recipient: 'store_manager', kind: 'message' }, 'human', 'human'), said, waiting]))
  expect(stream().getByText('请补充你要处理的文件路径。').closest('details.team-collaboration')).toBeNull()
})

test.each(['other-run', 'wrong-role', 'untrusted', 'after-finish', 'explicit-reply', 'official-delivery'])
('旧发言只有可信的同轮工具开始才作为过程合并，不误藏答复：%s', (caseName) => {
  const said = event(2, 'msg.said', { phase: 'team', run_id: 'run', text: '正在查看。' }, 'dev', 'agent')
  const tool = event(4, 'team.tool_started', { run_id: 'run', role: 'dev', name: 'workspace_read' })
  expect(teamProgressMessages([tool, said])).toEqual(new Set([2]))
  if (caseName === 'other-run') tool.payload.run_id = 'other'
  if (caseName === 'wrong-role') tool.payload.role = 'test'
  if (caseName === 'untrusted') tool.actor.kind = 'agent'
  if (caseName === 'explicit-reply') said.payload.response_kind = 'reply'
  if (caseName === 'official-delivery') said.payload.delivery_seq = 1
  const finish = event(3, 'team.run_finished', { run_id: 'run', role: 'dev', status: 'completed' })
  expect(teamProgressMessages([said, ...(caseName === 'after-finish' ? [finish] : []), tool])).toEqual(new Set())
})

async function mount(initial: TaskDetail) {
  let current = initial
  const fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if (url === '/api/task-summaries') return json({ tasks: [current.summary ?? { id: 'team-task', goal: '团队接力', status: 'done', status_label: '已完成',
      version: 1, latest_seq: 1, conversation_count: 1 }] })
    if (url === '/api/tasks/team-task') return json(current)
    if (url === '/api/tasks/team-task/questionnaires' && init.method === 'POST') {
      const body = JSON.parse(String(init.body))
      current = { ...current, questionnaires: current.questionnaires?.map((question) => question.seq === body.question_seq
        ? { ...question, status: 'answered', response: { answers: body.answers } } : question),
        events: [...current.events, event((current.events.at(-1)?.seq ?? 0) + 1, 'team.message_received',
          { text: '我的补充：在职，转行。', recipient: 'store_manager', kind: 'message', request_id: body.request_id }, 'human', 'human')] }
      return json(current)
    }
    if (url === '/api/tasks/team-task/command' && init.method === 'POST') {
      const body = JSON.parse(String(init.body))
      current = { ...current, events: [...current.events, event((current.events.at(-1)?.seq ?? 0) + 1, 'team.message_received',
        { text: body.instruction, kind: 'message', request_id: body.request_id }, 'human', 'human')] }
      return json(current)
    }
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  await screen.findByRole('region', { name: '团队进度' }, { timeout: 3000 })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  return { fetchMock, update: async (next: TaskDetail) => {
    current = next
    await act(async () => fireEvent(document, new Event('visibilitychange')))
  } }
}

test('原生问题卡直接在聊天展示，一组回答只提交一次并保留普通聊天草稿', async () => {
  const question = { seq: 3, role_id: 'store_manager', title: '先了解你的背景', status: 'pending' as const, questions: [
    { id: 'stage', question: '目前什么阶段？', selection: 'single' as const, options: [
      { id: 'working', label: '在职', recommended: true }, { id: 'student', label: '在读' }] },
    { id: 'goals', question: '希望解决什么？', selection: 'multiple' as const, options: [
      { id: 'change', label: '转行' }, { id: 'plan', label: '行动计划' }] },
  ] }
  const initial = { ...detail([event(2, 'team.message_received', { text: '职业规划', recipient: 'store_manager', kind: 'message' }, 'human', 'human'),
    event(3, 'team.questions_requested', { role_id: 'store_manager', title: question.title })]), questionnaires: [question] }
  const { fetchMock } = await mount(initial)
  const card = await screen.findByRole('form', { name: `补充信息：${question.title}` })
  expect(card.closest('[data-actor="store_manager"]')).toBeTruthy()
  expect(card.closest('details.team-collaboration')).toBeNull()
  expect((within(card).getByRole('radio', { name: /在职/ }) as HTMLInputElement).checked).toBe(false)
  fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '普通聊天草稿' } })
  fireEvent.click(within(card).getByRole('radio', { name: /在职/ }))
  fireEvent.click(within(card).getByRole('checkbox', { name: /转行/ }))
  fireEvent.click(within(card).getByRole('checkbox', { name: /行动计划/ }))
  expect(fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(0)
  await act(async () => { fireEvent.submit(card) })
  const posts = fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toBe('/api/tasks/team-task/questionnaires')
  expect(JSON.parse(String(posts[0][1].body))).toEqual({ question_seq: 3, request_id: expect.any(String), answers: [
    { question_id: 'stage', option_ids: ['working'], text: '', skipped: false },
    { question_id: 'goals', option_ids: ['change', 'plan'], text: '', skipped: false },
  ] })
  await screen.findByText('我的补充：在职，转行。')
  expect(screen.queryByRole('form', { name: `补充信息：${question.title}` })).toBeNull()
  expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('普通聊天草稿')
})

test('团队用户消息保留一份，角色公开发言使用 speaker，内部报告不重复公开回复', async () => {
  await mount(detail([
    event(2, 'team.message_received', { text: '团队接力', recipient: 'store_manager', kind: 'message', request_id: 'initial' }, 'human', 'human'),
    event(3, 'team.message_received', { text: '实现登录入口并交接', recipient: 'dev', kind: 'assignment' }, 'store_manager', 'agent'),
    event(4, 'team.message_received', { text: '已补齐入口', recipient: 'store_manager', kind: 'report', reply_to: 3 }, 'dev', 'agent'),
    event(5, 'msg.said', { text: '已补齐入口', phase: 'team', speaker: 'dev' }),
    event(6, 'conversation.received', { instruction: '团队接力', request_id: 'initial' }, 'human', 'human'),
    event(7, 'team.message_received', { text: '团队接力', recipient: 'dev', kind: 'message', request_id: 'initial' }, 'human', 'human'),
  ]))
  expect(stream().getAllByText('团队接力')).toHaveLength(1)
  expect(stream().getAllByText('已补齐入口')).toHaveLength(1)
  expect(stream().getByText('已补齐入口').closest('[data-actor="dev"]')).toBeTruthy()
  const notice = stream().getByText('@店长 → @米寇 · 工作交接').closest('article')!
  expect(notice.classList.contains('collaboration-notice')).toBe(true)
  expect(within(notice).getByText('实现登录入口并交接').closest('details')?.open).toBe(false)
  expect(notice.querySelector('.message-bubble')).toBeNull()
  expect(screen.getByLabelText('聊天记录').querySelectorAll('article')).toHaveLength(3)
  fireEvent.click(screen.getByRole('button', { name: /本次伙伴/ }))
  expect(within(screen.getByRole('dialog', { name: '本次伙伴' })).getByRole('button', { name: '点名 米寇' })).toBeTruthy()
  expect(within(screen.getByRole('dialog', { name: '本次伙伴' })).queryByRole('button', { name: '点名 三角' })).toBeNull()
})

test('不同请求发送相同原话仍保留两条用户消息', async () => {
  await mount(detail([
    event(2, 'team.message_received', { text: '继续', kind: 'message', request_id: 'a' }, 'human', 'human'),
    event(3, 'team.message_received', { text: '继续', kind: 'message', request_id: 'b' }, 'human', 'human'),
  ]))
  expect(stream().getAllByText('继续')).toHaveLength(2)
})

test.each([true, false])('人类直接点名猫的回答保持可见，兼容旧来源推导（audience=%s）', async (explicit) => {
  await mount(detail([
    event(2, 'team.message_received', { text: '@米寇 登录态怎么保存？', recipient: 'dev', kind: 'message' }, 'human', 'human'),
    event(3, 'msg.said', { text: '登录态保存在服务端。', phase: 'team', reply_to: 2,
      ...(explicit ? { audience: 'customer', recipient: 'human' } : {}) }, 'dev', 'agent'),
  ]))
  const answer = stream().getByText('登录态保存在服务端。')
  expect(answer.closest('details.team-collaboration')).toBeNull()
  expect(answer.closest('[data-actor="dev"]')).toBeTruthy()
  expect(stream().queryByText(/团队协作 ·/)).toBeNull()
  expect(stream().queryByText('店长最终交付')).toBeNull()
})

test('连续内部消息折叠为团队协作，展开保留完整发言、收件人、交接和工具过程，失败与对客回复打断分组', async () => {
  const fullMessage = '内部实现说明'.repeat(150)
  const fullReceipt = '真实读取回执'.repeat(160)
  await mount(detail([
    event(2, 'team.message_received', { text: '补齐登录入口并交回店长', recipient: 'dev', kind: 'assignment' }, 'store_manager', 'agent'),
    event(3, 'team.run_started', { message_seq: 2, role: 'dev', run_id: 'implementation' }),
    event(4, 'team.tool_started', { role: 'dev', name: 'workspace_read', run_id: 'implementation', arguments: { path: 'web/src/App.tsx' } }),
    event(5, 'team.tool_finished', { role: 'dev', name: 'workspace_read', run_id: 'implementation', started_seq: 4, result: { status: 'ok', content: fullReceipt } }),
    event(6, 'msg.said', { text: fullMessage, phase: 'team', reply_to: 2, run_id: 'implementation', audience: 'team', recipient: 'store_manager' }, 'dev', 'agent'),
    event(7, 'team.message_received', { text: '请核对入口', recipient: 'test', kind: 'report', visible: true }, 'dev', 'agent'),
    event(8, 'team.run_finished', { role: 'test', status: 'failed', reason: '检查调用失败' }),
    event(9, 'msg.said', { text: '伙伴检查暂未完成，我会继续跟进。', phase: 'team', audience: 'customer', recipient: 'human' }, 'store_manager', 'agent'),
    event(10, 'msg.said', { text: '已补充检查入口', phase: 'team', audience: 'team', recipient: 'dev' }, 'test', 'agent'),
  ]))
  const summary = stream().getByText('团队协作 · 3 条')
  const group = summary.closest('details')!
  expect(group.open).toBe(false)
  expect(stream().getByText('检查调用失败').closest('details.team-collaboration')).toBeNull()
  expect(stream().getByText('伙伴检查暂未完成，我会继续跟进。').closest('details.team-collaboration')).toBeNull()
  expect(stream().getByText('团队协作 · 1 条').closest('details')?.open).toBe(false)
  fireEvent.click(summary)
  expect(group.open).toBe(true)
  expect(within(group).getByText(fullMessage)).toBeTruthy()
  const reply = within(group).getByText(fullMessage).closest('article')!
  expect(within(reply).getByText('收件人：')).toBeTruthy()
  expect(within(reply).getByText('@店长')).toBeTruthy()
  expect(within(group).getByText('@店长 → @米寇 · 工作交接')).toBeTruthy()
  fireEvent.click(within(group).getByText('这次接力的工作'))
  expect(within(group).getByText('补齐登录入口并交回店长')).toBeTruthy()
  const tools = await within(group).findByText('工具过程 · 1 项')
  fireEvent.click(tools)
  expect(within(group).getByText(fullReceipt)).toBeTruthy()
  expect(within(group).getByText('web/src/App.tsx')).toBeTruthy()
  fireEvent.click(summary)
  expect(group.open).toBe(false)
})

test.each([{ phase: 'team', source: 'team' }, { phase: 'team' }, { source: 'team' }])
('native 发布文件归入连续团队过程，成果板与成果筛选保留入口：%j', async (origin) => {
  const payload = { title: 'test_summary.py', filename: 'test_summary.py', summary: '补齐摘要测试',
    content: 'def test_summary():\n    assert True\n', format: 'code', kind: 'file', ...origin }
  const initial = detail([
    event(2, 'msg.said', { phase: 'team', audience: 'team', recipient: 'store_manager', text: '开始补齐测试' }, 'dev', 'agent'),
    event(3, 'deliverable.published', payload, 'dev', 'agent'),
    event(4, 'msg.said', { phase: 'team', audience: 'team', recipient: 'store_manager', text: '测试文件已发布，交回店长核对' }, 'dev', 'agent'),
    event(5, 'team.objective_completed', { summary: '测试和说明已交付', run_id: 'delivery-run' }),
  ])
  initial.task.deliverables = [{ ...payload, seq: 3, actor: 'dev' }]
  await mount(initial)
  const group = stream().getByText('团队协作 · 3 条').closest('details')!
  expect(group.open).toBe(false)
  expect(within(group).getByText('· 阶段成果')).toBeTruthy()
  expect(stream().queryByText('· 发布成果')).toBeNull()
  expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1)
  expect(stream().getByText('测试和说明已交付').closest('details.team-collaboration')).toBeNull()
  expect(await board().findByRole('button', { name: '下载 test_summary.py' })).toBeTruthy()
  fireEvent.click(board().getByRole('button', { name: '查看正文' }))
  expect(within(await screen.findByRole('dialog')).getByText('def test_summary(): assert True')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '关闭成果正文' }))
  fireEvent.click(within(screen.getByRole('group', { name: '对话筛选' })).getByRole('button', { name: '成果', exact: true }))
  const filteredGroup = stream().getByText('团队协作 · 1 条').closest('details')!
  fireEvent.click(within(filteredGroup).getByText('团队协作 · 1 条'))
  expect(filteredGroup.open).toBe(true)
  expect(within(filteredGroup).getByText('· 阶段成果')).toBeTruthy()
  expect(within(filteredGroup).getByRole('button', { name: '查看正文' })).toBeTruthy()
})

test.each([{}, { phase: 'legacy', source: 'external' }, { phase: 'teamish', source: 'unknown' }])
('来源不明的历史发布成果保持可见：%j', async (origin) => {
  await mount(detail([event(2, 'deliverable.published', { title: '旧成果', summary: '旧文件摘要', ...origin }, 'dev', 'agent')]))
  const publication = stream().getByText('· 发布成果')
  expect(publication.closest('details.team-collaboration')).toBeNull()
  expect(stream().queryByText('· 阶段成果')).toBeNull()
  expect(stream().queryByText('· 店长最终交付')).toBeNull()
})

test('可信完成先以店长身份呈现，后续引用替换成一份最终交付，验证留在 details，普通 done 问答不合并', async () => {
  const completion = event(4, 'team.objective_completed', { summary: '工具登记的完成摘要', verification: '实际运行了两项入口检查。', run_id: 'delivery-run' })
  const app = await mount(detail([completion], teamState({ status: 'done' })))
  const original = stream().getByText('工具登记的完成摘要')
  expect(original.closest('[data-actor="store_manager"]')).toBeTruthy()
  expect(original.closest('article')?.querySelector('[role="img"][aria-label="店长"]')).toBeTruthy()
  expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1)
  const preparing = event(5, 'msg.said', { text: '我再整理说明', phase: 'team', audience: 'customer', recipient: 'human', delivery_seq: 4, run_id: 'delivery-run' }, 'store_manager', 'agent')
  await app.update(detail([completion, preparing], teamState({ status: 'done' })))
  await waitFor(() => expect(stream().getByText('我再整理说明')).toBeTruthy())
  expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1)
  await app.update(detail([
    completion, preparing,
    event(6, 'team.tool_started', { name: 'workspace_read', role: 'store_manager', run_id: 'delivery-run', arguments: { path: 'README.md' } }),
    event(7, 'team.tool_finished', { name: 'workspace_read', role: 'store_manager', run_id: 'delivery-run', started_seq: 6, result: { status: 'ok', content: '操作说明依据' } }),
    event(8, 'msg.said', { text: '入口和说明均已交付，可从登录页开始。', phase: 'team', audience: 'customer', recipient: 'human', delivery_seq: 4, run_id: 'delivery-run' }, 'store_manager', 'agent'),
    event(9, 'msg.said', { text: '可以，下一步我会解释登录流程。', phase: 'team', audience: 'customer', recipient: 'human', run_id: 'question-run' }, 'store_manager', 'agent'),
  ], teamState({ status: 'done' })))
  await waitFor(() => expect(stream().queryByText('工具登记的完成摘要')).toBeNull())
  await waitFor(() => expect(stream().queryByText('我再整理说明')).toBeNull())
  const final = stream().getByText('入口和说明均已交付，可从登录页开始。')
  expect(final.closest('details.team-collaboration')).toBeNull()
  expect(final.closest('[data-actor="store_manager"]')).toBeTruthy()
  expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1)
  expect(stream().getAllByText('入口和说明均已交付，可从登录页开始。')).toHaveLength(1)
  const verification = stream().getByText('实际运行了两项入口检查。').closest('details')!
  expect(verification.open).toBe(false)
  fireEvent.click(within(verification).getByText('查看判断与依据'))
  expect(verification.open).toBe(true)
  expect(stream().getByText('可以，下一步我会解释登录流程。').closest('article')?.textContent).not.toContain('店长最终交付')
})

test('多次交付仅最新可信目标完成标最终，重新打开后历史全为记录，再次完成才恢复最终标记', async () => {
  const first = event(2, 'team.objective_completed', { summary: '初次交付', run_id: 'first-run' })
  const firstMessage = event(3, 'msg.said', { phase: 'team', audience: 'customer', text: '初次操作说明', delivery_seq: 2, run_id: 'first-run' }, 'store_manager', 'agent')
  const second = event(4, 'team.objective_completed', { summary: '补齐测试后的交付', run_id: 'second-run' })
  const secondMessage = event(5, 'msg.said', { phase: 'team', audience: 'customer', text: '补齐后的操作说明', delivery_seq: 4, run_id: 'second-run' }, 'store_manager', 'agent')
  const history = [second, secondMessage, first, firstMessage]
  const forgedUpdate = event(100, 'team.objective_updated', { goal: '伪造重开' }, 'dev', 'agent')
  const app = await mount(detail([...history, forgedUpdate], teamState({ status: 'done' })))
  const firstRow = stream().getByText('初次操作说明').closest('article')!
  const secondRow = stream().getByText('补齐后的操作说明').closest('article')!
  expect(within(firstRow).getByText('· 店长交付记录')).toBeTruthy()
  expect(within(secondRow).getByText('· 店长最终交付')).toBeTruthy()
  expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1)
  expect(firstRow.querySelector('[data-actor="store_manager"]')).toBeTruthy()
  const reopened = event(6, 'team.objective_updated', { goal: '补充移动端验证' })
  const forgedCompletion = event(101, 'team.objective_completed', { summary: '伪造最终交付' }, 'dev', 'agent')
  await app.update(detail([reopened, ...history, forgedCompletion], teamState({ status: 'done' })))
  await waitFor(() => expect(stream().queryByText('· 店长最终交付')).toBeNull())
  expect(stream().getAllByText('· 店长交付记录')).toHaveLength(2)
  expect(stream().getByText('初次操作说明')).toBeTruthy()
  expect(stream().getByText('补齐后的操作说明')).toBeTruthy()
  await app.update(detail([reopened, ...history, forgedCompletion,
    event(102, 'team.objective_completed', { summary: '移动端验证后交付', run_id: 'third-run' }),
  ]))
  await waitFor(() => expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1))
  expect(within(stream().getByText('移动端验证后交付').closest('article')!).getByText('· 店长最终交付')).toBeTruthy()
  expect(stream().getAllByText('· 店长交付记录')).toHaveLength(2)
})

test('历史交付各自默认折叠为店长交付记录，完整正文与验证可展开，当前最终交付保持可见', async () => {
  const oldBody = '历史完整操作说明。'.repeat(120)
  await mount(detail([
    event(2, 'team.objective_completed', { summary: oldBody, verification: '初次交付的验证范围。' }),
    event(3, 'team.objective_completed', { summary: '补齐测试后的历史说明', verification: '补齐测试的验证范围。' }),
    event(4, 'team.objective_completed', { summary: '当前最终操作说明', verification: '当前验证范围。' }),
  ], teamState({ status: 'done' })))
  const summaries = stream().getAllByText('店长交付记录', { selector: 'summary' })
  expect(summaries).toHaveLength(2)
  const first = summaries[0].closest('details')!
  const second = summaries[1].closest('details')!
  expect(first.open).toBe(false)
  expect(second.open).toBe(false)
  expect(within(first).getByText(oldBody)).toBeTruthy()
  expect(stream().queryByText(/团队协作 ·/)).toBeNull()
  const final = stream().getByText('当前最终操作说明')
  expect(final.closest('details')).toBeNull()
  expect(within(final.closest('article')!).getByText('· 店长最终交付')).toBeTruthy()
  fireEvent.click(summaries[0])
  expect(first.open).toBe(true)
  expect(second.open).toBe(false)
  expect(within(first).getByText(oldBody).closest('[data-actor="store_manager"]')).toBeTruthy()
  const verification = within(first).getByText('初次交付的验证范围。').closest('details')!
  expect(verification.open).toBe(false)
  fireEvent.click(within(verification).getByText('查看判断与依据'))
  expect(verification.open).toBe(true)
  expect(within(first).getByText('初次交付的验证范围。')).toBeTruthy()
  fireEvent.click(summaries[0])
  expect(first.open).toBe(false)
  expect(final.closest('details')).toBeNull()
})

test.each(['nonleader', 'spoofed speaker', 'untrusted completion', 'missing completion', 'future completion', 'another run', 'new objective',
  'other run reference', 'missing completion run', 'missing message run', 'missing both runs', 'empty run', 'numeric run'])
('正式交付拒绝不可信引用：%s', (invalid) => {
  const completion = event(4, 'team.objective_completed', { summary: '可信摘要', run_id: 'leader-run' })
  const said = event(6, 'msg.said', { phase: 'team', text: '自称最终交付', delivery_seq: 4, run_id: 'leader-run' }, 'store_manager', 'agent')
  const events = [completion, said]
  if (invalid === 'nonleader') said.actor.id = 'dev'
  if (invalid === 'spoofed speaker') { said.actor.id = 'dev'; said.payload.speaker = 'store_manager' }
  if (invalid === 'untrusted completion') completion.actor.kind = 'agent'
  if (invalid === 'missing completion') said.payload.delivery_seq = 99
  if (invalid === 'future completion') completion.seq = 7
  if (invalid === 'another run') events.push(event(5, 'team.run_started', { role: 'store_manager', run_id: 'leader-run' }))
  if (invalid === 'new objective') events.push(event(5, 'team.objective_updated', { goal: '继续补充' }))
  if (invalid === 'other run reference') {
    said.payload.run_id = 'another-leader-run'
    events.push(event(3, 'team.run_started', { role: 'store_manager', run_id: 'another-leader-run' }))
  }
  if (invalid === 'missing completion run' || invalid === 'missing both runs') delete completion.payload.run_id
  if (invalid === 'missing message run' || invalid === 'missing both runs') delete said.payload.run_id
  if (invalid === 'empty run') { completion.payload.run_id = ' '; said.payload.run_id = ' ' }
  if (invalid === 'numeric run') { completion.payload.run_id = 123; said.payload.run_id = 123 }
  expect(teamDeliveries(events, 'store_manager')).toEqual({ messages: new Map(), consumed: new Set() })
})

test('迁移前真实形状 completion 无 run_id 时，以可信店长 run 关联并只显示最后一份交付正文', async () => {
  const completion = event(6, 'team.objective_completed', { summary: '登记的交付摘要', verification: '已执行真实测试。' })
  const first = event(7, 'msg.said', { phase: 'team', text: '我再整理操作说明', audience: 'customer', recipient: 'human', reply_to: 2, delivery_seq: 6, run_id: 'legacy-run' }, 'store_manager', 'agent')
  const final = event(10, 'msg.said', { ...first.payload, text: '最终操作说明：运行测试后打开入口。' }, 'store_manager', 'agent')
  const events = [
    event(2, 'team.message_received', { kind: 'message', recipient: 'store_manager', text: '补齐测试与说明' }, 'human', 'human'),
    event(3, 'team.run_started', { message_seq: 2, role: 'store_manager', run_id: 'legacy-run' }),
    event(4, 'team.run_started', { message_seq: 1, role: 'dev', run_id: 'worker-run' }),
    event(5, 'team.run_finished', { message_seq: 1, role: 'dev', run_id: 'worker-run', status: 'completed' }),
    completion, first,
    event(8, 'team.tool_started', { name: 'workspace_read', role: 'store_manager', run_id: 'legacy-run', arguments: { path: 'README.md' } }),
    event(9, 'team.tool_finished', { started_seq: 8, name: 'workspace_read', role: 'store_manager', run_id: 'legacy-run', result: { status: 'ok', content: '真实操作说明' } }),
    final,
    event(11, 'team.run_finished', { message_seq: 2, role: 'store_manager', run_id: 'legacy-run', status: 'completed' }),
  ]
  expect(teamDeliveries([...events].reverse(), 'store_manager')).toEqual({ messages: new Map([[6, final]]), consumed: new Set([7, 10]) })
  await mount(detail(events, teamState({ status: 'done' })))
  expect(stream().queryByText('登记的交付摘要')).toBeNull()
  expect(stream().queryByText('我再整理操作说明')).toBeNull()
  expect(stream().getAllByText(final.payload.text as string)).toHaveLength(1)
  expect(stream().getAllByText('· 店长最终交付')).toHaveLength(1)
  expect(stream().getByText(final.payload.text as string).closest('[data-actor="store_manager"]')).toBeTruthy()
  expect(Object.hasOwn(completion.payload, 'run_id')).toBe(false)
})

test.each(['missing start', 'unknown run', 'untrusted start', 'wrong start role', 'late start', 'finished before completion',
  'another leader started', 'another leader finished', 'explicit different completion run', 'invalid completion run'])
('旧 completion 的 run 关联拒绝证据不完整或冲突：%s', (invalid) => {
  const start = event(3, 'team.run_started', { role: 'store_manager', run_id: 'legacy-run' })
  const completion = event(6, 'team.objective_completed', { summary: '旧交付摘要' })
  const final = event(8, 'msg.said', { phase: 'team', text: '声称旧交付正文', delivery_seq: 6, run_id: 'legacy-run' }, 'store_manager', 'agent')
  const events = [start, completion, final]
  if (invalid === 'missing start') events.splice(0, 1)
  if (invalid === 'unknown run') final.payload.run_id = 'unknown-run'
  if (invalid === 'untrusted start') start.actor.kind = 'agent'
  if (invalid === 'wrong start role') start.payload.role = 'dev'
  if (invalid === 'late start') start.seq = 7
  if (invalid === 'finished before completion') events.push(event(5, 'team.run_finished', { role: 'store_manager', run_id: 'legacy-run', status: 'completed' }))
  if (invalid === 'another leader started' || invalid === 'another leader finished') events.push(event(4, 'team.run_started', { role: 'store_manager', run_id: 'another-run' }))
  if (invalid === 'another leader finished') events.push(event(5, 'team.run_finished', { role: 'store_manager', run_id: 'another-run', status: 'completed' }))
  if (invalid === 'explicit different completion run') completion.payload.run_id = 'another-run'
  if (invalid === 'invalid completion run') completion.payload.run_id = null
  expect(teamDeliveries(events, 'store_manager')).toEqual({ messages: new Map(), consumed: new Set() })
})

test('同 run 的配置店长交付按 seq 保留最后一条有效正文，重复引用不增加正式结论', () => {
  const start = event(3, 'team.run_started', { role: 'custom_manager', run_id: 'delivery-run' })
  const completion = event(4, 'team.objective_completed', { summary: '登记摘要', run_id: 'delivery-run' })
  const final = event(5, 'msg.said', { phase: 'team', text: '完整交付正文', delivery_seq: 4, run_id: 'delivery-run' }, 'custom_manager', 'agent')
  const repeated = event(6, 'msg.said', { ...final.payload, text: '补齐后的完整操作说明' }, 'custom_manager', 'agent')
  const ordinary = event(7, 'msg.said', { phase: 'team', text: '完成后解释', run_id: 'delivery-run' }, 'custom_manager', 'agent')
  const foreign = event(8, 'msg.said', { ...final.payload, run_id: 'other-run', text: '另一轮伪引用' }, 'custom_manager', 'agent')
  const blank = event(9, 'msg.said', { ...final.payload, text: ' ' }, 'custom_manager', 'agent')
  const projected = teamDeliveries([blank, ordinary, foreign, repeated, final, completion, start], 'custom_manager')
  expect(projected.messages).toEqual(new Map([[4, repeated]]))
  expect(projected.consumed).toEqual(new Set([5, 6]))
  expect(teamDeliveries([start, completion, final], 'store_manager').consumed.size).toBe(0)
})

test('非店长即便伪造店长 speaker 也不能替换旧正式交付，旧内部回复只有来源核对成功才折叠', async () => {
  await mount(detail([
    event(2, 'team.message_received', { text: '交接实现', recipient: 'dev', kind: 'assignment' }, 'store_manager', 'agent'),
    event(3, 'msg.said', { text: '旧内部回复', phase: 'team', reply_to: 2 }, 'dev', 'agent'),
    event(4, 'msg.said', { text: '缺少来源的旧发言', phase: 'team' }, 'dev', 'agent'),
    event(5, 'msg.said', { text: '收件人不匹配的旧发言', phase: 'team', reply_to: 2 }, 'test', 'agent'),
    event(6, 'team.objective_completed', { summary: '旧记录的可信交付' }),
    event(7, 'msg.said', { text: '伪造的交付正文', phase: 'team', speaker: 'store_manager', delivery_seq: 6 }, 'dev', 'agent'),
    event(8, 'msg.said', { text: '缺少可信 run 关联的店长回复', phase: 'team', delivery_seq: 6, run_id: 'unknown-run' }, 'store_manager', 'agent'),
  ]))
  expect(stream().getByText('旧内部回复').closest('details.team-collaboration')).toBeTruthy()
  for (const text of ['缺少来源的旧发言', '收件人不匹配的旧发言']) expect(stream().getByText(text).closest('details.team-collaboration')).toBeNull()
  const official = stream().getByText('旧记录的可信交付').closest('article')!
  expect(within(official).getByText('· 店长最终交付')).toBeTruthy()
  expect(stream().getByText('伪造的交付正文').closest('article')?.textContent).not.toContain('店长最终交付')
  expect(stream().getByText('缺少可信 run 关联的店长回复').closest('article')?.textContent).not.toContain('店长最终交付')
})

test.each(['missing', 'wrong type', 'wrong recipient', 'system source', 'future source'])('旧消息无有效来源时不推断内部：%s', (invalid) => {
  const source = event(2, 'team.message_received', { recipient: 'dev' }, 'store_manager', 'agent')
  const said = event(3, 'msg.said', { phase: 'team', reply_to: 2 }, 'dev', 'agent')
  if (invalid === 'missing') said.payload.reply_to = 1
  if (invalid === 'wrong type') source.type = 'msg.said'
  if (invalid === 'wrong recipient') source.payload.recipient = 'test'
  if (invalid === 'system source') source.actor.kind = 'system'
  if (invalid === 'future source') source.seq = 4
  expect(teamMessageAudience(said, new Map([[source.seq, source]]))).toEqual({})
})

test('模型调用失败在聊天中明确提示，不只留在折叠审计里', async () => {
  await mount(detail([
    event(2, 'team.message_received', { text: '请继续修改', recipient: 'store_manager', kind: 'message' }, 'human', 'human'),
    event(3, 'team.run_started', { message_seq: 2, role: 'store_manager' }),
    event(4, 'team.run_finished', { message_seq: 2, role: 'store_manager', status: 'failed', reason: '本轮调用未完成，已有文件保留。' }),
  ]))
  expect(stream().getByText(/这次回复未完成/)).toBeTruthy()
  expect(stream().getByText('本轮调用未完成，已有文件保留。')).toBeTruthy()
})

test.each(['message', 'report'])('send_message 的显式 %s 即使没有 turn.text 也保留正文、接收者和回复关系', async (kind) => {
  await mount(detail([
    event(2, 'team.message_received', { text: '登录态由哪一层保存？', recipient: 'store_manager', kind, visible: true, reply_to: 1 }, 'dev', 'agent'),
    event(3, 'team.message_received', { text: '自动报告无需再次展示', recipient: 'store_manager', kind: 'report' }, 'dev', 'agent'),
    event(4, 'team.message_received', { text: '自动进度无需再次展示', recipient: 'store_manager', kind: 'progress' }, 'dev', 'agent'),
  ]))
  const row = stream().getByText('登录态由哪一层保存？').closest('article')!
  expect(row.querySelector('[data-actor="dev"].is-agent')).toBeTruthy()
  expect(within(row).getByText('@店长')).toBeTruthy()
  expect(within(row).getByText('回复 #1')).toBeTruthy()
  expect(stream().queryByText('自动报告无需再次展示')).toBeNull()
  expect(stream().queryByText('自动进度无需再次展示')).toBeNull()
})

test.each(['message', 'report'])('只有同轮工具明确关联的显式 %s 全文回声去重，相似文本、下一轮与其他角色原话均保留', async (kind) => {
  await mount(detail([
    event(2, 'msg.said', { text: '需要确认入口范围', phase: 'team', run_id: 'run-a' }, 'dev', 'agent'),
    event(3, 'team.tool_started', { name: 'send_message', role: 'dev', run_id: 'run-a', call_id: 'send' }),
    event(4, 'team.message_received', { text: '需要确认入口范围', recipient: 'store_manager', kind, visible: true }, 'dev', 'agent'),
    event(5, 'team.tool_finished', { name: 'send_message', role: 'dev', started_seq: 3, result: { status: 'ok', message_seq: 4 } }),
    event(6, 'msg.said', { text: '需要确认入口范围。', phase: 'team', run_id: 'run-a' }, 'dev', 'agent'),
    event(7, 'msg.said', { text: '需要确认入口范围', phase: 'team', run_id: 'run-b' }, 'dev', 'agent'),
    event(8, 'msg.said', { text: '需要确认入口范围', phase: 'team', run_id: 'run-a' }, 'test', 'agent'),
  ]))
  expect(stream().getAllByText('需要确认入口范围')).toHaveLength(3)
  expect(stream().getByText('需要确认入口范围。')).toBeTruthy()
  expect(screen.getByLabelText('聊天记录').querySelectorAll('article')).toHaveLength(5)
})

test.each([
  ['message', true], ['message', false], ['notification', true], ['notification', false],
] as const)('同轮内部 %s 不吞掉同文对客答复（显式受众=%s）', async (kind, explicit) => {
  const text = '双击 index.html 就能打开。'
  const answer = event(7, 'msg.said', { text, phase: 'team', run_id: 'direct-reply', reply_to: 2,
    response_kind: 'reply', ...(explicit ? { audience: 'customer', recipient: 'human' } : {}) }, 'dev', 'agent')
  const events = [event(2, 'team.message_received', { text: '@米寇 怎么打开？', recipient: 'dev', kind: 'message' }, 'human', 'human'),
    event(3, 'team.run_started', { role: 'dev', message_seq: 2, run_id: 'direct-reply', revision: 1 }),
    event(4, 'team.tool_started', { name: 'send_message', role: 'dev', run_id: 'direct-reply', message_seq: 2,
      arguments: { recipient: 'store_manager', text, ...(kind === 'notification' ? { notify_only: true } : {}) } }),
    event(5, 'team.message_received', { text, recipient: 'store_manager', kind, visible: true, revision: 1 }, 'dev', 'agent'),
    event(6, 'team.tool_finished', { role: 'dev', name: 'send_message', started_seq: 4, result: { status: 'ok', message_seq: 5 } }),
    answer, event(8, 'team.run_finished', { role: 'dev', message_seq: 2, run_id: 'direct-reply', status: 'completed' })]
  expect(teamMessageEchoes(events).has(answer.seq)).toBe(false)
  await mount(detail(events))
  const copies = stream().getAllByText(text)
  expect(copies).toHaveLength(2)
  expect(copies.filter((copy) => !copy.closest('details.team-collaboration'))).toHaveLength(1)
  expect(copies.find((copy) => !copy.closest('details.team-collaboration'))?.closest('[data-actor="dev"]')).toBeTruthy()
})

test.each(['message', 'report', 'notification'])('内部 %s 回声只合并同收件人，同文发给另一伙伴仍保留', (kind) => {
  const start = event(2, 'team.tool_started', { name: 'send_message', role: 'dev', run_id: 'echo' })
  const sent = event(3, 'team.message_received', { text: '入口已核对', recipient: 'store_manager', kind, visible: true }, 'dev', 'agent')
  const receipt = event(4, 'team.tool_finished', { role: 'dev', started_seq: 2, result: { status: 'ok', message_seq: 3 } })
  const same = event(5, 'msg.said', { text: '入口已核对', phase: 'team', run_id: 'echo', audience: 'team', recipient: 'store_manager' }, 'dev', 'agent')
  const other = event(6, 'msg.said', { ...same.payload, recipient: 'test' }, 'dev', 'agent')
  expect(teamMessageEchoes([start, sent, receipt, same, other])).toEqual(new Set([5]))
})

test('report 仅在 visible 严格为 true 时公开，隐藏的自动回报不会去掉公开发言', async () => {
  const hidden = [undefined, false, 'true', 1].map((visible, index) => event(index + 2, 'team.message_received', {
    text: `隐藏回报 ${index}`, kind: 'report', visible, recipient: 'store_manager',
  }, 'dev', 'agent'))
  await mount(detail([
    ...hidden,
    event(6, 'team.message_received', { text: '带可见标记的进度仍隐藏', kind: 'progress', visible: true }, 'dev', 'agent'),
    event(7, 'msg.said', { text: '自动回报对应的公开发言', phase: 'team', run_id: 'run-auto' }, 'dev', 'agent'),
    event(8, 'team.tool_started', { name: 'send_message', role: 'dev', run_id: 'run-auto' }),
    event(9, 'team.message_received', { text: '自动回报对应的公开发言', kind: 'report' }, 'dev', 'agent'),
    event(10, 'team.tool_finished', { name: 'send_message', role: 'dev', started_seq: 8, result: { status: 'ok', message_seq: 9 } }),
  ]))
  for (let index = 0; index < hidden.length; index++) expect(stream().queryByText(`隐藏回报 ${index}`)).toBeNull()
  expect(stream().queryByText('带可见标记的进度仍隐藏')).toBeNull()
  expect(stream().getAllByText('自动回报对应的公开发言')).toHaveLength(1)
  expect(screen.getByLabelText('聊天记录').querySelectorAll('article')).toHaveLength(2)
})

test.each(['missing receipt', 'untrusted receipt', 'untrusted start', 'wrong start role', 'wrong receipt role', 'failed receipt', 'wrong tool'])
('显式 report 缺少可信 send_message 关联时不按同文去重：%s', (invalid) => {
  const said = event(2, 'msg.said', { text: '显式回复', phase: 'team', run_id: 'run-report' }, 'dev', 'agent')
  const start = event(3, 'team.tool_started', { name: 'send_message', role: 'dev', run_id: 'run-report' })
  const report = event(4, 'team.message_received', { text: '显式回复', kind: 'report', visible: true }, 'dev', 'agent')
  const receipt = event(5, 'team.tool_finished', { name: 'send_message', role: 'dev', started_seq: 3, result: { status: 'ok', message_seq: 4 } })
  if (invalid === 'untrusted receipt') receipt.actor.kind = 'agent'
  if (invalid === 'untrusted start') start.actor.kind = 'agent'
  if (invalid === 'wrong start role') start.payload.role = 'test'
  if (invalid === 'wrong receipt role') receipt.payload.role = 'test'
  if (invalid === 'failed receipt') receipt.payload.result = { status: 'error', message_seq: 4 }
  if (invalid === 'wrong tool') start.payload.name = 'delegate'
  expect(teamMessageEchoes([said, start, report, ...(invalid === 'missing receipt' ? [] : [receipt])])).toEqual(new Set())
})

test('waiting 安静等待不算任务完成，讲解和追问继续从原聊天框发送 command', async () => {
  const app = await mount(detail([
    event(2, 'team.run_finished', { message_seq: 1, role: 'store_manager', status: 'waiting' }),
    event(3, 'team.tool_finished', { role: 'store_manager', name: 'wait_for_reply', result: { status: 'ok', waiting: true } }),
  ], teamState({ checkpoint: '等待伙伴回复，收到消息后继续。' })))
  expect(board().getByText('本轮结束，等待消息')).toBeTruthy()
  expect(board().getByText('等待伙伴回复，收到消息后继续。')).toBeTruthy()
  expect(stream().queryByText('等待伙伴回复')).toBeNull()
  expect(screen.queryByRole('button', { name: '提问', exact: true })).toBeNull()
  expect(screen.queryByRole('button', { name: '发送提问' })).toBeNull()
  fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '解释一下登录流程' } })
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(stream().getByText('解释一下登录流程')).toBeTruthy())
  const posts = app.fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toBe('/api/tasks/team-task/command')
  expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({ instruction: '解释一下登录流程', collaboration: 'team' })
  expect(app.fetchMock.mock.calls.some(([url]) => url.endsWith('/questions'))).toBe(false)
})

test.each(['waiting', 'failed', 'running'])('旧版本 %s 不覆盖当前已恢复状态，完整历史仍可展开', async (status) => {
  const team = teamState({ revision: 9, checkpoint: '最近消息已处理，等待新的要求。', runs: [
    { message_seq: 2, role: 'dev', revision: 1, status },
    { message_seq: 4, role: 'store_manager', revision: 9, status: 'completed' },
  ] })
  expect(teamStatus(team)).toBe('消息处理完成，任务待交付')
  await mount(detail([], team))
  expect(board().getByText('消息处理 · 2 项')).toBeTruthy()
  expect(board().getByText('消息处理完成，任务待交付')).toBeTruthy()
  expect(board().getByText('最近消息已处理，等待新的要求。')).toBeTruthy()
  expect(teamStatus(teamState({ runs: [{ role: 'dev', status: 'failed', revision: 1 }, { role: 'dev', status: 'completed', revision: 1 }] }))).toBe('消息处理完成，任务待交付')
})

test('消息处理完成不等于任务交付，只有目标完成事件改变已完成筛选，更新目标后重新进行', async () => {
  const runs = [event(2, 'team.message_received', { text: '请继续', kind: 'message' }, 'human', 'human'),
    event(3, 'team.run_started', { message_seq: 2, role: 'dev', status: 'running' }),
    event(4, 'team.run_finished', { message_seq: 2, role: 'dev', status: 'done' })]
  const app = await mount(detail(runs))
  expect(board().getByText('消息处理完成，任务待交付')).toBeTruthy()
  const filters = within(screen.getByRole('group', { name: '任务状态筛选' }))
  fireEvent.click(filters.getByRole('button', { name: '未完成' }))
  await waitFor(() => expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /团队接力.*任务待交付/ })).toBeTruthy())
  await app.update(detail([...runs, event(5, 'team.objective_completed', { summary: '入口与说明已交付' })]))
  await screen.findByText('入口与说明已交付')
  fireEvent.click(filters.getByRole('button', { name: '已完成' }))
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /团队接力.*店长确认可交付/ })).toBeTruthy()
  await app.update(detail([...runs, event(5, 'team.objective_completed', { summary: '入口与说明已交付' }),
    event(6, 'team.objective_updated', { goal: '补齐移动端入口' })], teamState({ status: 'done', revision: 2 })))
  await screen.findByRole('heading', { name: '补齐移动端入口', level: 1 })
  fireEvent.click(filters.getByRole('button', { name: '未完成' }))
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /补齐移动端入口.*任务待交付/ })).toBeTruthy()
})

test('team.enabled 隐藏旧工作检查门闩、旧计划和 checkpoint，仅展示团队进度', async () => {
  await mount(detail([
    event(2, 'brief.generated', { reason: 'work_checkpoint', brief: { dispute: '旧进度卡住', checkpoint: { state: 'waiting_local_check' } } }),
    event(3, 'planning.started'), event(4, 'planning.finished', { status: 'installed', reason: '旧计划已经下发' }),
    event(5, 'work.claimed', { work_item_id: 'old' }),
    event(6, 'team.run_finished', { message_seq: 2, role: 'dev', status: 'failed', reason: '模型调用失败' }),
  ]))
  expect(board().getByText('继续完善入口与交付说明')).toBeTruthy()
  expect(board().getByText(/消息处理失败.*消息 #2/)).toBeTruthy()
  expect(screen.queryByRole('region', { name: '工作项与验收' })).toBeNull()
  expect(screen.getByRole('button', { name: '任务脉络' })).toBeTruthy()
  for (const text of ['旧进度卡住', '旧计划已经下发', '旧检查门闩', '旧命令行进度', '等待实际检查', '此任务尚无工作项与验收记录']) expect(screen.queryByText(text)).toBeNull()
  expect(stream().queryByText('消息处理失败')).toBeNull()
})

test('工具调用在进度中配对展示简洁真实回执，模型用量和未知数据不挤进聊天', async () => {
  await mount(detail([
    event(2, 'team.tool_started', { role: 'dev', call_id: 'read', name: 'workspace_read', arguments: { path: 'web/src/App.tsx' } }),
    event(3, 'team.tool_started', { role: 'test', call_id: 'pending', name: 'workspace_run', arguments: { command: ['pnpm', 'test'] } }),
    event(4, 'team.tool_finished', { role: 'dev', started_seq: 2, call_id: 'read', name: 'workspace_read', result: { status: 'ok', content: '读取到真实文件摘要', private_blob: { raw: '不可直接展示的内部JSON' } } }),
    event(5, 'team.model_called', { role: 'dev', usage: { input_tokens: 120, output_tokens: 30, total_tokens: 150 } }),
    event(6, 'team.tool_finished', { role: 'design', name: 'workspace_write', result: { status: 'error', error: { message: '文件不可写' } } }),
  ]))
  const tools = board().getByText('工具过程 · 3 项').closest('details')!
  expect(tools.open).toBe(false)
  fireEvent.click(within(tools).getByText('工具过程 · 3 项'))
  const rows = within(board().getByRole('list', { name: '团队工具过程' })).getAllByRole('listitem')
  expect(rows).toHaveLength(3)
  expect(rows[0].textContent).toContain('工具已完成')
  expect(rows[0].textContent).toContain('web/src/App.tsx')
  expect(rows[0].textContent).toContain('读取到真实文件摘要')
  expect(rows[1].textContent).toContain('已发起，等待回执')
  expect(rows[1].textContent).toContain('pnpm test')
  expect(rows[2].textContent).toContain('工具未完成')
  expect(rows[2].textContent).toContain('文件不可写')
  expect(board().getByText('@米寇 · 150 token')).toBeTruthy()
  expect(screen.queryByText('不可直接展示的内部JSON')).toBeNull()
  expect(stream().queryByText(/读取到真实文件摘要|pnpm test|模型用量/)).toBeNull()
})

test('工具回执优先按 started_seq 配对，展示真实 preview、退出码和失败，未知用量不伪装成零', async () => {
  await mount(detail([
    event(2, 'team.tool_started', { role: 'dev', name: 'workspace_run', call_id: 'same', run_id: 'first' }),
    event(3, 'team.tool_started', { role: 'dev', name: 'workspace_run', call_id: 'same', run_id: 'second' }),
    event(4, 'team.tool_finished', { role: 'dev', name: 'workspace_run', started_seq: 3, call_id: 'same', run_id: 'second', result: {
      status: 'ok', outcome: 'failed', command: ['pnpm', 'test'], exit_code: 1, stdout: { preview: '已运行两项测试' }, stderr: { preview: '登录断言未通过' },
    } }),
    event(5, 'team.model_called', { role: 'dev', tokens: 0, token_usage: 'unavailable' }),
    event(6, 'team.objective_completed', { summary: '不能作为店长交付结论' }, 'dev', 'agent'),
  ]))
  const rows = within(board().getByRole('list', { name: '团队工具过程' })).getAllByRole('listitem')
  expect(rows).toHaveLength(2)
  expect(rows[0].textContent).toContain('已发起，等待回执')
  expect(rows[0].textContent).not.toContain('已运行两项测试')
  expect(rows[1].textContent).toContain('工具未完成')
  for (const text of ['pnpm test', '退出码：1', '已运行两项测试', '登录断言未通过']) expect(rows[1].textContent).toContain(text)
  expect(board().getByText('@米寇 · 用量未报告')).toBeTruthy()
  expect(screen.queryByText('店长确认可交付')).toBeNull()
  expect(stream().queryByText('不能作为店长交付结论')).toBeNull()
})

test('相同事件版本的权威摘要刷新保留真实团队处理状态和草稿，不按消息数量暗示完成', async () => {
  const initial = detail([
    event(2, 'team.message_received', { text: '继续完善入口', kind: 'message' }, 'human', 'human'),
    event(3, 'team.run_started', { message_seq: 2, role: 'dev' }),
    event(4, 'team.run_finished', { message_seq: 2, role: 'dev', status: 'completed' }),
  ])
  initial.summary = { id: 'team-task', goal: '团队接力', status: 'done', status_label: '已完成', version: 4, latest_seq: 4,
    conversation_count: 99, team: { enabled: true, status: 'running', revision: 1, pending_count: 0 } }
  const app = await mount(initial)
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '继续核对交付的草稿' } })
  const detailReads = app.fetchMock.mock.calls.filter(([url]) => url === '/api/tasks/team-task').length
  const summaryReads = app.fetchMock.mock.calls.filter(([url]) => url === '/api/task-summaries').length
  const refreshed = { ...initial.summary, conversation_count: 233 }
  const original = app.fetchMock.getMockImplementation()!
  app.fetchMock.mockImplementation((url, init) => url === '/api/task-summaries' ? json({ tasks: [refreshed] })
    : url === '/api/tasks/team-task' ? new Promise<Response>(() => {}) : original(url, init))
  await act(async () => fireEvent(document, new Event('visibilitychange')))
  const selected = within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /团队接力.*消息处理完成，任务待交付/ })
  expect(selected.getAttribute('aria-current')).toBe('page')
  expect(within(selected).getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  expect(selected.textContent).not.toMatch(/条对话|对话待同步|店长确认可交付/)
  expect(input.value).toBe('继续核对交付的草稿')
  expect(stream().getByText('继续完善入口')).toBeTruthy()
  expect(board().getByRole('region', { name: '团队进度' })).toBeTruthy()
  expect(app.fetchMock.mock.calls.filter(([url]) => url === '/api/task-summaries')).toHaveLength(summaryReads + 1)
  expect(app.fetchMock.mock.calls.filter(([url]) => url === '/api/tasks/team-task')).toHaveLength(detailReads + 1)
  expect(app.fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(0)
  expect(app.fetchMock.mock.calls.some(([url]) => url === '/api/tasks')).toBe(false)
})

test('详情根级 team 可接入，演示 fallback 未开启 team 时沿用原工作项显示', async () => {
  const initial = detail([event(2, 'team.message_received', { text: '根级团队消息', kind: 'message' }, 'human', 'human')], null)
  initial.team = teamState()
  const app = await mount(initial)
  expect(stream().getByText('根级团队消息')).toBeTruthy()
  expect(board().getByRole('region', { name: '团队进度' })).toBeTruthy()
  await app.update(detail([event(3, 'human.commanded', { instruction: '演示模式继续' }, 'human', 'human')], teamState({ enabled: false })))
  await screen.findByRole('region', { name: '工作项与验收' })
  expect(stream().getByText('演示模式继续')).toBeTruthy()
  expect(screen.queryByRole('region', { name: '团队进度' })).toBeNull()
})
