/// <reference types="node" />
import { createHash } from 'node:crypto'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { ChatItem } from '../src/App'
import { MessageDetails } from '../src/MessageDetails'
import { NativeCommandReceipts } from '../src/NativeCommandReceipts'
import { completion, detail, event, handoff, leader, members, summaries } from './native-handoff.fixtures'

type Data = Record<string, unknown>
const sha = 'a'.repeat(64)
const artifact = (extra: Data = {}) => ({ seq: 5, filename: '工程.zip', content_sha256: sha, ...extra })
const output = (extra: Data = {}) => ({ bytes: 6, sha256: sha, excerpt: '已记录', complete: true, truncated: false, utf8_valid: true, ...extra })
const executionContext = (extra: Data = {}) => ({ version: 1, invocation: 'direct_argv', environment: 'minimal_with_allowlist',
  path_policy: 'os_default', platform: 'win32', ...extra })
const receipt = (extra: Data = {}): Data => ({
  version: 1, source_seq: 30, source_hash: 'b'.repeat(64), started_seq: 29, started_hash: 'c'.repeat(64), role: 'dev', tool: 'workspace_run',
  request: { command: '检查', args: ['真实参数', '--原样'], cwd: 'src', timeout: 30 }, request_complete: true, stdin: null,
  argv: ['原环境程序', '--宿主参数', '真实参数', '--原样'], authorized_root: 'F:/授权工程', cwd: 'src', environment: null,
  status: 'ok', outcome: 'passed', exit_code: 0, complete: true, scope_current: null, artifacts_unchanged: null,
  artifact_check_valid: null, selected_versions_current: null, stdout: output(), stderr: output({ bytes: 0, excerpt: '' }),
  checked_artifacts: null, delivery_artifact_seqs: [], ...extra,
})
const snapshot = (records: unknown = [receipt()], seq = 40, artifacts: unknown = [artifact()]) => {
  const saved = completion(seq, { ...handoff(), command_receipts: records })
  saved.payload.artifacts = artifacts
  return saved
}
const chat = (records: unknown = [receipt()], extra: Partial<ChatItem> = {}): ChatItem => ({
  id: 40, kind: 'agent', evidence: snapshot(records), auditName: id => id === 'dev' ? '开发成员' : '协作成员', ...extra,
} as ChatItem)
const mount = (records: unknown = [receipt()], extra: Partial<ChatItem> = {}) => render(<MessageDetails item={chat(records, extra)} />)
const fold = (label: string, root: HTMLElement = document.body) => within(root).getByText(label, { selector: 'summary' }).closest('details')!
const open = (label: string, root: HTMLElement = document.body) => {
  const element = fold(label, root)
  if (!element.open) fireEvent.click(element.querySelector('summary')!)
  expect(element.open).toBe(true)
  return element
}
const openReceipt = () => {
  open('为什么这样做 · 如何运行')
  const outer = open('实际命令记录')
  const inner = outer.querySelector('details')!
  fireEvent.click(inner.querySelector('summary')!)
  return inner
}
const clipboard = (writeText = vi.fn().mockResolvedValue(undefined)) => {
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  return writeText
}

test('实际消息详情内嵌默认折叠事实，建议另列；展开和复制都不执行命令', async () => {
  const saved = snapshot()
  const before = JSON.stringify(saved)
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  mount(undefined, { evidence: saved })
  expect(fold('为什么这样做 · 如何运行').open).toBe(false)
  expect(fold('实际命令记录').open).toBe(false)
  expect(fold('实际命令记录').querySelector('details')!.open).toBe(false)
  const body = openReceipt()
  expect(screen.getByRole('heading', { name: '成员建议的运行与复查' })).toBeTruthy()
  expect(body.textContent).toContain('完成事件 #30 · 伙伴：开发成员')
  expect(body.textContent).toContain('退出码 0')
  expect(body.textContent).toContain('范围有效性未知')
  expect(body.textContent).not.toMatch(/检查通过|执行成功|验收已通过/)
  for (const label of ['实际工具请求', '原环境实际程序参数', '已采集输出', '版本与事件来源']) expect(fold(label, body).open).toBe(false)
  const writeText = clipboard()
  open('实际工具请求', body)
  const button = within(body).getByRole('button', { name: '复制完整工具请求' })
  expect(button.querySelector('svg')).toBeTruthy()
  expect(button.getAttribute('title')).toBe('复制完整工具请求')
  fireEvent.click(button)
  await screen.findByText('已复制完整工具请求')
  expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(receipt().request)
  expect(writeText).toHaveBeenCalledTimes(1)
  expect(fetchMock).not.toHaveBeenCalled()
  expect(document.querySelectorAll('main, .delivery-panel, a, script, iframe')).toHaveLength(0)
  expect(document.querySelectorAll('.message-details')).toHaveLength(1)
  expect(JSON.stringify(saved)).toBe(before)
})

test('完整应用的最终交付使用宿主completion事实，后续成员自报不能覆盖', async () => {
  const saved = snapshot()
  const spoken = event(41, 'msg.said', { phase: 'team', audience: 'customer', recipient: 'human', delivery_seq: 40,
    run_id: 'run-40', text: '交付正文', handoff: { ...handoff(), command_receipts: [receipt({ request: { command: '伪造的建议参数' } })] } },
  { id: 'store_manager', kind: 'agent', role: 'store_manager' })
  const current = detail([saved, spoken])
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/task-summaries') return json(summaries([current]))
    if (url === '/api/tasks/native-ui') return json(current)
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    throw new Error(`意外请求：${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  await screen.findByRole('region', { name: '团队进度' }, { timeout: 3000 })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  const bubble = (await screen.findByText('交付正文')).closest('article')!
  expect(fold('实际命令记录', bubble).open).toBe(false)
  open('为什么这样做 · 如何运行', bubble)
  expect(within(bubble).getByRole('heading', { name: '成员建议的运行与复查' })).toBeTruthy()
  expect(bubble.textContent).toContain('真实参数')
  expect(bubble.textContent).not.toContain('伪造的建议参数')
  const calls = fetchMock.mock.calls.length
  open('实际命令记录', bubble)
  await act(async () => fireEvent.click(fold('实际命令记录', bubble).querySelector('summary')!))
  expect(fetchMock.mock.calls).toHaveLength(calls)
})

test('复制拒绝与不支持均反馈失败，可再次复制，不回退执行', async () => {
  const writeText = clipboard(vi.fn().mockRejectedValueOnce(new Error('拒绝访问')).mockResolvedValue(undefined))
  mount()
  const body = openReceipt()
  open('实际工具请求', body)
  fireEvent.click(screen.getByRole('button', { name: '复制完整工具请求' }))
  await screen.findByText('复制失败，可手动选择请求正文。')
  fireEvent.click(screen.getByRole('button', { name: '复制完整工具请求' }))
  await screen.findByText('已复制完整工具请求')
  expect(writeText).toHaveBeenCalledTimes(2)
  vi.stubGlobal('navigator', { clipboard: undefined })
  fireEvent.click(screen.getByRole('button', { name: '复制完整工具请求' }))
  await screen.findByText('复制失败，可手动选择请求正文。')
})

test.each([
  { request_complete: false }, { request: null, request_complete: false }, { request: null },
  { stdin: { bytes: 18, sha256: sha }, request_complete: false },
  { stdin: { bytes: 0, sha256: sha } },
])('请求缺失、预算省略和标准输入省略均禁止完整复制：%j', extra => {
  mount([receipt(extra)])
  const body = openReceipt()
  open('实际工具请求', body)
  expect(within(body).queryByRole('button', { name: '复制完整工具请求' })).toBeNull()
  expect(fold('实际工具请求', body).textContent).toContain('不能完整复现')
  if ('stdin' in extra) {
    expect(body.textContent).toContain('仅保留配置输入摘要，不证明程序已读取')
    expect(body.textContent).toContain(sha)
  }
})

test('输出截断和历史范围不影响完整输入复制，三个限制各自呈现', () => {
  mount([receipt({ scope_current: false, stdout: output({ truncated: true, complete: false, utf8_valid: false }) })], { historicalDelivery: true })
  const body = openReceipt()
  open('实际工具请求', body)
  open('已采集输出', body)
  expect(screen.getByRole('button', { name: '复制完整工具请求' })).toBeTruthy()
  expect(body.textContent).toContain('历史交付中的冻结记录')
  expect(body.textContent).toContain('回执范围已失效')
  expect(body.textContent).toContain('预览已截断')
  expect(body.textContent).toContain('当前展示非完整原文')
  expect(body.textContent).toContain('采集未完成')
  expect(body.textContent).toContain('已采集输出含无效 UTF-8')
  expect(body.textContent).not.toContain('不能完整复现')
})

test('普通工程授权根不冒充客户端目录，也不绑定本次交付包', () => {
  mount()
  const body = openReceipt()
  open('执行环境与目录', body)
  expect(body.textContent).toContain('工程授权/锁根：F:/授权工程')
  expect(body.textContent).toContain('这不是外部客户端的工作目录')
  expect(body.textContent).toContain('普通工程命令：未绑定交付成果字节')
  expect(body.textContent).toContain('实际记录的相对工作目录：src')
  expect(body.textContent).not.toContain('F:/授权工程/src')
  expect(body.textContent).not.toContain('成果副本模式')
})

test.each(['os_default', 'allowlist_configured'] as const)('执行上下文仅在原有环境折叠区显示精确事实与边界：%s', async path_policy => {
  const value = receipt({ execution_context: executionContext({ path_policy }) })
  const saved = snapshot([value])
  const before = JSON.stringify(saved)
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  const writeText = clipboard()
  mount([value], { evidence: saved })
  const body = openReceipt()
  expect(fold('执行环境与目录', body).open).toBe(false)
  const environment = open('执行环境与目录', body)
  expect(environment.textContent).toContain('宿主启动方式：按 argv 直接启动；精简环境。')
  expect(environment.textContent).toContain(path_policy === 'os_default'
    ? 'PATH：系统默认 PATH，不是用户终端 PATH' : 'PATH：有显式允许配置，不保证与用户终端 PATH 一致')
  expect(environment.textContent).not.toContain(path_policy === 'os_default' ? '有显式允许配置' : '系统默认 PATH')
  for (const label of ['平台：Windows', '以上元数据仅描述外层启动配置，不证明程序成功启动',
    '查找不到程序不证明整机未安装', '直接 argv 不代表程序内部没有子 shell', '不等于操作系统沙箱']) {
    expect(environment.textContent).toContain(label)
  }
  expect(body.querySelectorAll(':scope > div > details')).toHaveLength(5)
  expect(body.textContent).not.toMatch(/未作为执行依据|未记录执行上下文/)
  open('实际工具请求', body)
  fireEvent.click(within(body).getByRole('button', { name: '复制完整工具请求' }))
  await within(body).findByText('已复制完整工具请求')
  expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(value.request)
  expect(writeText.mock.calls[0][0]).not.toContain('execution_context')
  expect(fetchMock).not.toHaveBeenCalled()
  expect(JSON.stringify(saved)).toBe(before)
})

test.each([
  ['launch_error', '启动失败'], ['not_started', '未开始执行'],
] as const)('外层启动配置与是否成功启动独立，失败仍可携带元数据：%s', (outcome, label) => {
  mount([receipt({ execution_context: executionContext({ path_policy: 'allowlist_configured' }),
    status: 'error', outcome, exit_code: null, argv: [] })])
  const body = openReceipt()
  expect(body.querySelector('summary')!.textContent).toContain(label)
  const environment = open('执行环境与目录', body)
  expect(environment.textContent).toContain('宿主启动方式：按 argv 直接启动；精简环境。')
  expect(environment.textContent).toContain('有显式允许配置，不保证与用户终端 PATH 一致')
  expect(environment.textContent).toContain('以上元数据仅描述外层启动配置，不证明程序成功启动')
  expect(body.textContent).not.toMatch(/未作为执行依据|记录为正常结束/)
})

test.each([
  ['win32', 'Windows'], ['linux', 'Linux'], ['darwin', 'macOS'], ['other', '其他平台'],
] as const)('平台只取回执枚举，不取浏览器平台：%s', (platform, label) => {
  vi.stubGlobal('navigator', { platform: '不应显示的客户端平台' })
  mount([receipt({ execution_context: executionContext({ platform }) })])
  const environment = open('执行环境与目录', openReceipt())
  expect(environment.textContent).toContain(`平台：${label}`)
  expect(environment.textContent).not.toContain('不应显示')
})

test.each([
  [false, null], [true, null], [true, 'temporary_artifact_copy_not_sandbox'],
] as const)('缺少执行上下文的旧回执保留事实，环境策略显示未知：历史 %s，环境 %s', (historicalDelivery, environment) => {
  mount([receipt({ environment })], { historicalDelivery })
  const body = openReceipt()
  const settings = open('执行环境与目录', body)
  expect(settings.textContent).toContain('启动方式、进程环境、PATH 策略与平台未知（未记录执行上下文）')
  expect(settings.textContent).not.toMatch(/精简环境|按 argv 直接启动|系统默认 PATH|有显式允许配置|平台：Windows/)
  expect(body.textContent).toContain('完成事件 #30')
  expect(body.textContent).not.toContain('未作为执行依据')
  if (historicalDelivery) expect(body.textContent).toContain('历史交付中的冻结记录')
  if (environment) expect(settings.textContent).toContain('成果副本模式')
})

test.each([
  null, undefined, {}, [], 'direct_argv',
  ...[{ version: 2 }, { version: true }, { invocation: 'shell' }, { invocation: ['direct_argv'] },
    { environment: 'inherited' }, { environment: null }, { path_policy: 'user_terminal' }, { path_policy: {} },
    { platform: 'windows' }, { platform: null }, { platform: 'toString' },
    { extra: '不应显示' }, { env: { PRIVATE: '不应显示' } },
  ].map(extra => executionContext(extra)),
  ...['version', 'invocation', 'environment', 'path_policy', 'platform'].map(key => {
    const value: Data = executionContext()
    delete value[key]
    return value
  }),
  Object.assign(Object.create({ platform: 'win32' }), executionContext({ platform: undefined })),
].map((value, index) => [index, value] as const))('执行上下文畸形、缺字段或额外键不得作为可信事实：%s', (_, execution_context) => {
  mount([receipt({ execution_context })])
  const outer = open('实际命令记录')
  expect(outer.textContent).toContain('未作为执行依据')
  expect(outer.querySelector('details, pre, button')).toBeNull()
  expect(outer.textContent).not.toMatch(/精简环境|按 argv 直接启动|系统默认 PATH|有显式允许配置|不应显示/)
})

test('合法执行上下文不能绕过宿主身份、事件来源或回执必填校验', () => {
  const value = receipt({ execution_context: executionContext() })
  const view = render(<NativeCommandReceipts event={snapshot([value])} />)
  expect(view.getByText('实际命令记录')).toBeTruthy()
  for (const actor of [
    { id: 'team', kind: 'agent', role: 'team' }, { id: 'other', kind: 'system', role: 'team' },
    { id: 'team', kind: 'system', role: 'other' }, Object.create({ id: 'team', kind: 'system', role: 'team' }),
  ]) {
    view.rerender(<NativeCommandReceipts event={{ ...snapshot([value]), actor }} />)
    expect(view.container.childElementCount).toBe(0)
  }
  view.rerender(<NativeCommandReceipts event={{ ...snapshot([value]), type: 'msg.said' }} />)
  expect(view.container.childElementCount).toBe(0)
  for (const invalid of [receipt({ execution_context: executionContext(), source_seq: 40 }),
    receipt({ execution_context: executionContext(), started_seq: 30 }), receipt({ execution_context: executionContext(), unknown: true })]) {
    view.rerender(<NativeCommandReceipts event={snapshot([invalid])} />)
    expect(view.container.textContent).toContain('未作为执行依据')
    expect(view.container.textContent).not.toContain('精简环境')
  }
  delete value.environment
  view.rerender(<NativeCommandReceipts event={snapshot([value])} />)
  expect(view.container.textContent).toContain('未作为执行依据')
})

test('临时副本相对目录、历史比较和交付版本关联分别显示，不混为执行通过', () => {
  const value = receipt({ environment: 'temporary_artifact_copy_not_sandbox', cwd: '对照/项目', scope_current: true,
    artifacts_unchanged: true, artifact_check_valid: true, selected_versions_current: false,
    checked_artifacts: [artifact({ directory: '旧版' }), artifact({ seq: 6, content_sha256: 'd'.repeat(64), directory: '新版' })],
    delivery_artifact_seqs: [6], request: { command: '比较', artifact_versions: [{ artifact_seq: 5, directory: '旧版' }, { artifact_seq: 6, directory: '新版' }] } })
  mount([value], { evidence: snapshot([value], 40, [artifact({ seq: 6, content_sha256: 'd'.repeat(64) })]) })
  const body = openReceipt()
  open('执行环境与目录', body)
  open('版本与事件来源', body)
  expect(body.textContent).toContain('执行根为已清理的临时成果副本')
  expect(body.textContent).toContain('实际记录的工作目录（相对临时副本根）：对照/项目')
  expect(body.textContent).toContain('回执记录时范围有效')
  expect(body.textContent).toContain('确切版本检查：回执记录有效')
  expect(body.textContent).toContain('包含检查时的历史版本')
  const refs = within(body).getByLabelText('命令版本与事件来源').querySelectorAll('li')
  expect(refs[0].textContent).toContain('未确认与本次交付的关联')
  expect(refs[1].textContent).toContain('宿主记录与本次交付的序号、名称和字节指纹相符')
  expect(body.textContent).not.toContain('F:/授权工程/对照')
  expect(body.textContent).not.toMatch(/检查通过|执行成功/)
})

test.each([
  ['failed', 2, '执行失败'], ['timeout', null, '执行超时'], ['launch_error', null, '启动失败'],
  ['effects_unknown', null, '执行影响未知'], ['not_started', null, '未开始执行'], [null, null, '执行结果未知'],
] as const)('忠实展示失败和未知：%s', (outcome, exit_code, label) => {
  mount([receipt({ status: 'error', outcome, exit_code, complete: null })])
  const body = openReceipt()
  expect(body.querySelector('summary')!.textContent).toContain(label)
  expect(body.querySelector('summary')!.textContent).toContain(exit_code === null ? '退出码未知' : '退出码 2')
  expect(body.textContent).toContain('采集是否完成未知')
  expect(body.textContent).not.toMatch(/检查通过|执行成功|记录为正常结束/)
})

test.each([
  ['not_started', '未开始执行'], ['launch_error', '启动失败'],
] as const)('未启动的空程序参数保留原始数组，完整请求仍可复制：%s', async (outcome, label) => {
  const value = receipt({ argv: [], status: 'error', outcome, exit_code: null })
  const before = JSON.stringify(value)
  const writeText = clipboard()
  mount([value])
  const body = openReceipt()
  expect(body.querySelector('summary')!.textContent).toContain(label)
  expect(body.querySelector('summary')!.textContent).toContain('退出码未知')
  open('原环境实际程序参数', body)
  expect(within(body).getByLabelText('原环境实际程序参数正文').textContent).toBe('[]')
  expect(body.textContent).not.toContain('程序参数未记录或因预算省略')
  expect(body.textContent).not.toMatch(/已启动|记录为正常结束|检查通过|执行成功/)
  open('实际工具请求', body)
  fireEvent.click(within(body).getByRole('button', { name: '复制完整工具请求' }))
  await within(body).findByText('已复制完整工具请求')
  expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(value.request)
  expect(JSON.stringify(value)).toBe(before)
})

test.each([
  { argv: [], request_complete: true, stdin: null, canCopy: true },
  { argv: null, request_complete: true, stdin: null, canCopy: true },
  { argv: [], request_complete: false, stdin: null, canCopy: false },
  { argv: [], request_complete: true, stdin: { bytes: 0, sha256: sha }, canCopy: false },
])('空值与空数组分别展示，复制仅由完整请求和输入省略决定：%j', ({ canCopy, ...extra }) => {
  mount([receipt({ ...extra, status: 'error', outcome: 'not_started', exit_code: null })])
  const body = openReceipt()
  open('原环境实际程序参数', body)
  if (extra.argv === null) {
    expect(within(body).queryByLabelText('原环境实际程序参数正文')).toBeNull()
    expect(body.textContent).toContain('程序参数未记录或因预算省略')
  } else {
    expect(within(body).getByLabelText('原环境实际程序参数正文').textContent).toBe('[]')
    expect(body.textContent).not.toContain('程序参数未记录或因预算省略')
  }
  open('实际工具请求', body)
  expect(Boolean(within(body).queryByRole('button', { name: '复制完整工具请求' }))).toBe(canCopy)
  if (extra.stdin !== null) expect(body.textContent).toContain('配置输入：0 字节')
})

test('宿主剔除未知参数后的已知请求仍可读，完整性为假不归因于标准输入', () => {
  const knownRequest = { command: '检查', args: [], cwd: '' }
  const value = receipt({ request: knownRequest, request_complete: false, stdin: null, argv: [],
    status: 'error', outcome: 'not_started', exit_code: null })
  const before = JSON.stringify(value)
  mount([value])
  const body = openReceipt()
  const requestFold = open('实际工具请求', body)
  expect(JSON.parse(within(body).getByLabelText('实际工具请求正文').textContent!)).toEqual(knownRequest)
  expect(requestFold.textContent).toContain('请求包含省略项或完整性未知，不能完整复现')
  expect(requestFold.querySelector('button')).toBeNull()
  expect(requestFold.textContent).not.toMatch(/标准输入正文已省略|配置输入|env|PRIVATE|未知值正文/)
  expect(JSON.stringify(value)).toBe(before)
})

test('空值保持未知，矛盾的副本检查状态不能确认有效', () => {
  mount([receipt({ request: null, request_complete: false, argv: null, authorized_root: null, cwd: null,
    environment: 'temporary_artifact_copy_not_sandbox', outcome: null, exit_code: null, complete: null,
    stdout: null, stderr: output({ complete: null, utf8_valid: null, bytes: null, sha256: null }),
    checked_artifacts: [artifact()], artifact_check_valid: true })])
  const body = openReceipt()
  for (const label of ['执行环境与目录', '原环境实际程序参数', '已采集输出', '版本与事件来源']) open(label, body)
  for (const text of ['工程授权/锁根：未知', '执行结果未知', '退出码未知', '范围有效性未知', '有效性未知或字段不一致',
    '是否最新未知', '程序参数未记录或因预算省略', '标准输出采集是否完成未知', '输出流的 UTF-8 有效性未知']) expect(body.textContent).toContain(text)
  expect(body.textContent).not.toMatch(/确切版本检查：回执记录有效|无输出。|退出码 0/)
})

test('有成果引用但环境未知时不根据授权根或请求选择推断执行根', () => {
  mount([receipt({ checked_artifacts: [artifact()], delivery_artifact_seqs: [], request: { command: '检查', artifact_seqs: [5] },
    environment: null, scope_current: true, artifacts_unchanged: true, artifact_check_valid: true })])
  const body = openReceipt()
  open('执行环境与目录', body)
  open('版本与事件来源', body)
  expect(body.textContent).toContain('执行环境未知，不能确认实际副本根')
  expect(body.textContent).not.toContain('执行根为已清理的临时成果副本')
  expect(body.textContent).not.toContain('确切版本检查：回执记录有效')
  expect(body.textContent).toContain('未确认与本次交付的关联')
})

test('标准输出只有保存前缀，不捏造尾部；元数据缺失独立显示未知', () => {
  mount([receipt({ stdout: { excerpt: '保存的开头', truncated: true }, stderr: { bytes: 32, sha256: sha } })])
  const body = openReceipt()
  open('已采集输出', body)
  expect(within(body).getByLabelText('标准输出输出文本').textContent).toBe('保存的开头')
  expect(body.textContent).toContain('字节数未知')
  expect(body.textContent).toContain('预览已截断')
  expect(body.textContent).toContain('未保存输出正文，仅提供字节数与 SHA256')
  expect(body.textContent).toContain('标准输出采集是否完成未知')
  expect(body.textContent).not.toContain('末尾（已保存片段）')
})

test('命令失败、范围失效与副本改变独立显示，历史版本不自动判失败', () => {
  mount([receipt({ environment: 'temporary_artifact_copy_not_sandbox', status: 'error', outcome: 'failed', exit_code: 9,
    scope_current: false, artifacts_unchanged: false, artifact_check_valid: false, selected_versions_current: false,
    checked_artifacts: [artifact()], delivery_artifact_seqs: [] })])
  const body = openReceipt()
  open('版本与事件来源', body)
  for (const label of ['执行失败', '退出码 9', '回执范围已失效', '执行副本：记录为已改变', '确切版本检查：回执记录无效',
    '包含检查时的历史版本']) expect(body.textContent).toContain(label)
})

test.each([
  null, {}, [], '原始回执', { status: 'partial' },
  ...[{ version: 2 }, { source_seq: 40 }, { source_seq: 41 }, { source_seq: true }, { source_seq: 1.5 },
    { started_seq: 30 }, { started_seq: 0 }, { source_hash: null }, { source_hash: 'bad' }, { source_hash: 'A'.repeat(64) },
    { started_hash: [] }, { started_hash: 'bad' }, { role: {} }, { tool: 'browser_check' },
    { status: 'partial' }, { outcome: 'approved' }, { outcome: {} }, { exit_code: '0' }, { complete: 'true' },
    { scope_current: 'true' }, { artifacts_unchanged: 1 }, { artifact_check_valid: {} }, { selected_versions_current: [] },
    { request_complete: null }, { request: { command: '检查', env: { PRIVATE: '不应显示' } } }, { request: { stdin: '不应显示' } },
    { request: {} }, { request: { command: ['检查'] } }, { request: { command: '检查', args: [1] } }, { request: { command: '检查', timeout: NaN } },
    { request: { command: '检查', timeout: 0 } }, { request: { command: '检查', timeout: 86401 } },
    { request: { command: '检查', artifact_seqs: [true] } }, { request: { artifact_versions: [{ seq: 5 }] } },
    { stdin: { bytes: -1, sha256: sha } }, { stdin: { bytes: 1, sha256: 'bad' } }, { stdin: { bytes: 1, sha256: sha, body: '不应显示' } },
    { argv: ['原程序', {}] }, { argv: '不是数组' }, { authorized_root: {} }, { cwd: false }, { environment: {} },
    { environment: '未识别环境' }, { stdout: '原始字符串' }, { stdout: {} }, { stdout: output({ bytes: -1 }) }, { stdout: output({ sha256: 'bad' }) },
    { stdout: output({ complete: 'true' }) }, { stdout: output({ utf8_valid: 'true' }) }, { stdout: output({ tail_excerpt: '不应拼接' }) },
    { stderr: output({ excerpt: '长'.repeat(172) }) }, { checked_artifacts: [artifact({ seq: 30 })] },
    { checked_artifacts: [artifact({ content_sha256: 'bad' })] }, { checked_artifacts: [artifact({ filename: {} })] },
    { checked_artifacts: [artifact({ directory: null })] }, { checked_artifacts: [artifact(), artifact()] },
    { checked_artifacts: Array.from({ length: 9 }, (_, i) => artifact({ seq: i + 1 })) },
    { unknown: '不应显示' }, { request: { command: '长'.repeat(342) } },
    { argv: ['长'.repeat(342)] }, { cwd: '长'.repeat(342) },
  ].map(extra => receipt(extra)),
  ...['source_seq', 'request_complete', 'scope_current', 'stdout', 'delivery_artifact_seqs'].map(key => {
    const value = receipt()
    delete value[key]
    return value
  }),
  Object.assign(Object.create({ source_seq: 30 }), receipt({ source_seq: undefined })),
].map((value, index) => [index, value] as const))('严格拒绝未知、恶意和不完整记录 %s', (_, value) => {
  const { container } = mount([value])
  const outer = open('实际命令记录')
  expect(outer.textContent).toContain('未作为执行依据')
  expect(outer.querySelector('details, pre, button')).toBeNull()
  expect(container.querySelector('a, script, img, iframe')).toBeNull()
  expect(outer.textContent).not.toContain('不应显示')
})

test.each([null, {}, '回执', [receipt(), receipt()], Array.from({ length: 5 }, (_, i) => receipt({ source_seq: 30 + i, started_seq: 20 + i }))])('拒绝畸形容器、重复来源和超量，不截出看似完整记录：%j', records => {
  mount(records)
  const outer = open('实际命令记录')
  expect(outer.textContent).toContain('未作为执行依据')
  expect(outer.querySelector('details')).toBeNull()
})

test('非法项不污染相邻合法项，也不能通过原型继承伪造宿主', () => {
  const view = mount([receipt(), { status: 'partial' }])
  expect(fold('实际命令记录').querySelectorAll(':scope > div > details')).toHaveLength(1)
  expect(view.container.textContent).toContain('第 2 条命令记录不完整')
  const forged = { ...snapshot(), actor: Object.create({ id: 'team', kind: 'system', role: 'team' }) }
  view.rerender(<NativeCommandReceipts event={forged} />)
  expect(view.container.childElementCount).toBe(0)
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('仅HOST完成事件提供事实入口：%j', actor => {
  const view = mount(undefined, { evidence: { ...snapshot(), actor } })
  expect(view.queryByText('实际命令记录')).toBeNull()
})

test('普通发言的自报记录、旧无字段与空数组没有新空入口', () => {
  const view = mount(undefined, { evidence: { ...snapshot(), type: 'msg.said' }, details: ['原有说明'] })
  expect(screen.queryByText('实际命令记录')).toBeNull()
  view.rerender(<MessageDetails item={chat(undefined, { evidence: completion(40) })} />)
  expect(screen.queryByText('实际命令记录')).toBeNull()
  open('为什么这样做 · 如何运行')
  expect(screen.getByRole('heading', { name: '运行与复查' })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: '成员建议的运行与复查' })).toBeNull()
  view.rerender(<MessageDetails item={chat([])} />)
  expect(screen.queryByText('实际命令记录')).toBeNull()
})

test('宿主小写指纹保持原值，来源必须严格早于本次交付；不改写完整请求', () => {
  const value = receipt({ source_hash: '0123456789abcdef'.repeat(4), started_hash: 'fedcba9876543210'.repeat(4),
    request: { command: '检查', args: [''], cwd: '', artifact_seqs: [5] }, checked_artifacts: [artifact()], delivery_artifact_seqs: [5] })
  const before = JSON.stringify(value)
  const view = mount([value])
  const body = openReceipt()
  open('版本与事件来源', body)
  expect(body.textContent).toContain(`完成事件 #30 · 指纹：${'0123456789abcdef'.repeat(4)}`)
  expect(JSON.stringify(value)).toBe(before)
  view.rerender(<MessageDetails item={chat([value], { evidence: snapshot([value], 30) })} />)
  expect(fold('实际命令记录').textContent).toContain('未作为执行依据')
})

test('长行和HTML作为可选中、限高、换行的纯文本，不产生链接或执行入口', () => {
  const hostile = '<img src=x onerror=alert(1)>[链接](javascript:alert(1))$(命令);'
  const long = '长'.repeat(150)
  const value = receipt({ request: { command: long, args: [hostile] }, argv: [hostile, long],
    authorized_root: long, cwd: long, stdout: output({ excerpt: long, truncated: true }) })
  const view = mount([value])
  const body = openReceipt()
  for (const label of ['实际工具请求', '原环境实际程序参数', '已采集输出', '版本与事件来源']) open(label, body)
  expect(view.container.textContent).toContain(hostile)
  expect(body.querySelector('summary')!.textContent).toContain('命令名节选')
  expect(body.querySelector('summary')!.textContent!.length).toBeLessThan(120)
  for (const pre of body.querySelectorAll('pre')) {
    expect(pre.className).toContain('whitespace-pre-wrap')
    expect(pre.className).toContain('[overflow-wrap:anywhere]')
    expect(pre.getAttribute('tabindex')).toBe('0')
    expect(pre.closest('.max-h-48')).toBeTruthy()
  }
  expect(within(body).getByLabelText('命令版本与事件来源').className).toContain('max-h-64')
  expect(view.container.querySelector('a, img, script, iframe, form')).toBeNull()
  expect(screen.queryByRole('button', { name: /重跑|执行/ })).toBeNull()
})

test.each(['stdout', 'stderr'] as const)('有效 UTF-8 的 NUL 输出保留命令事实、原始指纹和请求复制：%s', async stream => {
  const raw = '开头\0中间\n\t结尾\r\n'
  const bytes = new TextEncoder().encode(raw)
  const digest = createHash('sha256').update(bytes).digest('hex')
  expect(new TextDecoder('utf-8', { fatal: true }).decode(bytes)).toBe(raw)
  const value = receipt({ [stream]: output({ excerpt: raw, bytes: bytes.length, sha256: digest, utf8_valid: true }) })
  const before = JSON.stringify(value)
  const writeText = clipboard()
  mount([value])
  const body = openReceipt()
  expect(body.textContent).toContain('完成事件 #30')
  expect(body.querySelector('summary')!.textContent).toContain('退出码 0')
  const outputs = open('已采集输出', body)
  const label = stream === 'stdout' ? '标准输出' : '错误输出'
  const group = within(outputs).getByRole('group', { name: label })
  expect(within(group).getByLabelText(`${label}输出文本`).textContent).toBe('开头\\u0000中间\n\t结尾\r\n')
  expect(outputs.textContent).toContain(`${label}：${bytes.length} 字节`)
  expect(outputs.textContent).toContain(`${label} SHA-256：${digest}`)
  expect(outputs.textContent).toContain(`${label}：部分控制字符以 Unicode 转义显示；字节数、SHA-256 和 UTF-8 有效性仍对应原始采集输出。`)
  expect(body.textContent).not.toMatch(/未作为执行依据|已采集输出含无效 UTF-8/)
  expect(body.textContent).not.toContain('\0')
  open('实际工具请求', body)
  fireEvent.click(within(body).getByRole('button', { name: '复制完整工具请求' }))
  await within(body).findByText('已复制完整工具请求')
  const copied = writeText.mock.calls[0][0]
  expect(JSON.parse(copied)).toEqual(value.request)
  expect(copied).not.toContain('\0')
  expect(copied).not.toContain('开头')
  expect(JSON.stringify(value)).toBe(before)
})

test('输出的其他 C0、DEL 与 C1 控制字符明确转义，不改变换行与 Tab', () => {
  const raw = '\u0001\b\u000b\f\u001b\u001f\u007f\u0085\u009b\u009f\r\n\t正文'
  const bytes = new TextEncoder().encode(raw)
  const digest = createHash('sha256').update(bytes).digest('hex')
  mount([receipt({ stdout: output({ excerpt: raw, bytes: bytes.length, sha256: digest }) })])
  const body = openReceipt()
  const outputs = open('已采集输出', body)
  expect(within(outputs).getByLabelText('标准输出输出文本').textContent)
    .toBe('\\u0001\\u0008\\u000b\\u000c\\u001b\\u001f\\u007f\\u0085\\u009b\\u009f\r\n\t正文')
  expect(outputs.textContent).toContain(`标准输出：${bytes.length} 字节`)
  expect(outputs.textContent).toContain(digest)
  expect(outputs.textContent).not.toContain('已采集输出含无效 UTF-8')
})

test('控制字符在原始 UTF-8 预算内校验，显示转义扩张不丢失合法摘要', () => {
  const raw = '\0'.repeat(512)
  const value = receipt({ stdout: output({ excerpt: raw, bytes: 512, truncated: true }) })
  const before = JSON.stringify(value)
  mount([value])
  const body = openReceipt()
  const outputs = open('已采集输出', body)
  expect(within(outputs).getByLabelText('标准输出输出文本').textContent).toBe('\\u0000'.repeat(512))
  expect(outputs.textContent).toContain('标准输出：512 字节')
  expect(outputs.textContent).toContain('预览已截断')
  expect(outputs.textContent).toContain('当前展示非完整原文')
  expect(JSON.stringify(value)).toBe(before)
})

test('多字节文字与 NUL 共同占用输出字节预算，超限仍拒绝', () => {
  const raw = `${'中'.repeat(170)}\0\u001f`
  expect(new TextEncoder().encode(raw)).toHaveLength(512)
  const view = mount([receipt({ stdout: output({ excerpt: raw, bytes: 512 }) })])
  const body = openReceipt()
  open('已采集输出', body)
  expect(within(body).getByLabelText('标准输出输出文本').textContent).toBe(`${'中'.repeat(170)}\\u0000\\u001f`)
  view.rerender(<MessageDetails item={chat([receipt({ stdout: output({ excerpt: `${raw}\0`, bytes: 513 }) })])} />)
  const outer = open('实际命令记录')
  expect(outer.textContent).toContain('未作为执行依据')
  expect(outer.querySelector('pre, button')).toBeNull()
})

test.each(['', '第一行\r\n\t第二行', '原文\\u0000'])('空输出、换行 Tab 和原有转义文本保持原样：%j', raw => {
  mount([receipt({ stdout: output({ excerpt: raw, bytes: new TextEncoder().encode(raw).length }) })])
  const outputs = open('已采集输出', openReceipt())
  const group = within(outputs).getByRole('group', { name: '标准输出' })
  if (raw === '') expect(group.textContent).toContain('无输出。')
  else expect(within(group).getByLabelText('标准输出输出文本').textContent).toBe(raw)
  expect(outputs.textContent).not.toContain('部分控制字符以 Unicode 转义显示')
})

test.each([
  { request: { command: '检查\0' } }, { request: { command: '检查', args: ['参数\0正文'] } },
  { argv: ['原程序\0'] }, { cwd: '目录\0' },
])('输出放开 NUL 不放宽请求、原环境参数或目录校验：%j', extra => {
  const view = mount([receipt({ ...extra, stdout: output({ excerpt: '合法输出\0', utf8_valid: true }) })])
  const outer = open('实际命令记录')
  expect(outer.textContent).toContain('未作为执行依据')
  expect(outer.querySelector('details, pre, button')).toBeNull()
  expect(view.container.textContent).not.toContain('\0')
})

test.each([
  ['检查指纹被改错', { checked_artifacts: [artifact({ content_sha256: 'b'.repeat(64) })] }, [artifact()]],
  ['检查名称被改错', { checked_artifacts: [artifact({ filename: '另一个.zip' })] }, [artifact()]],
  ['交付指纹不符', {}, [artifact({ content_sha256: 'b'.repeat(64) })]],
  ['交付名称不符', {}, [artifact({ filename: '另一个.zip' })]],
  ['交付序号不符', {}, [artifact({ seq: 6 })]],
  ['交付清单未知', {}, null], ['交付清单为空', {}, []], ['交付清单形状非法', {}, {}],
  ['旧版仅有序号', {}, [5]], ['交付指纹缺失', {}, [{ seq: 5, filename: '工程.zip' }]],
  ['交付名称缺失', {}, [{ seq: 5, content_sha256: sha }]],
  ['交付指纹类型非法', {}, [artifact({ content_sha256: null })]],
  ['同序号交付引用矛盾', {}, [artifact(), artifact({ content_sha256: 'b'.repeat(64) })]],
  ['同序号交付引用重复', {}, [artifact(), artifact()]],
  ['宿主未声明关联', { delivery_artifact_seqs: [] }, [artifact()]],
] as const)('关联不完整或矛盾时只降级关联，不隐藏命令事实：%s', (_, extra, deliveryArtifacts) => {
  const value = receipt({ checked_artifacts: [artifact()], delivery_artifact_seqs: [5], ...extra })
  const evidence = snapshot([value], 40, deliveryArtifacts)
  const before = JSON.stringify(evidence)
  mount([value], { evidence })
  const body = openReceipt()
  open('实际工具请求', body)
  open('已采集输出', body)
  const versions = open('版本与事件来源', body)
  expect(versions.textContent).toContain('未确认与本次交付的关联')
  expect(versions.textContent).not.toContain('宿主记录与本次交付的序号、名称和字节指纹相符')
  expect(body.textContent).toContain('完成事件 #30')
  expect(body.textContent).not.toContain('未作为执行依据')
  expect(within(body).getByRole('button', { name: '复制完整工具请求' })).toBeTruthy()
  expect(within(body).getByLabelText('标准输出输出文本').textContent).toBe('已记录')
  expect(JSON.stringify(evidence)).toBe(before)
})

test('交付 artifacts 字段缺失时不确认关联，仍保留来源及输出', () => {
  const value = receipt({ checked_artifacts: [artifact()], delivery_artifact_seqs: [5] })
  const evidence = snapshot([value])
  delete evidence.payload.artifacts
  mount([value], { evidence })
  const body = openReceipt()
  const versions = open('版本与事件来源', body)
  expect(versions.textContent).toContain('未确认与本次交付的关联')
  expect(versions.textContent).not.toContain('字节指纹相符')
  expect(body.textContent).toContain('回执采集完整')
})

test('只有交付三元组精确相等且宿主已声明时确认关联，挂载目录不替代身份', () => {
  const value = receipt({ checked_artifacts: [artifact({ directory: '执行目录' })], delivery_artifact_seqs: [5] })
  mount([value], { evidence: snapshot([value], 40, [artifact()]) })
  const body = openReceipt()
  const versions = open('版本与事件来源', body)
  expect(versions.textContent).toContain('宿主记录与本次交付的序号、名称和字节指纹相符')
  expect(versions.textContent).not.toContain('未确认与本次交付的关联')
  expect(versions.textContent).toContain('副本挂载目录：执行目录')
})

test('一份检查记录的身份不符不影响其他记录的独立关联与命令事实', () => {
  const value = receipt({ checked_artifacts: [artifact({ content_sha256: 'b'.repeat(64) }),
    artifact({ seq: 6, filename: '第二份.zip', content_sha256: 'd'.repeat(64) })], delivery_artifact_seqs: [5, 6] })
  const delivery = [artifact(), artifact({ seq: 6, filename: '第二份.zip', content_sha256: 'd'.repeat(64) })]
  mount([value], { evidence: snapshot([value], 40, delivery) })
  const body = openReceipt()
  const versions = open('版本与事件来源', body)
  const items = versions.querySelectorAll('li')
  expect(items[0].textContent).toContain('未确认与本次交付的关联')
  expect(items[1].textContent).toContain('宿主记录与本次交付的序号、名称和字节指纹相符')
  expect(body.textContent).not.toContain('未作为执行依据')
})

test.each([null, [artifact({ seq: 6 })]])('宿主声明缺少对应的完整检查引用时，仅关联未知：%j', checked_artifacts => {
  const value = receipt({ checked_artifacts, delivery_artifact_seqs: [5] })
  mount([value], { evidence: snapshot([value], 40, [artifact()]) })
  const body = openReceipt()
  const versions = open('版本与事件来源', body)
  expect(versions.textContent).toContain('未确认与本次交付的关联')
  expect(versions.textContent).not.toContain('字节指纹相符')
  expect(body.textContent).toContain('完成事件 #30')
  expect(body.textContent).not.toContain('未作为执行依据')
})
