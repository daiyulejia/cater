import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { TeamDirectory } from '../src/TeamDirectory'
import type { TeamAvailability } from '../src/TeamDirectory'
import App from '../src/App'

const members = [{ id: 'product', name: '月亮' }, { id: 'design', name: '罐头' }]
const available: TeamAvailability = { checked_at: '2026-10-14T03:00:00Z', all_available: true, members: members.map((member) => ({ role_id: member.id, status: 'available', reason: 'recent_success' })) }

function Directory({ state, refresh, mention }: { state?: TeamAvailability; refresh: () => void; mention: (name: string) => void }) {
  const [open, setOpen] = useState(false)
  return <><textarea aria-label="聊天草稿" defaultValue="留在这儿" /><TeamDirectory members={members} availability={state} open={open} onOpenChange={setOpen} refreshing={false} error="" onRefresh={refresh} onMention={mention} portrait={() => null} /></>
}

test('伙伴页呈现营业依据，刷新和点名不自动发送', async () => {
  const refresh = vi.fn(), mention = vi.fn()
  render(<Directory state={available} refresh={refresh} mention={mention} />)
  expect(screen.getByText('全员营业')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  const page = screen.getByRole('dialog', { name: '今天在店里' })
  expect(within(page).getAllByText('近五分钟内调用成功')).toHaveLength(2)
  expect(refresh).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '刷新伙伴状态' }))
  expect(refresh).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('button', { name: '点名 月亮' }))
  expect(mention).toHaveBeenCalledWith('月亮')
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect((screen.getByRole('textbox', { name: '聊天草稿' }) as HTMLTextAreaElement).value).toBe('留在这儿')
  expect(document.activeElement).toBe(screen.getByRole('textbox', { name: '聊天草稿' }))
})

test('休假、演示与未知状态不冒充全员营业', () => {
  const state: TeamAvailability = { ...available, members: [{ role_id: 'product', status: 'on_leave', reason: 'missing_key' }, { role_id: 'design', status: 'unverified', reason: 'not_recently_checked' }] }
  const view = render(<Directory state={state} refresh={vi.fn()} mention={vi.fn()} />)
  expect(screen.queryByText('全员营业')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  expect(screen.getByText('尚未提供模型凭据')).toBeTruthy()
  expect(screen.getByText('待确认')).toBeTruthy()
  view.unmount()
  render(<Directory state={{ ...available, all_available: false, members: members.map((member) => ({ role_id: member.id, status: 'demo', reason: 'local_demo' })) }} refresh={vi.fn()} mention={vi.fn()} />)
  expect(screen.getByText('演示营业')).toBeTruthy()
})

test('真实页面刷新失败后撤回营业声明，点名回原任务草稿', async () => {
  let failed = false
  const fetch = vi.fn(async (url: string) => {
    if (url === '/api/organization/profile') {
      if (failed) throw new Error('offline')
      return json({ leader: { id: 'store_manager', name: '店长' }, workers: members, cabinet: [], availability: available })
    }
    if (url === '/api/organization') return json([])
    const task = { id: 'task', goal: '这项工作', messages: [], assignments: [], status: 'pending' }
    return json(url === '/api/tasks' ? [task] : { task, events: [], activity: { active: false, state: 'idle' } })
  })
  const json = (body: unknown) => new Response(JSON.stringify(body))
  vi.stubGlobal('fetch', fetch)
  render(<App />)
  await screen.findByText('全员营业')
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '补充说明' } })
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '点名 罐头' }))
  await waitFor(() => expect(input.value).toBe('补充说明 @罐头 '))
  failed = true
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  await screen.findByRole('alert')
  expect(screen.queryByText('全员营业')).toBeNull()
  expect(fetch.mock.calls.some((call) => call[0].includes('/command'))).toBe(false)
})
