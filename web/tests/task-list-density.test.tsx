import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { TaskList } from '../src/TaskList'

type Row = { id: string; goal: string; status: string; status_label: string; conversation_count?: number }
const row = (id: string, goal: string, count?: number, done = false): Row => ({
  id, goal, status: done ? 'done' : 'running', status_label: done ? '已交付' : '任务进行中',
  ...(count === undefined ? {} : { conversation_count: count }),
})
const props = {
  selectedId: 'active', drafts: {}, collapsed: false, searchOpen: true,
  onChoose: vi.fn(), statusLabel: (task: Row) => task.status_label,
}
const nav = () => within(screen.getByRole('navigation', { name: '任务列表' }))

test.each([undefined, 0, 233, 1000000])('消息数量 %s 不显示为条数、进度或完成状态', (count) => {
  render(<TaskList {...props} tasks={[row('active', '核对入口', count)]} />)
  const active = nav().getByRole('button', { name: '核对入口 任务进行中' })
  expect(active.getAttribute('aria-current')).toBe('page')
  expect(active.textContent).toBe('核对入口任务进行中')
  expect(active.textContent).not.toMatch(/条对话|条消息|对话待同步|已完成|已交付|%/)
  expect(screen.queryByRole('progressbar')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '未完成', exact: true }))
  expect(nav().getByRole('button', { name: '核对入口 任务进行中' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '已完成', exact: true }))
  expect(nav().queryByRole('button', { name: '核对入口 任务进行中' })).toBeNull()
  expect(nav().getByText('没有匹配的任务。')).toBeTruthy()
  expect(nav().getByRole('button', { name: '恢复全部任务' })).toBeTruthy()
})

test('去掉条数后选中、搜索、草稿标记及回调仍正常，零条记录也可按真实状态交付', () => {
  const tasks = [row('active', '入口核对', 1000000), row('other', '兼容核对'), row('done', '已验收说明', 0, true)]
  const choose = vi.fn()
  const drafts = { active: '尚未发送的要求', other: '  ' }
  const view = render(<TaskList {...props} tasks={tasks} drafts={drafts} onChoose={choose} />)
  const active = nav().getByRole('button', { name: /入口核对.*任务进行中/ })
  expect(within(active).getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  const other = nav().getByRole('button', { name: '兼容核对 任务进行中' })
  expect(within(other).queryByRole('img', { name: '有未发送内容' })).toBeNull()
  fireEvent.click(other)
  expect(choose.mock.calls).toEqual([['other']])
  view.rerender(<TaskList {...props} tasks={tasks} drafts={drafts} onChoose={choose} selectedId="other" />)
  expect(nav().getByRole('button', { name: '兼容核对 任务进行中' }).getAttribute('aria-current')).toBe('page')
  expect(nav().getByRole('button', { name: /入口核对/ }).getAttribute('aria-current')).toBeNull()

  const search = screen.getByRole('searchbox', { name: '搜索任务标题或编号' })
  fireEvent.change(search, { target: { value: 'active' } })
  expect(nav().getAllByRole('button')).toHaveLength(1)
  expect(within(nav().getByRole('button')).getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  expect(screen.getByRole('status').textContent).toBe('找到 1 / 3 件事')
  fireEvent.click(screen.getByRole('button', { name: '清除任务搜索' }))
  fireEvent.change(search, { target: { value: '兼容' } })
  expect(nav().getByRole('button', { name: '兼容核对 任务进行中' })).toBeTruthy()
  expect(nav().getAllByRole('button')).toHaveLength(1)

  fireEvent.click(screen.getByRole('button', { name: '清除任务搜索' }))
  fireEvent.click(screen.getByRole('button', { name: '已完成', exact: true }))
  expect(nav().getByRole('button', { name: '已验收说明 已交付' })).toBeTruthy()
  expect(nav().getAllByRole('button')).toHaveLength(1)
  expect(screen.getByRole('navigation', { name: '任务列表' }).textContent).not.toMatch(/条对话|条消息|对话待同步/)
  expect(choose).toHaveBeenCalledTimes(1)
})
