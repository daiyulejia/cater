import { render, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity } from '../src/conversationActivityState'
import type { TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const hash = 'a'.repeat(64)
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): DetailEvent => ({ seq, type, payload, actor })
const started = event(10, 'team.tool_started', { name: 'browser_check', role: 'dev', run_id: 'run', message_seq: 1, call_id: 'browser' })
const result = (): Record<string, unknown> => ({
  status: 'ok', outcome: 'passed', scope_current: true,
  checked_artifacts: [{ seq: 5, filename: '入口.html', content_sha256: hash }, { seq: 6, filename: '交互.js', content_sha256: 'b'.repeat(64) }],
  browser_plan: { entrypoint: '入口.html', files: ['入口.html', '交互.js'], steps: [
    { action: 'click_at', label: '点击递增', selector: '#add', x: 5, y: 5 },
    { action: 'assert_text', label: '检查计数', selector: '#count', text: '1' },
  ] },
  browser_report: { outcome: 'passed', steps: [
    { index: 1, action: 'click_at', label: '点击递增', status: 'passed' },
    { index: 2, action: 'assert_text', label: '检查计数', status: 'passed', actual: true, observed: '1' },
  ], counts: { page_errors: 0 }, screenshot: { sha256: hash, size: 256, path: 'TEMP_ONLY.png', url: 'https://temp.invalid/image' } },
  stdout: { excerpt: 'RAW_STDOUT_REPORT', preview: 'RAW_STDOUT_REPORT', complete: true, truncated: false },
  stderr: { excerpt: 'RAW_STDERR', complete: true }, exit_code: 0, duration_ms: 83,
})
const finished = (value: unknown = result(), actor = host) => event(11, 'team.tool_finished', { ...started.payload, started_seq: 10, status: 'ok', result: value }, actor)
const team: TeamState = { enabled: true, objective: '检查交互', status: 'running', revision: 0, runs: [], assignments: [], pending_count: 0, checkpoint: '' }
const activity = (receipt?: DetailEvent, start = started) => conversationActivity({ team, events: [
  event(1, 'team.message_received', { recipient: 'dev', kind: 'message' }, { id: 'human', kind: 'human', role: 'human' }),
  event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }),
  start, ...(receipt ? [receipt] : []),
], names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState: 'running' })
const auditContent = (container: HTMLElement) => {
  const copy = container.cloneNode(true) as HTMLElement
  copy.querySelector('details')?.remove()
  return copy.textContent ?? ''
}

test('通过回执在现有折叠过程和审计展示实际观察、固定版本与截图元数据', () => {
  const receipt = finished()
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{ dev: '开发' }} />)
  expect(progress.container.querySelector('details')?.open).toBe(false)
  const content = progress.container.textContent ?? ''
  for (const expected of ['工具过程 · 1 项', '浏览器检查 · 检查通过', '固定已发布文件副本', '不代表工作区现状', '不等于整体验收通过',
    '入口.html', '交互.js', '成果事件 #5', '成果事件 #6', hash, '执行步骤 1', '点击元素内坐标', '执行步骤 2', '实际判断：满足', '实际观察：1', '256 字节', '83 毫秒']) {
    expect(content).toContain(expected)
  }
  expect(content).not.toMatch(/RAW_STDOUT_REPORT|RAW_STDERR|TEMP_ONLY|temp\.invalid|browser_check/)
  expect(progress.container.querySelector('a, img, script')).toBeNull()
  expect(progress.container.querySelector('[aria-label="被检查的文件版本"]')?.closest('div')?.className).toContain('[overflow-wrap:anywhere]')
  const audit = render(<EventDetails event={receipt} />)
  expect(auditContent(audit.container)).toContain('检查通过')
  expect(auditContent(audit.container)).toContain('实际观察：1')
  expect(auditContent(audit.container)).not.toMatch(/RAW_STDOUT_REPORT|RAW_STDERR|TEMP_ONLY|temp\.invalid/)
  expect(within(audit.container).queryByLabelText('原始事件 JSON')).toBeNull()
  expect(audit.container.textContent).not.toContain('RAW_STDOUT_REPORT')
  const line = activity(receipt).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('浏览器检查 · 检查通过')
  expect(line.warning).toBe(false)
})

test.each([
  ['failed', '检查未通过'], ['unavailable', '检查不可用'], ['timeout', '检查超时'], ['launch_error', '检查启动失败'],
])('status=ok 不能将 %s 背书为工具通过', (outcome, expected) => {
  const value = result()
  value.outcome = outcome
  value.browser_report = { outcome, error: '实际执行错误', steps: outcome === 'failed' ? [
    { index: 1, action: 'assert_text', label: '检查计数', status: 'failed', actual: false, observed: '0' },
  ] : [] }
  value.message = '检查未取得通过证据'
  const receipt = finished(value)
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  const audit = render(<EventDetails event={receipt} />)
  for (const content of [progress.container.textContent, auditContent(audit.container)]) {
    expect(content).toContain(expected)
    expect(content).toContain('实际执行错误')
    expect(content).not.toMatch(/工具已完成|工具已通过|工具成功|检查通过/)
    if (outcome === 'failed') expect(content).toContain('实际观察：0')
  }
  const line = activity(receipt).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain(expected)
  expect(line.text).not.toContain('已完成')
  expect(line.warning).toBe(true)
})

test('历史通过保留步骤和文件事件，但不能作为当前成果通过依据', () => {
  const receipt = finished({ ...result(), scope_current: false })
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  const audit = render(<EventDetails event={receipt} />)
  for (const content of [progress.container.textContent, auditContent(audit.container)]) {
    expect(content).toContain('历史回执，不能作为当前成果依据')
    expect(content).toContain('历史版本')
    expect(content).toContain('成果事件 #5')
    expect(content).toContain('实际观察：1')
    expect(content).not.toMatch(/工具已完成|检查通过/)
  }
  expect(activity(receipt).lines.find(line => line.role === 'dev')?.text).toContain('历史回执')
})

const malformed: [string, (value: Record<string, unknown>) => unknown][] = [
  ['空结果', () => null], ['数组结果', () => []], ['空对象', () => ({})],
  ['缺报告', value => ({ ...value, browser_report: undefined })],
  ['空报告', value => ({ ...value, browser_report: [] })],
  ['报告结果矛盾', value => ({ ...value, browser_report: { ...(value.browser_report as object), outcome: 'failed' } })],
  ['无错误计数', value => ({ ...value, browser_report: { ...(value.browser_report as object), counts: undefined } })],
  ['页面错误', value => ({ ...value, browser_report: { ...(value.browser_report as object), page_errors: ['页面异常'] } })],
  ['错误列表畸形', value => ({ ...value, browser_report: { ...(value.browser_report as object), page_errors: {} } })],
  ...(['actual', 'status', 'index', 'action'] as const).map((key): [string, (value: Record<string, unknown>) => unknown] => [
    `执行步骤${key}矛盾`, value => {
      const report = value.browser_report as { steps: Record<string, unknown>[] }
      return { ...value, browser_report: { ...report, steps: report.steps.map((step, index) => index === 1
        ? { ...step, [key]: { actual: false, status: 'not_run', index: 3, action: 'click' }[key] } : step) } }
    },
  ]),
  ['未采集完整', value => ({ ...value, stdout: { complete: false } })],
  ['报告截断', value => ({ ...value, browser_report: { ...(value.browser_report as object), truncated: true } })],
  ['结果截断', value => ({ ...value, truncated: true })],
  ['stderr不完整', value => ({ ...value, stderr: { complete: false } })],
  ['无范围', value => ({ ...value, scope_current: undefined })],
  ['范围类型错误', value => ({ ...value, scope_current: 'true' })],
  ['无计划', value => ({ ...value, browser_plan: null })],
  ['入口未绑定', value => ({ ...value, browser_plan: { ...(value.browser_plan as object), entrypoint: '别处.html' } })],
  ['步骤畸形', value => ({ ...value, browser_report: { outcome: 'passed', steps: [null], counts: { page_errors: 0 } } })],
  ['无文件', value => ({ ...value, checked_artifacts: [] })],
  ['畸形文件', value => ({ ...value, checked_artifacts: [null, { seq: '5', filename: {}, content_sha256: '<img src=x>' }] })],
  ['缺哈希', value => ({ ...value, checked_artifacts: [{ seq: 5, filename: '入口.html' }] })],
  ['退出码矛盾', value => ({ ...value, exit_code: 1 })],
  ['工具状态错误', value => ({ ...value, status: 'error' })],
  ['副本被修改', value => ({ ...value, artifacts_unchanged: false })],
  ['无断言', value => ({ ...value, browser_plan: { entrypoint: '入口.html', steps: [{ action: 'click' }] } })],
]

test.each(malformed)('畸形或不完整回执不宣称通过：%s', (_name, change) => {
  const receipt = finished(change(result()))
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  const audit = render(<EventDetails event={receipt} />)
  for (const content of [progress.container.textContent, auditContent(audit.container)]) {
    expect(content).toContain('结果待确认')
    expect(content).not.toMatch(/检查通过|工具已完成|\[object Object\]|<img src=x>/)
  }
  expect(activity(receipt).lines.find(line => line.role === 'dev')?.text).toContain('结果待确认')
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('不可信来源不能伪造宿主浏览器检查：%j', actor => {
  const receipt = finished(result(), actor)
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('已发起，等待回执')
  expect(progress.container.textContent).not.toMatch(/检查通过|实际观察|成果事件/)
  const audit = render(<EventDetails event={receipt} />)
  expect(auditContent(audit.container)).toContain('检查回执来源待核对')
  expect(auditContent(audit.container)).not.toMatch(/检查通过|实际观察|固定已发布/)
  expect(activity(receipt).lines.find(line => line.role === 'dev')?.text).toContain('已发起，等待回执')
  const fakeStart = { ...started, actor }
  const hidden = render(<TeamToolProgress events={[fakeStart, receipt]} names={{}} />)
  expect(hidden.container.textContent).toBe('')
  expect(activity(receipt, fakeStart).lines.find(line => line.role === 'dev')?.text).not.toContain('浏览器检查')
})

test('独立结束事件和等待中的工具保持原有中文过程入口', () => {
  const waiting = render(<TeamToolProgress events={[started]} names={{}} />)
  expect(waiting.container.textContent).toContain('浏览器检查 · 已发起，等待回执')
  expect(waiting.container.textContent).not.toContain('实际观察')
  const audit = render(<EventDetails event={started} />)
  expect(auditContent(audit.container)).toContain('已发起，等待回执')
  expect(auditContent(audit.container)).not.toContain('browser_check')
  const orphan = render(<TeamToolProgress events={[finished()]} names={{}} />)
  expect(orphan.container.textContent).toContain('浏览器检查 · 检查通过')
})

test('配对结束事件省略工具名时仍按浏览器结果判断，不能被外层ok掩盖', () => {
  const receipt = finished({ ...result(), outcome: 'unavailable' })
  delete receipt.payload.name
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('浏览器检查 · 检查不可用')
  expect(progress.container.textContent).not.toMatch(/检查通过|工具已完成|RAW_STDOUT/)
  const line = activity(receipt).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('浏览器检查 · 检查不可用')
  expect(line.warning).toBe(true)
})

test('外层工具错误不能被内层通过覆盖', () => {
  const receipt = finished()
  receipt.payload.status = 'error'
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  const audit = render(<EventDetails event={receipt} />)
  expect(progress.container.textContent).toContain('结果待确认')
  expect(auditContent(audit.container)).toContain('结果待确认')
  expect(activity(receipt).lines.find(line => line.role === 'dev')?.text).not.toContain('检查通过')
})

test('长文本、哈希和恶意字段保持可折行纯文本，不渲染链接或计划表达式', () => {
  const value = result()
  value.browser_report = { outcome: 'failed', steps: [{ index: 1, action: 'assert_js', status: 'failed',
    label: '<script>throw 1</script>', actual: false, observed: '长'.repeat(2000), error: 'E'.repeat(2000) }],
    screenshot: { path: 'TEMP_ONLY', url: 'javascript:alert(1)', sha256: hash, size: 123 }, private: 'HIDDEN_REPORT' }
  value.browser_plan = { entrypoint: '入口.html', steps: [{ action: 'assert_js', expression: 'HIDDEN_EXPRESSION' }] }
  value.outcome = 'failed'
  const progress = render(<TeamToolProgress events={[started, finished(value)]} names={{}} full />)
  expect(progress.container.textContent).toContain('显示已截断')
  expect(progress.container.textContent).toContain('<script>throw 1</script>')
  expect(progress.container.textContent).not.toMatch(/TEMP_ONLY|javascript:|HIDDEN_REPORT|HIDDEN_EXPRESSION|RAW_STDOUT/)
  expect(progress.container.querySelector('a, img, script')).toBeNull()
})
