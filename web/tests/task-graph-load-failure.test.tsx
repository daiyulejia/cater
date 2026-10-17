import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import { detail, leader, members } from './native-handoff.fixtures'

vi.mock('../src/TaskGraph', async () => { throw new TypeError('private-module-token-and-path') })

test('图谱导入失败仍有标题和关闭操作，不自动刷新或改变任务草稿', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const current = detail([], 'graph-failure')
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetch = vi.fn((url: string, _init?: RequestInit) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ id: current.task.id, goal: current.task.goal,
      status: current.task.status, status_label: '进行中', latest_seq: 1, version: 1, team: current.task.team }] })
    if (url === '/api/organization') return json(members)
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    if (url === `/api/tasks/${current.task.id}`) return json(current)
    throw new Error(`Unexpected fixture request: ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  await act(async () => { render(<App />) })
  fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '失败也不能丢的草稿' } })
  const trigger = screen.getByRole('button', { name: '任务脉络', exact: true })
  fireEvent.click(trigger)
  let dialog = screen.getByRole('dialog', { name: '任务脉络', exact: true })
  expect((await within(dialog).findByRole('alert')).textContent).toContain('这部分暂时打不开')
  expect(dialog.textContent).not.toContain('private-module')
  expect(within(dialog).getByRole('button', { name: '保留草稿并刷新' })).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭任务脉络' }))
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect(screen.queryByRole('dialog', { name: '任务脉络', exact: true })).toBeNull()
  expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('失败也不能丢的草稿')
  fireEvent.click(trigger)
  dialog = screen.getByRole('dialog', { name: '任务脉络', exact: true })
  expect((await within(dialog).findByRole('alert')).textContent).toContain('这部分暂时打不开')
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})
