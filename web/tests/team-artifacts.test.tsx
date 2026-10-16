import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeAll, beforeEach, expect, test, vi } from 'vitest'
import type { Artifact } from '../src/Artifact'
import { TeamArtifacts } from '../src/TeamArtifacts'
import type { TeamEvent, TeamState } from '../src/team'

const hash = 'a'.repeat(64), host = { id: 'team', role: 'team', kind: 'system' }
const item = (seq: number, filename = 'app.js', body = `版本正文${seq}`): Artifact => ({ seq, filename, content: body,
  title: filename, summary: `第${seq}次发布`, content_sha256: hash, format: 'code', actor: 'dev', source: 'external' })
const event = (seq: number, type: string, payload: Record<string, unknown> = {}): TeamEvent => ({ seq, type, payload, actor: host })
const started = event(1, 'team.started')
const state = (extra: Partial<TeamState> = {}): TeamState => ({ enabled: true, objective: '小项目', status: 'running', revision: 0,
  runs: [], pending_count: 0, assignments: [], checkpoint: '', ...extra })
const completion = (file: Artifact, seq = 20) => event(seq, 'team.objective_completed', { artifacts: [
  { seq: file.seq, filename: file.filename, content_sha256: hash }] })
const props = (extra: Partial<Parameters<typeof TeamArtifacts>[0]> = {}): Parameters<typeof TeamArtifacts>[0] => ({
  taskId: 'task-a', team: state(), artifacts: [item(2), item(5)], events: [started], names: { dev: '米寇' }, ...extra,
})
const ready = (extra: Partial<Parameters<typeof TeamArtifacts>[0]> = {}) => props({ team: state({ status: 'done', revision: 20 }),
  events: [started, completion(item(5))], ...extra })
const jsonZip = () => new Response(new Blob(['zip-bytes']), { status: 200, headers: { 'Content-Type': 'application/zip' } })
const tick = async () => act(async () => { await Promise.resolve() })

beforeAll(async () => { await import('../src/Artifact') })

beforeEach(() => {
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:fixture')
    static revokeObjectURL = vi.fn()
  })
})

test('只展示最新候选，历史默认折叠且正文可查看，不发请求或启动任务', async () => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  render(<TeamArtifacts {...props()} />)
  const current = await screen.findByRole('list', { name: '本轮发布' })
  expect(current.textContent).toContain('事件 #5')
  expect(current.textContent).not.toContain('事件 #2')
  expect(screen.getByText('本轮最新发布，尚未确认交付。')).toBeTruthy()
  const summary = screen.getByText('历史发布 · 1 份')
  expect(summary.closest('details')?.open).toBe(false)
  expect(screen.queryByRole('list', { name: '历史发布' })).toBeNull()
  fireEvent.click(summary)
  const history = await screen.findByRole('list', { name: '历史发布' })
  expect(history.textContent).toContain('米寇 · 事件 #2')
  fireEvent.click(await within(history).findByRole('button', { name: '查看正文' }))
  expect((await screen.findByRole('dialog')).textContent).toContain('版本正文2')
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByRole('button', { name: '下载本次交付包' })).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

test('新目标发布区为空，旧文件仅在历史中；每次换目标收起历史', async () => {
  const view = render(<TeamArtifacts {...props()} />)
  fireEvent.click(screen.getByText('历史发布 · 1 份'))
  await screen.findByRole('list', { name: '历史发布' })
  view.rerender(<TeamArtifacts {...props({ team: state({ revision: 10 }), events: [started, event(10, 'team.objective_updated')] })} />)
  expect(screen.getByText('本轮还没有发布成果。')).toBeTruthy()
  expect(screen.getByText('历史发布 · 2 份').closest('details')?.open).toBe(false)
  expect(screen.queryByRole('list', { name: '本轮发布' })).toBeNull()
})

test('冻结交付不被后来同名发布覆盖，后发布默认折叠', async () => {
  render(<TeamArtifacts {...ready({ artifacts: [item(2), item(5), item(25)] })} />)
  const current = await screen.findByRole('list', { name: '本次交付文件' })
  expect(current.textContent).toContain('事件 #5')
  expect(current.textContent).not.toContain('事件 #25')
  expect(screen.getByText('交付后发布 · 1 份').closest('details')?.open).toBe(false)
  fireEvent.click(screen.getByText('交付后发布 · 1 份'))
  const later = await screen.findByRole('list', { name: '交付后发布' })
  expect(later.textContent).toContain('事件 #25')
  expect(screen.getByText('这些版本不在本次交付包内。')).toBeTruthy()
})

test.each([undefined, null, {}, [{ seq: 999, filename: 'app.js', content_sha256: hash }]])('旧记录/失配清单仍可查发布，但没有整包按钮：%j', artifacts => {
  render(<TeamArtifacts {...ready({ events: [event(20, 'team.objective_completed', { artifacts })] })} />)
  expect(screen.getByRole('alert').textContent).toBeTruthy()
  expect(screen.getByRole('heading', { name: /已发布版本/ })).toBeTruthy()
  expect(screen.queryByRole('button', { name: '下载本次交付包' })).toBeNull()
})

test('合法无文件交付不列出旧文件或显示空包下载', () => {
  render(<TeamArtifacts {...ready({ events: [event(20, 'team.objective_completed', { artifacts: [] })] })} />)
  expect(screen.getByText('这次交付没有附带文件。')).toBeTruthy()
  expect(screen.getByText('历史发布 · 2 份')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '下载本次交付包' })).toBeNull()
})

test('整包请求绑定交付序号、携带认证、不重复点击、不调用模型', async () => {
  let respond!: (value: Response) => void
  const fetch = vi.fn(() => new Promise<Response>(resolve => { respond = resolve }))
  vi.stubGlobal('fetch', fetch)
  const create = vi.fn(() => 'blob:test'), revoke = vi.fn()
  vi.spyOn(URL, 'createObjectURL').mockImplementation(create)
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(revoke)
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  render(<TeamArtifacts {...ready({ taskId: 'a/b', accessToken: 'test-auth' })} />)
  const button = screen.getByRole('button', { name: '下载本次交付包' })
  fireEvent.click(button); fireEvent.click(button)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toBe('/api/tasks/a%2Fb/delivery?delivery_seq=20')
  expect(fetch.mock.calls[0][1].headers).toEqual({ 'X-Cater-Access-Token': 'test-auth' })
  expect((button as HTMLButtonElement).disabled).toBe(true)
  await act(async () => respond(jsonZip()))
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
  expect(create).toHaveBeenCalledTimes(1)
  expect((button as HTMLButtonElement).disabled).toBe(false)
})

test.each([409, 401, 500])('下载HTTP错误可手动重试，不自动发第二次请求：%s', async code => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response('', { status: code })).mockResolvedValueOnce(jsonZip())
  vi.stubGlobal('fetch', fetch)
  const create = vi.fn(() => 'blob:retry'); vi.spyOn(URL, 'createObjectURL').mockImplementation(create)
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  render(<TeamArtifacts {...ready()} />)
  fireEvent.click(screen.getByRole('button', { name: '下载本次交付包' }))
  expect((await screen.findByRole('alert')).textContent).toContain(code === 409 ? '交付版本已变化' : code === 401 ? '访问凭据已失效' : '暂时无法取回')
  await tick()
  expect(fetch).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '下载本次交付包' }))
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
  expect(fetch).toHaveBeenCalledTimes(2)
})

test.each(['task', 'delivery', 'token'])('切换上下文时取消旧下载，迟到结果不触发保存：%s', async change => {
  let respond!: (value: Response) => void
  const fetch = vi.fn(() => new Promise<Response>(resolve => { respond = resolve }))
  vi.stubGlobal('fetch', fetch)
  const create = vi.fn(() => 'blob:late'); vi.spyOn(URL, 'createObjectURL').mockImplementation(create)
  const view = render(<TeamArtifacts {...ready({ accessToken: 'before' })} />)
  fireEvent.click(screen.getByRole('button', { name: '下载本次交付包' }))
  const signal = fetch.mock.calls[0][1].signal as AbortSignal
  view.rerender(<TeamArtifacts {...ready(change === 'task' ? { taskId: 'task-b', accessToken: 'before' }
    : change === 'token' ? { accessToken: 'after' }
      : { team: state({ status: 'done', revision: 30 }), events: [completion(item(5), 30)], accessToken: 'before' })} />)
  await waitFor(() => expect(signal.aborted).toBe(true))
  await act(async () => respond(jsonZip()))
  await tick()
  expect(create).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
  expect((screen.getByRole('button', { name: '下载本次交付包' }) as HTMLButtonElement).disabled).toBe(false)
})

test('下载超时会恢复按钮并显示错误，不无声重试', async () => {
  vi.useFakeTimers()
  const fetch = vi.fn(() => new Promise<Response>(() => {})); vi.stubGlobal('fetch', fetch)
  render(<TeamArtifacts {...ready()} />)
  fireEvent.click(screen.getByRole('button', { name: '下载本次交付包' }))
  await act(async () => { await vi.advanceTimersByTimeAsync(8001) })
  expect(screen.getByRole('alert').textContent).toContain('请求超时')
  expect((screen.getByRole('button', { name: '下载本次交付包' }) as HTMLButtonElement).disabled).toBe(false)
  expect(fetch).toHaveBeenCalledTimes(1)
})
