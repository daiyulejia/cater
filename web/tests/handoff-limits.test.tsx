import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import type { ChatItem } from '../src/App'
import { MessageDetails } from '../src/MessageDetails'
import { completion, event, handoff } from './native-handoff.fixtures'

const label = '使用边界 · 2项'
const limits = ['尚未验证手机端离线使用。', '仅适用于本次提供的样例数据。']
const saved = () => ({ ...handoff(), limits: [...limits] })
const itemFor = (evidence = completion(10, saved()), patch: Partial<ChatItem> = {}) => ({
  id: evidence.seq, kind: 'agent', evidence, ...patch,
}) as ChatItem
const limitsFold = () => screen.getByText(label, { selector: 'summary' }).closest('details')!

test('边界入口直接可见，与原因说明并列独立开合，原文只有一份且没有额外请求', () => {
  const item = itemFor()
  const before = JSON.stringify(item)
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  const { container } = render(<MessageDetails item={item} />)
  const summary = screen.getByText(label, { selector: 'summary' })
  const boundary = limitsFold()
  const reasoning = screen.getByText('为什么这样做 · 如何运行', { selector: 'summary' }).closest('details')!
  expect(boundary.parentElement).toBe(reasoning.parentElement)
  expect(boundary.parentElement?.closest('details')).toBeNull()
  expect(boundary.open).toBe(false)
  expect(reasoning.open).toBe(false)
  expect(summary.querySelector('button, a, input')).toBeNull()
  summary.focus()
  expect(document.activeElement).toBe(summary)
  fireEvent.click(summary)
  expect(boundary.open).toBe(true)
  expect(reasoning.open).toBe(false)
  expect(within(boundary).getAllByRole('listitem').map(node => node.textContent)).toEqual(limits)
  for (const text of limits) {
    expect(screen.getAllByText(text)).toHaveLength(1)
    expect(within(reasoning).queryByText(text)).toBeNull()
  }
  fireEvent.click(reasoning.querySelector('summary')!)
  expect(reasoning.open).toBe(true)
  fireEvent.click(summary)
  expect(boundary.open).toBe(false)
  expect(reasoning.open).toBe(true)
  expect(container.querySelector('[role="alert"], [aria-live]')).toBeNull()
  expect(container.textContent).not.toMatch(/已知故障|严重性|高风险|所有检查已通过/)
  expect(fetchMock).not.toHaveBeenCalled()
  expect(JSON.stringify(item)).toBe(before)
})

test.each([
  ['无 handoff', completion(10, null)],
  ['空 limits', completion(10, { ...saved(), limits: [] })],
  ['缺少 limits', completion(10, { ...saved(), limits: undefined })],
  ['limits 格式无效', completion(10, { ...saved(), limits: [' '] })],
  ['其它字段无效', completion(10, { ...saved(), reproduce: '非法步骤' })],
  ['引用无效', completion(10, { ...saved(), evidence: [{ ...handoff().evidence[0], source_seq: 10 }] })],
  ['普通发言自报', event(10, 'msg.said', { handoff: saved() })],
  ['非宿主完成', { ...completion(10, saved()), actor: { id: 'team', kind: 'agent', role: 'team' } }],
  ['已省略', { ...completion(10, saved()), payload: { handoff: saved(), handoff_status: 'omitted_invalid', handoff_warning: '说明已省略' } }],
] as const)('%s 不产生边界入口或残留原文', (_, evidence) => {
  render(<MessageDetails item={itemFor(evidence, { details: ['保留原验证说明'] })} />)
  expect(screen.queryByText(/^使用边界 · /)).toBeNull()
  for (const text of limits) expect(screen.queryByText(text)).toBeNull()
  expect(screen.getByText('保留原验证说明')).toBeTruthy()
})

test('仅引用省略时按既有校验保留边界，不恢复未验证引用', () => {
  const evidence = completion(10, saved())
  evidence.payload.handoff_status = 'references_omitted_invalid'
  evidence.payload.handoff_warning = '讲解已保留，引用核对失败未保存，不视为验证证据。'
  const { container } = render(<MessageDetails item={itemFor(evidence)} />)
  fireEvent.click(screen.getByText(label))
  expect(within(limitsFold()).getAllByRole('listitem').map(node => node.textContent)).toEqual(limits)
  expect(container.querySelector('blockquote')).toBeNull()
})

test.each([undefined, '未经核对的警告'])('引用省略缺失或伪造警告时不展示边界，旧的无状态记录仍可查看：%s', warning => {
  const evidence = completion(10, saved())
  evidence.payload.handoff_status = 'references_omitted_invalid'
  if (warning !== undefined) evidence.payload.handoff_warning = warning
  const { rerender } = render(<MessageDetails item={itemFor(evidence)} />)
  expect(screen.queryByText(label)).toBeNull()
  rerender(<MessageDetails item={itemFor(completion(10, saved()))} />)
  fireEvent.click(screen.getByText(label))
  expect(within(limitsFold()).getAllByRole('listitem').map(node => node.textContent)).toEqual(limits)
})

test('切换消息更新计数并重置开合，切为无效或普通消息后移除边界', () => {
  const { rerender } = render(<MessageDetails item={itemFor()} />)
  fireEvent.click(screen.getByText(label))
  expect(limitsFold().open).toBe(true)
  const next = itemFor(completion(20, { ...saved(), limits: ['新版只支持手动导入。'] }))
  rerender(<MessageDetails item={next} />)
  const fold = screen.getByText('使用边界 · 1项').closest('details')!
  expect(fold.open).toBe(false)
  expect(within(fold).getByText('新版只支持手动导入。')).toBeTruthy()
  for (const text of limits) expect(screen.queryByText(text)).toBeNull()
  rerender(<MessageDetails item={itemFor(completion(20, null))} />)
  expect(screen.queryByText(/^使用边界 · /)).toBeNull()
  rerender(<MessageDetails item={itemFor(event(21, 'msg.said', { handoff: saved() }))} />)
  expect(screen.queryByText(/^使用边界 · /)).toBeNull()
  expect(screen.queryByText('新版只支持手动导入。')).toBeNull()
})

test('历史入口只显示本条保存的边界，缺少 handoff 的旧交付不借用新边界', () => {
  const old = completion(8)
  delete old.payload.handoff
  const { rerender } = render(<MessageDetails item={itemFor(old, { historicalDelivery: true })} />)
  expect(screen.queryByText(/^使用边界 · /)).toBeNull()
  rerender(<MessageDetails item={itemFor(completion(10, saved()), { historicalDelivery: true })} />)
  expect(screen.getByText('这是这次历史交付当时记录的使用边界，不代表当前版本。')).toBeTruthy()
  expect(screen.queryByText('使用前，可以先看看这些适用条件与尚未验证的范围。')).toBeNull()
  fireEvent.click(screen.getByText(label))
  expect(within(limitsFold()).getAllByRole('listitem').map(node => node.textContent)).toEqual(limits)
})

test('长文本、空白和换行照实保留，HTML、链接与命令不执行也不按词标红', () => {
  const text = `  尚未验证，并非已知故障。\nhttps://example.invalid/${'long-path'.repeat(80)}\n<img src=x onerror=alert(1)> [打开](javascript:alert(1)) $(echo unsafe)  `
  const { container } = render(<MessageDetails item={itemFor(completion(10, { ...saved(), limits: [text] }))} />)
  const fold = screen.getByText('使用边界 · 1项').closest('details')!
  fireEvent.click(fold.querySelector('summary')!)
  expect(within(fold).getByRole('listitem').textContent).toBe(text)
  expect(container.querySelector('img, script, a, button, [role="alert"]')).toBeNull()
  expect(fold.querySelector('[class*="text-red"], [class*="text-amber"]')).toBeNull()
})
