import { createHash, webcrypto } from 'node:crypto'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ArchivePreview } from '../src/ArchivePreview'
import { READ_TIMEOUT_MS } from '../src/request'

const sha = (text: string) => createHash('sha256').update(text).digest('hex')
function fixture(content = '# 已核对正文\n\n中文内容。', path = 'src/说明 & #1.md') {
  const entry = { path, bytes: new TextEncoder().encode(content).length, sha256: sha(content), executable: false }
  return { seq: 7, filename: '发布.zip', format: 'archive', content_bytes: 100, content_sha256: sha('zip'),
    archive: { version: 1, total_bytes: entry.bytes, files: [entry] } }
}
const content = '# 已核对正文\n\n中文内容。'
const item = fixture()
function payload(value = item, text: string | null = content) {
  return { status: 'ok', artifact_seq: value.seq, archive_sha256: value.content_sha256,
    ...value.archive.files[0], content_kind: text === null ? 'binary' : 'utf8', content: text }
}
const response = (body: unknown = payload(), type = 'application/json; charset=utf-8') => new Response(JSON.stringify(body), { headers: { 'Content-Type': type } })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { resolve, promise }
}
function open(path = item.archive.files[0].path) {
  const fold = screen.getByLabelText('工程文件清单').closest('details')!
  if (!fold.open) fireEvent.click(fold.querySelector('summary')!)
  const trigger = screen.getByRole('button', { name: `阅读 ${path}` })
  trigger.focus()
  fireEvent.click(trigger)
  return trigger
}
const copyButton = () => screen.getByRole('button', { name: '复制正文' }) as HTMLButtonElement
const ready = () => waitFor(() => expect(copyButton().disabled).toBe(false))
const writeText = vi.fn()
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('fetch', vi.fn(async () => response()))
  writeText.mockReset().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
})

test('仅显式打开才GET，编码任务和唯一path参数，令牌只在header；校验完成才显示和复制', async () => {
  const digest = deferred<ArrayBuffer>()
  const hashing = vi.fn(() => digest.promise)
  vi.stubGlobal('crypto', { subtle: { digest: hashing } })
  render(<ArchivePreview item={item} taskId="群/一 ?" accessToken="private-token" />)
  expect(fetch).not.toHaveBeenCalled()
  open()
  expect(copyButton().disabled).toBe(true)
  expect(screen.getByRole('status').textContent).toContain('正在读取并校验')
  await waitFor(() => expect(hashing).toHaveBeenCalledOnce())
  expect(fetch).toHaveBeenCalledExactlyOnceWith(`/api/tasks/${encodeURIComponent('群/一 ?')}/artifacts/7/file?path=${encodeURIComponent(item.archive.files[0].path)}`, expect.objectContaining({
    signal: expect.any(AbortSignal), headers: { 'X-Cater-Access-Token': 'private-token' },
  }))
  expect(screen.queryByText('中文内容。')).toBeNull()
  fireEvent.click(copyButton())
  expect(writeText).not.toHaveBeenCalled()
  await act(async () => { digest.resolve(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(content))) })
  await ready()
  const reader = screen.getByRole('dialog', { name: item.archive.files[0].path })
  expect(within(reader).getByText('中文内容。')).toBeTruthy()
  expect(within(reader).getByText(/发布.zip · 事件 #7/)).toBeTruthy()
  expect(reader.textContent).toContain(item.content_sha256)
  fireEvent.click(copyButton())
  await screen.findByText('已复制')
  expect(writeText).toHaveBeenCalledExactlyOnceWith(content)
})

test.each(['', ' \n\t', '完整中文🙂\r\n', '\uFEFF有BOM', 'a'.repeat(65536)])('完整UTF8包括空串和64KiB边界可读 #%#', async text => {
  const value = fixture(text, 'src/main.txt')
  vi.mocked(fetch).mockResolvedValueOnce(response(payload(value, text)))
  render(<ArchivePreview item={value} taskId="task" />)
  open(value.archive.files[0].path)
  await ready()
  if (text === '') expect(screen.getByText('空文件（0 字节）。')).toBeTruthy()
  else expect(screen.getByRole('dialog').querySelector('pre code')?.textContent).toBe(text)
  fireEvent.click(copyButton())
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(text))
})

test('二进制只呈现已绑定的元数据，无正文与复制入口', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(response(payload(item, null)))
  render(<ArchivePreview item={item} taskId="task" />)
  open()
  await screen.findByText('二进制文件不预览，仅展示元数据。请下载整工程查看。')
  expect(copyButton().disabled).toBe(true)
  const reader = screen.getByRole('dialog')
  expect(reader.querySelector('pre, img, iframe, object')).toBeNull()
  expect(reader.textContent).toContain(item.archive.files[0].sha256)
  expect(reader.textContent).toContain(`${item.archive.files[0].bytes} 字节`)
  fireEvent.click(copyButton())
  expect(writeText).not.toHaveBeenCalled()
})

test('大于64KiB的成员禁用且有原因提示，整包下载入口保留', () => {
  const value = fixture('a'.repeat(65537), 'large.txt')
  render(<ArchivePreview item={value} taskId="task" />)
  const trigger = open('large.txt') as HTMLButtonElement
  expect(trigger.disabled).toBe(true)
  expect(trigger.title).toContain('超过 65,536 字节')
  expect(screen.queryByRole('dialog')).toBeNull()
  expect((screen.getByRole('button', { name: '下载 发布.zip' }) as HTMLButtonElement).disabled).toBe(false)
  expect(fetch).not.toHaveBeenCalled()
})

test.each([
  ['status', 'failed'], ['artifact_seq', 8], ['artifact_seq', '7'], ['archive_sha256', sha('other')],
  ['path', 'other.md'], ['bytes', item.archive.files[0].bytes + 1], ['bytes', String(item.archive.files[0].bytes)],
  ['sha256', sha('other')], ['executable', true], ['executable', 'false'],
  ['content_kind', 'html'], ['content_kind', 'binary'], ['content', null], ['content', 1],
  ['content', '篡改'], ['content', 'a'.repeat(65537)], ['content', content.replace('已', '未')],
] as const)('响应篡改 %s 时拒绝显示和复制，支持重试 #%#', async (key, value) => {
  vi.mocked(fetch).mockResolvedValueOnce(response({ ...payload(), [key]: value }))
  render(<ArchivePreview item={item} taskId="task" />)
  open()
  await screen.findByRole('alert')
  expect(copyButton().disabled).toBe(true)
  expect(screen.getByRole('dialog').querySelector('article')?.textContent).not.toContain('中文内容。')
  expect(writeText).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '重试读取文件' }))
  await ready()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('中文内容。')).toBeTruthy()
})

test.each(Object.keys(payload()))('缺少响应字段 %s 时拒绝', async key => {
  const body: Record<string, unknown> = payload()
  delete body[key]
  vi.mocked(fetch).mockResolvedValueOnce(response(body))
  render(<ArchivePreview item={item} taskId="task" />)
  open()
  await screen.findByRole('alert')
  expect(copyButton().disabled).toBe(true)
  expect(screen.queryByText('中文内容。')).toBeNull()
})

test.each([
  ['错误MIME', () => response(payload(), 'text/html')],
  ['空MIME', () => response(payload(), '')],
  ['破损JSON', () => new Response('{private-token /disk/secret', { headers: { 'Content-Type': 'application/json' } })],
  ['数组', () => response([payload()])], ['空对象', () => response(null)],
  ...[400, 401, 404, 409, 413, 500].map(status => [`HTTP ${status}`, () => new Response('private-token /disk/secret', { status })] as const),
] as const)('%s 响应固定错误，不泄露服务端正文', async (_name, failed) => {
  vi.mocked(fetch).mockResolvedValueOnce(failed())
  render(<ArchivePreview item={item} taskId="task" />)
  open()
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).not.toMatch(/private-token|secret/)
  expect(copyButton().disabled).toBe(true)
  expect(screen.queryByText('中文内容。')).toBeNull()
  expect(screen.getByRole('button', { name: '重试读取文件' })).toBeTruthy()
})

test.each([undefined, {}, { subtle: { digest: vi.fn().mockRejectedValue(new Error('/disk/secret')) } }])('缺少或失败的crypto不能绕过校验', async crypto => {
  vi.stubGlobal('crypto', crypto)
  render(<ArchivePreview item={item} taskId="task" />)
  open()
  expect((await screen.findByRole('alert')).textContent).not.toContain('secret')
  expect(copyButton().disabled).toBe(true)
  expect(screen.queryByText('中文内容。')).toBeNull()
})

test('空正文也必须核对摘要，未配对surrogate不能借UTF8替换通过', async () => {
  const value = fixture('', 'empty.txt')
  value.archive.files[0].sha256 = sha('other')
  const view = render(<ArchivePreview item={value} taskId="task" />)
  vi.mocked(fetch).mockResolvedValueOnce(response(payload(value, '')))
  open('empty.txt')
  await screen.findByRole('alert')
  expect(copyButton().disabled).toBe(true)
  expect(screen.queryByText('空文件（0 字节）。')).toBeNull()
  const surrogate = fixture('\ufffd', 'surrogate.txt')
  vi.mocked(fetch).mockResolvedValueOnce(response(payload(surrogate, '\ud800')))
  view.rerender(<ArchivePreview item={surrogate} taskId="task" />)
  open('surrogate.txt')
  await screen.findByRole('alert')
  expect(copyButton().disabled).toBe(true)
})

type Scope = 'task' | 'token' | 'seq' | 'parenthash' | 'manifest' | 'path' | 'hash' | 'bytes' | 'executable' | 'unmount'
function changed(scope: Scope) {
  const value = fixture()
  if (scope === 'seq') value.seq++
  if (scope === 'parenthash') value.content_sha256 = sha('new zip')
  if (scope === 'manifest') value.archive.files.push({ ...value.archive.files[0], path: 'other.txt' })
  if (scope === 'path') value.archive.files[0].path = 'new/path.md'
  if (scope === 'hash') value.archive.files[0].sha256 = sha('new member')
  if (scope === 'bytes') value.archive.files[0].bytes++
  if (scope === 'executable') value.archive.files[0].executable = true
  value.archive.total_bytes = value.archive.files.reduce((sum, entry) => sum + entry.bytes, 0)
  return <ArchivePreview item={value} taskId={scope === 'task' ? 'new' : 'task'} accessToken={scope === 'token' ? 'new' : 'token'} />
}
const scopes: Scope[] = ['task', 'token', 'seq', 'parenthash', 'manifest', 'path', 'hash', 'bytes', 'executable', 'unmount']

test.each(scopes)('切换%s立即关闭旧modal并abort，迟到响应无效', async scope => {
  const old = deferred<Response>()
  vi.mocked(fetch).mockReturnValueOnce(old.promise)
  const view = render(<ArchivePreview item={item} taskId="task" accessToken="token" />)
  open()
  const signal = vi.mocked(fetch).mock.calls[0][1]!.signal!
  if (scope === 'unmount') view.unmount()
  else view.rerender(changed(scope))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(signal.aborted).toBe(true)
  await act(async () => { old.resolve(response()) })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText('中文内容。')).toBeNull()
  expect(writeText).not.toHaveBeenCalled()
})

test.each(scopes.filter(scope => scope !== 'unmount'))('已显示正文切换%s也在同次渲染清空', async scope => {
  const view = render(<ArchivePreview item={item} taskId="task" accessToken="token" />)
  open()
  await ready()
  view.rerender(changed(scope))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.queryByText('中文内容。')).toBeNull()
})

test('正文解析晚到不会覆盖新请求错误，清单变无效也清空', async () => {
  const body = deferred<unknown>()
  const slow = response()
  const reading = vi.spyOn(slow, 'json').mockReturnValue(body.promise)
  vi.mocked(fetch).mockResolvedValueOnce(slow).mockResolvedValueOnce(new Response('secret', { status: 401 }))
  const view = render(<ArchivePreview item={item} taskId="task" accessToken="token" />)
  open()
  await waitFor(() => expect(reading).toHaveBeenCalledOnce())
  const signal = vi.mocked(fetch).mock.calls[0][1]!.signal!
  view.rerender(changed('task'))
  open()
  await screen.findByRole('alert')
  await act(async () => { body.resolve(payload()) })
  expect(signal.aborted).toBe(true)
  expect(screen.getByRole('alert').textContent).toContain('访问令牌')
  expect(copyButton().disabled).toBe(true)
  view.rerender(<ArchivePreview item={{ ...item, archive: null }} taskId="new" />)
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('关闭后digest晚到不会污染重新打开的阅读器，Esc焦点回到文件图标', async () => {
  const digest = deferred<ArrayBuffer>()
  const hashing = vi.fn().mockReturnValueOnce(digest.promise).mockImplementation((_algorithm, bytes) => webcrypto.subtle.digest('SHA-256', bytes))
  vi.stubGlobal('crypto', { subtle: { digest: hashing } })
  render(<ArchivePreview item={item} taskId="task" />)
  const trigger = open()
  await waitFor(() => expect(hashing).toHaveBeenCalledOnce())
  const signal = vi.mocked(fetch).mock.calls[0][1]!.signal!
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect(signal.aborted).toBe(true)
  open()
  await ready()
  await act(async () => { digest.resolve(new ArrayBuffer(32)) })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('中文内容。')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '关闭文件预览' }))
  await waitFor(() => expect(document.activeElement).toBe(trigger))
})

test('超时包含正文读取和digest阶段，迟到不能显示，重试可恢复', async () => {
  vi.useFakeTimers()
  const digest = deferred<ArrayBuffer>()
  const hashing = vi.fn().mockReturnValueOnce(digest.promise).mockImplementation((_algorithm, bytes) => webcrypto.subtle.digest('SHA-256', bytes))
  vi.stubGlobal('crypto', { subtle: { digest: hashing } })
  render(<ArchivePreview item={item} taskId="task" />)
  await act(async () => { open(); await Promise.resolve() })
  await act(async () => { await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS) })
  expect(screen.getByRole('alert').textContent).toContain('文件读取超时')
  await act(async () => { digest.resolve(await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(content))) })
  expect(copyButton().disabled).toBe(true)
  expect(screen.queryByText('中文内容。')).toBeNull()
  vi.useRealTimers()
  fireEvent.click(screen.getByRole('button', { name: '重试读取文件' }))
  await ready()
})

test.each(['README.md', 'index.html'])('惰性渲染 %s：无活动HTML、远程图片或危险链接', async path => {
  const text = '<script>window.location="https://remote.invalid"</script>\n\n<img src="https://remote.invalid/pixel">\n\n![图片](https://remote.invalid/image)\n\n[运行](javascript:alert%281%29) [凭据](https://user:secret@example.com/) [文档](https://example.com/docs)\n\n| A | B |\n|---|---|\n| 1 | 2 |'
  const value = fixture(text, path)
  vi.mocked(fetch).mockResolvedValueOnce(response(payload(value, text)))
  render(<ArchivePreview item={value} taskId="task" />)
  open(path)
  await ready()
  const reader = screen.getByRole('dialog')
  expect(reader.querySelector('script,img,iframe,object,embed,video,audio')).toBeNull()
  expect(within(reader).queryByRole('link', { name: '运行' })).toBeNull()
  expect(within(reader).queryByRole('link', { name: '凭据' })).toBeNull()
  if (path.endsWith('.md')) {
    expect(within(reader).getByRole('table')).toBeTruthy()
    expect(within(reader).getByRole('link', { name: '文档' }).getAttribute('rel')).toBe('noopener noreferrer')
  } else expect(reader.querySelector('pre code')?.textContent).toBe(text)
})

test('长路径标题换行，工具栏限高可滚动；复制失败可重试且不破坏正文', async () => {
  const path = 'src/' + '很长的文件名'.repeat(40) + '.txt'
  const value = fixture('code', path)
  vi.mocked(fetch).mockResolvedValueOnce(response(payload(value, 'code')))
  writeText.mockRejectedValueOnce(new Error('denied'))
  render(<ArchivePreview item={value} taskId="task" />)
  open(path)
  await ready()
  const reader = screen.getByRole('dialog')
  expect(within(reader).getByRole('heading', { name: path }).className).toContain('[overflow-wrap:anywhere]')
  expect(reader.querySelector('header')?.className).toContain('max-h-[50dvh]')
  expect(reader.querySelector('header')?.className).toContain('overflow-y-auto')
  fireEvent.click(copyButton())
  await screen.findByText('复制未完成')
  fireEvent.click(copyButton())
  await screen.findByText('已复制')
  expect(reader.querySelector('pre code')?.textContent).toBe('code')
})
