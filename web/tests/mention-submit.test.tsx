import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, test, vi } from 'vitest'
import App from '../src/App'
import { MentionInput } from '../src/MentionInput'

const members = [{ id: 'moon', name: 'Moon' }, { id: 'sun', name: 'Sun' }]
const shortcuts = [{}, { ctrlKey: true }, { metaKey: true }]

function Composer({ submit }: { submit: () => void }) {
  const [value, setValue] = useState('')
  return <MentionInput value={value} label="Message" placeholder="Message" members={members} onChange={setValue} onSubmit={submit} portrait={() => null} />
}

function write(input: HTMLTextAreaElement, value: string) {
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value, selectionStart: value.length, selectionEnd: value.length } })
}

function setup() {
  const submit = vi.fn()
  render(<Composer submit={submit} />)
  const input = screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement
  return { input, submit }
}

test.each(shortcuts)('Enter submits once without inserting a newline: %j', (modifiers) => {
  const { input, submit } = setup()
  write(input, 'First line\nSecond line')
  expect(fireEvent.keyDown(input, { key: 'Enter', ...modifiers })).toBe(false)
  expect(submit).toHaveBeenCalledTimes(1)
  expect(input.value).toBe('First line\nSecond line')
})

test.each(shortcuts)('Shift+Enter leaves the native newline behavior intact: %j', (modifiers) => {
  const { input, submit } = setup()
  write(input, '@')
  expect(fireEvent.keyDown(input, { key: 'Enter', shiftKey: true, ...modifiers })).toBe(true)
  expect(submit).not.toHaveBeenCalled()
  expect(input.value).toBe('@')
  expect(screen.getAllByRole('option')).toHaveLength(2)
})

test.each(shortcuts)('Enter selects the active mention before submitting: %j', (modifiers) => {
  const { input, submit } = setup()
  write(input, '@')
  fireEvent.keyDown(input, { key: 'ArrowDown' })
  expect(fireEvent.keyDown(input, { key: 'Enter', ...modifiers })).toBe(false)
  expect(input.value).toBe('@Sun ')
  expect(screen.queryByRole('listbox')).toBeNull()
  expect(submit).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: 'Enter', repeat: true, ...modifiers })
  expect(submit).not.toHaveBeenCalled()
  fireEvent.keyUp(input, { key: 'Enter' })
  fireEvent.keyDown(input, { key: 'Enter', ...modifiers })
  expect(submit).toHaveBeenCalledTimes(1)
})

test.each(shortcuts)('Holding Enter cannot submit again or choose a mention: %j', (modifiers) => {
  const { input, submit } = setup()
  write(input, 'Message')
  fireEvent.keyDown(input, { key: 'Enter', ...modifiers })
  expect(fireEvent.keyDown(input, { key: 'Enter', repeat: true, ...modifiers })).toBe(false)
  expect(submit).toHaveBeenCalledTimes(1)
  write(input, '@')
  expect(fireEvent.keyDown(input, { key: 'Enter', repeat: true, ...modifiers })).toBe(false)
  expect(input.value).toBe('@')
  expect(submit).toHaveBeenCalledTimes(1)
})

test.each(shortcuts)('Composition lifecycle does not send or choose a mention: %j', (modifiers) => {
  const { input, submit } = setup()
  write(input, '@')
  fireEvent.compositionStart(input)
  expect(screen.queryByRole('listbox')).toBeNull()
  expect(fireEvent.keyDown(input, { key: 'Enter', ...modifiers })).toBe(true)
  expect(input.value).toBe('@')
  expect(submit).not.toHaveBeenCalled()
  fireEvent.compositionEnd(input)
  expect(screen.getAllByRole('option')).toHaveLength(2)
  fireEvent.keyDown(input, { key: 'Enter', ...modifiers })
  expect(input.value).toBe('@Moon ')
  expect(submit).not.toHaveBeenCalled()
})

test.each([
  { isComposing: true },
  { keyCode: 229 },
])('Native IME marker blocks Enter even without compositionStart: %j', (marker) => {
  const { input, submit } = setup()
  for (const value of ['Message', '@']) {
    write(input, value)
    for (const modifiers of shortcuts) {
      expect(fireEvent.keyDown(input, { key: 'Enter', ...marker, ...modifiers })).toBe(true)
      expect(input.value).toBe(value)
      expect(submit).not.toHaveBeenCalled()
    }
  }
  fireEvent.compositionEnd(input)
  expect(fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 })).toBe(true)
  expect(input.value).toBe('@')
  expect(submit).not.toHaveBeenCalled()
})

test.each(['@Unknown', 'mail@example.com'])('Enter submits when there is no selectable mention: %s', (value) => {
  const { input, submit } = setup()
  write(input, value)
  expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(false)
  expect(input.value).toBe(value)
  expect(submit).toHaveBeenCalledTimes(1)
})

test('Enter submits after Escape dismisses mention suggestions', () => {
  const { input, submit } = setup()
  write(input, '@')
  fireEvent.keyDown(input, { key: 'Escape' })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(input.value).toBe('@')
  expect(submit).toHaveBeenCalledTimes(1)
})

const json = (value: unknown) => new Response(JSON.stringify(value))
const summary = (id: string) => ({ id, goal: `${id} task`, status: 'pending', status_label: 'Pending', conversation_count: 1, latest_seq: 1, version: 1 })
const detail = (id: string) => ({
  task: { id, goal: `${id} task`, acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] },
  events: [{ seq: 1, type: 'task.created', actor: { id: 'human' }, payload: { goal: `${id} task` } }],
  summary: summary(id), activity: { active: false, state: 'idle' }, review: {},
})

describe('App submission guards through the shared MentionInput', () => {
  test.each(['new', 'existing'])('Plain Enter respects blank and pending guards in a %s conversation', async (mode) => {
    window.sessionStorage.clear()
    let resolve!: (response: Response) => void
    const pending = new Promise<Response>((done) => { resolve = done })
    const endpoint = mode === 'new' ? '/api/tasks' : '/api/tasks/a/command'
    const fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
      if (url === endpoint && init.method === 'POST') return pending
      if (url === '/api/task-summaries') return json({ tasks: mode === 'new' ? [] : [summary('a')] })
      if (url === '/api/organization') return json([])
      if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: 'Manager' }, cabinet: [], workers: [] })
      if (url === '/api/projects') return json([])
      if (url.startsWith('/api/rules')) return json({ rules: [] })
      if (url === '/api/tasks/a') return json(detail('a'))
      throw new Error(`Unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    await act(async () => { render(<App />) })
    const label = mode === 'new' ? '\u53d1\u8d77\u4efb\u52a1\u5bf9\u8bdd' : '\u5411\u56e2\u961f\u53d1\u9001\u6d88\u606f'
    const input = await screen.findByRole('textbox', { name: label }) as HTMLTextAreaElement
    const posts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
    write(input, ' \n\t ')
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(posts()).toHaveLength(0)
    write(input, '  A new direction  ')
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await waitFor(() => expect(posts()).toHaveLength(1))
    expect(posts()[0][0]).toBe(endpoint)
    expect(JSON.parse(String(posts()[0][1]?.body))[mode === 'new' ? 'goal' : 'instruction']).toBe('A new direction')
    write(input, 'Keep this next draft')
    for (const modifiers of shortcuts) {
      await act(async () => { fireEvent.keyDown(input, { key: 'Enter', ...modifiers }) })
    }
    fireEvent.keyDown(input, { key: 'Enter', repeat: true })
    expect(posts()).toHaveLength(1)
    expect(input.value).toBe('Keep this next draft')
    await act(async () => { resolve(json(detail(mode === 'new' ? 'b' : 'a'))) })
    if (mode === 'new') {
      fireEvent.click(screen.getByRole('button', { name: '\u65b0\u5efa\u4efb\u52a1' }))
    }
    expect((screen.getByRole('textbox', { name: label }) as HTMLTextAreaElement).value).toBe('Keep this next draft')
    expect(posts()).toHaveLength(1)
  })
})
