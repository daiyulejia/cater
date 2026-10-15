import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import type { TeamState } from '../src/team'
import { teamMessageEchoes, teamStatus } from '../src/team'

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

async function mount(initial: TaskDetail) {
  let current = initial
  const fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if (url === '/api/task-summaries') return json({ tasks: [current.summary ?? { id: 'team-task', goal: '团队接力', status: 'done', status_label: '已完成',
      version: 1, latest_seq: 1, conversation_count: 1 }] })
    if (url === '/api/tasks/team-task') return json(current)
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
  await screen.findByRole('region', { name: '团队进度' })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  return { fetchMock, update: async (next: TaskDetail) => {
    current = next
    await act(async () => fireEvent(document, new Event('visibilitychange')))
  } }
}

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
  expect(board().getByText('任务进行中')).toBeTruthy()
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
  expect(screen.queryByRole('button', { name: '任务脉络' })).toBeNull()
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

test('相同事件版本的轻量摘要刷新保留已核对消息计数和处理状态', async () => {
  const initial = detail([
    event(2, 'team.message_received', { text: '继续完善入口', kind: 'message' }, 'human', 'human'),
    event(3, 'team.run_started', { message_seq: 2, role: 'dev' }),
    event(4, 'team.run_finished', { message_seq: 2, role: 'dev', status: 'completed' }),
  ])
  initial.summary = { id: 'team-task', goal: '团队接力', status: 'done', status_label: '已完成', version: 4, latest_seq: 4,
    conversation_count: 99, team: { enabled: true, status: 'running', revision: 1, pending_count: 0 } }
  const app = await mount(initial)
  const original = app.fetchMock.getMockImplementation()!
  app.fetchMock.mockImplementation((url, init) => url === '/api/tasks/team-task' ? new Promise<Response>(() => {}) : original(url, init))
  await act(async () => fireEvent(document, new Event('visibilitychange')))
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /2 条对话.*消息处理完成，任务待交付/ })).toBeTruthy()
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
