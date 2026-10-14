import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { MentionInput } from '../src/MentionInput'

const members = [{ id: 'product', name: '月亮' }, { id: 'design', name: '罐头' }, { id: 'manager', name: '店长' }]

function Composer({ submit, initial = '' }: { submit: () => void; initial?: string }) {
  const [value, setValue] = useState(initial)
  return <MentionInput value={value} label="消息" placeholder="说说吧" members={members} onChange={setValue} onSubmit={submit} portrait={() => null} />
}

function input() { return screen.getByRole('textbox', { name: '消息' }) as HTMLTextAreaElement }
function type(value: string, caret = value.length) {
  input().focus()
  fireEvent.change(input(), { target: { value, selectionStart: caret, selectionEnd: caret } })
}

test('中文点名过滤并保留后续正文，不自动发送', async () => {
  const submit = vi.fn()
  render(<Composer submit={submit} />)
  type('请@月帮我检查页面', 3)
  expect(screen.getAllByRole('option')).toHaveLength(1)
  fireEvent.click(screen.getByRole('option', { name: '月亮' }))
  expect(input().value).toBe('请 @月亮 帮我检查页面')
  await waitFor(() => expect(input().selectionStart).toBe('请 @月亮 '.length))
  expect(document.activeElement).toBe(input())
  expect(screen.queryByRole('listbox')).toBeNull()
  expect(submit).not.toHaveBeenCalled()
})

test('方向键循环选人，Enter 确认点名而快捷键显式发送', () => {
  const submit = vi.fn()
  render(<Composer submit={submit} />)
  type('@')
  fireEvent.keyDown(input(), { key: 'ArrowUp' })
  expect(screen.getByRole('option', { name: '店长' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(input(), { key: 'ArrowDown' })
  fireEvent.keyDown(input(), { key: 'ArrowDown' })
  expect(screen.getByRole('option', { name: '罐头' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(input(), { key: 'Enter' })
  expect(input().value).toBe('@罐头 ')
  expect(submit).not.toHaveBeenCalled()
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true })
  expect(submit).toHaveBeenCalledTimes(1)
})

test('邮件、选区和无匹配不误插入，Esc 和失焦收起建议', () => {
  render(<Composer submit={vi.fn()} />)
  type('hello@moon')
  expect(screen.queryByRole('listbox')).toBeNull()
  type('@未知')
  expect(screen.getByText('没有匹配的伙伴')).toBeTruthy()
  fireEvent.keyDown(input(), { key: 'Escape' })
  expect(screen.queryByRole('listbox')).toBeNull()
  type('@月')
  input().setSelectionRange(0, 2); fireEvent.select(input())
  expect(screen.queryByRole('listbox')).toBeNull()
  type('@')
  fireEvent.blur(input())
  expect(screen.queryByRole('listbox')).toBeNull()
})

test('中文输入法组合期间不处理 Enter 或发送快捷键', () => {
  const submit = vi.fn()
  render(<Composer submit={submit} />)
  type('@')
  fireEvent.compositionStart(input())
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true })
  expect(submit).not.toHaveBeenCalled()
  expect(screen.queryByRole('listbox')).toBeNull()
  fireEvent.compositionEnd(input())
  expect(screen.getByRole('listbox')).toBeTruthy()
  fireEvent.keyDown(input(), { key: 'Enter', keyCode: 229, ctrlKey: true })
  expect(submit).not.toHaveBeenCalled()
})
