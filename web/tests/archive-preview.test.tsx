import { webcrypto } from 'node:crypto'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { ArchivePreview } from '../src/ArchivePreview'
import { READ_TIMEOUT_MS } from '../src/request'

const bytes = new TextEncoder().encode('snapshot')
const hash = Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2, '0')).join('')
const entry = { path: 'src/main.ts', bytes: 8, sha256: hash, executable: false }
const item = { seq: 7, filename: '工程.zip', format: 'archive', content_bytes: bytes.length, content_sha256: hash,
  archive: { version: 1, files: [entry], total_bytes: entry.bytes } }
const response = () => new Response(bytes, { headers: { 'Content-Type': 'application/zip' } })
const button = () => screen.getByRole('button', { name: /下载/ }) as HTMLButtonElement
const click = () => fireEvent.click(button())
const createURL = vi.fn(() => 'blob:verified')
const revokeURL = vi.fn()
let saved: { filename: string; href: string; connected: boolean }[]

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { resolve, promise }
}

beforeEach(() => {
  saved = []
  createURL.mockClear()
  revokeURL.mockClear()
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('URL', class extends URL { static createObjectURL = createURL; static revokeObjectURL = revokeURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    saved.push({ filename: this.download, href: this.href, connected: this.isConnected })
  })
  vi.stubGlobal('fetch', vi.fn(async () => response()))
})

test('工程清单默认折叠，256份限制高度，长路径换行且不生成执行入口', () => {
  const files = Array.from({ length: 256 }, (_, index) => ({ ...entry, path: `src/${String(index).padStart(3, '0')}-${'长'.repeat(80)}.ts`, executable: index === 0 }))
  const view = render(<ArchivePreview item={{ ...item, archive: { version: 1, files, total_bytes: 2048 } }} taskId="task" />)
  const fold = screen.getByText('工程文件 · 256 份').closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(fold.querySelector('summary')!)
  expect(fold.open).toBe(true)
  expect(screen.getByLabelText('工程文件清单').className).toContain('max-h-48 overflow-y-auto')
  expect(screen.getByText(files[0].path).className).toContain('[overflow-wrap:anywhere]')
  expect(screen.getByText('8 字节 · 可执行文件')).toBeTruthy()
  expect(screen.getByText('发布快照，不代表验收通过。')).toBeTruthy()
  expect(view.container.querySelector('a,iframe,script,object')).toBeNull()
  expect(button().title).toBe('下载 工程.zip')
})

test.each([
  null, [], 'secret', {}, { ...item.archive, version: 2 }, { ...item.archive, total_bytes: 9 },
  { ...item.archive, files: [] }, { ...item.archive, files: Array(257).fill(entry) },
  ...['../secret', '/secret', 'C:/secret', 'a\\secret', 'a//secret', 'a/./secret', 'CON.txt', 'a/secret.', 'a/\u202esecret', 'a/．．/secret'].map(path => ({ ...item.archive, files: [{ ...entry, path }] })),
  ...[{ bytes: -1 }, { bytes: 0.5 }, { bytes: true }, { sha256: 'secret' }, { executable: 'true' }, { extra: 'secret' }, { path: { secret: 'secret' } }].map(change => ({ ...item.archive, files: [{ ...entry, ...change }] })),
  { ...item.archive, files: [entry, entry], total_bytes: 16 },
  { ...item.archive, files: [{ ...entry, path: 'src' }, entry], total_bytes: 16 },
  { ...item.archive, files: [entry, { ...entry, path: 'SRC/other.ts' }], total_bytes: 16 },
] as unknown[])('异常清单整体拒绝，不输出未经验证的元数据：%j', archive => {
  const view = render(<ArchivePreview item={{ ...item, archive }} taskId="task" />)
  expect(screen.getByText('工程文件 · 清单不可用')).toBeTruthy()
  expect(screen.queryByLabelText('工程文件清单')).toBeNull()
  expect(view.container.textContent).not.toMatch(/secret|\[object Object\]/)
  expect(button().disabled).toBe(true)
  click()
  expect(fetch).not.toHaveBeenCalled()
})

test('实际请求编码taskId、仅header带令牌，校验通过才使用安全文件名下载并回收URL', async () => {
  render(<ArchivePreview item={item} taskId="群/一 ?" accessToken="private-token" />)
  click()
  await waitFor(() => expect(saved).toHaveLength(1))
  expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/tasks/%E7%BE%A4%2F%E4%B8%80%20%3F/artifacts/7', expect.objectContaining({
    headers: { 'X-Cater-Access-Token': 'private-token' }, signal: expect.any(AbortSignal),
  }))
  expect(saved[0]).toEqual({ filename: '工程.zip', href: 'blob:verified', connected: true })
  expect(createURL).toHaveBeenCalledOnce()
  expect(document.querySelector('a[download]')).toBeNull()
  await waitFor(() => expect(revokeURL).toHaveBeenCalledWith('blob:verified'), { timeout: 2000 })
})

test.each(['../bad.zip', 'C:\\bad.zip', 'CON.zip', 'CON .zip', 'CONIN$.zip', 'bad.zip.exe', 'bad\n.zip', 'bad\u202e.zip', 'x'.repeat(181) + '.zip'])('不安全filename使用平面zip回退：%s', async filename => {
  render(<ArchivePreview item={{ ...item, filename }} taskId="task" />)
  expect(button().title).toBe('下载 工程归档-7.zip')
  click()
  await waitFor(() => expect(saved[0]?.filename).toBe('工程归档-7.zip'))
})

test.each([
  ['大小', () => new Response('wrong size', { headers: { 'Content-Type': 'application/zip' } }), '归档大小'],
  ['hash', () => new Response('SNAPSHOT', { headers: { 'Content-Type': 'application/zip' } }), '归档 SHA-256'],
  ['类型', () => new Response(bytes, { headers: { 'Content-Type': 'text/html' } }), '归档响应格式异常'],
  ['401', () => new Response('private-token /disk/secret', { status: 401 }), '下载需要有效的访问令牌'],
  ['409', () => new Response('private-token /disk/secret', { status: 409 }), '归档与发布快照不一致'],
  ['500', () => new Response('private-token /disk/secret', { status: 500 }), '归档下载失败'],
] as const)('%s错误阻止URL创建，错误不泄露服务端正文，支持失败重试', async (_name, failed, message) => {
  vi.mocked(fetch).mockResolvedValueOnce(failed())
  render(<ArchivePreview item={item} taskId="task" />)
  click()
  expect((await screen.findByRole('alert')).textContent).toContain(message)
  expect(screen.getByRole('alert').textContent).not.toMatch(/private-token|secret/)
  expect(createURL).not.toHaveBeenCalled()
  expect(button().disabled).toBe(false)
  click()
  await waitFor(() => expect(saved).toHaveLength(1))
  expect(screen.queryByRole('alert')).toBeNull()
})

test.each([undefined, {}])('浏览器不支持SHA时明确拒绝，不绕过校验：%j', crypto => {
  vi.stubGlobal('crypto', crypto)
  render(<ArchivePreview item={item} taskId="task" />)
  click()
  return screen.findByRole('alert').then(alert => {
    expect(alert.textContent).toContain('不支持 SHA-256')
    expect(createURL).not.toHaveBeenCalled()
  })
})

test.each([
  { taskId: undefined, item }, { taskId: '', item }, { taskId: '  ', item },
  ...[0, -1, NaN, 1.5].map(seq => ({ taskId: 'task', item: { ...item, seq } })),
  ...[{ content_bytes: -1 }, { content_bytes: 0.5 }, { content_sha256: '<bad>' }, { content_sha256: undefined }].map(change => ({ taskId: 'task', item: { ...item, ...change } })),
])('缺少有效taskId、seq或校验信息不能下载：%j', props => {
  render(<ArchivePreview {...props} />)
  expect(button().disabled).toBe(true)
  click()
  expect(fetch).not.toHaveBeenCalled()
})

test.each(['task', 'seq', 'token', 'unmount'] as const)('切换%s中止旧请求，旧响应不触发下载或污染新错误', async scope => {
  const old = deferred<Response>()
  vi.mocked(fetch).mockReturnValueOnce(old.promise)
  const view = render(<ArchivePreview item={item} taskId="first" accessToken="old" />)
  click()
  click()
  expect(fetch).toHaveBeenCalledOnce()
  const signal = vi.mocked(fetch).mock.calls[0][1]!.signal!
  if (scope === 'unmount') view.unmount()
  else view.rerender(<ArchivePreview item={{ ...item, seq: scope === 'seq' ? 8 : 7 }} taskId={scope === 'task' ? 'second' : 'first'} accessToken={scope === 'token' ? 'new' : 'old'} />)
  expect(signal.aborted).toBe(true)
  await act(async () => { old.resolve(new Response('private-token', { status: 409 })) })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(createURL).not.toHaveBeenCalled()
  if (scope !== 'unmount') {
    expect(button().disabled).toBe(false)
    click()
    await waitFor(() => expect(saved).toHaveLength(1))
  }
})

test('摘要校验期间scope失效，即使digest忽略取消也不能下载', async () => {
  const digest = deferred<ArrayBuffer>()
  const digestMock = vi.fn(() => digest.promise)
  vi.stubGlobal('crypto', { subtle: { digest: digestMock } })
  const view = render(<ArchivePreview item={item} taskId="task" accessToken="old" />)
  click()
  await waitFor(() => expect(digestMock).toHaveBeenCalledOnce())
  view.rerender(<ArchivePreview item={item} taskId="task" accessToken="new" />)
  await act(async () => { digest.resolve(await webcrypto.subtle.digest('SHA-256', bytes)) })
  expect(createURL).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(button().disabled).toBe(false)
})

test('切群后旧正文成功返回也不能下载，旧错误不会覆盖新请求错误', async () => {
  const body = deferred<Blob>()
  const old = response()
  vi.spyOn(old, 'blob').mockReturnValue(body.promise)
  vi.mocked(fetch).mockResolvedValueOnce(old).mockResolvedValueOnce(new Response('', { status: 401 }))
  const view = render(<ArchivePreview item={item} taskId="old" />)
  await act(async () => { click(); await Promise.resolve() })
  view.rerender(<ArchivePreview item={item} taskId="new" />)
  click()
  await screen.findByRole('alert')
  await act(async () => { body.resolve(await response().blob()) })
  expect(screen.getByRole('alert').textContent).toContain('下载需要有效的访问令牌')
  expect(createURL).not.toHaveBeenCalled()
})

test('AbortError不向用户提示，仍可重试；crypto失败不泄露内部错误', async () => {
  vi.mocked(fetch).mockRejectedValueOnce(new DOMException('private-token', 'AbortError'))
  render(<ArchivePreview item={item} taskId="task" />)
  click()
  await waitFor(() => expect(button().disabled).toBe(false))
  expect(screen.queryByRole('alert')).toBeNull()
  vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockRejectedValue(new Error('private-token')) } })
  click()
  expect((await screen.findByRole('alert')).textContent).toBe('归档下载未完成，请重试。')
  expect(createURL).not.toHaveBeenCalled()
})

test('正文超时沿用request deadline，晚到正文不能生成URL且可以重试', async () => {
  vi.useFakeTimers()
  const body = deferred<Blob>()
  const slow = response()
  vi.spyOn(slow, 'blob').mockReturnValue(body.promise)
  vi.mocked(fetch).mockResolvedValueOnce(slow)
  render(<ArchivePreview item={item} taskId="task" />)
  await act(async () => { click(); await Promise.resolve() })
  await act(async () => { await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS) })
  expect(screen.getByRole('alert').textContent).toContain('下载超时')
  expect(button().disabled).toBe(false)
  await act(async () => { body.resolve(await response().blob()) })
  expect(createURL).not.toHaveBeenCalled()
  vi.useRealTimers()
  click()
  await waitFor(() => expect(saved).toHaveLength(1))
})
