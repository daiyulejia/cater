import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import type { ChatItem } from '../src/App'
import { DeliveryOmissionNote } from '../src/DeliveryOmissionNote'
import { deliveryOmissionReason } from '../src/deliveryOmission'
import { MessageDetails } from '../src/MessageDetails'
import type { TeamEvent } from '../src/team'

const reason = '本轮只需答复，已复核成果有意不附。'
const handoff = () => ({ version: 1, why: '按本轮要求直接答复', changed: [], impact: [], alternatives: [], reproduce: [], limits: [], evidence: [] })
const completion = (payload: Record<string, unknown> = { omitted_approved_artifacts_reason: reason }): TeamEvent => ({
  seq: 10, type: 'team.objective_completed', actor: { id: 'team', kind: 'system', role: 'team' }, payload,
})
const itemFor = (evidence = completion(), extra: Partial<ChatItem> = {}): ChatItem => ({
  id: 10, actor: 'team', display: '店长', role: 'team', kind: 'agent', systemActor: true,
  category: 'deliverable', text: '本轮答复', targets: [], evidence: evidence as ChatItem['evidence'], ...extra,
})

test.each([false, true])('有无 handoff 都在原位置折叠展示说明：%s', hasHandoff => {
  const evidence = completion({ omitted_approved_artifacts_reason: reason, ...(hasHandoff ? { handoff: handoff() } : {}) })
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  const summary = screen.getByText(hasHandoff ? '为什么这样做 · 如何运行' : '交付文件说明', { selector: 'summary' })
  const fold = summary.closest('details')!
  expect(container.firstElementChild).toBe(fold)
  expect(container.querySelectorAll('.message-details')).toHaveLength(1)
  expect(fold.open).toBe(false)
  fireEvent.click(summary)
  expect(fold.open).toBe(true)
  expect(within(fold).getByRole('heading', { name: '本次未附已复核成果' })).toBeTruthy()
  expect(within(fold).getByText('店长说明')).toBeTruthy()
  expect(within(fold).getByText(reason)).toBeTruthy()
  expect(container.querySelector('button, a, [role="alert"], [role="status"]')).toBeNull()
  expect(container.textContent).not.toMatch(/已附文件|验收通过|自动验收/)
})

test('有原有详情时保留原折叠标题和内容', () => {
  render(<MessageDetails item={itemFor(completion(), { details: ['原有验证范围'] })} />)
  const fold = screen.getByText('查看判断与依据').closest('details')!
  expect(within(fold).getByText('原有验证范围')).toBeTruthy()
  expect(within(fold).getByText(reason)).toBeTruthy()
})

test.each([false, true])('无新字段的旧回执保持原行为：handoff=%s', hasHandoff => {
  const evidence = completion(hasHandoff ? { handoff: handoff() } : {})
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  expect(screen.queryByText('本次未附已复核成果')).toBeNull()
  expect(screen.queryByText('店长说明')).toBeNull()
  expect(screen.queryByText('交付文件说明')).toBeNull()
  if (hasHandoff) expect(screen.getByText('为什么这样做 · 如何运行')).toBeTruthy()
  else expect(container.childElementCount).toBe(0)
})

test('旧详情与可选 handoff 警告不被新说明取代', () => {
  const evidence = completion({ handoff_status: 'omitted_invalid', handoff_warning: '原有说明省略警告' })
  const { rerender } = render(<MessageDetails item={itemFor(evidence, { details: ['原有详情'] })} />)
  expect(screen.getByText('查看判断与依据')).toBeTruthy()
  expect(screen.getByText('原有详情')).toBeTruthy()
  expect(screen.getByText('可选交付说明：原有说明省略警告')).toBeTruthy()
  rerender(<MessageDetails item={itemFor({ ...evidence, payload: { ...evidence.payload, omitted_approved_artifacts_reason: reason } })} />)
  expect(screen.getByText('查看判断与依据')).toBeTruthy()
  expect(screen.getByText('可选交付说明：原有说明省略警告')).toBeTruthy()
  expect(screen.getByText(reason)).toBeTruthy()
})

test.each([undefined, null, false, true, 1, [], {}, new String('理由'), '', ' \t\r\n\u3000\u00a0', '\ufeff', '\u0085', '\x1c', ' \ufeff\u0085\x1c\n', '理'.repeat(1001), `${' '.repeat(1000)}理`, '理由\0'])('畸形、空白或超长理由不生成说明：%j', value => {
  const evidence = completion({ omitted_approved_artifacts_reason: value })
  expect(deliveryOmissionReason(evidence)).toBeUndefined()
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  expect(container.childElementCount).toBe(0)
})

test.each(['理'.repeat(1000), '\u{20000}'.repeat(1000), '\u{1F600}'.repeat(1000), ' \n店长的原文\t ', ' \ufeff\u0085\x1c理由\x1f\ufeff '])('按字符校验上限并保留原文空白（%#）', value => {
  const evidence = completion({ omitted_approved_artifacts_reason: value })
  expect(deliveryOmissionReason(evidence)).toBe(value)
  const { container } = render(<DeliveryOmissionNote event={evidence} />)
  expect(container.querySelector('p.whitespace-pre-wrap')?.textContent).toBe(value)
})

test.each(['\u{1F600}'.repeat(1001), '\u{20000}'.repeat(1001)])('超过 1000 个 Unicode 码点不生成说明（%#）', value => {
  const evidence = completion({ omitted_approved_artifacts_reason: value })
  expect(deliveryOmissionReason(evidence)).toBeUndefined()
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  expect(container.childElementCount).toBe(0)
})

test.each(['\uD800', '\uDFFF', '理\uD800由', '\uDC00\uD800', '\uD800\uD800\uDC00', '\u{1F600}\uDC00'])('含孤立代理码点的理由不生成说明（%#）', value => {
  const evidence = completion({ omitted_approved_artifacts_reason: value })
  expect(deliveryOmissionReason(evidence)).toBeUndefined()
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  expect(container.childElementCount).toBe(0)
})

const invalidEvents: [string, unknown][] = [
  ...[0, -1, 1.5, '10', true, null, undefined, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1].map(seq => [`seq=${String(seq)}`, { ...completion(), seq }] as [string, unknown]),
  ...['msg.said', 'team.tool_finished', 'team.objective_updated', ' team.objective_completed', 'team.objective_completed ', null, 1, ['team.objective_completed']].map(type => [`type=${String(type)}`, { ...completion(), type }] as [string, unknown]),
  ...[
    { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
    { id: 'platform', kind: 'system', role: 'team' }, { id: 'system', kind: 'system', role: 'team' },
    { id: 'store_manager', kind: 'agent', role: 'store_manager' }, { id: 'team', kind: 'system', role: 'platform' },
    { id: 'team ', kind: 'system', role: 'team' }, { id: 'team', kind: 'system ', role: 'team' },
    { id: 'team', kind: 'system', role: ' team' }, { id: ['team'], kind: 'system', role: 'team' },
    { id: 'team', kind: ['system'], role: 'team' }, { id: 'team', kind: 'system', role: ['team'] },
    { id: 'team', kind: 'system' }, { id: 'team', role: 'team' }, { kind: 'system', role: 'team' },
  ].map(actor => [`actor=${JSON.stringify(actor)}`, { ...completion(), actor }] as [string, unknown]),
]

test.each(invalidEvents)('伪造身份、类型或无效序号不能提供说明：%s', (_label, value) => {
  const evidence = value as TeamEvent
  expect(deliveryOmissionReason(evidence)).toBeUndefined()
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  expect(container.childElementCount).toBe(0)
})

test.each([
  null, undefined, [], {},
  ...[null, [], 'team', Object.create({ id: 'team', kind: 'system', role: 'team' })].map(actor => ({ ...completion(), actor })),
  ...[null, [], '理由', Object.create({ omitted_approved_artifacts_reason: reason })].map(payload => ({ ...completion(), payload })),
  Object.create(completion()),
])('组件拒绝非事件结构及继承字段：%j', value => {
  const { container } = render(<DeliveryOmissionNote event={value as TeamEvent} />)
  expect(container.childElementCount).toBe(0)
})

test('客户端和 agent 的 payload 自报 HOST、完成类型和理由均不能冒充', () => {
  const fake = completion({
    actor: completion().actor, type: 'team.objective_completed', seq: 10,
    omitted_approved_artifacts_reason: reason, historicalDelivery: false,
  })
  const { container, rerender } = render(<MessageDetails item={itemFor({ ...fake, actor: { id: 'platform', kind: 'system', role: 'team' } })} />)
  expect(container.childElementCount).toBe(0)
  rerender(<MessageDetails item={itemFor({ ...fake, type: 'msg.said', actor: { id: 'store_manager', kind: 'agent', role: 'team' } })} />)
  expect(container.childElementCount).toBe(0)
})

test('HTML 和 Markdown 理由仅显示原文，不生成可执行元素', () => {
  const raw = '<script>throw new Error("execute")</script>\n<img src=x onerror=alert(1)>\n[链接](javascript:alert(1)) <b>说明</b>'
  const { container } = render(<MessageDetails item={itemFor(completion({ omitted_approved_artifacts_reason: raw }))} />)
  expect(container.querySelector('p.whitespace-pre-wrap')?.textContent).toBe(raw)
  expect(container.querySelector('script, img, a, b, iframe')).toBeNull()
  expect(container.innerHTML).toContain('&lt;script&gt;')
})

function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}

test('历史状态仅取自 item，展示和切换不会修改冻结的输入或继承旧理由', () => {
  const evidence = completion({ omitted_approved_artifacts_reason: reason, historicalDelivery: false })
  const item = freeze(itemFor(evidence, { historicalDelivery: true }))
  const before = JSON.stringify(item)
  const { container, rerender } = render(<MessageDetails item={item} />)
  expect(screen.getByRole('heading', { name: '历史交付：本次未附已复核成果', hidden: true })).toBeTruthy()
  expect(screen.queryByText('本次未附已复核成果', { exact: true })).toBeNull()
  fireEvent.click(screen.getByText('交付文件说明'))
  expect(JSON.stringify(item)).toBe(before)
  expect(item.evidence).toBe(evidence)
  rerender(<MessageDetails item={freeze(itemFor(completion({ ...evidence.payload, historicalDelivery: true })))} />)
  expect(screen.getByText('本次未附已复核成果', { exact: true })).toBeTruthy()
  expect(screen.queryByText('历史交付：本次未附已复核成果')).toBeNull()
  rerender(<MessageDetails item={freeze(itemFor(completion({})))} />)
  expect(container.childElementCount).toBe(0)
  expect(JSON.stringify(item)).toBe(before)
})
