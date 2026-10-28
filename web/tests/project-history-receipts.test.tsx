import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { EventDetails } from '../src/EventDetails'
import { NativeProjectHistoryReceipt } from '../src/NativeProjectHistoryReceipt'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity, nativeProjectHistoryReceiptStatus } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const sha = 'a'.repeat(64), fileSha = 'b'.repeat(64)
const hostile = '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[链接](javascript:alert(1))\nhttps://example.invalid'
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, payload, actor })
const ref = (seq = 40) => ({ seq, filename: `历史文件${seq}.txt`, content_sha256: fileSha })
const entry = (extra = {}) => ({ task_id: 'source-task', delivery_seq: 50, delivery_hash: sha,
  objective_excerpt: '旧目标', objective_truncated: false, summary_excerpt: '历史成果摘要', summary_truncated: false,
  artifact_count: 5, artifacts: [ref(40), ref(41), ref(42)], artifacts_truncated: true, verification_kind: 'manager_declared', ...extra })
const index = (extra = {}): Record<string, unknown> => ({ status: 'ok', complete: true, section: 'index', historical_only: true,
  available: true, project_id: 'project-a', as_of_seq: 100, as_of_hash: sha,
  entries: [entry()], total: 2, before_seq: null, next_before_seq: 50, has_more: true, ...extra })
const delivery = (extra = {}) => ({ objective: '历史任务目标', summary: '完整历史摘要', verification: '当时记录过一次检查',
  artifacts: [ref(40)], handoff: { version: 1, why: '当时采用这个接口', changed: ['增加统计接口'], impact: ['调用方可读取总数'],
    alternatives: ['曾比较其他接口'], reproduce: ['python historical_check.py'], limits: ['当时未验证手机'],
    evidence: [{ source_seq: 40, source_hash: fileSha, quote: hostile, kind: 'artifact' }] }, ...extra })
const body = (section = 'delivery', content = JSON.stringify(delivery()), extra = {}): Record<string, unknown> => ({
  status: 'ok', complete: true, historical_only: true, section, as_of_seq: 100, as_of_hash: sha,
  source: { project_id: 'project-a', task_id: 'source-task', delivery_seq: 50, delivery_hash: sha,
    ...(section === 'artifact' ? { artifact_seq: 40, content_sha256: fileSha, filename: '历史文件40.txt', author: 'author' } : {}) },
  content, offset: 0, total_chars: Array.from(content).length, next_offset: null, has_more: false, ...extra,
})
const start = (section = 'index', args = {}) => event(110, 'team.tool_started', { name: 'read_project_history', role: 'dev', run_id: 'run', message_seq: 1,
  call_id: 'history', arguments: { section, ...(section === 'index' ? {} : { delivery_seq: 50 }), ...(section === 'artifact' ? { artifact_seq: 40 } : {}), ...args } })
const finish = (result: unknown = index(), started = start(), actor = host) => event(111, 'team.tool_finished', {
  ...started.payload, started_seq: started.seq, status: 'ok', result,
}, actor)
const team: TeamState = { enabled: true, objective: '当前全新目标', status: 'running', revision: 0, runs: [], assignments: [], pending_count: 0, checkpoint: '' }
const activity = (events: TeamEvent[], executionState = 'running') => conversationActivity({ team, events: [
  event(1, 'team.message_received', { recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
  event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), ...events,
], names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState })
const surfaces = (result: unknown, started = start()) => [
  render(<TeamToolProgress events={[started, finish(result, started)]} names={{ dev: '开发' }} full />),
  render(<EventDetails event={finish(result, started)} />),
]
const defaultText = (container: HTMLElement) => {
  const clone = container.cloneNode(true) as HTMLElement
  // Keep the outer tool process: this measures its contents after opening that existing disclosure.
  const root = clone.querySelector('[aria-label="团队工具过程"]') ?? clone
  root.querySelectorAll('details').forEach(details => details.remove())
  return root.textContent ?? ''
}

test('目录突出历史摘要，来源范围折叠、状态仅出现一次，不倾倒长说明', () => {
  for (const view of surfaces(index())) {
    const visible = defaultText(view.container)
    for (const expected of ['读取同项目历史', '历史目录已读取', '历史成果摘要', '当时目标：旧目标', '文件 5 份', '仅列部分文件引用',
      '完整引用见历史交付说明', '本页 1 条', '共 2 条', '下一页上界 #50（不含）']) expect(visible).toContain(expected)
    expect(visible.split('历史目录已读取')).toHaveLength(2)
    expect(visible).not.toMatch(/project-a|source-task|SHA-256|当前目标|磁盘状态|独立实测|工具已完成|read_project_history/)
    expect(visible.length).toBeLessThan(450)
    const details = within(view.container).getByText('来源与范围').closest('details')!
    expect(details.open).toBe(false)
    fireEvent.click(within(details).getByText('来源与范围'))
    expect(details.open).toBe(true)
    for (const expected of ['project-a', 'source-task', sha, fileSha, '不代表当前目标、验收或磁盘状态', '不是独立实测']) expect(details.textContent).toContain(expected)
    expect(view.container.querySelector('a, img, video, audio, iframe, script')).toBeNull()
    expect(view.container.textContent).not.toMatch(/原始事件 JSON|开发者详情/)
  }
  const started = start(), receipt = finish()
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(progress.container.querySelector('details')?.open).toBe(false)
  expect(progress.container.textContent).toContain('工具过程 · 1 项')
  const line = activity([started, receipt]).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('读取同项目历史 · 历史目录已读取')
  expect(line.warning).toBe(false)
})

test('空目录与翻页后末页保留真实索引范围', () => {
  for (const value of [index({ entries: [], total: 0, next_before_seq: null, has_more: false }),
    index({ entries: [entry({ delivery_seq: 49 })], before_seq: 50, next_before_seq: null, has_more: false })]) {
    for (const view of surfaces(value)) {
      expect(view.container.textContent).toContain('历史目录已读取')
      expect(view.container.textContent).not.toContain('下一页上界')
      if (value.total === 0) expect(view.container.textContent).toContain('当前项目暂无历史交付')
    }
  }
})

test('完整delivery JSON使用中文字段投影，交接与完整文件引用按需展开', () => {
  const refs = Array.from({ length: 8 }, (_, i) => ref(30 + i))
  const value = body('delivery', JSON.stringify(delivery({ artifacts: refs, private: 'PRIVATE_JSON', command: 'INTERNAL_COMMAND' })))
  for (const view of surfaces(value, start('delivery'))) {
    const visible = defaultText(view.container)
    for (const expected of ['历史交付说明已读取', '历史摘要', '完整历史摘要', '当时目标', '历史任务目标', '店长当时验证说明', '当时记录过一次检查']) expect(visible).toContain(expected)
    expect(visible).not.toMatch(/"summary"|"verification"|PRIVATE_JSON|INTERNAL_COMMAND|当时采用这个接口|python historical_check/)
    expect(view.container.querySelector('pre')).toBeNull()
    expect(within(view.container).getByText('当时交接说明').closest('details')?.open).toBe(false)
    const files = within(view.container).getByText('交付文件引用 · 8 份').closest('details')!
    expect(files.open).toBe(false)
    fireEvent.click(within(files).getByText('交付文件引用 · 8 份'))
    expect(within(files).getAllByRole('listitem')).toHaveLength(8)
    expect(files.textContent).toContain('历史文件37.txt')
    expect(view.container.textContent).toContain('当时未验证范围')
    expect(view.container.textContent).toContain('来源事件 #40')
    expect(view.container.textContent).toContain(hostile)
    expect(view.container.textContent).not.toContain('PRIVATE_JSON')
    expect(view.container.querySelector('a, script, img, button')).toBeNull()
  }
})

test('历史部分交付说明保留不完整警告且不展示未校验引用', () => {
  const original = delivery()
  const partial = { ...original.handoff, evidence: [] }
  const value = body('delivery', JSON.stringify(delivery({ handoff: undefined,
    handoff_status: 'omitted_invalid', handoff_warning: '仅保存部分内容；其他内容和引用未保存，请结合记录核对。', handoff_partial: partial })))
  for (const view of surfaces(value, start('delivery'))) {
    expect(view.container.textContent).toContain('当时交付说明：仅保存部分内容；其他内容和引用未保存，请结合记录核对。')
    expect(view.container.textContent).toContain(partial.why)
    expect(view.container.querySelector('blockquote')).toBeNull()
  }
  const invalid = body('delivery', JSON.stringify(delivery({ handoff: undefined,
    handoff_status: 'omitted_invalid', handoff_warning: '仅保存部分内容；其他内容和引用未保存，请结合记录核对。',
    handoff_partial: { ...partial, evidence: original.handoff.evidence } })))
  for (const view of surfaces(invalid, start('delivery'))) {
    expect(view.container.textContent).not.toContain(partial.why)
    expect(view.container.querySelector('blockquote')).toBeNull()
  }
})

test('历史省略警告独立于正文保留，矛盾载荷的引文不会被展示', () => {
  const warning = '讲解已保留，引用核对失败未保存，不视为验证证据。'
  const value = body('delivery', JSON.stringify(delivery({ handoff_status: 'references_omitted_invalid', handoff_warning: warning })))
  for (const view of surfaces(value, start('delivery'))) {
    expect(view.container.textContent).toContain(`当时交付说明：${warning}`)
    expect(view.container.querySelector('blockquote')).toBeNull()
  }
  const omitted = body('delivery', JSON.stringify(delivery({ handoff: undefined,
    handoff_status: 'omitted_invalid', handoff_warning: '可选交付说明未通过校验，未保存；主要交付结论与成果已保留。' })))
  for (const view of surfaces(omitted, start('delivery'))) {
    expect(view.container.textContent).toContain('当时交付说明：可选交付说明未通过校验')
    expect(within(view.container).queryByText('当时交接说明')).toBeNull()
  }
})

test('历史分页保留说明警告，未来快照回执不被信任', () => {
  const content = '私密片段'
  const warning = '仅保存部分内容；其他内容和引用未保存，请结合记录核对。'
  const paged = body('delivery', content, { total_chars: 20, next_offset: 4, has_more: true,
    handoff_status: 'omitted_invalid', handoff_warning: warning })
  for (const view of surfaces(paged, start('delivery'))) {
    expect(view.container.textContent).toContain(`当时交付说明：${warning}`)
    expect(view.container.textContent).toContain('正文未显示，请结合完整记录核对')
    expect(view.container.textContent).not.toContain(content)
  }
  for (const view of surfaces({ ...paged, handoff_warning: '伪造分页警告' }, start('delivery'))) {
    expect(view.container.textContent).not.toContain('伪造分页警告')
  }
  const forged = finish({ ...paged, as_of_seq: 111 }, start('delivery'))
  expect(nativeProjectHistoryReceiptStatus(forged)).toBe('历史回执待核对')
  expect(nativeProjectHistoryReceiptStatus({ ...finish(paged, start('delivery')), seq: 0 })).toBe('历史回执待核对')
})

test('带状态的历史分页隐藏矛盾引文，旧无状态记录和历史文件仍可读取', () => {
  const content = 'UNVERIFIED_QUOTE🐾'
  const warning = '讲解已保留，引用核对失败未保存，不视为验证证据。'
  const size = Array.from(content).length
  for (const extra of [
    { offset: 0, total_chars: size + 1, next_offset: size, has_more: true },
    { offset: 4, total_chars: size + 4, next_offset: null, has_more: false },
  ]) {
    for (const state of [{ handoff_status: 'references_omitted_invalid', handoff_warning: warning },
      { handoff_status: 'unknown', handoff_warning: 'PRIVATE_WARNING' }]) {
      const value = body('delivery', content, { ...extra, ...state })
      for (const view of surfaces(value, start('delivery'))) {
        expect(view.container.textContent).not.toContain(content)
        expect(view.container.textContent).not.toContain('PRIVATE_WARNING')
        expect(view.container.textContent).toContain('正文未显示，请结合完整记录核对')
      }
    }
    const legacy = body('delivery', content, extra)
    for (const view of surfaces(legacy, start('delivery'))) expect(view.container.textContent).toContain(content)
    const artifact = body('artifact', content, { ...extra, handoff_status: 'unknown' })
    for (const view of surfaces(artifact, start('artifact'))) expect(view.container.textContent).toContain(content)
  }
})

test('完整历史正文与页级省略状态矛盾时不展示引文', () => {
  const warning = '讲解已保留，引用核对失败未保存，不视为验证证据。'
  const value = body('delivery', JSON.stringify(delivery()), { handoff_status: 'references_omitted_invalid', handoff_warning: warning })
  for (const view of surfaces(value, start('delivery'))) {
    expect(view.container.textContent).toContain('历史交付说明状态不一致，请核对原记录。')
    expect(view.container.textContent).toContain(`当时交付说明：${warning}`)
    expect(view.container.querySelector('blockquote')).toBeNull()
  }
  for (const content of ['{"broken":"UNVERIFIED_QUOTE', JSON.stringify({ handoff: { evidence: [{ quote: 'UNVERIFIED_QUOTE' }] } })]) {
    const malformed = body('delivery', content, { handoff_status: 'references_omitted_invalid', handoff_warning: warning })
    for (const view of surfaces(malformed, start('delivery'))) {
      expect(view.container.textContent).toContain('历史交付说明状态不一致，请核对原记录。')
      expect(view.container.textContent).toContain(`当时交付说明：${warning}`)
      expect(view.container.textContent).not.toContain('UNVERIFIED_QUOTE')
    }
  }
})

test('历史正文截断不会拆开 Unicode 字符', () => {
  const reason = 'a'.repeat(1999) + '🐾' + '余下内容'
  const value = body('delivery', JSON.stringify(delivery({ handoff: { ...delivery().handoff, why: reason } })))
  for (const view of surfaces(value, start('delivery'))) {
    expect(view.container.textContent).toContain('🐾\n（显示已截断）')
    expect(view.container.textContent).not.toContain('\ud83d\n')
  }
})

test('历史目录提示可用的部分说明，但不从非布尔标记推断', () => {
  for (const view of surfaces(index({ entries: [entry({ has_handoff_partial: true })], total: 1, has_more: false, next_before_seq: null }))) {
    expect(view.container.textContent).toContain('当时交付说明仅部分保存。')
  }
  for (const view of surfaces(index({ entries: [entry({ has_handoff_partial: 'true' })], total: 1, has_more: false, next_before_seq: null }))) {
    expect(view.container.textContent).not.toContain('当时交付说明仅部分保存。')
  }
})

test('合法但未知JSON折叠，全文非JSON兼容为纯文本；artifact JSON保持原始文件数据', () => {
  for (const content of ['{"unknown":"不默认强推"}', '["未知数据"]', 'null']) {
    for (const view of surfaces(body('delivery', content), start('delivery'))) {
      const details = within(view.container).getByText('其他历史数据 · 纯文本').closest('details')!
      expect(details.open).toBe(false)
      expect(defaultText(view.container)).not.toContain(content)
      expect(details.querySelector('pre')?.textContent).toBe(content)
    }
  }
  for (const view of surfaces(body('delivery', hostile), start('delivery'))) expect(view.container.querySelector('pre')?.textContent).toBe(hostile)
  for (const view of surfaces(body('artifact', '{"summary":"只是文件内容"}'), start('artifact'))) {
    expect(view.container.querySelector('pre')?.textContent).toBe('{"summary":"只是文件内容"}')
    expect(view.container.textContent).toContain('历史文件已读取')
    expect(view.container.textContent).toContain('历史文件：历史文件40.txt')
  }
})

test.each(['delivery', 'artifact'])('%s首、中、末节选不伪装全文，Unicode码点分页与后续游标准确', section => {
  const content = '{"summary":"😀汉字"}'
  const size = Array.from(content).length
  for (const [offset, more] of [[0, true], [5, true], [5, false]] as const) {
    const value = body(section, content, { offset, total_chars: offset + size + (more ? 3 : 0), has_more: more, next_offset: more ? offset + size : null })
    for (const view of surfaces(value, start(section, { offset, length: size }))) {
      expect(view.container.querySelector('pre')?.textContent).toBe(content)
      expect(view.container.textContent).toContain('节选，非全文')
      expect(view.container.textContent).toContain(`本页字符：${offset} 至 ${offset + size}`)
      if (more) expect(view.container.textContent).toContain(`下一页起点 ${offset + size}`)
      else expect(view.container.textContent).not.toContain('下一页起点')
      expect(view.container.textContent).not.toMatch(/回执内容不完整|历史回执待核对/)
    }
  }
})

test('长正文仍有界，UI截断不生成伪造游标，长目录摘要明确节选', () => {
  for (const view of surfaces(body('artifact', '长'.repeat(16000)), start('artifact'))) {
    expect(view.container.querySelector('pre')!.textContent!.length).toBeLessThan(8100)
    expect(view.container.textContent).toContain('显示已截断')
    expect(view.container.textContent).not.toContain('下一页起点')
    expect(view.container.querySelector('pre')?.className).toContain('[overflow-wrap:anywhere]')
  }
  for (const view of surfaces(index({ entries: [entry({ objective_truncated: true, summary_truncated: true })] }))) {
    expect(view.container.textContent).toContain('（摘要节选）')
    expect(view.container.textContent).toContain('（目标节选）')
  }
})

test('遵循当前宿主180 UTF-8字节文件名界限，展示截断不破坏引用身份', () => {
  const filename = 'x'.repeat(176) + '.txt'
  const reference = { ...ref(40), filename }
  for (const view of surfaces(index({ entries: [entry({ artifact_count: 1, artifacts: [reference], artifacts_truncated: false })] }))) {
    expect(view.container.textContent).toContain('历史目录已读取')
    expect(defaultText(view.container)).toContain('显示已截断')
    expect(within(view.container).getByText('来源与范围').closest('details')?.textContent).toContain(filename)
  }
  for (const view of surfaces(body('delivery', JSON.stringify(delivery({ artifacts: [reference] }))), start('delivery'))) {
    expect(within(view.container).getByText('交付文件引用 · 1 份').closest('details')?.textContent).toContain(filename)
  }
})

test('前置拒绝只有未开始状态，不是unknown，不渲染夹带历史或内部配置', () => {
  const rejected = { ...index(), status: 'error', outcome: 'not_started', code: 'project_history_unavailable',
    message: '固定中文拒绝', content: 'PRIVATE_BODY', note: 'PRIVATE_NOTE', command: 'INTERNAL_COMMAND', cwd: 'INTERNAL_CWD', env: { PRIVATE_TOKEN: 'SECRET_ENV' } }
  for (const view of surfaces(rejected)) {
    expect(view.container.textContent).toContain('未开始读取历史')
    expect(view.container.textContent).not.toMatch(/历史成果摘要|PRIVATE_|INTERNAL_|SECRET_|unknown|影响|重放|原始事件 JSON|开发者详情|来源与范围/)
  }
  const paused = activity([start(), finish(rejected)], 'paused')
  expect(paused.lines.find(line => line.role === 'dev')?.text).toContain('未开始读取历史')
  expect(paused.lines.find(line => line.role === 'dev')?.warning).toBe(true)
})

const badIndex: [string, unknown][] = [
  ['null', null], ['数组', []], ['字符串', 'ok'], ['空对象', {}],
  ...['historical_only', 'complete', 'section', 'as_of_seq', 'as_of_hash', 'available', 'project_id', 'entries', 'total', 'before_seq', 'next_before_seq', 'has_more'].map((key): [string, unknown] => [`缺${key}`, index({ [key]: undefined })]),
  ['非历史', index({ historical_only: false })], ['外层complete假', index({ complete: false })], ['error掩盖', index({ status: 'error' })],
  ['错误outcome', index({ outcome: 'returned' })], ['不完整原结果', index({ truncated: true })],
  ['版本类型', index({ as_of_seq: '100' })], ['版本坏哈希', index({ as_of_hash: 'bad' })], ['has_more字符串', index({ has_more: 'true' })],
  ['游标不符', index({ next_before_seq: 49 })], ['无后续游标却has_more', index({ next_before_seq: null })], ['末页游标非null', index({ total: 1, has_more: false })],
  ['总数不符', index({ total: 0 })], ['无效上界', index({ before_seq: 200 })], ['空页却有后续', index({ entries: [] })],
  ['畸形entry', index({ entries: [null] })], ['非店长声明', index({ entries: [entry({ verification_kind: 'independent_passed' })] })],
  ['refs超3', index({ entries: [entry({ artifacts: [ref(40), ref(41), ref(42), ref(43)] })] })],
  ['refs缺项', index({ entries: [entry({ artifacts: [ref(40)] })] })], ['refs坏哈希', index({ entries: [entry({ artifacts: [ref(40), ref(41), { ...ref(42), content_sha256: '' }] })] })],
  ['省略标记矛盾', index({ entries: [entry({ artifacts_truncated: false })] })], ['重复task', index({ entries: [entry(), entry({ delivery_seq: 49 })], total: 3, next_before_seq: 49 })],
  ['逆序错误', index({ entries: [entry(), entry({ task_id: 'other', delivery_seq: 51 })], total: 3, next_before_seq: 51 })],
  ['未来交付', index({ entries: [entry({ delivery_seq: 101 })], next_before_seq: 101 })], ['摘要坏shape', index({ entries: [entry({ summary_excerpt: {} })] })],
]
test.each(badIndex)('坏或旧索引谨慎降级，不显示不可信正文：%s', (_name, value) => {
  for (const view of surfaces(value)) {
    expect(view.container.textContent).toContain('历史回执待核对')
    expect(view.container.textContent).not.toMatch(/历史目录已读取|历史成果摘要|工具已完成|\[object Object\]|原始事件 JSON/)
    expect(view.container.querySelector('pre, [aria-label="同项目历史交付"]')).toBeNull()
  }
})

test.each([
  ['坏source', { source: null }], ['正文对象', { content: {} }], ['总数矛盾', { total_chars: 1 }],
  ['错误offset', { offset: -1 }], ['next错', { has_more: true, total_chars: 9999, next_offset: 9 }], ['末页非null', { next_offset: 0 }],
  ['缺total', { total_chars: undefined }], ['无section', { section: undefined }], ['非历史', { historical_only: false }],
])('坏正文不冒充历史完整读取：%s', (_name, extra) => {
  for (const view of surfaces(body('delivery', 'PRIVATE_BODY', extra), start('delivery'))) {
    expect(view.container.textContent).toContain('历史回执待核对')
    expect(view.container.textContent).not.toContain('PRIVATE_BODY')
  }
})

test('参数和返回来源不一致、缺文件版本、伪装工具均不能投影成功', () => {
  const source = body('artifact', hostile).source as Record<string, unknown>
  for (const [value, started] of [
    [index(), start('artifact')], [index(), start('index', { as_of_seq: 99 })], [index(), start('index', { before_seq: 60 })],
    [body(), start('delivery', { delivery_seq: 49 })], [body('artifact', hostile), start('artifact', { artifact_seq: 41 })],
    [body('artifact', hostile, { source: { ...source, content_sha256: undefined } }), start('artifact')],
    [body('artifact', hostile, { source: { ...source, delivery_hash: 'bad' } }), start('artifact')],
    [body('artifact', hostile), start('artifact', { offset: 2 })], [body('artifact', hostile), start('artifact', { length: 1 })],
    [index(), { ...start(), payload: { ...start().payload, project_scope: 'other-project' } }],
    [body(), { ...start('delivery'), payload: { ...start('delivery').payload, project_scope: 'other-project' } }],
  ] as const) {
    for (const view of surfaces(value, started)) {
      expect(view.container.textContent).toContain('历史回执待核对')
      expect(view.container.querySelector('pre')).toBeNull()
    }
  }
  const other = finish(index())
  other.payload.name = 'workspace_read'
  expect(render(<NativeProjectHistoryReceipt event={other} />).container.textContent).toBe('')
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('伪造宿主不能显示跨群历史或背书来源：%j', actor => {
  const receipt = finish(index(), start(), actor)
  const progress = render(<TeamToolProgress events={[start(), receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('已发起，等待回执')
  expect(progress.container.textContent).not.toContain('历史成果摘要')
  const audit = render(<EventDetails event={receipt} />)
  expect(audit.container.textContent).toContain('历史回执来源待核对')
  expect(audit.container.textContent).not.toMatch(/source-task|project-a|历史成果摘要|原始事件 JSON/)
  expect(render(<NativeProjectHistoryReceipt event={receipt} />).container.textContent).toBe('')
  expect(activity([start(), receipt]).lines.find(line => line.role === 'dev')?.text).toContain('已发起，等待回执')
  expect(render(<TeamToolProgress events={[{ ...start(), actor }, receipt]} names={{}} />).container.textContent).toBe('')
})

test('仅发起、独立结束、缺工具名配对和旧tool别名沿既有过程兼容', () => {
  const started = start(), receipt = finish()
  for (const view of [render(<TeamToolProgress events={[started]} names={{}} />), render(<EventDetails event={started} />)]) {
    expect(view.container.textContent).toContain('读取同项目历史')
    expect(view.container.textContent).toContain('已发起，等待回执')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(render(<TeamToolProgress events={[receipt]} names={{}} />).container.textContent).toContain('历史目录已读取')
  delete receipt.payload.name
  delete receipt.payload.arguments
  expect(render(<TeamToolProgress events={[started, receipt]} names={{}} />).container.textContent).toContain('工具过程 · 1 项')
  expect(activity([started, receipt]).lines.find(line => line.role === 'dev')?.text).toContain('历史目录已读取')
  started.payload.tool = started.payload.name
  delete started.payload.name
  const old = finish(index(), started)
  expect(render(<EventDetails event={old} />).container.textContent).toContain('历史目录已读取')
  expect(activity([started, old]).lines.find(line => line.role === 'dev')?.text).toContain('读取同项目历史 · 历史目录已读取')
})

test('暂停历史读取不恢复执行、不交付、不答复顾客，不遮蔽其他角色的活动', () => {
  const paused = activity([start(), finish()], 'paused')
  expect(paused.notice).toContain('已暂停后续执行')
  expect(paused.message).toContain('答复待确认')
  expect(paused.nextCheckAt).toBeUndefined()
  const line = paused.lines.find(line => line.role === 'dev')!
  expect(line.moving).toBe(false)
  expect(line.warning).toBe(false)
  expect(line.text).not.toMatch(/验收通过|已交付|当前目标已完成|已恢复/)
  const unrelated = { ...finish(), payload: { ...finish().payload, role: 'other' } }
  expect(activity([start(), unrelated]).lines.find(line => line.role === 'dev')?.text).toContain('已发起，等待回执')
})

test('额外参数、配置、note及source额外字段不进入展示', () => {
  const started = start('artifact', { command: 'INTERNAL_COMMAND', env: { PRIVATE_TOKEN: 'SECRET_ENV' } })
  const value = body('artifact', hostile)
  value.note = 'PRIVATE_NOTE'
  value.source = { ...(value.source as object), private: 'PRIVATE_SOURCE', cwd: 'INTERNAL_CWD' }
  for (const view of surfaces(value, started)) {
    expect(view.container.querySelector('pre')?.textContent).toBe(hostile)
    expect(view.container.textContent).not.toMatch(/PRIVATE_|INTERNAL_|SECRET_|开发者详情|原始事件 JSON/)
    expect(view.container.querySelector('a, img, script, video, audio')).toBeNull()
  }
})

test('App只在当前角色与折叠过程显示历史，不增加常驻面板或聊天气泡', async () => {
  window.sessionStorage.clear()
  const leader = { id: 'manager', name: '店长', role: 'leader' }, worker = { id: 'dev', name: '开发', role: 'dev' }
  const events = [event(1, 'team.message_received', { text: '核对同项目历史', recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
    event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), start(), finish()]
  const detail: TaskDetail = { task: { id: 'history-ui', goal: team.objective, status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [], team },
    team, events: events.map(item => ({ ...item, category: 'lifecycle' })), activity: { active: true, state: 'running' }, review: {} }
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method && init.method !== 'GET') throw new Error('Unexpected fixture mutation')
    if (url === '/api/organization') return json([leader, worker])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [worker] })
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'history-ui', goal: team.objective, status: 'running', latest_seq: 111, version: 111, conversation_count: 1, team }] })
    if (url === '/api/tasks/history-ui') return json(detail)
    throw new Error(`Unexpected fixture request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  const view = render(<App />)
  const progress = await within(view.container).findByRole('region', { name: '当前工作进展' })
  expect(within(progress).getAllByText(/读取同项目历史 · 历史目录已读取/)).toHaveLength(1)
  const chat = within(view.container).getByLabelText('聊天记录')
  expect(chat.textContent).not.toMatch(/读取同项目历史|历史成果摘要|历史目录已读取/)
  const historicalRows = view.container.querySelector('[aria-label="同项目历史交付"]')
  expect(historicalRows?.closest('details')?.open).toBe(false)
  expect(progress.contains(historicalRows)).toBe(false)
  fireEvent.click(within(progress).getByRole('button', { name: '查看过程' }))
  const dialog = await within(view.container.ownerDocument.body).findByRole('dialog', { name: '成果与过程' })
  await waitFor(() => expect(dialog.textContent).toContain('历史成果摘要'))
  expect(within(dialog).getByText('工具过程 · 1 项').closest('details')?.open).toBe(false)
  expect(chat.textContent).not.toContain('历史成果摘要')
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})

test('普通工具保留原有回执和开发者详情', () => {
  const started = start()
  started.payload.name = 'workspace_read'
  const receipt = finish({ status: 'ok', content: '原有文件正文' }, started)
  expect(render(<TeamToolProgress events={[started, receipt]} names={{}} />).container.textContent).toContain('读取文件 · 工具已完成')
  const audit = render(<EventDetails event={receipt} />)
  expect(within(audit.container).getByText('开发者详情').closest('details')?.open).toBe(false)
})

test.each([
  { stdout: false, stderr: null }, { stdout: null, stderr: false }, { stdout: true }, {}, undefined,
  null, [], { stdout: false, other: true }, { stdout: false, stderr: 'false' },
])('历史引用编码诊断按流显示，畸形映射不伪造状态：%j', output_utf8_valid => {
  const saved = delivery()
  Object.assign(saved.handoff.evidence[0], { quote: 'count=13/32\uFFFD',
    ...(output_utf8_valid === undefined ? {} : { output_utf8_valid }) })
  for (const view of surfaces(body('delivery', JSON.stringify(saved)), start('delivery'))) {
    const fold = within(view.container).getByText('当时引用摘录 · 历史记录').closest('details')!
    expect(fold.open).toBe(false)
    fireEvent.click(fold.querySelector('summary')!)
    expect(fold.querySelector('blockquote')?.textContent).toBe('count=13/32\uFFFD')
    const valid = output_utf8_valid && !Array.isArray(output_utf8_valid)
      && Object.entries(output_utf8_valid).every(([key, state]) => ['stdout', 'stderr'].includes(key) && (state === null || typeof state === 'boolean'))
    for (const [key, name] of [['stdout', '标准输出'], ['stderr', '错误输出']]) {
      const state = valid ? (output_utf8_valid as Record<string, unknown>)[key] : undefined
      const hint = state === false ? '已采集输出含无效 UTF-8；不代表当前引用片段已损坏。'
        : state === null ? '输出流的 UTF-8 有效性未知。' : undefined
      if (hint) expect(within(fold).getAllByText(`${name}：${hint}`)).toHaveLength(1)
      else expect(fold.textContent).not.toContain(`${name}：`)
    }
    expect(fold.textContent).not.toMatch(/执行失败|CP936|GBK/)
  }
})
