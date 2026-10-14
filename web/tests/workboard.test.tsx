import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { WorkboardPanel } from '../src/WorkboardPanel'
import { ArtifactPreview } from '../src/Artifact'

function Workroom({ taskId = 'task-a' }: { taskId?: string }) {
  const [open, setOpen] = useState(false)
  return <><button id="workboard-trigger" onClick={() => setOpen(true)}>成果</button><textarea aria-label="草稿" defaultValue="继续看看" /><WorkboardPanel taskId={taskId} open={open} onOpenChange={setOpen}><ArtifactPreview item={{ seq: 7, title: '猫咖成果', summary: '摘要', content: '这是成果正文', format: 'text' }} /></WorkboardPanel></>
}

test('桌面标题位于滚动区外，同任务更新保留位置，仅切任务时复位', () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  const onOpenChange = vi.fn()
  const view = render(<WorkboardPanel taskId="task-a" open={false} onOpenChange={onOpenChange}><p>甲任务成果</p></WorkboardPanel>)
  const panel = screen.getByRole('complementary', { name: '成果与过程' })
  const content = screen.getByText('甲任务成果').parentElement!
  const heading = within(panel).getByRole('heading', { name: '成果与过程' })
  expect(content.parentElement).toBe(panel)
  expect(heading.parentElement?.parentElement).toBe(panel)
  expect(content.contains(heading)).toBe(false)
  content.scrollTop = 320

  view.rerender(<WorkboardPanel taskId="task-a" open={false} onOpenChange={onOpenChange}><p>甲任务更新后的成果</p></WorkboardPanel>)
  expect(screen.getByText('甲任务更新后的成果').parentElement).toBe(content)
  expect(content.scrollTop).toBe(320)

  view.rerender(<WorkboardPanel taskId="task-b" open={false} onOpenChange={onOpenChange}><p>乙任务成果</p></WorkboardPanel>)
  expect(screen.getByText('乙任务成果').parentElement).toBe(content)
  expect(content.scrollTop).toBe(0)
})

test('任务标识变化不重挂手机抽屉，关闭后仍返回原入口', async () => {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
  const view = render(<Workroom taskId="task-a" />)
  const trigger = screen.getByRole('button', { name: '成果', exact: true })
  trigger.focus(); fireEvent.click(trigger)
  const panel = screen.getByRole('dialog', { name: '成果与过程' })
  const close = within(panel).getByRole('button', { name: '关闭成果面板' })
  await waitFor(() => expect(document.activeElement).toBe(close))

  view.rerender(<Workroom taskId="task-b" />)
  expect(screen.getByRole('dialog', { name: '成果与过程' })).toBe(panel)
  expect(document.activeElement).toBe(close)
  fireEvent.click(close)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(trigger)
  expect((screen.getByRole('textbox', { name: '草稿' }) as HTMLTextAreaElement).value).toBe('继续看看')
})

test('成果面板聚焦关闭按钮，Esc 返回聊天入口并保留草稿', async () => {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
  render(<Workroom />)
  const trigger = screen.getByRole('button', { name: '成果', exact: true })
  trigger.focus(); fireEvent.click(trigger)
  const panel = screen.getByRole('dialog', { name: '成果与过程' })
  await waitFor(() => expect(document.activeElement).toBe(within(panel).getByRole('button', { name: '关闭成果面板' })))
  expect(document.querySelector('#workboard-trigger')?.closest('[aria-hidden="true"]')).toBeTruthy()
  fireEvent.keyDown(panel, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(trigger)
  expect((screen.getByRole('textbox', { name: '草稿' }) as HTMLTextAreaElement).value).toBe('继续看看')
})

test('正文在成果板之上展开，逐层关闭不会跳回背景聊天', async () => {
  render(<Workroom />)
  fireEvent.click(screen.getByRole('button', { name: '成果', exact: true }))
  const entry = within(screen.getByRole('dialog', { name: '成果与过程' })).getByRole('button', { name: '查看正文' })
  entry.focus(); fireEvent.click(entry)
  expect(within(screen.getByRole('dialog', { name: '猫咖成果' })).getByText('这是成果正文')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '关闭成果正文' }))
  await waitFor(() => expect(screen.getByRole('dialog', { name: '成果与过程' })).toBeTruthy())
  expect(document.activeElement).toBe(entry)
  fireEvent.click(screen.getByRole('button', { name: '关闭成果面板' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

test('切到桌面自动收起弹层，媒体监听器卸载后移除', async () => {
  let listener: (() => void) | undefined
  const remove = vi.fn()
  const media = { matches: false, addEventListener: vi.fn((_name: string, callback: () => void) => { listener = callback }), removeEventListener: remove }
  vi.stubGlobal('matchMedia', vi.fn(() => media))
  const view = render(<Workroom />)
  fireEvent.click(screen.getByRole('button', { name: '成果', exact: true }))
  expect(screen.getByRole('dialog')).toBeTruthy()
  media.matches = true
  act(() => listener?.())
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  view.unmount()
  expect(remove).toHaveBeenCalledWith('change', listener)
})
