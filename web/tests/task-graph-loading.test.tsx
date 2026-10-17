import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import { detail, leader, members } from './native-handoff.fixtures'

const moduleGate = vi.hoisted(() => {
  let release!: () => void
  const ready = new Promise<void>(resolve => { release = resolve })
  return { ready, release }
})
vi.mock('../src/TaskGraph', async () => {
  await moduleGate.ready
  return { TaskGraph: () => <p>图谱内容已返回</p>, TaskGraphContents: () => <p>图谱内容已返回</p> }
})

test('图谱模块未返回仍能关闭，切群后迟到内容不重开或丢失草稿', async () => {
  const tasks = [detail([], 'graph-a'), detail([], 'graph-b')]
  tasks[0].task.goal = '甲任务'
  tasks[1].task.goal = '乙任务'
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetch = vi.fn((url: string, _init?: RequestInit) => {
    if (url === '/api/task-summaries') return json({ tasks: tasks.map(({ task }) => ({ id: task.id,
      goal: task.goal, status: task.status, status_label: '进行中', latest_seq: 1, version: 1, team: task.team })) })
    if (url === '/api/organization') return json(members)
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    const task = tasks.find(entry => url === `/api/tasks/${entry.task.id}`)
    if (task) return json(task)
    throw new Error(`Unexpected fixture request: ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  try {
    await act(async () => { render(<App />) })
    expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
    fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '保留乙的草稿' } })
    const trigger = screen.getByRole('button', { name: '任务脉络', exact: true })
    fireEvent.click(trigger)
    let dialog = screen.getByRole('dialog', { name: '任务脉络', exact: true })
    expect(within(dialog).getByRole('status').textContent).toContain('正在读取任务脉络')
    expect(within(dialog).queryByText('图谱内容已返回')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: '关闭任务脉络' }))
    expect(screen.queryByRole('dialog', { name: '任务脉络', exact: true })).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(trigger))
    fireEvent.click(trigger)
    dialog = screen.getByRole('dialog', { name: '任务脉络', exact: true })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '任务脉络', exact: true })).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(trigger))
    fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /甲任务/ }))
    expect(screen.getByRole('heading', { name: '甲任务', level: 1 })).toBeTruthy()
    await act(async () => { moduleGate.release() })
    expect(screen.queryByRole('dialog', { name: '任务脉络', exact: true })).toBeNull()
    expect(screen.queryByText(/正在读取任务脉络/)).toBeNull()
    fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /乙任务/ }))
    expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('保留乙的草稿')
    fireEvent.click(screen.getByRole('button', { name: '任务脉络', exact: true }))
    dialog = screen.getByRole('dialog', { name: '任务脉络', exact: true })
    expect(await within(dialog).findByText('图谱内容已返回')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: '关闭任务脉络' }))
    expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  } finally {
    await act(async () => { moduleGate.release() })
  }
})
