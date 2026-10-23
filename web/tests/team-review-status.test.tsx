import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { TeamProgressPanel, TeamToolProgress } from '../src/TeamProgressPanel'
import type { TeamEvent, TeamReview, TeamState } from '../src/team'

const names = { author: '月亮', reviewer: '罐头' }
const state: TeamState = {
  enabled: true, objective: '修复边界条件', status: 'running', pending_count: 0,
  runs: [], assignments: [], checkpoint: '', revision: 20,
}
const review = (status: TeamReview['status'], overrides: Partial<TeamReview> = {}): TeamReview => ({
  root_seq: 30, request_seq: 30, author: 'author', reviewer: 'reviewer',
  artifacts: [{ seq: 25, filename: '边界说明.md', content_sha256: 'a'.repeat(64) }],
  status, revision: state.revision, ...overrides,
})
const panel = (team: TeamState, events: TeamEvent[] = []) => <TeamProgressPanel team={team} events={events} names={names} />

test.each([['index', '材料索引'], ['goal', '来源目标'], ['source', '来源对话'], ['artifact', '来源成果']])('来源材料读取 %s 显示中文范围和分页边界', (section, label) => {
  const actor = { id: 'team', kind: 'system', role: 'team' }
  render(<TeamToolProgress names={names} events={[
    { seq: 1, type: 'team.tool_started', actor, payload: { role: 'author', name: 'read_background', arguments: { section, artifact_seq: 8, source_id: 'source:3' } } },
    { seq: 2, type: 'team.tool_finished', actor, payload: { role: 'author', name: 'read_background', started_seq: 1,
      result: { status: 'ok', section, offset: 0, next_offset: 20, has_more: true, total_chars: 80, entries: section === 'index' ? [{ id: 'source:3' }] : undefined,
        source: { filename: '说明.txt', seq: 8 }, content: '历史正文片段' } } },
  ]} />)
  expect(screen.getByText('读取来源材料')).toBeTruthy()
  expect(screen.getByText(`读取范围：${label} · 创建时冻结的历史参考`)).toBeTruthy()
  expect(screen.getByText('还有后续材料 · 下一页起点 20，本页不代表全文。')).toBeTruthy()
  expect(screen.getByText('历史正文片段')).toBeTruthy()
  expect(screen.queryByText('read_background')).toBeNull()
  if (section === 'index') expect(screen.getByText('返回 1 项来源材料索引')).toBeTruthy()
  if (section === 'artifact') expect(screen.getByText('来源成果：#8')).toBeTruthy()
  if (section === 'source') expect(screen.getByText('来源引用：source:3')).toBeTruthy()
})

test.each([
  ['pending', '待复核'], ['changes_requested', '待修复'], ['approved', '已复核通过'],
  ['outdated', '新版待复核'], ['stale', '历史复核'],
] as const)('复核状态 %s 使用中文状态和真实伙伴名', (status, label) => {
  render(panel({ ...state, reviews: [review(status)] }))
  const region = screen.getByRole('region', { name: '修复与复核' })
  expect(within(region).getByText(label)).toBeTruthy()
  expect(within(region).getByText(/@月亮 送审 · @罐头 复核/)).toBeTruthy()
  expect(region.textContent).not.toContain('@author')
  expect(region.textContent).not.toContain('@reviewer')
  expect(within(screen.getByRole('region', { name: '团队进度' })).getByRole('region', { name: '修复与复核' })).toBe(region)
})

test('送审负责人不被误称为实际修改者', () => {
  render(<TeamProgressPanel team={{ ...state, reviews: [review('pending', { author: 'owner' })] }}
    events={[]} names={{ ...names, owner: '店长' }} />)
  const region = screen.getByRole('region', { name: '修复与复核' })
  expect(within(region).getByText(/@店长 送审 · @罐头 复核/)).toBeTruthy()
  expect(region.textContent).not.toContain('@店长 修改')
})

test('旧数据缺少 reviews 或数组为空时不显示空区域', () => {
  const view = render(panel(state))
  expect(screen.queryByRole('region', { name: '修复与复核' })).toBeNull()
  view.rerender(panel({ ...state, reviews: [] }))
  expect(screen.queryByText('修复与复核')).toBeNull()
})

test('未关闭复核优先可见，已通过和历史记录默认收起且不改变输入顺序', () => {
  const reviews = [review('approved'), review('stale', { root_seq: 31, request_seq: 31 }), review('pending', { root_seq: 32, request_seq: 32 })]
  render(panel({ ...state, reviews }))
  const region = screen.getByRole('region', { name: '修复与复核' })
  const lists = within(region).getAllByRole('list')
  expect(within(lists[0]).getByText('待复核')).toBeTruthy()
  expect(screen.getByText('待复核').closest('details')).toBeNull()
  const closed = screen.getByText('已通过与历史 · 2 项').closest('details')!
  expect(closed.open).toBe(false)
  expect(within(closed).getByText('已复核通过')).toBeTruthy()
  expect(within(closed).getByText('历史复核')).toBeTruthy()
  fireEvent.click(screen.getByText('已通过与历史 · 2 项'))
  expect(closed.open).toBe(true)
  expect(reviews.map((item) => item.status)).toEqual(['approved', 'stale', 'pending'])
})

test('文件和长反馈默认折叠，展开后保留文件名与反馈且不泄露摘要哈希', () => {
  const feedback = '请补充边界场景的验证。\n'.repeat(100)
  const item = review('changes_requested', { feedback })
  render(panel({ ...state, reviews: [item] }))
  const summary = screen.getByText('文件与反馈 · 1 份')
  const detail = summary.closest('details')!
  expect(detail.open).toBe(false)
  expect(screen.getByText('边界说明.md · 成果 #25').closest('details')).toBe(detail)
  expect(detail.querySelector('p')?.textContent).toBe(feedback)
  expect(summary.textContent).not.toContain(feedback)
  expect(screen.getByRole('region', { name: '修复与复核' }).textContent).not.toContain(item.artifacts[0].content_sha256)
  fireEvent.click(summary)
  expect(detail.open).toBe(true)
})

test('同一成果变化后撤回旧通过状态，新提交不沿用旧反馈', () => {
  const approved = review('approved', { feedback: '旧版已验证' })
  const view = render(panel({ ...state, reviews: [approved] }))
  expect(screen.getByText('已复核通过')).toBeTruthy()
  view.rerender(panel({ ...state, reviews: [{ ...approved, status: 'outdated' }] }))
  expect(screen.queryByText('已复核通过')).toBeNull()
  expect(screen.queryByText(/已通过与历史/)).toBeNull()
  expect(screen.getByText('新版待复核').closest('details')).toBeNull()
  view.rerender(panel({ ...state, reviews: [review('pending', {
    request_seq: 40, artifacts: [{ seq: 39, filename: '边界说明.md', content_sha256: 'b'.repeat(64) }],
  })] }))
  expect(screen.queryByText('已复核通过')).toBeNull()
  expect(screen.queryByText('旧版已验证')).toBeNull()
  expect(screen.getByText('待复核')).toBeTruthy()
  expect(screen.getByText('边界说明.md · 成果 #39')).toBeTruthy()
})

test('后端将复核标为 stale 时展示历史复核', () => {
  const approved = review('approved')
  const view = render(panel({ ...state, reviews: [approved] }))
  expect(screen.getByText('已复核通过')).toBeTruthy()
  view.rerender(panel({ ...state, revision: 50, reviews: [{ ...approved, status: 'stale' }] }))
  expect(screen.queryByText('已复核通过')).toBeNull()
  expect(screen.getByText('历史复核')).toBeTruthy()
  expect(screen.getByText('历史复核').closest('details')?.open).toBe(false)
  expect(approved.status).toBe('approved')
})

test('正常交付后 team.revision 大于 approved.revision 仍展示已复核通过', () => {
  const approved = review('approved')
  const view = render(panel({ ...state, reviews: [approved] }))
  view.rerender(panel({ ...state, status: 'done', revision: 50, reviews: [approved] }))
  expect(screen.getByText('已复核通过')).toBeTruthy()
  expect(screen.queryByText('历史复核')).toBeNull()
  const closed = screen.getByText('已通过与历史 · 1 项').closest('details')!
  expect(closed.open).toBe(false)
  fireEvent.click(screen.getByText('已通过与历史 · 1 项'))
  expect(closed.open).toBe(true)
  expect(within(closed).getByText('已复核通过')).toBeTruthy()
  expect(approved.revision).toBe(20)
})

test('新复核工具在现有工具过程中使用中文映射', () => {
  const events: TeamEvent[] = ['request_review', 'record_review'].map((name, index) => ({
    seq: 60 + index, type: 'team.tool_started', actor: { id: 'team', kind: 'system', role: 'team' },
    payload: { name, role: index ? 'reviewer' : 'author' },
  }))
  render(panel(state, events))
  expect(screen.getByText('请求复核')).toBeTruthy()
  expect(screen.getByText('记录复核结论')).toBeTruthy()
  expect(screen.queryByText('request_review')).toBeNull()
  expect(screen.queryByText('record_review')).toBeNull()
})
