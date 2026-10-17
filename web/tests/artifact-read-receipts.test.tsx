import { fireEvent, render, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { EventDetails } from '../src/EventDetails'
import { NativeArtifactReceipt } from '../src/NativeArtifactReceipt'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity, nativeArtifactReceiptStatus } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const sha = 'a'.repeat(64)
const hostile = '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[链接](javascript:alert(1))\nhttps://example.invalid'
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host) => ({ seq, type, payload, actor })
const start = (args = {}) => event(10, 'team.tool_started', { name: 'read_artifact', role: 'dev', run_id: 'run', message_seq: 1,
  call_id: 'artifact', arguments: { artifact_seq: 4, ...args } })
const legacy = (content = hostile, extra = {}): Record<string, unknown> => ({ status: 'ok', seq: 4, filename: '成果.txt', content,
  content_sha256: sha, content_bytes: new TextEncoder().encode(content).length, ...extra })
const page = (full = '甲😀乙🚀丙', offset = 0, length = 2, extra = {}): Record<string, unknown> => {
  const chars = Array.from(full), content = chars.slice(offset, offset + length).join(''), end = offset + Array.from(content).length
  return legacy(content, { content_bytes: new TextEncoder().encode(full).length, read_range: {
    unit: 'unicode_codepoints', offset, returned_chars: end - offset, total_chars: chars.length,
    has_more: end < chars.length, next_offset: end < chars.length ? end : null, content_complete: offset === 0 && end === chars.length,
  }, ...extra })
}
const finish = (result: unknown = page(), started = start({ length: 2 }), actor = host) => event(11, 'team.tool_finished', {
  ...started.payload, started_seq: started.seq, status: 'ok', result,
}, actor)
const surfaces = (value: unknown, started = start({ length: 2 })) => [
  render(<TeamToolProgress events={[started, finish(value, started)]} names={{ dev: '开发' }} full />),
  render(<EventDetails event={finish(value, started)} />),
]
const team: TeamState = { enabled: true, objective: '当前目标', status: 'running', revision: 0, runs: [], assignments: [], pending_count: 0, checkpoint: '' }
const activityEvents = (receipt = finish(), started = start({ length: 2 })) => [
  event(1, 'team.message_received', { text: '读取成果', recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
  event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), started, receipt,
]
const activity = (events: TeamEvent[], executionState = 'running') => conversationActivity({ team, events, names: { dev: '开发' },
  leaderId: 'manager', sending: false, executionState })

test('双入口显示确切版本与码点范围，正文和全文指纹默认折叠且可达', () => {
  for (const view of surfaces(page())) {
    expect(view.container.textContent).toContain('成果版本 #4 · 成果.txt')
    expect(view.container.textContent).toContain('本页起点：0 · 本页 2 字符 · 全文共 5 字符')
    expect(view.container.textContent).toContain('下一页起点 2')
    expect(view.container.textContent).toContain('本页未覆盖全文')
    expect(view.container.textContent).toContain('读取不代表复核通过、验证完成或任务交付')
    expect(view.container.textContent?.split('成果本页已读取')).toHaveLength(2)
    const body = within(view.container).getByText('成果正文 · 本页纯文本').closest('details')!
    const fingerprint = within(view.container).getByText('全文指纹与大小').closest('details')!
    expect(body.open).toBe(false)
    expect(fingerprint.open).toBe(false)
    fireEvent.click(within(body).getByText('成果正文 · 本页纯文本'))
    expect(body.open).toBe(true)
    const pre = within(body).getByLabelText('成果读取正文')
    expect(pre.textContent).toBe('甲😀')
    expect(pre.tabIndex).toBe(0)
    expect(pre.className).toContain('max-h-64')
    expect(pre.className).toContain('overflow-auto')
    expect(pre.className).toContain('[overflow-wrap:anywhere]')
    fireEvent.click(within(fingerprint).getByText('全文指纹与大小'))
    expect(fingerprint.open).toBe(true)
    expect(fingerprint.textContent).toContain(sha)
    expect(fingerprint.textContent).toContain('17 字节')
    expect(fingerprint.textContent).toContain('属于完整原文，不是本页')
    expect(view.container.textContent).not.toMatch(/工具已完成|开发者详情|原始事件 JSON/)
  }
})

test.each([3, 5])('尾页和EOF空页不声称全文已读，offset=%i', offset => {
  for (const view of surfaces(page(undefined, offset, 2), start({ offset, length: 2 }))) {
    expect(view.container.textContent).toContain(`本页起点：${offset}`)
    expect(view.container.textContent).toContain('已到末页，无下一页')
    expect(view.container.textContent).toContain('本页未覆盖全文；末页不代表已读取全文')
    expect(view.container.textContent).not.toContain('本页覆盖全文')
    expect(view.container.textContent).not.toContain('下一页起点')
  }
})

test.each(['', '😀🚀'])('从0开始完整覆盖才标记本页覆盖全文：%j', content => {
  for (const view of surfaces(page(content, 0, 8000), start({ length: 8000 }))) {
    expect(view.container.textContent).toContain('成果正文已读取')
    expect(view.container.textContent).toContain('本页覆盖全文')
    expect(view.container.textContent).not.toContain('本页未覆盖全文')
  }
})

test('分页默认值、最大页与旧完整正文兼容，显示截断不伪造下一页', () => {
  for (const [result, started] of [
    [page('字'.repeat(9000), 1, 8000), start({ offset: 1 })],
    [page('😀'.repeat(16001), 0, 16000), start({ length: 16000 })],
    [page('甲😀', 0, 8000), start({ length: 8000 })],
  ] as const) for (const view of surfaces(result, started)) expect(view.container.querySelector('pre')).not.toBeNull()
  for (const content of ['', '😀🚀', 'x'.repeat(17000)]) for (const view of surfaces(legacy(content), start())) {
    expect(view.container.textContent).toContain(`返回 ${Array.from(content).length} 字符`)
    expect(view.container.textContent).toContain('未分页正文回执')
    expect(view.container.textContent).not.toContain('下一页起点')
    expect(Array.from(view.container.querySelector('pre')!.textContent!).length).toBeLessThanOrEqual(16000)
    if (content.length > 16000) expect(view.container.textContent).toContain('显示已截断')
  }
})

test('旧metadata-only不伪造正文、范围或完整，缺指纹如实显示', () => {
  for (const value of [legacy('', { content: undefined }), { status: 'ok', seq: 4, filename: '旧文件.txt' },
    { status: 'ok', seq: 4, title: '历史标题<script>literal</script>', summary: 'PRIVATE_SUMMARY', uri: 'javascript:PRIVATE_URI' }]) {
    const result: Record<string, unknown> = { ...value }
    delete result.content
    for (const view of surfaces(result, start())) {
      expect(view.container.textContent).toContain('正文不可读')
      expect(view.container.textContent).toContain('未保存可读取正文')
      expect(view.container.textContent).not.toMatch(/覆盖全文|本页起点|成果正文已读取/)
      expect(view.container.querySelector('pre')).toBeNull()
      if (result.filename === undefined) {
        expect(view.container.textContent).toContain('成果标题：历史标题<script>literal</script>')
        expect(view.container.textContent).toContain('文件名未提供')
        expect(view.container.textContent).toContain('全文 SHA-256：未提供')
        expect(view.container.textContent).toContain('全文大小：未提供')
        expect(view.container.textContent).not.toMatch(/PRIVATE_|成果.txt/)
        expect(view.container.querySelector('a, script')).toBeNull()
      }
    }
  }
  const bare = render(<NativeArtifactReceipt event={finish({ status: 'ok', seq: 4 }, start())} />)
  expect(bare.container.textContent).toContain('文件名未提供')
  expect(bare.container.textContent).toContain('未保存可读取正文')
})

test.each([false, true])('合法历史正文没有文件名仍展示，分页=%j', paged => {
  const result = paged ? page('legacy body', 0, 6) : legacy('legacy body')
  delete result.filename
  result.title = '历史正文标题'
  const started = paged ? start({ length: 6 }) : start()
  const finished = finish(result, started)
  delete finished.payload.arguments
  delete finished.payload.status
  for (const view of [render(<TeamToolProgress events={[started, finished]} names={{}} />), render(<EventDetails event={finished} />)]) {
    expect(view.container.textContent).toContain('文件名未提供')
    expect(view.container.textContent).toContain('成果标题：历史正文标题')
    expect(view.container.textContent).toContain(paged ? '成果本页已读取' : '成果正文已读取')
    expect(view.container.textContent).not.toContain('回执待核对')
    expect(view.container.querySelector('pre')?.textContent).toBe(paged ? 'legacy' : 'legacy body')
    expect(view.container.querySelector('pre')?.closest('details')?.open).toBe(false)
  }
})

test('正文和长文件名只显示纯文本，额外内部字段不进入两入口', () => {
  const content = `${hostile}\n${'W'.repeat(12000)}`
  const result = page(content, 0, 16000, { filename: `${'长'.repeat(400)}.txt`, note: 'PRIVATE_NOTE',
    summary: 'PRIVATE_SUMMARY', cwd: 'INTERNAL_CWD', error: 'PRIVATE_ERROR', env: { key: 'PRIVATE_KEY' } })
  for (const view of surfaces(result, start({ length: 16000, command: 'PRIVATE_COMMAND' }))) {
    expect(view.container.querySelector('pre')?.textContent).toBe(content)
    expect(view.container.textContent).toContain(result.filename)
    expect(view.container.querySelector('a, img, script, iframe, video, audio')).toBeNull()
    expect(view.container.textContent).not.toMatch(/PRIVATE_|INTERNAL_|开发者详情|原始事件 JSON/)
    expect(view.container.querySelector('pre')?.closest('div')?.className).toContain('[overflow-wrap:anywhere]')
  }
})

const invalidRanges: [string, Record<string, unknown>][] = [
  ...['unit', 'offset', 'returned_chars', 'total_chars', 'has_more', 'next_offset', 'content_complete'].map(key => [`缺${key}`, { [key]: undefined }] as [string, Record<string, unknown>]),
  ['单位', { unit: 'utf16' }], ['offset字符串', { offset: '0' }], ['offset布尔', { offset: false }], ['负offset', { offset: -1 }],
  ['非整数offset', { offset: 0.5 }], ['NaN', { offset: NaN }], ['无限', { total_chars: Infinity }], ['unsafe', { total_chars: Number.MAX_SAFE_INTEGER + 1 }],
  ['returned字符串', { returned_chars: '2' }], ['returned布尔', { returned_chars: true }], ['错UTF16计数', { returned_chars: 3 }],
  ['total字符串', { total_chars: '5' }], ['total布尔', { total_chars: true }], ['总数不足', { total_chars: 1 }],
  ['has_more字符串', { has_more: 'true' }], ['has_more矛盾', { has_more: false, next_offset: null }],
  ['next字符串', { next_offset: '2' }], ['next错误', { next_offset: 3 }], ['next为空', { next_offset: null }],
  ['complete字符串', { content_complete: 'false' }], ['伪造完整', { content_complete: true }],
]
test.each(invalidRanges)('严格校验范围且不暴露正文：%s', (_name, extra) => {
  const result = page()
  result.read_range = { ...(result.read_range as object), ...extra }
  for (const view of surfaces(result)) {
    expect(view.container.textContent).toContain('成果读取回执待核对')
    expect(view.container.textContent).not.toMatch(/甲|😀|成果本页已读取|工具已完成|原始事件 JSON/)
    expect(view.container.querySelector('pre, details details')).toBeNull()
  }
})

test.each([
  null, [], 'ok', {}, page(undefined, 0, 2, { read_range: null }), page(undefined, 0, 2, { read_range: [] }),
  page(undefined, 0, 2, { content: {} }), page(undefined, 0, 2, { content: undefined }),
  page(undefined, 0, 2, { seq: '4' }), page(undefined, 0, 2, { filename: '' }),
  page(undefined, 0, 2, { content_sha256: 'bad' }), page(undefined, 0, 2, { content_sha256: undefined }),
  page(undefined, 0, 2, { content_bytes: '17' }), page(undefined, 0, 2, { content_bytes: undefined }),
  page(undefined, 0, 2, { content_bytes: 1 }), page(undefined, 0, 2, { complete: false }),
  page(undefined, 0, 2, { truncated: true }), page(undefined, 0, 2, { status: 'partial' }),
  page(undefined, 0, 2, { outcome: 'partial' }), page('', 0, 2, { content_bytes: 1 }),
  page('甲😀', 1, 2, { read_range: { unit: 'unicode_codepoints', offset: 1, returned_chars: 1, total_chars: 2, has_more: false, next_offset: 2, content_complete: false } }),
  page('字'.repeat(16001), 0, 16001), legacy(hostile, { content_bytes: 1 }), legacy(hostile, { content: null }),
  legacy(hostile, { content_sha256: undefined }), legacy(hostile, { content_bytes: undefined }), legacy(hostile, { filename: null }),
])('畸形、缺字段和partial回执不投影成功：%j', value => {
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain('成果读取回执待核对')
    expect(view.container.querySelector('pre')).toBeNull()
    expect(view.container.textContent).not.toMatch(/成果本页已读取|成果正文已读取|工具已完成|原始事件 JSON|alert\(1\)/)
  }
})

test.each([
  { offset: -1 }, { offset: true }, { offset: '0' }, { offset: 1.5 }, { offset: null }, { offset: undefined },
  { length: 0 }, { length: false }, { length: '2' }, { length: 16001 }, { length: null }, { length: undefined },
  { offset: 1 }, { length: 1 }, { artifact_seq: 5 }, { artifact_seq: '4' },
])('请求类型或版本/范围不一致不能展示正文：%j', args => {
  for (const view of surfaces(page(), start({ length: 2, ...args }))) {
    expect(view.container.textContent).toContain('成果读取回执待核对')
    expect(view.container.querySelector('pre')).toBeNull()
  }
})

test('显式分页却返回旧完整正文不冒充范围，未返回正文的分页不降级成metadata-only', () => {
  for (const result of [legacy(), page(undefined, 0, 2, { content: undefined }),
    { status: 'ok', seq: 4, title: '历史标题', summary: 'PRIVATE_SUMMARY', uri: 'PRIVATE_URI' }]) for (const view of surfaces(result)) {
    expect(view.container.textContent).toContain('成果读取回执待核对')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  const result = page('abcde', 0, 2, { content: '' })
  result.read_range = { unit: 'unicode_codepoints', offset: 0, returned_chars: 0, total_chars: 5, has_more: true, next_offset: 0, content_complete: false }
  expect(nativeArtifactReceiptStatus(finish(result))).toBe('成果读取回执待核对')
})

test.each(['error', 'failed', 'partial', 'timeout'])('外层或内层失败不得用ok覆盖：%s', status => {
  for (const receipt of [finish(page(undefined, 0, 2, { status })), { ...finish(), payload: { ...finish().payload, status } }]) {
    const view = render(<EventDetails event={receipt} />)
    expect(view.container.querySelector('pre')).toBeNull()
    expect(view.container.textContent).not.toMatch(/成果本页已读取|成果正文已读取|工具已完成|甲|原始事件 JSON/)
    expect(activity(activityEvents(receipt)).lines.find(line => line.role === 'dev')?.warning).toBe(true)
  }
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('system/team identity三项都需可信：%j', actor => {
  const receipt = finish(page(), start({ length: 2 }), actor)
  const view = render(<EventDetails event={receipt} />)
  expect(view.container.textContent).toContain('成果读取来源待核对')
  expect(view.container.textContent).not.toMatch(/甲|成果.txt|SHA-256|原始事件 JSON/)
  expect(render(<NativeArtifactReceipt event={receipt} />).container.textContent).toBe('')
  const progress = render(<TeamToolProgress events={[start({ length: 2 }), receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('已发起，等待回执')
  expect(progress.container.querySelector('pre')).toBeNull()
  expect(render(<TeamToolProgress events={[{ ...start(), actor }, receipt]} names={{}} />).container.textContent).toBe('')
  expect(activity(activityEvents(receipt)).lines.find(line => line.role === 'dev')?.text).toContain('已发起，等待回执')
})

test('仅发起、独立结束、缺名配对及tool别名兼容，活动状态共享校验', () => {
  const started = start({ length: 2 }), receipt = finish()
  for (const view of [render(<EventDetails event={started} />), render(<TeamToolProgress events={[started]} names={{}} />)]) {
    expect(view.container.textContent).toContain('已发起，等待回执')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(render(<TeamToolProgress events={[receipt]} names={{}} />).container.textContent).toContain('成果本页已读取')
  delete receipt.payload.name
  delete receipt.payload.arguments
  const paired = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(paired.container.textContent).toContain('工具过程 · 1 项')
  expect(paired.container.textContent).toContain('成果本页已读取')
  const line = activity(activityEvents(receipt, started)).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('读取成果 · 成果本页已读取')
  expect(line.warning).toBe(false)
  const alias = { ...started, payload: { ...started.payload, tool: 'read_artifact', name: undefined } }
  const aliasedReceipt = finish(page(), alias)
  expect(render(<EventDetails event={aliasedReceipt} />).container.textContent).toContain('成果本页已读取')
  expect(activity(activityEvents(aliasedReceipt, alias)).lines.find(line => line.role === 'dev')?.text).toContain('读取成果 · 成果本页已读取')
  const paused = activity(activityEvents(), 'paused')
  expect(paused.notice).toContain('已暂停后续执行')
  expect(paused.lines.find(line => line.role === 'dev')?.moving).toBe(false)
  expect(paused.message).toContain('答复待确认')
  expect(nativeArtifactReceiptStatus({ ...finish(), type: 'msg.said' })).toBe('成果读取回执待核对')
  expect(render(<NativeArtifactReceipt event={finish(page(), { ...start(), payload: { name: 'workspace_read' } })} />).container.textContent).toBe('')
})

test('详情重绑事件或范围清除正文和指纹展开', () => {
  const first = finish(), next = { ...finish(page(undefined, 3, 2), start({ offset: 3, length: 2 })), seq: 12 }
  for (const direct of [false, true]) for (const rebound of [next, { ...next, seq: first.seq }]) {
    const view = render(direct ? <NativeArtifactReceipt event={first} /> : <EventDetails event={first} />)
    for (const summary of view.container.querySelectorAll('summary')) fireEvent.click(summary)
    expect([...view.container.querySelectorAll('details')].every(detail => detail.open)).toBe(true)
    view.rerender(direct ? <NativeArtifactReceipt event={rebound} /> : <EventDetails event={rebound} />)
    expect([...view.container.querySelectorAll('details')].every(detail => !detail.open)).toBe(true)
    expect(view.container.querySelector('pre')?.textContent).toBe('🚀丙')
  }
})

test('App保持工具过程折叠，不新增主聊天正文气泡（仅本地fetch fixture）', async () => {
  const leader = { id: 'manager', name: '店长', role: 'leader' }, worker = { id: 'dev', name: '开发', role: 'dev' }
  const events = activityEvents()
  const detail: TaskDetail = { task: { id: 'artifact-ui', goal: team.objective, status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [], team },
    team, events: events.map(item => ({ ...item, category: 'lifecycle' })), activity: { active: true, state: 'running' }, review: {} }
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method && init.method !== 'GET') throw new Error('Unexpected fixture mutation')
    if (url === '/api/organization') return json([leader, worker])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [worker] })
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'artifact-ui', goal: team.objective, status: 'running', latest_seq: 11, version: 11, conversation_count: 1, team }] })
    if (url === '/api/tasks/artifact-ui') return json(detail)
    throw new Error(`Unexpected fixture request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  const view = render(<App />)
  const progress = await within(view.container).findByRole('region', { name: '当前工作进展' })
  expect(progress.textContent).toContain('读取成果 · 成果本页已读取')
  const chat = within(view.container).getByLabelText('聊天记录')
  expect(chat.textContent).not.toMatch(/成果本页已读取|甲😀|成果版本 #4/)
  const process = within(view.container).getByText('工具过程 · 1 项').closest('details')!
  expect(process.open).toBe(false)
  expect(process.querySelector('pre')?.textContent).toBe('甲😀')
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})
