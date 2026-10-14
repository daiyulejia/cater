import { useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ConversationMembers } from '../src/ConversationMembers'

const members = [
  { id: 'product', name: '月亮' },
  { id: 'design', name: '罐头' },
  { id: 'dev', name: '米寇' },
  { id: 'test', name: '三角' },
]
const portrait = (id: string) => <span role="img" aria-label={`头像 ${id}`} />

test('入口保持紧凑，展开只显示传入的当前对话成员', () => {
  const draw = vi.fn(portrait)
  render(<ConversationMembers participants={['design', 'product']} members={members} portrait={draw} onMention={vi.fn()} onOpenDirectory={vi.fn()} />)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.queryByText('罐头')).toBeNull()
  const trigger = screen.getByRole('button', { name: '本次伙伴，2 位' })
  expect(trigger.getAttribute('aria-haspopup')).toBe('dialog')
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(trigger)
  const dialog = screen.getByRole('dialog', { name: '本次伙伴' })
  expect(trigger.getAttribute('aria-expanded')).toBe('true')
  expect(within(dialog).getAllByRole('listitem').map((row) => row.textContent)).toEqual(['罐头', '月亮'])
  expect(within(dialog).queryByText('米寇')).toBeNull()
  expect(within(dialog).queryByText('三角')).toBeNull()
  expect(dialog.textContent).not.toMatch(/营业|在线|都在|今天在店里/)
  expect(draw.mock.calls.every(([id]) => ['design', 'product'].includes(id))).toBe(true)
})

test('入口最多三个头像，人数仍包含全部参与者', () => {
  render(<ConversationMembers participants={members.map((member) => member.id)} members={members} portrait={portrait} onMention={vi.fn()} onOpenDirectory={vi.fn()} />)
  const trigger = screen.getByRole('button', { name: '本次伙伴，4 位' })
  expect(within(trigger).getAllByRole('img', { hidden: true })).toHaveLength(3)
})

test('参与 ID 与团队重复项去重，保留首次出现顺序', () => {
  render(<ConversationMembers participants={['design', 'product', 'design', 'product']} members={[...members, { id: 'design', name: '重复名字' }]} portrait={portrait} onMention={vi.fn()} onOpenDirectory={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '本次伙伴，2 位' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getAllByRole('listitem').map((row) => row.textContent)).toEqual(['罐头', '月亮'])
  expect(screen.queryByText('重复名字')).toBeNull()
})

test('未知 ID 保留身份待确认且不可点名，空 ID 不计数', () => {
  const draw = vi.fn(portrait)
  render(<ConversationMembers participants={['former_member', '', '  ', 'product', 'former_member']} members={members} portrait={draw} onMention={vi.fn()} onOpenDirectory={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '本次伙伴，2 位' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText('former_member')).toBeTruthy()
  expect(within(dialog).getByText('身份待确认')).toBeTruthy()
  expect(within(dialog).queryByRole('button', { name: '点名 former_member' })).toBeNull()
  expect(within(dialog).getAllByRole('listitem')).toHaveLength(2)
  expect(draw.mock.calls.every(([id]) => id === 'product')).toBe(true)
})

test('空对话不以全部团队成员补位，仍能打开伙伴簿', async () => {
  const directory = vi.fn()
  render(<ConversationMembers participants={[]} members={members} portrait={portrait} onMention={vi.fn()} onOpenDirectory={directory} />)
  fireEvent.click(screen.getByRole('button', { name: '本次伙伴，0 位' }))
  expect(screen.getByText('暂无对话成员')).toBeTruthy()
  expect(screen.queryByRole('list')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '猫咖伙伴簿' }))
  await waitFor(() => expect(directory).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('点名回调传 ID，焦点回到对应会话草稿且无网络或提交动作', async () => {
  const mention = vi.fn(), directory = vi.fn(), submit = vi.fn(), fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  render(<><textarea aria-label="其他草稿" /><section><form onSubmit={submit}>
    <textarea aria-label="当前会话草稿" defaultValue="补充说明" />
    <ConversationMembers participants={['design']} members={members} portrait={portrait} onMention={mention} onOpenDirectory={directory} />
  </form></section></>)
  fireEvent.click(screen.getByRole('button', { name: '本次伙伴，1 位' }))
  fireEvent.click(screen.getByRole('button', { name: '点名 罐头' }))
  await waitFor(() => expect(mention).toHaveBeenCalledWith('design'))
  expect(mention).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('dialog')).toBeNull()
  const composer = screen.getByRole('textbox', { name: '当前会话草稿' }) as HTMLTextAreaElement
  expect(document.activeElement).toBe(composer)
  expect(composer.value).toBe('补充说明')
  expect(directory).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
  expect(submit).not.toHaveBeenCalled()
})

test('关闭与 Escape 返回入口，重新打开不重复触发上次点名', async () => {
  const mention = vi.fn(), directory = vi.fn()
  render(<ConversationMembers participants={['product']} members={members} portrait={portrait} onMention={mention} onOpenDirectory={directory} />)
  const trigger = screen.getByRole('button', { name: '本次伙伴，1 位' })
  trigger.focus()
  fireEvent.click(trigger)
  const close = screen.getByRole('button', { name: '关闭本次伙伴' })
  expect(document.activeElement).toBe(close)
  fireEvent.click(close)
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  fireEvent.click(trigger)
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(trigger)
  fireEvent.click(trigger)
  fireEvent.click(screen.getByRole('button', { name: '点名 月亮' }))
  await waitFor(() => expect(mention).toHaveBeenCalledTimes(1))
  expect(document.activeElement).toBe(trigger)
  fireEvent.click(trigger)
  fireEvent.click(screen.getByRole('button', { name: '关闭本次伙伴' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(mention).toHaveBeenCalledTimes(1)
  expect(directory).not.toHaveBeenCalled()
})

test('打开全局伙伴簿完成弹窗交接并保留新弹窗焦点', async () => {
  const mention = vi.fn(), directory = vi.fn()
  function Handoff() {
    const [open, setOpen] = useState(false)
    return <>
      <ConversationMembers participants={['product']} members={members} portrait={portrait} onMention={mention} onOpenDirectory={() => { directory(); setOpen(true) }} />
      <Dialog.Root open={open} onOpenChange={setOpen}>
        <Dialog.Portal><Dialog.Overlay /><Dialog.Content>
          <Dialog.Title>猫咖伙伴簿</Dialog.Title><Dialog.Description>全部伙伴</Dialog.Description>
          <Dialog.Close asChild><button type="button">关闭伙伴簿</button></Dialog.Close>
        </Dialog.Content></Dialog.Portal>
      </Dialog.Root>
    </>
  }
  render(<Handoff />)
  fireEvent.click(screen.getByRole('button', { name: '本次伙伴，1 位' }))
  fireEvent.click(screen.getByRole('button', { name: '猫咖伙伴簿' }))
  await waitFor(() => expect(screen.getByRole('dialog', { name: '猫咖伙伴簿' })).toBeTruthy())
  expect(screen.queryByRole('dialog', { name: '本次伙伴' })).toBeNull()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: '关闭伙伴簿' }))
  expect(directory).toHaveBeenCalledTimes(1)
  expect(mention).not.toHaveBeenCalled()
})
