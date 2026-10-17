import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ChatFilters } from '../src/ChatFilters'
import type { ChatFilter } from '../src/ChatFilters'

const labels = ['全部', '协作', '决策', '成果']
const values: ChatFilter[] = ['all', 'collaboration', 'decision', 'deliverable']
const trigger = (label = '全部') => screen.getByRole('button', { name: `筛选对话：${label}` })

test('桌面保留四段按钮，状态完全由 value 控制', () => {
  const change = vi.fn()
  const view = render(<ChatFilters value="all" onChange={change} />)
  const group = screen.getByRole('group', { name: '对话筛选' })
  expect(group.classList.contains('hidden')).toBe(true)
  expect(group.classList.contains('sm:flex')).toBe(true)
  expect(within(group).getAllByRole('button').map((button) => button.textContent)).toEqual(labels)
  fireEvent.click(within(group).getByRole('button', { name: '决策' }))
  expect(change).toHaveBeenCalledExactlyOnceWith('decision')
  expect(within(group).getByRole('button', { name: '全部' }).getAttribute('aria-pressed')).toBe('true')
  view.rerender(<ChatFilters value="decision" onChange={change} />)
  for (const [index, button] of within(group).getAllByRole('button').entries()) {
    expect(button.getAttribute('aria-pressed')).toBe(String(index === 2))
  }
  expect(trigger('决策').title).toBe('筛选对话：决策')
})

test.each(values)('手机入口仅图标，四个原生 radio 反映受控值 %s', (value) => {
  render(<ChatFilters value={value} onChange={vi.fn()} />)
  const button = trigger(labels[values.indexOf(value)])
  expect(button.classList.contains('origin-tool')).toBe(true)
  expect(button.classList.contains('sm:hidden')).toBe(true)
  expect(button.parentElement?.classList.contains('sm:hidden')).toBe(true)
  expect(button.textContent).toBe('')
  fireEvent.click(button)
  const dialog = screen.getByRole('dialog', { name: '筛选对话' })
  const radios = within(dialog).getAllByRole<HTMLInputElement>('radio')
  expect(radios.map((radio) => radio.type)).toEqual(values.map(() => 'radio'))
  expect(radios.map((radio) => radio.value)).toEqual(values)
  labels.forEach((label, index) => {
    expect(within(dialog).getByLabelText(label)).toBe(radios[index])
    expect(radios[index].checked).toBe(values[index] === value)
  })
})

test('选择只回调一次并关闭，重新打开仍读取父级 value', async () => {
  const change = vi.fn()
  const view = render(<ChatFilters value="all" onChange={change} />)
  const button = trigger()
  fireEvent.click(button)
  fireEvent.click(within(screen.getByRole('dialog')).getByLabelText('协作'))
  expect(change).toHaveBeenCalledExactlyOnceWith('collaboration')
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(document.activeElement).toBe(button))
  fireEvent.click(button)
  expect(within(screen.getByRole('dialog')).getByRole<HTMLInputElement>('radio', { name: '全部' }).checked).toBe(true)
  view.rerender(<ChatFilters value="deliverable" onChange={change} />)
  expect(within(screen.getByRole('dialog')).getByRole<HTMLInputElement>('radio', { name: '成果' }).checked).toBe(true)
  expect(change).toHaveBeenCalledTimes(1)
})

test('再次点击当前项也关闭且仅回调一次', () => {
  const change = vi.fn()
  render(<ChatFilters value="decision" onChange={change} />)
  fireEvent.click(trigger('决策'))
  fireEvent.click(within(screen.getByRole('dialog')).getByText('决策'))
  expect(change).toHaveBeenCalledExactlyOnceWith('decision')
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('X、Escape 和外部点击只关闭，焦点恢复原入口', async () => {
  const change = vi.fn()
  render(<ChatFilters value="collaboration" onChange={change} />)
  const button = trigger('协作')
  for (const action of ['close', 'escape', 'outside']) {
    button.focus()
    fireEvent.click(button)
    const dialog = screen.getByRole('dialog')
    if (action === 'close') fireEvent.click(within(dialog).getByRole('button', { name: '关闭筛选对话' }))
    if (action === 'escape') fireEvent.keyDown(dialog, { key: 'Escape' })
    if (action === 'outside') {
      // Radix installs its document pointer listener after the opening event.
      await new Promise((resolve) => setTimeout(resolve, 0))
      const overlay = dialog.previousElementSibling!
      fireEvent.pointerDown(overlay, { pointerType: 'mouse', button: 0 })
      fireEvent.click(overlay)
    }
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(button))
    expect(change).not.toHaveBeenCalled()
  }
})

test('原生 radio 键盘激活只选择一次，不触发表单提交或网络请求', () => {
  const change = vi.fn(), submit = vi.fn((event) => event.preventDefault()), fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  function Controlled() {
    const [value, setValue] = useState<ChatFilter>('all')
    return <form onSubmit={submit}><ChatFilters value={value} onChange={(next) => { change(next); setValue(next) }} /></form>
  }
  render(<Controlled />)
  fireEvent.click(trigger())
  const radio = within(screen.getByRole('dialog')).getByRole('radio', { name: '成果' })
  radio.focus()
  fireEvent.keyDown(radio, { key: ' ' })
  expect(change).not.toHaveBeenCalled()
  fireEvent.keyUp(radio, { key: ' ' })
  // jsdom does not synthesize the browser's native Space activation click.
  fireEvent.click(radio, { detail: 0 })
  expect(change).toHaveBeenCalledExactlyOnceWith('deliverable')
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(trigger('成果')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '决策' }))
  expect(change).toHaveBeenCalledTimes(2)
  expect(submit).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})

test('多个实例的 radio id 和 name 不碰撞', () => {
  render(<><ChatFilters value="all" onChange={vi.fn()} /><ChatFilters value="decision" onChange={vi.fn()} /></>)
  fireEvent.click(trigger())
  const first = screen.getAllByRole<HTMLInputElement>('radio').map(({ id, name }) => ({ id, name }))
  fireEvent.click(screen.getByRole('button', { name: '关闭筛选对话' }))
  fireEvent.click(trigger('决策'))
  const second = screen.getAllByRole<HTMLInputElement>('radio')
  expect(new Set([...first, ...second].map(({ id }) => id)).size).toBe(8)
  expect(new Set(first.map(({ name }) => name)).size).toBe(1)
  expect(new Set(second.map(({ name }) => name)).size).toBe(1)
  expect(second[0].name).not.toBe(first[0].name)
})
