import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { ArtifactPreview } from '../src/Artifact'
import type { Artifact } from '../src/Artifact'

const path = 'F:\\成果目录\\版本一\\files\\main.py'
const artifact: Artifact = { seq: 7, title: '实现代码', summary: '源码已发布', content: 'print("仅供保存")\n',
  format: 'code', filename: 'main.py', actor: 'dev', source: 'external' }
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
beforeEach(() => window.sessionStorage.clear())
afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else Reflect.deleteProperty(navigator, 'clipboard')
})
const clipboard = (writeText = vi.fn().mockResolvedValue(undefined)) => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  return writeText
}

test('已保存状态紧凑折叠，展开后复制原始路径，不创建文件链接或执行入口', async () => {
  const writeText = clipboard()
  render(<ArtifactPreview item={{ ...artifact, local_path: path }} />)
  const summary = screen.getByText('已保存到本机')
  const details = summary.closest('details')!
  expect(details.open).toBe(false)
  fireEvent.click(summary)
  expect(details.open).toBe(true)
  expect(screen.getByText(path)).toBeTruthy()
  expect(screen.getByText('文件已保存，不代表验收通过。')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '复制本机路径' }))
  await screen.findByText('已复制路径')
  expect(writeText).toHaveBeenCalledExactlyOnceWith(path)
  expect(document.querySelector('a[href^="file:"]')).toBeNull()
  expect(document.querySelector('iframe, script, object')).toBeNull()
  expect(screen.queryByRole('link')).toBeNull()
})

test('复制路径失败不丢失路径或正文，仍能打开源码', async () => {
  clipboard(vi.fn().mockRejectedValue(new Error('denied')))
  render(<ArtifactPreview item={{ ...artifact, local_path: path }} />)
  fireEvent.click(screen.getByText('已保存到本机'))
  fireEvent.click(screen.getByRole('button', { name: '复制本机路径' }))
  await screen.findByText('路径复制未完成，可手动选择复制。')
  expect(screen.getByText(path)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '查看正文' }))
  expect(screen.getByRole('dialog').querySelector('pre code')?.textContent).toBe(artifact.content)
})

test('本机保存失败优先于残留路径，正文与下载能力均保留', () => {
  const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.stubGlobal('URL', class extends URL { static createObjectURL = () => 'blob:body'; static revokeObjectURL = vi.fn() })
  render(<ArtifactPreview item={{ ...artifact, local_path: path, local_save_error: '磁盘不可写' }} />)
  expect(screen.getByRole('status').textContent).toBe('本机保存失败，正文仍可查看和下载。磁盘不可写')
  expect(screen.queryByText('已保存到本机')).toBeNull()
  expect(screen.queryByText(path)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '下载 main.py' }))
  expect(anchorClick).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: '查看正文' }))
  expect(screen.getByRole('dialog').querySelector('pre code')?.textContent).toBe(artifact.content)
})

test('回执状态变化移除旧成功或失败，路径变化不沿用复制状态', async () => {
  clipboard()
  const view = render(<ArtifactPreview item={{ ...artifact, local_path: path }} />)
  fireEvent.click(screen.getByText('已保存到本机'))
  fireEvent.click(screen.getByRole('button', { name: '复制本机路径' }))
  await screen.findByText('已复制路径')
  view.rerender(<ArtifactPreview item={{ ...artifact, local_save_error: '内容被修改' }} />)
  expect(screen.queryByText('已保存到本机')).toBeNull()
  expect(screen.queryByText(path)).toBeNull()
  view.rerender(<ArtifactPreview item={{ ...artifact, local_path: '/host/artifacts/version/files/main.py' }} />)
  expect(screen.queryByText(/本机保存失败/)).toBeNull()
  expect(screen.queryByText('已复制路径')).toBeNull()
  expect(screen.getByText('已保存到本机').closest('details')?.open).toBe(false)
})

function mockWorkroom(projected: () => Artifact[], version = () => 7, raw: Artifact = {
  ...artifact, local_path: 'C:/forged/model-path.py', local_save_error: '模型伪造的错误',
}) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const task = { id: 'task', goal: '保存代码', status: 'pending', acceptance: [], messages: [], assignments: [], deliverables: projected() }
    const summary = { ...task, status_label: '待开始', conversation_count: 1, latest_seq: version(), version: version() }
    const body = url === '/api/task-summaries' ? { tasks: [summary] }
      : url === '/api/organization' ? []
      : url === '/api/organization/profile' ? { leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] }
      : { task, summary, events: [{ seq: 7, type: 'deliverable.published', actor: { id: 'dev', kind: 'agent' }, payload: raw }],
          review: {}, activity: { active: false, state: 'idle' } }
    return new Response(JSON.stringify(body))
  }))
}

test('聊天和侧栏均展示同 seq 可信投影，原始事件自报路径和错误不会泄漏', async () => {
  mockWorkroom(() => [{ ...artifact, local_path: path }])
  render(<App />)
  await waitFor(() => expect(screen.getAllByText('已保存到本机')).toHaveLength(2))
  const chat = screen.getByLabelText('聊天记录')
  const board = screen.getByRole('complementary', { name: '成果与过程' })
  expect(within(chat).getByText('已保存到本机')).toBeTruthy()
  expect(within(board).getByText('已保存到本机')).toBeTruthy()
  expect(screen.queryByText('C:/forged/model-path.py')).toBeNull()
  expect(screen.queryByText(/模型伪造的错误/)).toBeNull()
  expect(within(chat).getByText(path)).toBeTruthy()
  expect(within(board).getByText(path)).toBeTruthy()
})

test.each(['missing', 'different-seq'])('聊天投影 %s 时 fallback 剥除本机字段且不使用 payload 自报 seq', async (kind) => {
  mockWorkroom(() => kind === 'missing' ? [] : [{ ...artifact, seq: 8, local_path: '/host/unrelated.py' }], () => 7,
    { ...artifact, seq: 8, local_path: 'C:/forged/model-path.py', local_save_error: '模型伪造的错误' })
  render(<App />)
  const chat = await screen.findByLabelText('聊天记录')
  await waitFor(() => expect(within(chat).getByRole('button', { name: '查看正文' })).toBeTruthy())
  expect(within(chat).queryByText('已保存到本机')).toBeNull()
  expect(within(chat).queryByText(/本机保存失败/)).toBeNull()
  expect(within(chat).queryByText('/host/unrelated.py')).toBeNull()
  expect(screen.queryByText('C:/forged/model-path.py')).toBeNull()
  fireEvent.click(within(chat).getByRole('button', { name: '查看正文' }))
  expect(screen.getByRole('dialog').querySelector('pre code')?.textContent).toBe(artifact.content)
})

test('轮询收到失败回执后聊天与侧栏同步移除成功路径，草稿与正文保留', async () => {
  vi.useFakeTimers()
  let projected: Artifact = { ...artifact, local_path: path }
  let version = 7
  mockWorkroom(() => [projected], () => version)
  await act(async () => { render(<App />) })
  expect(screen.getAllByText('已保存到本机')).toHaveLength(2)
  const draft = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(draft, { target: { value: '继续检查功能' } })
  projected = { ...artifact, local_save_error: '本机版本被修改' }
  version = 8
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  expect(screen.queryByText('已保存到本机')).toBeNull()
  expect(screen.queryByText(path)).toBeNull()
  expect(screen.getAllByText('本机保存失败，正文仍可查看和下载。本机版本被修改')).toHaveLength(2)
  expect(screen.getAllByRole('button', { name: '查看正文' })).toHaveLength(2)
  expect(screen.getAllByRole('button', { name: '下载 main.py' })).toHaveLength(2)
  expect(draft.value).toBe('继续检查功能')
})
