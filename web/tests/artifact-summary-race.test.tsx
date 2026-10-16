import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeAll, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'

beforeAll(async () => { await import('../src/TeamProgressPanel'); await import('../src/Artifact') })

test('摘要先收到新目标而详情挂起时撤销旧下载，迟到旧详情不能重新启用', async () => {
  window.sessionStorage.clear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  const hash = 'a'.repeat(64)
  const old: TaskDetail = {
    task: { id: 'race-task', goal: '旧目标', status: 'done', acceptance: [], messages: [], assignments: [],
      deliverables: [{ seq: 5, filename: 'app.js', title: 'app.js', summary: '固定文件', format: 'code', content: 'old-body', content_sha256: hash }],
      team: { enabled: true, objective: '旧目标', status: 'done', revision: 20, checkpoint: '', pending_count: 0, runs: [], assignments: [] } },
    events: [{ seq: 1, type: 'team.started', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' }, payload: {} },
      { seq: 20, type: 'team.objective_completed', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' }, payload: {
        summary: '旧交付说明', verification: '旧验证范围', artifacts: [{ seq: 5, filename: 'app.js', content_sha256: hash }] } }],
    activity: { active: false, state: 'idle' }, review: {},
  }
  let stage = 0
  let returnDownload!: (response: Response) => void
  let downloadSignal!: AbortSignal
  const lateDetails: ((value: Response) => void)[] = []
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetch = vi.fn((url: string, init?: RequestInit): Promise<Response> => {
    if (url.includes('/delivery?')) {
      downloadSignal = init!.signal as AbortSignal
      return new Promise(resolve => { returnDownload = resolve })
    }
    if (url === '/api/task-summaries') return Promise.resolve(json({ tasks: [{ id: old.task.id, goal: stage ? '新目标' : '旧目标',
      status: stage ? 'running' : 'done', status_label: stage ? '进行中' : '可交付', version: stage ? 30 : 20,
      latest_seq: stage ? 30 : 20, conversation_count: 1,
      team: { enabled: true, status: stage ? 'running' : 'done', revision: stage ? 30 : 20, pending_count: 0 } }] }))
    if (url === '/api/tasks/race-task') return stage ? new Promise(resolve => lateDetails.push(resolve)) : Promise.resolve(json(old))
    if (url === '/api/organization') return Promise.resolve(json([{ id: 'dev', name: '米寇', role: 'dev' }]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: '店长', role: 'leader' },
      cabinet: [], workers: [{ id: 'dev', name: '米寇', role: 'dev' }], mode: 'external_driver' }))
    return Promise.resolve(json({}))
  })
  vi.stubGlobal('fetch', fetch)
  const create = vi.fn(() => 'blob:old')
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = vi.fn() })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const app = render(<App />)
  const board = within(await screen.findByRole('complementary', { name: '成果与过程' }))
  fireEvent.click(await board.findByRole('button', { name: '下载本次交付包' }))
  expect(downloadSignal.aborted).toBe(false)
  stage = 1
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  await waitFor(() => expect(board.queryByRole('button', { name: '下载本次交付包' })).toBeNull())
  expect(downloadSignal.aborted).toBe(true)
  expect(board.getByText('新目标已收到，详细记录正在同步。')).toBeTruthy()
  const stream = within(screen.getByLabelText('聊天记录'))
  expect(stream.getByText('店长交付记录').closest('details')?.open).toBe(false)
  await act(async () => returnDownload(new Response(new Blob(['old-zip']))))
  await waitFor(() => expect(lateDetails.length).toBeGreaterThan(0))
  await act(async () => lateDetails[0](json(old)))
  expect(board.queryByRole('button', { name: '下载本次交付包' })).toBeNull()
  expect(create).not.toHaveBeenCalled()
  expect(click).not.toHaveBeenCalled()
  expect(fetch.mock.calls.filter(([path]) => path.includes('/delivery?'))).toHaveLength(1)
  app.unmount()
}, 15_000)
