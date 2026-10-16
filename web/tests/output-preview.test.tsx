import { fireEvent, render, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { CommandOutputs, OutputPreview } from '../src/CommandOutputPreview'
import { COMPACT_OUTPUT_LINES, hasBrowserReceipt, outputPreview, outputSummaryLines } from '../src/outputPreview'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { WorkspaceToolProgress } from '../src/WorkspacePanel'
import { DeliveryPanel } from '../src/DeliveryPanel'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'

const host = { id: 'team', role: 'team', kind: 'system' }
const stream = (complete = true) => ({ preview_mode: 'head_tail', excerpt: `HEAD\n  ${'中文'.repeat(1000)}`,
  tail_excerpt: `${'尾部'.repeat(1000)}\n  FATAL: 最后错误  `, complete, truncated: true, bytes: 20000, sha256: 'a'.repeat(64) })
const receipt = (type: string, payload: Record<string, unknown>, actor = host): DetailEvent => ({ seq: 12, type, actor, payload })
const visible = (element: HTMLElement) => {
  const clone = element.cloneNode(true) as HTMLElement
  clone.querySelectorAll('details:not([open])').forEach(details => Array.from(details.children).forEach(child => {
    if (child.tagName !== 'SUMMARY') child.remove()
  }))
  return clone.textContent ?? ''
}

test.each([
  ['旧字符串', '旧正文', '旧正文'],
  ['旧preview', { preview: '旧预览', excerpt: '备用正文' }, '旧预览'],
  ['短正文', { excerpt: '  短正文\n', complete: true, truncated: false }, '  短正文\n'],
  ['空输出', { excerpt: '', bytes: 0, complete: true }, '无输出。'],
  ['未完成空采集', { bytes: 0, complete: false }, '未采集到输出，采集尚未完成。'],
  ['错误型', { excerpt: { error: 'BAD' }, tail_excerpt: ['BAD'], preview_mode: 'head_tail' }, '未记录该片段正文。'],
  ['非dict', new Error('BAD'), '输出正文未记录。'],
  ['旧模式忽略尾字段', { excerpt: '原正文', tail_excerpt: 'BAD' }, '原正文'],
] as const)('%s兼容且不输出对象JSON', (_name, value, expected) => {
  const view = render(<OutputPreview name="测试" output={value} />)
  expect(view.container.textContent).toContain(expected)
  expect(view.container.textContent).not.toMatch(/\[object Object\]|BAD/)
  if (typeof value === 'object' && value !== null && 'preview_mode' in value) expect(view.container.textContent).toContain('中间省略')
  else expect(view.container.textContent).not.toContain('中间省略')
})

test.each([true, false])('首尾保持独立原文，紧凑尾部保留最后N字符，complete=%s', complete => {
  const output = stream(complete)
  const model = outputPreview(output, 100)
  expect(model.parts[1].text.endsWith(Array.from(output.tail_excerpt).slice(-100).join(''))).toBe(true)
  expect(model.parts[0].text.startsWith(output.excerpt.slice(0, 100))).toBe(true)
  expect(outputPreview(output).parts.map(part => part.text)).toEqual([output.excerpt, output.tail_excerpt])
  const view = render(<OutputPreview name="测试" output={output} limit={100} />)
  expect(visible(view.container)).toContain('FATAL: 最后错误')
  expect(visible(view.container)).toContain(complete ? '末尾（已保存片段）' : '已采集末尾（已保存片段）')
  expect(view.container.textContent).not.toMatch(/完整日志|最终结论|省略\d+字节/)
  const fold = within(view.container).getByText('已保存片段').closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(fold.querySelector('summary')!)
  expect(Array.from(fold.querySelectorAll('pre')).map(pre => pre.textContent)).toEqual([output.excerpt, output.tail_excerpt])
  expect(fold.textContent).not.toContain(output.excerpt + output.tail_excerpt)
})

test('超长单行按Unicode字符保留末尾，不裁断代理对；两流不合并', () => {
  const output = { ...stream(false), tail_excerpt: `${'长'.repeat(20000)}𠮷中文错误𠮷` }
  const view = render(<CommandOutputs stdout={output} stderr={{ ...output, tail_excerpt: 'ERROR_END' }} limit={5} />)
  const groups = view.container.querySelectorAll('[role="group"]')
  expect(visible(groups[0] as HTMLElement)).toContain('中文错误𠮷')
  expect(visible(groups[0] as HTMLElement)).not.toContain('ERROR_END')
  expect(visible(groups[1] as HTMLElement)).toContain('R_END')
  for (const pre of view.container.querySelectorAll('pre')) {
    expect(pre.className).toContain('whitespace-pre-wrap')
    expect(pre.className).toContain('[overflow-wrap:anywhere]')
    expect(pre.className).toContain('min-w-0')
  }
})

test('聊天摘要独立限制每段且尾部最后错误仍在；browser无报告也不回退正文', () => {
  const lines = outputSummaryLines('标准输出', stream(false), 20)
  expect(lines).toHaveLength(5)
  expect(lines[0]).toContain('开头')
  expect(lines[1]).toContain('中间省略，非完整原文')
  expect(lines[2]).toContain('FATAL: 最后错误')
  expect(lines[2]).toContain('已采集末尾')
  for (const payload of [{ check_kind: 'browser', browser_report: null }, { browser_plan: { entrypoint: 'index.html', steps: [] } }]) {
    expect(hasBrowserReceipt(payload)).toBe(true)
    expect(outputSummaryLines('标准输出', stream(false), 20, hasBrowserReceipt(payload)).join('')).not.toContain('FATAL')
  }
})

test.each([null, false, '', undefined, [], {}])('空browser字段不是专用回执，不隐藏普通日志：%j', value => {
  const payload = { browser_plan: value, browser_report: value, check_kind: 'command' }
  expect(hasBrowserReceipt(payload)).toBe(false)
  expect(outputSummaryLines('标准输出', stream(), 100, hasBrowserReceipt(payload)).join('')).toContain('FATAL: 最后错误')
})

test.each([
  { name: 'browser_check', browser_report: false },
  { tool: 'browser_check', browser_report: '' },
  { check_kind: 'browser', browser_report: null },
  { check_kind: 'browser', browser_report: ['malformed'] },
  { browser_plan: { entrypoint: '入口.html', steps: [] }, browser_report: 'malformed' },
  { browser_report: { outcome: 'unavailable', error: '报告无效' } },
])('真实浏览器身份或有效对象即使报告畸形也不回退原文：%j', payload => {
  expect(hasBrowserReceipt(payload)).toBe(true)
})

test.each(['\n', '\r\n', '\r'])('多短行首尾双限制，高度受限且尾行默认朝向视口底部：%j', newline => {
  const lines = Array.from({ length: 100 }, (_, i) => `行${i}`)
  const output = { ...stream(false), excerpt: lines.join(newline), tail_excerpt: `${lines.join(newline)}${newline}最后错误` }
  const model = outputPreview(output, 800)
  expect(model.clipped).toBe(true)
  expect(model.parts[0].text.split('\n').slice(0, -1)).toHaveLength(COMPACT_OUTPUT_LINES)
  expect(model.parts[1].text.split('\n').slice(1)).toHaveLength(COMPACT_OUTPUT_LINES)
  expect(model.parts[0].text).toContain('行0')
  expect(model.parts[0].text).not.toContain('行99')
  expect(model.parts[1].text.endsWith('最后错误')).toBe(true)
  expect(model.parts[1].text).not.toContain('行0')
  const view = render(<div style={{ width: 160 }}><OutputPreview name="窄屏中文" output={output} limit={800} /></div>)
  const tail = within(view.container).getAllByLabelText('窄屏中文 · 已采集末尾（已保存片段）')[0]
  expect(tail.parentElement!.className).toContain('max-h-48 overflow-auto flex flex-col-reverse')
  expect(tail.className).toContain('shrink-0')
  expect(tail.textContent!.endsWith('最后错误')).toBe(true)
  const fold = within(view.container).getByText('已保存片段').closest('details')!
  fireEvent.click(fold.querySelector('summary')!)
  const saved = fold.querySelector('div')!
  saved.scrollTop = 37
  const tailViewport = tail.parentElement!
  tailViewport.scrollTop = -20
  view.rerender(<div style={{ width: 160 }}><OutputPreview name="窄屏中文" output={{ ...output, bytes: 20001, complete: true }} limit={800} /></div>)
  expect(fold.open).toBe(true)
  expect(fold.querySelector('div')).toBe(saved)
  expect(saved.scrollTop).toBe(37)
  expect(tail.parentElement).toBe(tailViewport)
  expect(tailViewport.scrollTop).toBe(-20)
  expect(Array.from(fold.querySelectorAll('pre')).map(pre => pre.textContent)).toEqual([output.excerpt, output.tail_excerpt])
})

test('原生工具与旧工作区沿用折叠入口，紧凑尾部和两流均显示', () => {
  const result = { status: 'error', outcome: 'failed', exit_code: 1, stdout: stream(), stderr: stream(false) }
  const native = render(<TeamToolProgress events={[receipt('team.tool_finished', { name: 'workspace_run', result })]} names={{}} />)
  const fold = within(native.container).getByText('工具过程 · 1 项').closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(fold.querySelector('summary')!)
  expect(visible(fold)).toContain('FATAL: 最后错误')
  expect(visible(fold)).toContain('已采集末尾')
  expect(fold.querySelectorAll('[role="group"]')).toHaveLength(2)
  const attempt = { plan_seq: 1, work_item_id: 'front', attempt_id: 'first', scope: { instruction_seq: 1 }, role: 'dev' }
  const start = receipt('workspace.tool_started', { ...attempt, name: 'workspace_run', call_id: 'call', workspace_revision: 7 }, { id: 'workspace_gateway', kind: 'system', role: 'system' })
  const legacy = render(<WorkspaceToolProgress claim={2} name="测试" events={[
    { ...receipt('work.plan_created', { items: [{ id: 'front', owner: 'dev' }] }), seq: 1 },
    { ...receipt('work.claimed', attempt), seq: 2 }, start,
    { ...receipt('workspace.tool_finished', { ...start.payload, started_seq: start.seq, result }, start.actor), seq: 13 },
  ]} />)
  const legacyFold = legacy.container.querySelector('details')!
  expect(legacyFold.open).toBe(false)
  fireEvent.click(legacyFold.querySelector('summary')!)
  expect(visible(legacyFold)).toContain('FATAL: 最后错误')
  expect(legacyFold.querySelectorAll('[role="group"]')).toHaveLength(2)
})

test('事件详情与交付保留可引用的尾部独立片段和采集/字节/SHA事实', () => {
  const output = stream(false)
  const audit = render(<EventDetails event={receipt('verification.completed', { stdout: output, stderr: stream(), outcome: 'failed', exit_code: 1 })} />)
  const tail = within(audit.container).getByLabelText('标准输出 · 已采集末尾（已保存片段）')
  expect(tail.textContent).toBe(output.tail_excerpt)
  expect(tail.textContent).not.toContain(output.excerpt)
  const delivery = render(<DeliveryPanel taskId="mock" names={{}} delivery={{ status: 'rework', reason: '失败需修复', digest: 'mock', manifest: { sources: [], files: [] },
    requirements: [{ id: 'tail-evidence', status: 'failed', source_quote: 'FATAL: 最后错误', receipt_seq: 12 }],
    local_checks: [{ id: 'check', status: 'failed', source_quote: 'FATAL: 最后错误', receipt_seq: 12, receipts: [{ receipt_seq: 12, attempt_seq: 10, at: '2026-10-17', outcome: 'failed', exit_code: 1, stdout: output, stderr: stream() }] }],
  }} />)
  const fold = delivery.container.querySelector('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(fold.querySelector('summary')!)
  expect(visible(fold)).toContain('FATAL: 最后错误')
  expect(visible(fold)).toContain('20000 字节')
  expect(visible(fold)).toContain('a'.repeat(64))
  expect(visible(fold)).toContain('采集：未完成')
  expect(visible(fold)).not.toContain('整体验收通过')
  const quote = delivery.container.querySelector('blockquote')!
  expect(quote.textContent).toBe('“FATAL: 最后错误”')
  expect(quote.textContent).not.toContain('HEAD')
})

test.each([undefined, null, { outcome: 'failed' }])('browser专用回执不会暴露首尾或原始JSON：%j', browser_report => {
  const result = { stdout: { ...stream(false), excerpt: 'RAW_HEAD', tail_excerpt: 'RAW_TAIL' }, stderr: { excerpt: 'RAW_STDERR' }, browser_report }
  const native = receipt('team.tool_finished', { tool: 'browser_check', result })
  const view = render(<><TeamToolProgress events={[native]} names={{}} /><EventDetails event={native} /><EventDetails event={receipt('verification.completed', { ...result, stderr: { excerpt: '诊断' }, check_kind: 'browser' })} /></>)
  expect(view.container.textContent).not.toMatch(/RAW_HEAD|RAW_TAIL|RAW_STDERR|原始事件 JSON/)
})

test.each([null, false, '', { outcome: 'unavailable', error: '浏览器报告无效' }])('交付browser回执沿用身份隐藏JSON并保留失败说明：%j', browser_report => {
  const view = render(<DeliveryPanel taskId="mock" names={{}} delivery={{ status: 'rework', reason: '检查未完成', digest: 'mock', manifest: { sources: [], files: [] },
    local_checks: [{ id: 'browser', status: 'failed', receipts: [{ receipt_seq: 12, attempt_seq: 10, at: '2026-10-17', outcome: 'unavailable', check_kind: 'browser', browser_report,
      stdout: { ...stream(false), excerpt: 'RAW_HEAD', tail_excerpt: 'RAW_TAIL' } }] }],
  }} />)
  expect(view.container.textContent).not.toMatch(/RAW_HEAD|RAW_TAIL/)
  expect(view.container.textContent).toContain('采集：未完成')
  expect(view.container.textContent).toContain('a'.repeat(64))
  expect(view.container.textContent).toContain(browser_report && typeof browser_report === 'object' ? '浏览器报告无效' : '结果待核对')
})
