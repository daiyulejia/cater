import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { EventDetails } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { NativeMcpReceipt } from '../src/NativeMcpReceipt'
import { conversationActivity, nativeMcpReceiptStatus } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', role: 'team', kind: 'system' }
const sha = 'a'.repeat(64)
const hostile = '<script>alert(1)</script>\n<img src=x>\n[外链](javascript:alert(1))\nhttps://example.invalid'
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, payload, actor })
const start = (name = 'call_mcp_tool', args = {}) => event(10, 'team.tool_started', {
  name, role: 'dev', run_id: 'run', message_seq: 1, call_id: 'mcp',
  arguments: { ...(name === 'call_mcp_tool' ? { server_id: 'notes', tool_name: 'read_note', schema_sha256: sha } : {}), ...args },
})
const returned = (extra = {}): Record<string, unknown> => ({
  status: 'ok', complete: true, outcome: 'returned', server_id: 'notes', server_title: '本地记录', tool_name: 'read_note',
  effect: 'read', invocation_id: '10:mcp', configuration_current: true,
  mcp_result: { content: [{ type: 'text', text: hostile }], structuredContent: { value: '结构化内容', nested: { value: 42 } } }, ...extra,
})
const servers = (extra = {}): Record<string, unknown> => ({ status: 'ok', complete: true, operation: 'servers',
  servers: [{ id: 'notes', title: '本地记录', description: hostile, tool_count: 2 }],
  configuration_status: 'ok', offset: 0, total: 2, next_offset: 1, has_more: true, ...extra,
})
const directory = (extra = {}): Record<string, unknown> => ({ status: 'ok', complete: true, operation: 'list', server_id: 'notes', server_title: '本地记录',
  configuration_current: true, tools: [{ name: 'read_note', title: '读取记录', description: hostile, effect: 'read',
    schema_sha256: sha, input_schema: { type: 'object', properties: { note: { type: 'string' } } } }],
  offset: 0, total: 1, next_offset: 1, has_more: false, ...extra,
})
const finish = (result: unknown = returned(), started = start(), actor = host) => event(11, 'team.tool_finished', {
  ...started.payload, started_seq: 10, status: 'ok', result,
}, actor)
const team: TeamState = { enabled: true, objective: '核对工具', status: 'running', revision: 0, runs: [], assignments: [], pending_count: 0, checkpoint: '' }
const activity = (events: TeamEvent[], executionState = 'running') => conversationActivity({ team, events: [
  event(1, 'team.message_received', { recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
  event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), ...events,
], names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState })
const surfaces = (value: unknown, started = start()) => {
  const receipt = finish(value, started)
  return [render(<TeamToolProgress events={[started, receipt]} names={{ dev: '开发' }} full />), render(<EventDetails event={receipt} />)]
}
const expand = (container: HTMLElement, label: string) => {
  const details = within(container).getByText(label).closest('details')!
  expect(details.open).toBe(false)
  details.open = true
  fireEvent(details, new Event('toggle'))
  return details
}

test('服务目录仅为宿主元数据，沿用折叠记录与分页，不背书连接或执行', () => {
  const started = start('list_mcp_tools')
  for (const view of surfaces(servers(), started)) {
    const content = view.container.textContent ?? ''
    for (const expected of ['查看外部工具目录', '宿主服务目录已读取（未连接）', '本地记录', '配置工具数：2', '未连接服务', '未调用其中工具',
      '本页起点：0', '目录共 2 项', '下一页起点 1', hostile]) expect(content).toContain(expected)
    expect(content).not.toMatch(/工具已完成|工具通过|开发者详情|原始事件 JSON|list_mcp_tools/)
    expect(view.container.querySelector('a, img, script, video, audio, iframe')).toBeNull()
  }
  const progress = render(<TeamToolProgress events={[started, finish(servers(), started)]} names={{}} />)
  expect(progress.container.querySelector('details')?.open).toBe(false)
  expect(progress.container.textContent).toContain('工具过程 · 1 项')
  const line = activity([started, finish(servers(), started)]).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('查看外部工具目录 · 宿主服务目录已读取（未连接）')
  expect(line.warning).toBe(false)
})

test('真实工具目录显示宿主effect和版本，输入结构按需展开', () => {
  const started = start('list_mcp_tools', { server_id: 'notes' })
  for (const view of surfaces(directory(), started)) {
    for (const expected of ['工具目录已读取', '读取记录', 'read_note', sha, '宿主授权范围：读取', '已到末页。']) expect(view.container.textContent).toContain(expected)
    expect(view.container.querySelector('pre')).toBeNull()
    const details = expand(view.container, '输入结构 · JSON 数据')
    expect(details.textContent).toContain('"properties"')
    expect(details.textContent).toContain('"note"')
    expect(view.container.querySelector('a, img, script')).toBeNull()
  }
  expect(activity([started, finish(directory(), started)]).lines.find(line => line.role === 'dev')?.warning).toBe(false)
})

test('调用纯文本原样显示，结构化结果默认不挂载JSON，无执行或外链节点', () => {
  for (const view of surfaces(returned())) {
    for (const expected of ['调用外部工具', '调用已返回', '调用标识：10:mcp', '宿主授权范围：读取', '不代表账号已连通、应用验证或任务验收通过']) expect(view.container.textContent).toContain(expected)
    expect(view.container.querySelector('pre')?.textContent).toBe(hostile)
    expect(view.container.textContent).not.toContain('结构化内容')
    const details = expand(view.container, '结构化结果 · JSON 数据')
    expect(details.textContent).toContain('结构化内容')
    expect(view.container.querySelector('a, img, script, video, audio, iframe, button')).toBeNull()
    expect(view.container.textContent).not.toMatch(/工具已完成|检查通过|原始事件 JSON|开发者详情/)
  }
  const view = activity([start(), finish()])
  expect(view.lines.filter(line => line.text.includes('调用外部工具'))).toHaveLength(1)
  expect(view.lines.find(line => line.role === 'dev')?.warning).toBe(false)
})

test.each([
  [{ status: 'error', complete: true, outcome: 'not_started', code: 'mcp_unavailable', message: '尚未授权，未启动服务。' }, '未开始调用'],
  [returned({ status: 'error', outcome: 'tool_error', effect: 'write', mcp_result: { content: [{ type: 'text', text: '写入后返回错误' }], isError: true } }), '调用已结束，工具报错'],
  [returned({ status: 'error', complete: false, outcome: 'effects_unknown', mcp_result: undefined }), '调用结果及实际影响待核对'],
])('拒绝、terminal错误、未知采用不同中文状态，不被外层ok覆盖：%s', (value, expected) => {
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain(expected)
    expect(view.container.textContent).not.toMatch(/工具已完成|检查通过|调用已返回/)
    if (expected === '调用已结束，工具报错') {
      expect(view.container.textContent).toContain('不据此承诺没有副作用')
      expect(view.container.textContent).not.toContain('未取得可信终态')
    }
    if (expected === '调用结果及实际影响待核对') expect(view.container.textContent).toContain('不能确认调用是否结束或实际影响')
  }
  const line = activity([start(), finish(value)]).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain(expected)
  expect(line.warning).toBe(true)
})

test.each(['returned', 'tool_error'])('terminal %s但表示不完整时仍记录调用结束，不冒充unknown', outcome => {
  const value = returned({ status: outcome === 'tool_error' ? 'error' : 'ok', outcome, representation_complete: false,
    unsupported_content_types: ['image', 'resource_link'], result_bytes: 90000, result_sha256: sha, result_excerpt: '有界摘要',
    mcp_result: { content: [{ type: 'text', text: '保留的正文' }], isError: outcome === 'tool_error' } })
  for (const view of surfaces(value)) {
    for (const expected of ['结果表示不完整', '90000 字节', sha, 'image、resource_link', '保留的正文', '有界摘要']) expect(view.container.textContent).toContain(expected)
    expect(view.container.textContent).not.toMatch(/未取得可信终态|调用结果及实际影响待核对|回执内容不完整|工具已完成/)
    expect(view.container.querySelector('a, img, video, audio')).toBeNull()
  }
  expect(activity([start(), finish(value)]).lines.find(line => line.role === 'dev')?.text).toContain('结果表示不完整')
})

test('异常Unicode的escaped_json只按需显示结果数据，内部配置不入UI', () => {
  const original = returned({ mcp_result: { content: [{ type: 'text', text: '\ud800' }], structuredContent: { value: '安全结构' } },
    command: ['INTERNAL_COMMAND'], cwd: 'INTERNAL_CWD', env: { PRIVATE_TOKEN: 'SECRET_ENV' } })
  const value = returned({ mcp_result: undefined, representation_complete: false, result_encoding: 'escaped_json', result_json: JSON.stringify(original) })
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain('调用已返回（结果表示不完整）')
    expect(view.container.querySelector('pre')).toBeNull()
    expand(view.container, '转义结果片段 · JSON 数据')
    expect(view.container.textContent).toContain('\\ud800')
    expect(view.container.textContent).toContain('安全结构')
    expect(view.container.textContent).not.toMatch(/INTERNAL_|PRIVATE_TOKEN|SECRET_ENV|result_json|原始事件 JSON/)
  }
})

test('只投影允许字段，原始参数、执行配置及额外字段均不泄露', () => {
  const started = start('call_mcp_tool', { command: 'INTERNAL_COMMAND', cwd: 'INTERNAL_CWD', env: { PRIVATE_TOKEN: 'SECRET_ENV' }, arguments: { secret: 'SECRET_ARG' } })
  const value = returned({ command: 'INTERNAL_COMMAND', cwd: 'INTERNAL_CWD', env: { PRIVATE_TOKEN: 'SECRET_ENV' }, note: 'PRIVATE_NOTE',
    stdout: 'PRIVATE_STDOUT', private: 'PRIVATE_RESULT',
    mcp_result: { content: [{ type: 'text', text: '正文' }], structuredContent: { value: 1, command: 'INTERNAL_COMMAND', cwd: 'INTERNAL_CWD', env: { PRIVATE_TOKEN: 'SECRET_ENV' } } } })
  for (const view of surfaces(value, started)) {
    expand(view.container, '结构化结果 · JSON 数据')
    expect(view.container.textContent).not.toMatch(/INTERNAL_|PRIVATE_|SECRET_|开发者详情|原始事件 JSON/)
  }
  const audit = render(<EventDetails event={started} />)
  expect(audit.container.textContent).not.toMatch(/INTERNAL_|PRIVATE_|SECRET_|开发者详情|原始事件 JSON/)
})

const malformed: [string, unknown][] = [
  ['空值', null], ['数组', []], ['字符串', 'ok'], ['空对象', {}],
  ...['complete', 'outcome', 'server_id', 'tool_name', 'effect', 'invocation_id', 'configuration_current', 'mcp_result'].map((key): [string, unknown] => [
    `缺${key}`, returned({ [key]: undefined }),
  ]),
  ['complete类型错误', returned({ complete: 'true' })], ['配置类型错误', returned({ configuration_current: 'true' })],
  ['内容坏shape', returned({ mcp_result: { content: [null, { type: 'image', data: 'PRIVATE_IMAGE' }] } })],
  ['isError矛盾', returned({ mcp_result: { content: [], isError: true } })],
  ['isError类型错误', returned({ mcp_result: { content: [], isError: 'false' } })],
  ['terminal outcome矛盾', returned({ status: 'error' })], ['unknown complete矛盾', returned({ status: 'error', complete: true, outcome: 'effects_unknown' })],
  ['不完整表示缺证据', returned({ representation_complete: false })],
  ['不完整表示缺结果', returned({ representation_complete: false, mcp_result: undefined,
    result_bytes: 90000, result_sha256: sha, result_excerpt: '摘要不能代替回执' })],
  ['不完整表示错误缺isError', returned({ status: 'error', outcome: 'tool_error', representation_complete: false,
    mcp_result: { content: [] }, result_bytes: 90000, result_sha256: sha, result_excerpt: '摘要不能代替错误标记' })],
  ['转义坏JSON', returned({ representation_complete: false, result_encoding: 'escaped_json', result_json: '{' })],
  ['转义终态矛盾', returned({ representation_complete: false, result_encoding: 'escaped_json', result_json: '{"complete":false}' })],
  ['转义缺结果', returned({ mcp_result: undefined, representation_complete: false, result_encoding: 'escaped_json', result_json: JSON.stringify(returned({ mcp_result: undefined })) })],
  ['转义身份矛盾', returned({ mcp_result: undefined, representation_complete: false, result_encoding: 'escaped_json', result_json: JSON.stringify(returned({ server_id: 'other' })) })],
  ['返回服务不一致', returned({ server_id: 'other' })], ['返回工具不一致', returned({ tool_name: 'other' })],
  ['调用标识不一致', returned({ invocation_id: 'other-call' })],
]
test.each(malformed)('旧或坏shape谨慎降级，不抛错或声称返回：%s', (_name, value) => {
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain('外部工具结果待确认')
    expect(view.container.textContent).not.toMatch(/调用已返回|工具已完成|\[object Object\]|PRIVATE_IMAGE/)
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(activity([start(), finish(value)]).lines.find(line => line.role === 'dev')?.warning).toBe(true)
})

test.each([
  servers({ servers: [null] }), servers({ next_offset: 2 }), servers({ has_more: false }), servers({ configuration_status: undefined }),
  directory({ tools: [null] }), directory({ tools: [{ name: 'read_note', input_schema: [], effect: 'read', schema_sha256: sha }] }),
  directory({ tools: [{ name: 'read_note', input_schema: {}, effect: 'admin', schema_sha256: sha }] }),
  directory({ tools: [{ name: 'read_note', input_schema: {}, effect: 'read', schema_sha256: 'bad' }] }),
])('坏目录不背书metadata或工具定义：%j', value => {
  for (const view of surfaces(value, start('list_mcp_tools'))) {
    expect(view.container.textContent).toContain('外部工具结果待确认')
    expect(view.container.querySelector('[aria-label="外部工具目录"]')).toBeNull()
  }
})

test.each([
  { id: 'team', role: 'team', kind: 'agent' }, { id: 'team', role: 'team', kind: 'human' },
  { id: 'other', role: 'team', kind: 'system' }, { id: 'team', role: 'other', kind: 'system' },
])('伪造宿主不能渲染结果或更改当前角色活动：%j', actor => {
  const receipt = finish(returned(), start(), actor)
  const progress = render(<TeamToolProgress events={[start(), receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('已发起，等待回执')
  expect(progress.container.textContent).not.toContain(hostile)
  const audit = render(<EventDetails event={receipt} />)
  expect(audit.container.textContent).toContain('外部工具回执来源待核对')
  expect(audit.container.textContent).not.toMatch(/alert|结构化结果|原始事件 JSON/)
  const direct = render(<NativeMcpReceipt event={receipt} />)
  expect(direct.container.querySelector('pre')).toBeNull()
  expect(activity([start(), receipt]).lines.find(line => line.role === 'dev')?.text).toContain('已发起，等待回执')
  const fakeStart = { ...start(), actor }
  expect(render(<TeamToolProgress events={[fakeStart, receipt]} names={{}} />).container.textContent).toBe('')
})

test('历史配置与暂停不升级为当前授权、答复或交付，不产生额外活动气泡', () => {
  const value = returned({ configuration_current: false })
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain('历史回执：调用已返回')
    expect(view.container.textContent).toContain('配置已变化，此历史回执不授权新的动作')
  }
  const paused = activity([start(), finish(value)], 'paused')
  expect(paused.notice).toContain('已暂停后续执行')
  expect(paused.lines.find(line => line.role === 'dev')?.moving).toBe(false)
  expect(paused.lines.find(line => line.role === 'dev')?.warning).toBe(true)
  expect(paused.lines.filter(line => line.text.includes('调用外部工具'))).toHaveLength(1)
  expect(paused.message).toContain('答复待确认')
  expect(paused.nextCheckAt).toBeUndefined()
})

test('仅开始、独立回执、省略结束工具名均沿用原过程配对', () => {
  const started = start(), receipt = finish()
  for (const view of [render(<TeamToolProgress events={[started]} names={{}} />), render(<EventDetails event={started} />)]) {
    expect(view.container.textContent).toContain('调用外部工具')
    expect(view.container.textContent).toContain('已发起，等待回执')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(render(<TeamToolProgress events={[receipt]} names={{}} />).container.textContent).toContain('调用已返回')
  delete receipt.payload.name
  delete receipt.payload.arguments
  const paired = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(paired.container.textContent).toContain('工具过程 · 1 项')
  expect(paired.container.textContent).toContain('调用已返回')
  expect(activity([started, receipt]).lines.find(line => line.role === 'dev')?.text).toContain('调用外部工具 · 调用已返回')
})

test('旧tool别名仍投影到当前角色，拒绝结果夹带的工具内容不展示', () => {
  const started = start()
  started.payload.tool = started.payload.name
  delete started.payload.name
  const receipt = finish(returned(), started)
  for (const view of [render(<TeamToolProgress events={[started, receipt]} names={{}} />), render(<EventDetails event={receipt} />)]) {
    expect(view.container.textContent).toContain('调用已返回')
  }
  expect(activity([started, receipt]).lines.find(line => line.role === 'dev')?.text).toContain('调用外部工具 · 调用已返回')
  for (const view of surfaces(directory({ status: 'error', outcome: 'not_started' }), start('list_mcp_tools'))) {
    expect(view.container.textContent).toContain('未开始读取目录')
    expect(view.container.querySelector('[aria-label="外部工具目录"]')).toBeNull()
  }
})

test('长正文和结构化结果受界限约束，full也不无限展开', () => {
  const value = returned({ mcp_result: { content: Array.from({ length: 30 }, () => ({ type: 'text', text: '长'.repeat(12000) })),
    structuredContent: { data: '大'.repeat(30000) } } })
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain('显示已截断')
    expect(view.container.textContent!.length).toBeLessThan(11000)
    expect(view.container.querySelector('pre')?.className).toContain('[overflow-wrap:anywhere]')
    expand(view.container, '结构化结果 · JSON 数据')
    expect(view.container.textContent!.length).toBeLessThan(14000)
  }
})

test('目录配置不可用、目录读取失败和纯拒绝不冒充调用未知', () => {
  const started = start('list_mcp_tools')
  for (const [value, expected] of [
    [servers({ servers: [], total: 0, next_offset: 0, has_more: false, configuration_status: 'unavailable' }), '宿主服务配置不可用（未连接）'],
    [{ status: 'error', complete: true, outcome: 'discovery_failed' }, '工具目录读取失败'],
    [{ status: 'error', complete: true, outcome: 'not_started' }, '未开始读取目录'],
  ] as const) {
    expect(nativeMcpReceiptStatus(finish(value, started))).toBe(expected)
    expect(activity([started, finish(value, started)]).lines.find(line => line.role === 'dev')?.warning).toBe(true)
  }
})

test('普通工具仍保留原回执与开发者详情', () => {
  const started = start('workspace_read'), receipt = finish({ status: 'ok', content: '原有文件正文' }, started)
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('读取文件 · 工具已完成')
  expect(progress.container.textContent).toContain('原有文件正文')
  const audit = render(<EventDetails event={receipt} />)
  expect(within(audit.container).getByText('开发者详情').closest('details')?.open).toBe(false)
})

test('App仅在当前角色活动和折叠工具过程展示MCP，不向聊天插入回执气泡', async () => {
  window.sessionStorage.clear()
  const leader = { id: 'manager', name: '店长', role: 'leader' }, worker = { id: 'dev', name: '开发', role: 'dev' }
  const entries = [event(1, 'team.message_received', { text: '核对外部记录', recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
    event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), start(), finish(returned({ mcp_result: { content: [{ type: 'text', text: '唯一回执正文' }] } }))]
  const detail: TaskDetail = { task: { id: 'mcp-ui', goal: '核对工具', status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [], team },
    team, events: entries.map(entry => ({ ...entry, category: 'lifecycle' })), activity: { active: true, state: 'running' }, review: {} }
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method && init.method !== 'GET') throw new Error('Unexpected fixture mutation')
    if (url === '/api/organization') return json([leader, worker])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [worker] })
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'mcp-ui', goal: '核对工具', status: 'running', version: 11, latest_seq: 11, conversation_count: 1, team }] })
    if (url === '/api/tasks/mcp-ui') return json(detail)
    throw new Error(`Unexpected fixture request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  const view = render(<App />)
  const progress = await within(view.container).findByRole('region', { name: '当前工作进展' })
  expect(within(progress).getAllByText(/调用外部工具 · 调用已返回/)).toHaveLength(1)
  const chat = within(view.container).getByLabelText('聊天记录')
  expect(chat.textContent).not.toMatch(/调用外部工具|调用已返回|唯一回执正文/)
  fireEvent.click(within(progress).getByRole('button', { name: '查看过程' }))
  const dialog = await within(view.container.ownerDocument.body).findByRole('dialog', { name: '成果与过程' })
  await waitFor(() => expect(dialog.textContent).toContain('唯一回执正文'))
  expect(within(dialog).getByText('工具过程 · 1 项').closest('details')?.open).toBe(false)
  expect(chat.textContent).not.toContain('唯一回执正文')
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})
