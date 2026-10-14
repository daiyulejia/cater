import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { TaskList } from '../src/TaskList'

const tasks = [
  { id: 'WEB-1', goal: '搭建猫咖网页', status: 'done', messages: [] },
  { id: 'BUG-2', goal: '复盘 API 问题', status: 'rework', messages: [] },
  { id: 'WEB-3', goal: '补充猫咖交互', status: 'done', messages: [], activity: { active: true } },
  { id: 'PLAN-4', goal: '讨论下一步', status: 'pending', messages: [] },
]
const stateButton = (name: string) => within(screen.getByRole('group', { name: '任务状态筛选' })).getByRole('button', { name })
const list = () => within(screen.getByRole('navigation', { name: '任务列表' }))
const search = (value: string) => fireEvent.change(screen.getByRole('searchbox'), { target: { value } })

function show() {
  const choose = vi.fn()
  render(<TaskList tasks={tasks} selectedId="WEB-3" drafts={{ 'WEB-3': '待发送' }} collapsed={false} searchOpen onChoose={choose} statusLabel={(task) => task.status} />)
  return choose
}

test('标题、编号和大小写搜索组合筛选，不发送或切换对话', () => {
  const choose = show()
  search(' 猫咖 ')
  expect(list().getAllByRole('button')).toHaveLength(2)
  fireEvent.click(stateButton('已完成'))
  expect(list().getAllByRole('button')).toHaveLength(1)
  expect(list().getByRole('button', { name: /搭建猫咖网页/ })).toBeTruthy()
  fireEvent.click(stateButton('全部')); search('api')
  expect(list().getByRole('button', { name: /复盘 API 问题/ })).toBeTruthy()
  search('web-3')
  expect(list().getByRole('button', { name: /补充猫咖交互/ }).getAttribute('aria-current')).toBe('page')
  expect(screen.getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  expect(choose).not.toHaveBeenCalled()
})

test('重新执行的已完成任务仍在未完成中，协作结束不等于任务完成', () => {
  show(); fireEvent.click(stateButton('未完成'))
  expect(list().getAllByRole('button')).toHaveLength(3)
  expect(list().getByRole('button', { name: /补充猫咖交互/ })).toBeTruthy()
  expect(list().queryByRole('button', { name: /搭建猫咖网页/ })).toBeNull()
})

test('清除关键词保留状态筛选，无匹配可恢复全部', () => {
  show(); fireEvent.click(stateButton('已完成')); search('不存在')
  expect(screen.getByText('没有匹配的任务。')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '清除任务搜索' }))
  expect(stateButton('已完成').getAttribute('aria-pressed')).toBe('true')
  expect(list().getAllByRole('button')).toHaveLength(1)
  search('不存在'); fireEvent.click(screen.getByRole('button', { name: '恢复全部任务' }))
  expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('')
  expect(stateButton('全部').getAttribute('aria-pressed')).toBe('true')
  expect(list().getAllByRole('button')).toHaveLength(4)
})

test('无任务不冒充无搜索结果，点击匹配项才导航', () => {
  const choose = show()
  fireEvent.click(list().getByRole('button', { name: /讨论下一步/ }))
  expect(choose).toHaveBeenCalledExactlyOnceWith('PLAN-4')
  render(<TaskList tasks={[]} selectedId={null} drafts={{}} collapsed={false} searchOpen={false} onChoose={vi.fn()} statusLabel={() => ''} />)
  expect(screen.getByText('还没有任务，先和店长聊聊吧。')).toBeTruthy()
})
