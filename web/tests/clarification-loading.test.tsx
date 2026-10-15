import { act, fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'

const loading = vi.hoisted(() => {
  let release!: () => void
  let complete!: () => void
  const promise = new Promise<void>((resolve) => { release = resolve })
  const loaded = new Promise<void>((resolve) => { complete = resolve })
  return { promise, release, loaded, complete }
})

vi.mock('../src/ClarificationCard', async (importOriginal) => {
  await loading.promise
  const module = await importOriginal<typeof import('../src/ClarificationCard')>()
  loading.complete()
  return module
})

test('澄清冷加载超过一秒仍保留问题，并在模块到达后使用最新轮询状态', async () => {
  vi.useFakeTimers()
  const leader = { id: 'store_manager', name: '店长', role: 'leader' }
  const member = { id: 'dev', name: '米寇', role: 'dev' }
  let question = { seq: 2, role_id: member.id, question: '最初的问题', options: ['初始选项一', '初始选项二'], status: 'pending' }
  let version = 2
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const summary = () => ({ id: 'a', goal: '澄清加载任务', status: 'pending', status_label: '等待补充', conversation_count: 2, latest_seq: version, version })
  const fetchMock = vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json({ tasks: [summary()] })
    if (url === '/api/organization') return json([leader, member])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [member] })
    if (url === '/api/tasks/a') return json({
      task: { id: 'a', goal: '澄清加载任务', status: 'pending', acceptance: [], assignments: [], messages: [], deliverables: [] },
      summary: summary(), clarifications: [question],
      events: [{ seq: question.seq, type: 'clarification.requested', actor: { id: member.id, kind: 'agent', role: 'dev' }, payload: question }],
      activity: { active: false, state: 'idle' }, review: {},
    })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  try {
    await act(async () => { render(<App />) })
    expect(screen.getByText('最初的问题')).toBeTruthy()
    expect(screen.getByText('正在读取补充问题…')).toBeTruthy()
    expect(screen.queryByRole('form')).toBeNull()
    fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '仍在整理的普通草稿' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(screen.getByText('正在读取补充问题…')).toBeTruthy()
    expect(screen.queryByRole('form')).toBeNull()

    question = { ...question, seq: 3, question: '轮询更新后的问题', options: ['最新选项一', '最新选项二'] }
    version = 3
    await act(async () => { fireEvent(document, new Event('visibilitychange')) })
    expect(screen.getByText('轮询更新后的问题')).toBeTruthy()
    expect(screen.queryByText('最初的问题')).toBeNull()
    expect(screen.queryByRole('form')).toBeNull()

    await act(async () => { loading.release(); await loading.loaded; await vi.dynamicImportSettled() })
    expect(screen.getByRole('form', { name: '补充信息：轮询更新后的问题' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: '最新选项一' })).toBeTruthy()
    expect(screen.queryByRole('radio', { name: '初始选项一' })).toBeNull()
    expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('仍在整理的普通草稿')
    expect(fetchMock.mock.calls.every(([url]) => !url.endsWith('/clarifications'))).toBe(true)
  } finally {
    loading.release()
  }
})
