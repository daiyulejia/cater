import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { ConversationOrigin } from '../src/ConversationOrigin'
import type { ConversationBackground } from '../src/ConversationOrigin'
import { RulesPanel } from '../src/RulesPanel'

const goals: Record<string, string> = { a: '甲任务', b: '乙任务', c: '新任务' }
const snapshot: ConversationBackground = {
  version: 1, source_task_id: 'b', source_goal: '创建时的乙目标', source_seq: 12,
  source_hash: 'captured-hash', captured_at: '2026-10-15T02:00:00+00:00', omitted_artifacts: 2,
  sources: [{ task_id: 'b', id: 'goal:1', seq: 1, text: '当时补充的目标 <script>window.bad = true</script>' }],
  task_rules: [{ id: 'rule-original', text: '保留公共接口', revision: 2, seq: 5 }],
  artifacts: [
    { task_id: 'b', seq: 8, title: '来源成果', summary: '当时的成果摘要', content: '<img src="https://example.com/pixel" onerror="alert(1)"><script>alert(1)</script>', format: 'html', filename: 'draft.html', verification: 'accepted_at_capture' },
    { task_id: 'b', seq: 9, title: '成员草稿', summary: '只有摘要', verification: 'unverified' },
  ],
}

function summary(id: string) {
  return { id, goal: goals[id], status: 'pending', status_label: '待开始', conversation_count: 1, latest_seq: 1, version: 1 }
}

function detail(id: string, background?: ConversationBackground | null) {
  return { task: { id, goal: goals[id], acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] },
    events: [{ seq: 1, type: 'task.created', actor: { id: 'human', kind: 'human' }, payload: { goal: `${goals[id]}历史` } }],
    summary: summary(id), background, activity: { active: false, state: 'idle' }, review: {} }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/task-summaries') return json({ tasks: [summary('a'), summary('b')] })
    if (url === '/api/organization') return json([{ id: 'dev', name: '米寇', role: 'dev' }])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [{ id: 'dev', name: '米寇', role: 'dev' }] })
    if (url === '/api/tasks/b/derive' || url === '/api/tasks/a/derive') return json(detail('c', snapshot))
    if (/^\/api\/tasks\/[abc]$/.test(url)) return json(detail(url.split('/')[3], url.endsWith('/c') ? snapshot : undefined))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const mount = async () => { await act(async () => { render(<App />) }) }
const settle = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(0) }) }
const nav = () => within(screen.getByRole('navigation', { name: '任务列表' }))
const select = async (id: string) => { await act(async () => fireEvent.click(nav().getByRole('button', { name: new RegExp(`^${goals[id]}`) }))) }
const originalInput = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
const derivedInput = () => screen.getByRole('textbox', { name: '衍生任务目标' }) as HTMLTextAreaElement
const write = (input: HTMLElement, value: string) => fireEvent.change(input, { target: { value } })
const begin = () => fireEvent.click(screen.getByRole('button', { name: '基于此对话开新任务' }))
const cancel = () => fireEvent.click(screen.getByRole('button', { name: '取消新任务草稿' }))
const send = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))) }
const posts = () => fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')
const bodies = () => posts().map(([, init]) => JSON.parse(init.body as string))
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

test('进入和取消衍生草稿不发帖，保留原任务及普通新任务草稿', async () => {
  await mount()
  write(originalInput(), '乙任务未发送内容')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  write(screen.getByRole('textbox', { name: '发起任务对话' }), '普通新任务草稿')
  await select('b')
  begin()
  expect(derivedInput().value).toBe('')
  expect(document.activeElement).toBe(derivedInput())
  expect(screen.getByRole('region', { name: '新任务草稿来源' }).textContent).toContain('乙任务')
  expect(screen.getByText('乙任务历史')).toBeTruthy()
  expect((screen.getByRole('button', { name: '创建衍生任务' }) as HTMLButtonElement).disabled).toBe(true)
  write(derivedInput(), '新的目标草稿')
  cancel()
  expect(originalInput().value).toBe('乙任务未发送内容')
  expect(document.activeElement).toBe(originalInput())
  begin()
  expect(derivedInput().value).toBe('新的目标草稿')
  cancel()
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('普通新任务草稿')
  expect(posts()).toHaveLength(0)
})

test('发送新目标到来源 derive 接口，保留点名且不向原群发 command', async () => {
  await mount()
  write(originalInput(), '原任务草稿')
  begin()
  write(derivedInput(), '  @米寇 做一个独立的新目标  ')
  await send()
  expect(posts()).toHaveLength(1)
  expect(posts()[0][0]).toBe('/api/tasks/b/derive')
  expect(bodies()[0]).toEqual({ goal: '@米寇 做一个独立的新目标', request_id: expect.stringMatching(uuid), collaboration: 'team', run: true, background: true })
  expect(screen.getByRole('heading', { name: '新任务', level: 1 })).toBeTruthy()
  expect(screen.getByRole('button', { name: '查看对话来源' })).toBeTruthy()
  await select('b')
  expect(originalInput().value).toBe('原任务草稿')
  begin()
  expect(derivedInput().value).toBe('')
})

test('HTTP 与网络失败后重试保持同一 UUID 和请求体，轮询不影响重试', async () => {
  await mount()
  let attempts = 0
  handler = (url) => {
    if (!url.endsWith('/derive')) return
    attempts += 1
    if (attempts === 1) return new Response('暂时不能创建', { status: 503 })
    if (attempts === 2) return Promise.reject(new Error('网络中断'))
    return json(detail('c', snapshot))
  }
  begin(); write(derivedInput(), '重试同一目标')
  await send()
  expect(screen.getByRole('alert').textContent).toContain('暂时不能创建')
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  expect(derivedInput().value).toBe('重试同一目标')
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  await send()
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  await send()
  expect(bodies()).toHaveLength(3)
  expect(bodies()[0].request_id).toMatch(uuid)
  expect(bodies()[1]).toEqual(bodies()[0])
  expect(bodies()[2]).toEqual(bodies()[0])
  expect(screen.getByRole('heading', { name: '新任务', level: 1 })).toBeTruthy()
})

test('编辑目标、换来源产生新 UUID，409 不丢草稿或暗中换 key', async () => {
  await mount()
  handler = (url) => url.endsWith('/derive') ? new Response('同一创建请求不能更换目标或来源，请重新发起。', { status: 409 }) : undefined
  begin(); write(derivedInput(), '最初目标'); await send(); await send()
  expect(bodies()[0].request_id).toBe(bodies()[1].request_id)
  write(derivedInput(), '修改后的目标')
  expect(screen.queryByRole('alert')).toBeNull()
  await send()
  expect(bodies()[2].request_id).not.toBe(bodies()[1].request_id)
  expect(derivedInput().value).toBe('修改后的目标')
  await select('a'); begin(); write(derivedInput(), '修改后的目标'); await send()
  expect(posts()[3][0]).toBe('/api/tasks/a/derive')
  expect(bodies()[3].request_id).not.toBe(bodies()[2].request_id)
  expect(bodies().every((body) => uuid.test(body.request_id))).toBe(true)
})

test('同一提交取消再进入仍可用原 UUID 重试', async () => {
  await mount()
  handler = (url) => url.endsWith('/derive') ? new Response('暂时失败', { status: 503 }) : undefined
  begin(); write(derivedInput(), '保留这次提交'); await send()
  cancel(); begin(); await send()
  expect(bodies()[1]).toEqual(bodies()[0])
})

test('点击和快捷键防重入，切换后迟到成功不抢导航、不清其他草稿', async () => {
  await mount()
  write(originalInput(), '乙原稿')
  const pending = deferred<Response>()
  handler = (url) => url === '/api/tasks/b/derive' ? pending.promise : undefined
  begin(); write(derivedInput(), '乙衍生目标')
  fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))
  fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))
  fireEvent.keyDown(derivedInput(), { key: 'Enter', ctrlKey: true })
  expect(posts()).toHaveLength(1)
  expect((screen.getByRole('button', { name: '创建衍生任务' }) as HTMLButtonElement).disabled).toBe(true)
  await select('a'); write(originalInput(), '甲原稿'); begin(); write(derivedInput(), '甲衍生草稿')
  await act(async () => pending.resolve(json(detail('c', snapshot))))
  expect(screen.getByRole('heading', { name: '甲任务', level: 1 })).toBeTruthy()
  expect(derivedInput().value).toBe('甲衍生草稿')
  expect(nav().getByRole('button', { name: /^新任务/ })).toBeTruthy()
  cancel(); expect(originalInput().value).toBe('甲原稿')
  await select('b'); expect(originalInput().value).toBe('乙原稿')
})

test('取消正在创建的草稿后，成功只更新列表，不清除原群草稿', async () => {
  await mount()
  write(originalInput(), '同样的文本')
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/derive') ? pending.promise : undefined
  begin(); write(derivedInput(), '同样的文本')
  fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))
  cancel()
  await act(async () => pending.resolve(json(detail('c', snapshot))))
  expect(originalInput().value).toBe('同样的文本')
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
})

test('发送中编辑后改回原文，迟到成功不清除新编辑，也不导航', async () => {
  await mount()
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/derive') ? pending.promise : undefined
  begin(); write(derivedInput(), '提交目标')
  fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))
  write(derivedInput(), '继续编辑'); write(derivedInput(), '提交目标')
  await act(async () => pending.resolve(json(detail('c', snapshot))))
  expect(derivedInput().value).toBe('提交目标')
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
  handler = (url) => url.endsWith('/derive') ? new Response('稍后重试', { status: 503 }) : undefined
  await send()
  expect(bodies()[1].request_id).not.toBe(bodies()[0].request_id)
})

test('迟到错误只属于原提交，不污染另一任务或新编辑', async () => {
  await mount()
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/derive') ? pending.promise : undefined
  begin(); write(derivedInput(), '原提交')
  fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))
  await select('a'); write(originalInput(), '甲的草稿')
  await act(async () => pending.resolve(new Response('乙创建失败', { status: 503 })))
  expect(screen.queryByText(/乙创建失败/)).toBeNull()
  expect(originalInput().value).toBe('甲的草稿')
  await select('b'); begin()
  expect(screen.getByRole('alert').textContent).toContain('乙创建失败')
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  write(derivedInput(), '新的提交')
  expect(screen.queryByRole('alert')).toBeNull()
})

test('来源只显示捕获内容与独立规则，正文不执行 HTML；关闭及跳转保留草稿', async () => {
  await mount(); write(originalInput(), '原群保留草稿')
  begin(); write(derivedInput(), '新目标'); await send()
  write(originalInput(), '新群保留草稿')
  const trigger = screen.getByRole('button', { name: '查看对话来源' })
  trigger.focus(); fireEvent.click(trigger)
  const dialog = screen.getByRole('dialog', { name: '对话来源' })
  expect(within(dialog).getByText('创建时的乙目标')).toBeTruthy()
  expect(dialog.textContent).toContain('仅在创建时继承背景')
  expect(dialog.textContent).toContain('未复制验收结论')
  expect(dialog.textContent).toContain('项目通用店规仍适用')
  expect(within(dialog).getByText('保留公共接口')).toBeTruthy()
  expect(within(dialog).getByText('版本 2 · 事件 #5')).toBeTruthy()
  expect(within(dialog).getByText('未复制成果：2 项')).toBeTruthy()
  expect(dialog.querySelector('time')?.getAttribute('datetime')).toBe(snapshot.captured_at)
  expect(within(dialog).getByText('来源阶段当时通过（新任务待验收）')).toBeTruthy()
  expect(within(dialog).getByText('来源未验证')).toBeTruthy()
  fireEvent.click(within(dialog).getByText('查看成果正文'))
  expect(dialog.querySelector('pre')?.textContent).toBe(snapshot.artifacts[0].content)
  expect(dialog.querySelector('script, img, iframe')).toBeNull()
  expect(fetchMock.mock.calls.some(([url]) => url.includes('example.com'))).toBe(false)
  fireEvent.keyDown(dialog, { key: 'Escape' }); await settle()
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(trigger)
  expect(originalInput().value).toBe('新群保留草稿')
  fireEvent.click(trigger)
  fireEvent.click(screen.getByRole('button', { name: '前往原对话' })); await settle()
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
  expect(originalInput().value).toBe('原群保留草稿')
  await select('c')
  expect(originalInput().value).toBe('新群保留草稿')
})

test.each([undefined, null])('背景为 %s 的旧任务无来源入口，但可以衍生', async (background) => {
  handler = (url) => url === '/api/tasks/b' ? json(detail('b', background)) : undefined
  await mount()
  expect(screen.queryByRole('button', { name: '查看对话来源' })).toBeNull()
  begin()
  expect(derivedInput()).toBeTruthy()
})

test.each([
  ['accepted_at_capture', '来源阶段当时通过（新任务待验收）'],
  ['unverified', '来源未验证'],
  ['future_status', '待核对'],
  [undefined, '待核对'],
])('成果 verification=%s 使用保守中文标签', (verification, label) => {
  render(<ConversationOrigin background={{ ...snapshot, artifacts: [{ ...snapshot.artifacts[0], verification }] }} onNavigate={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '查看对话来源' }))
  expect(within(screen.getByRole('region', { name: '来源成果' })).getByText(label!)).toBeTruthy()
})

test('继承店规显示创建时继承，人工及成员来源标签保持原样', async () => {
  const rules = [
    { id: 'inherited', revision: 1, text: '继承的规则', scope: 'task', status: 'active', seq: 20, source: { kind: 'inherited', task_id: 'b', rule_id: 'old-rule', event_seq: 5 } },
    { id: 'human', revision: 1, text: '人工的规则', scope: 'task', status: 'active', seq: 21, source: { kind: 'human', actor: 'human' } },
    { id: 'agent', revision: 1, text: '成员的规则', scope: 'task', status: 'candidate', seq: 22, source: { kind: 'agent_report', actor: 'dev', event_seq: 9 } },
  ]
  const request = async <T,>(path: string): Promise<T> => (path === '/api/projects' ? [] : { project_id: null, rules, enforcement: '' }) as T
  render(<RulesPanel request={request} taskId="c" draft="" busy={false} onProjectChange={async () => {}} onRecorded={vi.fn()} onUpdated={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: /店规\s*未关联项目/ }))
  await settle()
  expect(screen.getByText('创建时继承 · #5')).toBeTruthy()
  expect(screen.getByText('由你制定')).toBeTruthy()
  expect(screen.getByText('成员建议 · #9')).toBeTruthy()
  expect(screen.queryByText('成员建议 · #5')).toBeNull()
})
