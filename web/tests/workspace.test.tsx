import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { WorkspacePanel, WorkspaceToolProgress } from '../src/WorkspacePanel'
import type { WorkspaceBinding, WorkspaceChange, WorkspaceOption } from '../src/WorkspacePanel'
import { readJsonResponse, request } from '../src/request'
import { CREATION_RECOVERY_KEY } from '../src/creationRecovery'

const options: WorkspaceOption[] = [
  { id: 'docs', title: '资料', root: 'F:/资料', mode: 'read_only', commands: [] },
  { id: 'edit', title: '猫咖前端', root: 'F:/猫咖/web', mode: 'edit', commands: [] },
  { id: 'host', title: '本机工程', root: 'F:/工程', mode: 'execute', commands: ['test', 'build'] },
]
const unbound = (): WorkspaceBinding => ({ workspace_id: null, revision: null, available: false, reason: '尚未绑定工作目录。', pending: [] })
const binding = (revision = 7): WorkspaceBinding => ({ ...unbound(), workspace_id: 'edit', revision, available: true, reason: '', title: '猫咖前端', root: 'F:/猫咖/web', mode: 'edit' })
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { resolve, reject, promise }
}
const props = (workspace: WorkspaceBinding | undefined = unbound()) => ({ taskId: 'a', workspace, active: false,
  loadOptions: vi.fn(async () => options), onBind: vi.fn(async (_change: WorkspaceChange) => binding()), onRefresh: vi.fn(async () => binding(10)) })
const open = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '工作目录', exact: true }))) }
const choose = (value: string) => fireEvent.change(screen.getByRole('combobox', { name: '可选目录' }), { target: { value } })
const save = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '保存绑定' }))) }
const disabled = (name: string) => (screen.getByRole('button', { name, exact: true }) as HTMLButtonElement).disabled

test('入口按需加载，只选预配置目录；中文权限与 execute 主机说明随选择呈现', async () => {
  const p = props()
  render(<WorkspacePanel {...p} />)
  expect(p.loadOptions).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '工作目录' }).querySelector('svg')).toBeTruthy()
  await open()
  expect(screen.getByRole('dialog').textContent).toContain('尚未绑定工作目录')
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(['选择预配置目录', '资料 · 只读', '猫咖前端 · 可读写', '本机工程 · 可读写并运行命令'])
  choose('docs')
  expect(screen.getByText('权限：只读')).toBeTruthy()
  expect(screen.queryByText(/当前不是沙箱/)).toBeNull()
  choose('edit')
  expect(screen.getByText('权限：可读写')).toBeTruthy()
  expect(screen.queryByText(/当前不是沙箱/)).toBeNull()
  choose('host')
  expect(screen.getByText('命令在这台电脑上运行，当前不是沙箱')).toBeTruthy()
  expect(screen.getByText('可用命令：test、build')).toBeTruthy()
  await save()
  expect(p.onBind).toHaveBeenCalledExactlyOnceWith({ workspace_id: 'host', expected_revision: null })
  expect(screen.getByText(/任务未自动执行/)).toBeTruthy()
})

test('解除绑定精确传 null 和原 revision，保存时同步阻止重复点击', async () => {
  const p = props(binding())
  const pending = deferred<WorkspaceBinding>()
  p.onBind.mockReturnValue(pending.promise)
  render(<WorkspacePanel {...p} />)
  await open()
  expect(within(screen.getByRole('region', { name: '当前工作目录' })).getByText('F:/猫咖/web')).toBeTruthy()
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: '解除绑定' }))
    fireEvent.click(screen.getByRole('button', { name: '解除绑定' }))
  })
  expect(p.onBind).toHaveBeenCalledExactlyOnceWith({ workspace_id: null, expected_revision: 7 })
  expect(disabled('保存中…')).toBe(true)
  expect(disabled('解除绑定')).toBe(true)
  await act(async () => pending.resolve({ ...unbound(), revision: 8 }))
  expect(screen.getByText(/工作目录已更新/)).toBeTruthy()
})

test.each(['active', 'pending'] as const)('%s 时可查看，但无法更换或解除；pending 不冒充执行成功', async (state) => {
  const p = props({ ...binding(), pending: state === 'pending' ? [{ seq: 12, task_id: 'a', name: 'workspace_write' }] : [] })
  p.active = state === 'active'
  render(<WorkspacePanel {...p} />)
  await open()
  expect(disabled('保存绑定')).toBe(true)
  expect(disabled('解除绑定')).toBe(true)
  expect((screen.getByRole('combobox') as HTMLSelectElement).disabled).toBe(true)
  expect(screen.getByText(state === 'active' ? /任务仍有活动/ : /工具结果未明/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '解除绑定' }))
  expect(p.onBind).not.toHaveBeenCalled()
})

test('revision 为 0 原样提交；不可用但仍预配置的同名目录可以重新绑定', async () => {
  const p = props({ ...binding(0), available: false })
  render(<WorkspacePanel {...p} />)
  await open()
  expect(disabled('保存绑定')).toBe(false)
  await save()
  expect(p.onBind).toHaveBeenCalledExactlyOnceWith({ workspace_id: 'edit', expected_revision: 0 })
})

test('旧 options 请求迟到不能覆盖新任务的目录列表', async () => {
  const pending = deferred<WorkspaceOption[]>()
  const p = props()
  p.loadOptions.mockReturnValue(pending.promise)
  const view = render(<WorkspacePanel {...p} />)
  await open()
  expect(screen.getByText('正在读取可选目录…')).toBeTruthy()
  const next = props()
  next.loadOptions.mockResolvedValue([])
  view.rerender(<WorkspacePanel {...next} taskId="b" />)
  await open()
  await act(async () => pending.resolve(options))
  expect(screen.getByText(/尚未配置可选工作目录/)).toBeTruthy()
  expect(screen.queryByRole('option', { name: /猫咖前端/ })).toBeNull()
})

test('空 options 明确未配置；失效绑定允许解除，不允许用消失的选项保存', async () => {
  const p = props({ ...binding(), available: false, reason: '权限已变化，需要重新绑定。' })
  p.loadOptions.mockResolvedValue([])
  render(<WorkspacePanel {...p} />)
  await open()
  expect(screen.getByText(/尚未配置可选工作目录/)).toBeTruthy()
  expect(screen.getByText('权限：可读写 · 不可用，需重新绑定')).toBeTruthy()
  expect(screen.queryByText(/沙箱/)).toBeNull()
  expect(disabled('保存绑定')).toBe(true)
  expect(disabled('解除绑定')).toBe(false)
  expect(screen.getByRole('option', { name: '当前绑定不在可选列表中' })).toBeTruthy()
})

test('读取失败和空配置区分，重新读取可恢复；workspace 缺失不能当作首次绑定', async () => {
  const p = props()
  p.workspace = undefined
  p.loadOptions.mockRejectedValueOnce(new Error('目录列表暂不可用'))
  const view = render(<WorkspacePanel {...p} />)
  await open()
  expect(screen.getByRole('alert').textContent).toContain('目录列表暂不可用')
  expect(screen.queryByText(/尚未配置可选工作目录/)).toBeNull()
  expect(disabled('保存绑定')).toBe(true)
  expect(screen.getByText(/暂未取得工作目录状态/)).toBeTruthy()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新读取' })))
  view.rerender(<WorkspacePanel {...p} workspace={binding(10)} />)
  choose('docs')
  await save()
  expect(p.onBind).toHaveBeenCalledExactlyOnceWith({ workspace_id: 'docs', expected_revision: 10 })
})

test('409 错误保留选择，重新读取后使用新 revision；轮询不会静默更新用户版本', async () => {
  const p = props(binding())
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('工作区绑定已变化，请刷新后重试。', { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ workspace: binding(11) }))))
  p.onBind.mockImplementation(async (change) => (await request<{ workspace: WorkspaceBinding }>('/api/tasks/a/workspace', {
    method: 'POST', body: JSON.stringify(change),
  }, readJsonResponse)).workspace)
  const view = render(<WorkspacePanel {...p} />)
  await open()
  choose('host')
  await save()
  expect(screen.getByRole('alert').textContent).toContain('工作区绑定已变化')
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('host')
  view.rerender(<WorkspacePanel {...p} workspace={binding(10)} />)
  expect(disabled('保存绑定')).toBe(true)
  expect(screen.getByText(/当前绑定已变化/)).toBeTruthy()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新读取' })))
  choose('host')
  await save()
  expect(p.onBind.mock.calls.map(([change]) => change)).toEqual([
    { workspace_id: 'host', expected_revision: 7 }, { workspace_id: 'host', expected_revision: 10 },
  ])
})

test.each(['resolve', 'reject'] as const)('导航后迟到 %s 不改变新任务弹层', async (settle) => {
  const p = props(binding())
  const pending = deferred<WorkspaceBinding>()
  p.onBind.mockReturnValue(pending.promise)
  const view = render(<WorkspacePanel {...p} />)
  await open()
  choose('host')
  await save()
  view.rerender(<WorkspacePanel {...props(unbound())} taskId="b" />)
  expect(screen.queryByRole('dialog')).toBeNull()
  await open()
  await act(async () => settle === 'resolve' ? pending.resolve(binding(11)) : pending.reject(new Error('旧任务的迟到错误')))
  expect(screen.getByRole('dialog').textContent).toContain('尚未绑定工作目录')
  expect(screen.queryByText(/旧任务的迟到错误|工作目录已更新/)).toBeNull()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('')
})

type ToolEvent = Parameters<typeof WorkspaceToolProgress>[0]['events'][number]
const scope = { instruction_seq: 1 }
const attempt = { work_item_id: 'front', attempt_id: 'first', plan_seq: 1, scope }
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = { id: 'workspace_gateway', kind: 'system' }): ToolEvent => ({ seq, type, actor, payload })
const plan = event(1, 'work.plan_created', { items: [{ id: 'front', owner: 'dev', objective: '补齐目录入口' }] }, { id: 'work_scheduler', kind: 'system' })
const claim = event(2, 'work.claimed', attempt, { id: 'work_scheduler', kind: 'system' })
const start = (seq: number, name: string, extra = {}): ToolEvent => event(seq, 'workspace.tool_started', { ...attempt, role: 'dev', name, call_id: `call-${seq}`, workspace_revision: 7, arguments: { path: 'src/入口.tsx' }, ...extra })
const finish = (start: ToolEvent, result: Record<string, unknown>): ToolEvent => event(start.seq + 1, 'workspace.tool_finished', { ...start.payload, arguments: undefined, started_seq: start.seq, result })
const capture = (text: string, complete = true, truncated = false) => ({ excerpt: text, bytes: new TextEncoder().encode(text).length, sha256: 'a'.repeat(64), complete, truncated })
type RunOverrides = { stdout?: ReturnType<typeof capture>; stderr?: ReturnType<typeof capture>; complete?: boolean; truncated?: boolean; error?: { code: string; message: string } }
const runResult = (outcome: 'passed' | 'failed' | 'timeout' | 'launch_error', exitCode: number | null, extra: RunOverrides = {}) => {
  const stdout = extra.stdout ?? capture(''), stderr = extra.stderr ?? capture('')
  return { status: outcome === 'passed' ? 'ok' : 'error', command: 'test', cwd: '.', outcome, exit_code: exitCode, timed_out: outcome === 'timeout', duration_ms: 12,
    stdout, stderr, bytes: stdout.bytes + stderr.bytes, truncated: stdout.truncated || stderr.truncated, complete: stdout.complete && stderr.complete,
    process_scope: 'windows_job', output: stdout.excerpt + stderr.excerpt,
    ...(outcome !== 'passed' ? { error: { code: outcome, message: 'Workspace command did not complete successfully.' } } : {}), ...extra }
}

test('真实工具事件在角色过程内呈现读/查找/修改/运行与结果，不显示 raw JSON 或源码写入参数', () => {
  const read = start(3, 'workspace_read')
  const search = start(5, 'workspace_search', { arguments: { path: 'src', query: '猫咖' } })
  const write = start(7, 'workspace_write', { arguments: { path: 'src/入口.tsx', content: '不展示的写入正文' } })
  const run = start(9, 'workspace_run', { arguments: { command: 'test' } })
  const unknown = start(11, 'workspace_write')
  const list = start(17, 'workspace_list', { arguments: { path: '.' } })
  const events = [plan, claim, read, finish(read, { status: 'ok', path: 'src/入口.tsx', sha256: 'a'.repeat(64), bytes: 100, offset: 0, returned_bytes: 29, content: '<img src=x onerror=alert(1)>', encoding: 'utf-8', truncated: true }),
    search, finish(search, { status: 'ok', path: 'src', matches: [{ path: 'src/入口.tsx', line: 3, column: 1, output: '欢迎来到猫咖', sha256: 'a'.repeat(64), bytes: 100, truncated: false }], scanned_bytes: 100, truncated: false }), write,
    finish(write, { status: 'ok', path: 'src/入口.tsx', sha256: 'b'.repeat(64), bytes: 20, created: false, truncated: false }), run,
    finish(run, runResult('failed', 1, { stdout: capture('测试未通过'), stderr: capture('错误详情') })), unknown,
    start(13, 'workspace_read', { role: 'test' }), start(14, 'workspace_run', { attempt_id: 'second' }), start(15, 'workspace_read', { scope: { instruction_seq: 8 } }),
    { ...start(16, 'workspace_write'), actor: { id: 'dev', kind: 'agent' } },
    list, finish(list, { status: 'ok', path: '.', entries: [{ path: 'src', type: 'directory', bytes: null }, { path: 'README.md', type: 'file', bytes: 100 }], truncated: false }),
    finish(unknown, { status: 'ok' }),
  ]
  events.at(-1)!.payload.call_id = 'wrong-call'
  const { container } = render(<WorkspaceToolProgress events={events} claim={2} name="米寇" />)
  expect(screen.getByText('目录操作 · 6 项')).toBeTruthy()
  expect(screen.getByText('读取文件')).toBeTruthy()
  expect(screen.getByText('查找内容')).toBeTruthy()
  expect(screen.getAllByText('修改文件')).toHaveLength(2)
  expect(screen.getByText('运行命令')).toBeTruthy()
  expect(screen.getByText(/退出码 1/)).toBeTruthy()
  expect(screen.getByText(/测试未通过/)).toBeTruthy()
  expect(screen.getByText(/错误详情/)).toBeTruthy()
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeTruthy()
  expect(screen.getByText('src/入口.tsx:3')).toBeTruthy()
  expect(screen.getByText('欢迎来到猫咖')).toBeTruthy()
  expect(screen.getByText('README.md')).toBeTruthy()
  expect(screen.getByText('已读取 2 项目录内容')).toBeTruthy()
  expect(screen.getByText(/已发起，结果待确认/)).toBeTruthy()
  expect(screen.queryByText(/不展示的写入正文|arguments_sha256/)).toBeNull()
  expect(container.querySelector('img')).toBeNull()
  expect(screen.getByText(/命令回执不代表任务验收通过/)).toBeTruthy()
})

test('缺少领取或对应计划 owner 时不捏造工具过程', () => {
  const run = start(3, 'workspace_run')
  const view = render(<WorkspaceToolProgress events={[run]} claim={2} name="米寇" />)
  expect(screen.queryByText(/目录操作/)).toBeNull()
  view.rerender(<WorkspaceToolProgress events={[claim, run]} claim={2} name="米寇" />)
  expect(screen.queryByText(/目录操作/)).toBeNull()
  view.rerender(<WorkspaceToolProgress events={[{ ...plan, seq: 9 }, claim, run]} claim={2} name="米寇" />)
  expect(screen.queryByText(/目录操作/)).toBeNull()
  view.rerender(<WorkspaceToolProgress events={[{ ...plan, payload: { items: [{ id: 'front', owner: 'test' }] } }, claim, run]} claim={2} name="米寇" />)
  expect(screen.queryByText(/目录操作/)).toBeNull()
})

test.each([
  runResult('timeout', null),
  runResult('failed', 0, { complete: false, stdout: capture('部分输出', false), error: { code: 'process_failed', message: '输出不完整' } }),
  runResult('failed', 0, { error: { code: 'process_failed', message: '进程清理失败' } }),
])('finished $outcome / complete=$complete 保留待核对，只有匹配的人工回执解除', (result) => {
  const run = start(3, 'workspace_run')
  const events = [plan, claim, run, finish(run, result)]
  const reconciled = event(5, 'workspace.tool_reconciled', { started_seq: 3, call_id: 'call-3', reason: '已检查文件和残留进程，无需重放', outcome: 'effects_reviewed_no_replay' }, { id: 'human', kind: 'human' })
  const view = render(<WorkspaceToolProgress events={events} claim={2} name="米寇" />)
  expect(screen.getByText('· 结果待核对')).toBeTruthy()
  expect(screen.queryByText('· 已完成')).toBeNull()
  view.rerender(<WorkspaceToolProgress events={[...events, { ...reconciled, payload: { ...reconciled.payload, call_id: 'wrong-call' } }]} claim={2} name="米寇" />)
  expect(screen.getByText('· 结果待核对')).toBeTruthy()
  view.rerender(<WorkspaceToolProgress events={[...events, { ...reconciled, actor: { id: 'human', kind: 'agent' } }]} claim={2} name="米寇" />)
  expect(screen.getByText('· 结果待核对')).toBeTruthy()
  view.rerender(<WorkspaceToolProgress events={[...events, reconciled]} claim={2} name="米寇" />)
  expect(screen.queryByText('· 结果待核对')).toBeNull()
  expect(screen.getByText(/已人工核对实际影响，未重放/)).toBeTruthy()
  expect(screen.getByText('已检查文件和残留进程，无需重放')).toBeTruthy()
  expect(screen.queryByText('· 已完成')).toBeNull()
})

test.each([
  ['passed', 0, '运行成功 · 退出码 0'],
  ['failed', 2, '运行失败 · 退出码 2'],
  ['launch_error', null, '启动失败'],
] as const)('最终运行契约 %s：capture dict 日志可见，完整回执无需人工核对', (outcome, exitCode, label) => {
  const run = start(3, 'workspace_run')
  const result = runResult(outcome, exitCode, { stdout: capture('标准输出摘录', true, true), stderr: capture('标准错误摘录'), truncated: true })
  render(<WorkspaceToolProgress events={[plan, claim, run, finish(run, result)]} claim={2} name="米寇" />)
  expect(screen.getByText(`· ${label}`)).toBeTruthy()
  expect(screen.getByText(/标准输出摘录/)).toBeTruthy()
  expect(screen.getByText(/标准错误摘录/)).toBeTruthy()
  expect(screen.getByText('结果仅返回部分内容。')).toBeTruthy()
  expect(screen.queryByText(/结果待核对|运行结果待核对/)).toBeNull()
})

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const detail = (id: string, workspace = unbound()): TaskDetail => ({ task: { id, goal: `${id}任务`, status: 'pending', acceptance: [], assignments: [], messages: [], deliverables: [] },
  events: [{ seq: workspace.revision ?? 1, type: 'task.created', category: 'system', actor: { id: 'human', kind: 'human', role: 'human' }, payload: { goal: `${id}任务` } }],
  review: {}, activity: { active: false, state: 'idle' }, workspace })
beforeEach(() => {
  window.sessionStorage.clear()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
})

test.each(['resolve', 'reject'] as const)('App 复用请求封装发送精确 body；POST 迟到 %s 不覆盖导航与草稿', async (settle) => {
  const pending = deferred<Response>()
  const data = { a: detail('a'), b: detail('b', binding()) }
  const run = start(3, 'workspace_run', { arguments: { command: 'test' } })
  data.b.events = [plan, claim, run, finish(run, runResult('passed', 0, { stdout: capture('前端检查通过') }))]
    .map((entry) => ({ ...entry, category: 'lifecycle', actor: { ...entry.actor, kind: entry.actor.kind ?? 'system', role: entry.actor.id } }))
  const fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if (url === '/api/task-summaries') return json({ tasks: ['a', 'b'].map((id) => ({ id, goal: `${id}任务`, status: 'pending', status_label: '待开始', version: 1, latest_seq: 1, conversation_count: 1 })) })
    if (url === '/api/organization') return json([])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] })
    if (url === '/api/workspaces') return json(options)
    if (url === '/api/tasks/b/workspace' && init.method === 'POST') return pending.promise
    if (url === '/api/tasks/a') return json(data.a)
    if (url === '/api/tasks/b') return json(data.b)
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  await act(async () => { render(<App />) })
  await screen.findByRole('heading', { name: 'b任务' })
  const process = screen.getByText('目录操作 · 1 项').closest('details')!
  expect(process.open).toBe(false)
  expect(within(screen.getByLabelText('米寇的目录操作')).getByText(/运行成功 · 退出码 0/)).toBeTruthy()
  expect(within(screen.getByLabelText('米寇的目录操作')).getByText('前端检查通过')).toBeTruthy()
  await open()
  choose('host')
  await save()
  const posts = fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toBe('/api/tasks/b/workspace')
  expect(JSON.parse(posts[0][1].body as string)).toEqual({ workspace_id: 'host', expected_revision: 7 })
  expect(new Headers(posts[0][1].headers).get('Content-Type')).toBe('application/json')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  await act(async () => fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /^a任务/ })))
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '新任务草稿' } })
  await act(async () => settle === 'resolve' ? pending.resolve(json(detail('b', binding(20)))) : pending.reject(new Error('旧请求失败')))
  expect(screen.getByRole('heading', { name: 'a任务' })).toBeTruthy()
  expect(input.value).toBe('新任务草稿')
  await open()
  expect(screen.getByRole('dialog').textContent).toContain('尚未绑定工作目录')
  expect(screen.queryByText(/旧请求失败|提交结果未知/)).toBeNull()
})

test('草稿面板按需选目录与清除，只回调本地选择，关闭未确认选项不生效', async () => {
  const onSelect = vi.fn()
  const loadOptions = vi.fn(async () => options)
  const view = render(<WorkspacePanel draft loadOptions={loadOptions} onSelect={onSelect} />)
  expect(loadOptions).not.toHaveBeenCalled()
  await open()
  expect(screen.getByText('新对话草稿')).toBeTruthy()
  expect(screen.queryByText(/暂未取得工作目录状态|重新核对当前计划/)).toBeNull()
  choose('host')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  expect(onSelect).not.toHaveBeenCalled()
  await open()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('')
  choose('host')
  fireEvent.click(screen.getByRole('button', { name: '用于新对话' }))
  expect(onSelect).toHaveBeenCalledExactlyOnceWith('host')
  expect(screen.queryByRole('dialog')).toBeNull()
  view.rerender(<WorkspacePanel draft workspaceId="host" loadOptions={loadOptions} onSelect={onSelect} />)
  await open()
  fireEvent.click(screen.getByRole('button', { name: '不使用目录' }))
  expect(onSelect).toHaveBeenLastCalledWith(undefined)
})

async function mountCreationWorkspace() {
  let catalog = options
  let submit: (url: string, init: RequestInit) => Response | Promise<Response> = () => json(detail('created', binding()))
  const fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method === 'POST') return submit(url, init)
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'b', goal: 'b任务', status: 'pending', status_label: '待开始', latest_seq: 7, version: 7, conversation_count: 1 }] })
    if (url === '/api/organization') return json([])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] })
    if (url === '/api/workspaces') return json(catalog)
    if (url === '/api/tasks/b') return json(detail('b', binding()))
    if (url === '/api/tasks/created' || url === '/api/tasks/derived') return json(detail(url.split('/')[3], binding()))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  await act(async () => { render(<App />) })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '新建任务' })))
  return {
    fetchMock,
    setSubmit: (next: typeof submit) => { submit = next },
    setCatalog: (next: WorkspaceOption[]) => { catalog = next },
    posts: () => fetchMock.mock.calls.filter(([, init]) => init.method === 'POST'),
    bodies: () => fetchMock.mock.calls.filter(([, init]) => init.method === 'POST').map(([, init]) => JSON.parse(init.body as string)),
  }
}
const newInput = () => screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
const writeNew = (text = '  首次发送的工程任务  ') => fireEvent.change(newInput(), { target: { value: text } })
const sendNew = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送新任务' }))) }
const selectDraftDirectory = async (id: string | undefined) => {
  await open()
  if (id) choose(id)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: id ? '用于新对话' : '不使用目录' })))
}

test.each([undefined, 'docs', 'host'])('首次发送目录 %s 随创建提交，选择不写后台，无目录省略字段；成功后清除下一份选择', async (id) => {
  const h = await mountCreationWorkspace()
  expect(h.fetchMock.mock.calls.some(([url]) => url === '/api/workspaces')).toBe(false)
  if (id) await selectDraftDirectory(id)
  expect(h.posts()).toHaveLength(0)
  writeNew()
  await sendNew()
  expect(h.posts().map(([url]) => url)).toEqual(['/api/tasks'])
  expect(h.bodies()[0]).toEqual({ goal: '首次发送的工程任务', request_id: expect.any(String), run: true, background: true, ...(id ? { workspace_id: id } : {}) })
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  await open()
  expect(screen.getByText('未选择工作目录')).toBeTruthy()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('')
})

test('创建重试保持原目录和原 ID；只浏览其他目录不改变原请求，清除目录生成新意图', async () => {
  const h = await mountCreationWorkspace()
  h.setSubmit(() => new Response('创建回包失败', { status: 503 }))
  await selectDraftDirectory('host')
  writeNew()
  await sendNew()
  const original = h.posts()[0][1].body
  expect(JSON.parse(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)!).attempt.workspaceId).toBe('host')
  await open()
  choose('docs')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  await sendNew()
  expect(h.posts()[1][1].body).toBe(original)
  await selectDraftDirectory(undefined)
  await sendNew()
  expect(h.bodies()[2]).not.toHaveProperty('workspace_id')
  expect(h.bodies()[2].request_id).not.toBe(h.bodies()[0].request_id)
})

test.each(['resolve', 'reject'] as const)('发送中改目录后旧响应 %s 不覆盖新选择、草稿或导航，改回原目录也用新 ID', async (settle) => {
  const h = await mountCreationWorkspace()
  const pending = deferred<Response>()
  h.setSubmit(() => pending.promise)
  await selectDraftDirectory('docs')
  writeNew()
  await sendNew()
  await selectDraftDirectory('host')
  expect(h.posts()).toHaveLength(1)
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  await act(async () => settle === 'resolve' ? pending.resolve(json(detail('created'))) : pending.reject(new Error('旧目录请求失败')))
  expect(newInput().value).toBe('  首次发送的工程任务  ')
  expect(screen.queryByRole('heading', { name: 'created任务' })).toBeNull()
  expect(screen.queryByText(/旧目录请求失败/)).toBeNull()
  await open()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('host')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  h.setSubmit(() => new Response('仍需重试', { status: 503 }))
  await sendNew()
  expect(h.bodies()[1].workspace_id).toBe('host')
  expect(h.bodies()[1].request_id).not.toBe(h.bodies()[0].request_id)
  await selectDraftDirectory('docs')
  await sendNew()
  expect(h.bodies()[2].workspace_id).toBe('docs')
  expect(new Set(h.bodies().map((body) => body.request_id)).size).toBe(3)
})

test('刷新恢复完整原目录与原请求；目录从列表消失也不静默替换，不自动重发', async () => {
  const h = await mountCreationWorkspace()
  h.setSubmit(() => new Response('提交回执丢失', { status: 503 }))
  await selectDraftDirectory('host')
  writeNew()
  await sendNew()
  const original = h.posts()[0][1].body
  cleanup()
  h.setCatalog([options[0]])
  await act(async () => { render(<App />) })
  expect(h.posts()).toHaveLength(1)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '恢复待确认任务' })))
  expect(newInput().value).toBe('  首次发送的工程任务  ')
  await open()
  expect(screen.getByRole('option', { name: '原选择不在可选列表中' })).toBeTruthy()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('host')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  await sendNew()
  expect(h.posts()[1][1].body).toBe(original)
})

test('新对话选择不应用已有任务；派生不继承父目录或新对话目录', async () => {
  const h = await mountCreationWorkspace()
  await selectDraftDirectory('docs')
  await act(async () => fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /^b任务/ })))
  await open()
  expect(within(screen.getByRole('region', { name: '当前工作目录' })).getByText('猫咖前端')).toBeTruthy()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('edit')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  expect(h.posts()).toHaveLength(0)
  h.setSubmit(() => json(detail('derived', unbound())))
  fireEvent.click(screen.getByRole('button', { name: '基于此对话开新任务' }))
  fireEvent.change(screen.getByRole('textbox', { name: '衍生任务目标' }), { target: { value: '独立衍生目标' } })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' })))
  expect(h.posts().map(([url]) => url)).toEqual(['/api/tasks/b/derive'])
  expect(h.bodies()[0]).not.toHaveProperty('workspace_id')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  await open()
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('docs')
})
