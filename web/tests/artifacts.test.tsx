import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { ArtifactPreview } from '../src/Artifact'
import type { Artifact } from '../src/Artifact'
import App from '../src/App'

const artifact: Artifact = { seq: 7, title: '猫咖页面草案', summary: '入口与验收说明', actor: 'design', source: 'local_demo', content: '# 可见成果\n\n这是正文。\n\n| 页面 | 行为 |\n| --- | --- |\n| 首页 | 发起对话 |', format: 'markdown', filename: '猫咖草案.md' }
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

const open = () => fireEvent.click(screen.getByRole('button', { name: '查看正文' }))

test('Markdown 阅读器有标题和表格，Esc 关闭后返回入口且保留草稿', async () => {
  render(<><textarea aria-label="草稿" defaultValue="仍在整理想法" /><ArtifactPreview item={artifact} publisher="罐头" /></>)
  const trigger = screen.getByRole('button', { name: '查看正文' })
  open()
  const reader = screen.getByRole('dialog', { name: artifact.title })
  expect(within(reader).getByRole('heading', { name: '可见成果' })).toBeTruthy()
  expect(within(reader).getByRole('table')).toBeTruthy()
  expect(within(reader).getByText('罐头 · 演示样例')).toBeTruthy()
  fireEvent.keyDown(reader, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(trigger)
  expect((screen.getByRole('textbox', { name: '草稿' }) as HTMLTextAreaElement).value).toBe('仍在整理想法')
})

test('Markdown 不执行原始 HTML、不加载远程图片、不产生危险链接', () => {
  const content = '<script>alert(1)</script>\n\n<img src="https://remote.invalid/track" onerror="alert(1)">\n\n![远程图片](https://remote.invalid/pixel)\n\n[运行](javascript:alert%281%29) [凭据](https://user:secret@example.com/) [正常链接](https://example.com/docs)'
  render(<ArtifactPreview item={{ ...artifact, content, uri: 'javascript:alert(1)' }} />)
  open()
  const reader = screen.getByRole('dialog')
  expect(reader.querySelector('script')).toBeNull()
  expect(reader.querySelector('img')).toBeNull()
  expect(within(reader).queryByRole('link', { name: '运行' })).toBeNull()
  expect(within(reader).queryByRole('link', { name: '凭据' })).toBeNull()
  expect(within(reader).getByRole('link', { name: '正常链接' }).getAttribute('href')).toBe('https://example.com/docs')
})

test('代码内容按原文展示，HTML 不变成可执行页面', () => {
  const content = '<script>window.location="https://remote.invalid"</script>\n  <button>示例</button>'
  render(<ArtifactPreview item={{ ...artifact, format: 'code', content }} />)
  open()
  const reader = screen.getByRole('dialog')
  expect(reader.querySelector('pre code')?.textContent).toBe(content)
  expect(reader.querySelector('script')).toBeNull()
})

test('复制的内容与发布正文一致，失败反馈不会损坏正文', async () => {
  const writeText = vi.fn().mockRejectedValueOnce(new Error('denied')).mockResolvedValueOnce(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  render(<ArtifactPreview item={artifact} />); open()
  fireEvent.click(screen.getByRole('button', { name: '复制正文' }))
  await screen.findByText('复制未完成')
  fireEvent.click(screen.getByRole('button', { name: '复制正文' }))
  await screen.findByText('已复制')
  expect(writeText).toHaveBeenLastCalledWith(artifact.content)
  expect(screen.getByText('这是正文。')).toBeTruthy()
})

test('下载只包含正文，危险文件名回退为安全文件名并释放临时 URL', async () => {
  vi.useFakeTimers()
  let captured: unknown
  vi.stubGlobal('Blob', class { constructor(parts: string[]) { captured = parts.join('') } })
  const revoke = vi.fn()
  const BrowserURL = URL
  vi.stubGlobal('URL', class extends BrowserURL { static createObjectURL = () => 'blob:artifact'; static revokeObjectURL = revoke })
  const names: string[] = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { names.push(this.download) })
  render(<ArtifactPreview item={{ ...artifact, filename: '../outside.md' }} />); open()
  fireEvent.click(screen.getByRole('button', { name: '下载正文' }))
  expect(captured).toBe(artifact.content)
  expect(names).toEqual(['成果-7.md'])
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(revoke).toHaveBeenCalledWith('blob:artifact')
})

test('旧摘要和超限正文不会制造空阅读器，本地路径不变成访问入口', () => {
  render(<><ArtifactPreview item={{ seq: 2, title: '旧记录', summary: '只有摘要', uri: 'C:\\private\\result.md' }} /><ArtifactPreview item={{ ...artifact, content: undefined, content_error: 'too_large' }} /></>)
  expect(screen.queryByRole('button', { name: '查看正文' })).toBeNull()
  expect(screen.queryByRole('link', { name: '打开来源' })).toBeNull()
  expect(screen.getByText('正文太大，需要拆分后重新发布。')).toBeTruthy()
  expect(screen.getByText('只有摘要')).toBeTruthy()
})

test('聊天成果和成果板都能打开同一份事件正文', async () => {
  const task = { id: 'task', goal: '猫咖页面', status: 'pending', acceptance: [], messages: [], assignments: [], deliverables: [artifact] }
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url === '/api/tasks' ? [task] : url === '/api/organization' ? [] : url === '/api/organization/profile' ? { leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [], mode: 'local_demo' } : { task, events: [{ seq: 7, type: 'deliverable.published', actor: { id: 'design' }, payload: artifact }], review: {}, activity: { active: false, state: 'idle' } }
    return new Response(JSON.stringify(body))
  }))
  render(<App />)
  await waitFor(() => expect(screen.getAllByRole('button', { name: '查看正文' })).toHaveLength(2))
  const entries = screen.getAllByRole('button', { name: '查看正文' })
  fireEvent.click(entries[0])
  expect(within(screen.getByRole('dialog')).getByText('这是正文。')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '关闭成果正文' }))
  fireEvent.click(entries[1])
  expect(within(screen.getByRole('dialog')).getByText('这是正文。')).toBeTruthy()
})
