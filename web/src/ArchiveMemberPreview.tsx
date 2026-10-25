import { useLayoutEffect, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Check, Copy, FileText, LoaderCircle, RotateCw, X } from 'lucide-react'
import type { ArchiveEntry } from './archiveManifest'
import { ArtifactContent } from './ArtifactContent'
import { request, RequestTimeoutError } from './request'

type Props = {
  taskId: string; accessToken: string; seq: number; archiveSha256: string
  filename: string; entry: ArchiveEntry; available: boolean
}
type Content = { kind: 'utf8'; text: string } | { kind: 'binary' }
type State = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; content: Content }
class PreviewError extends Error {}
const limit = 65536

async function readMember(response: Response, { seq, archiveSha256, entry }: Pick<Props, 'seq' | 'archiveSha256' | 'entry'>): Promise<Content> {
  if (!response.ok) throw new PreviewError(response.status === 401 ? '阅读需要有效的访问令牌，请检查后重试。'
    : response.status === 400 ? '文件阅读参数无效，请刷新后重试。'
      : response.status === 404 ? '发布快照中未找到此文件，请刷新后重试。'
        : response.status === 409 ? '归档与发布快照不一致，请刷新后重试。'
          : response.status === 413 ? '文件超过 65,536 字节，无法预览，请下载整包查看。' : '文件读取失败，请稍后重试。')
  if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new PreviewError('文件响应格式异常，阅读已停止。')
  const body: unknown = await response.json()
  if (typeof body !== 'object' || body === null || Array.isArray(body)) throw new PreviewError('文件校验信息异常，阅读已停止。')
  const value = body as Record<string, unknown>
  if (value.status !== 'ok' || value.artifact_seq !== seq ||
    typeof value.archive_sha256 !== 'string' || value.archive_sha256.toLowerCase() !== archiveSha256.toLowerCase() ||
    value.path !== entry.path || value.bytes !== entry.bytes || value.executable !== entry.executable ||
    typeof value.sha256 !== 'string' || value.sha256.toLowerCase() !== entry.sha256.toLowerCase() || entry.bytes > limit) {
    throw new PreviewError('文件身份与发布清单不一致，阅读已停止。')
  }
  if (value.content_kind === 'binary' && value.content === null) return { kind: 'binary' }
  if (value.content_kind !== 'utf8' || typeof value.content !== 'string') throw new PreviewError('文件内容格式异常，阅读已停止。')
  const bytes = new TextEncoder().encode(value.content)
  if (bytes.length !== entry.bytes || bytes.length > limit || new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) !== value.content) throw new PreviewError('文件大小或编码与发布记录不一致，阅读已停止。')
  if (!globalThis.crypto?.subtle?.digest) throw new PreviewError('当前浏览器不支持 SHA-256 校验，无法阅读文件。')
  const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  const hex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('')
  if (hex !== entry.sha256.toLowerCase()) throw new PreviewError('文件 SHA-256 与发布记录不一致，阅读已停止。')
  return { kind: 'utf8', text: value.content }
}

export function ArchiveMemberPreview(props: Props) {
  const scope = JSON.stringify([props.taskId, props.accessToken, props.seq, props.archiveSha256, props.filename, props.entry, props.available])
  return <MemberPreview key={scope} {...props} />
}

function MemberPreview(props: Props) {
  const { entry, available } = props
  const [open, setOpen] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const title = entry.bytes > limit ? '超过 65,536 字节，无法预览，请下载整包查看。' : `阅读 ${entry.path}`
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><button type="button" className="artifact-tool shrink-0 disabled:opacity-40" disabled={!available || entry.bytes > limit} title={title} aria-label={`阅读 ${entry.path}`}><FileText size={16} aria-hidden="true" /></button></Dialog.Trigger>
    {open && <MemberReader key={attempt} {...props} onRetry={() => setAttempt(value => value + 1)} />}
  </Dialog.Root>
}

function MemberReader({ taskId, accessToken, seq, archiveSha256, filename, entry, onRetry }: Props & { onRetry: () => void }) {
  const { path, bytes, sha256, executable } = entry
  const [state, setState] = useState<State>({ status: 'loading' })
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const active = useRef(false)
  useLayoutEffect(() => {
    active.current = true
    const controller = new AbortController()
    const current = () => active.current && !controller.signal.aborted
    void request(`/api/tasks/${encodeURIComponent(taskId)}/artifacts/${seq}/file?path=${encodeURIComponent(path)}`, {
      signal: controller.signal, headers: { 'X-Cater-Access-Token': accessToken },
    }, response => readMember(response, { seq, archiveSha256, entry: { path, bytes, sha256, executable } })).then(content => {
      if (current()) setState({ status: 'ready', content })
    }).catch(cause => {
      if (current()) setState({ status: 'error', message: cause instanceof PreviewError ? cause.message
        : cause instanceof RequestTimeoutError ? '文件读取超时，请稍后重试。' : '文件读取未完成，请重试。' })
    })
    return () => { active.current = false; controller.abort() }
  }, [taskId, accessToken, seq, archiveSha256, path, bytes, sha256, executable])
  const text = state.status === 'ready' && state.content.kind === 'utf8' ? state.content.text : null
  const copy = async () => {
    if (!active.current || text === null) return
    try { await navigator.clipboard.writeText(text); if (active.current) setCopyState('copied') }
    catch { if (active.current) setCopyState('failed') }
  }
  return <Dialog.Portal>
    <Dialog.Overlay className="artifact-overlay" />
    <Dialog.Content className="artifact-reader flex flex-col">
      <header className="artifact-toolbar max-h-[50dvh] shrink-0 overflow-y-auto px-5 py-4 sm:px-8">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0"><p className="mb-1 text-[11px] font-semibold text-[#a7644d]">工程文件 · 发布快照</p><Dialog.Title className="whitespace-pre-wrap text-lg font-bold leading-snug text-[#294a40] [overflow-wrap:anywhere]">{entry.path}</Dialog.Title></div>
          <div className="flex shrink-0 gap-1">
            <button type="button" className="artifact-tool disabled:opacity-40" disabled={text === null} title={copyState === 'copied' ? '已复制正文' : '复制正文'} aria-label="复制正文" onClick={() => void copy()}>{copyState === 'copied' ? <Check size={18} /> : <Copy size={18} />}</button>
            <Dialog.Close asChild><button type="button" className="artifact-tool" title="回到文件清单" aria-label="关闭文件预览"><X size={19} /></button></Dialog.Close>
          </div>
        </div>
        <Dialog.Description className="mt-2 text-xs leading-5 text-[#687e73] [overflow-wrap:anywhere]">{filename} · 事件 #{seq}<br />ZIP SHA-256：{archiveSha256}</Dialog.Description>
        <p className="mt-2 text-[11px] leading-5 text-[#687e73] [overflow-wrap:anywhere]">{entry.bytes} 字节 · {entry.executable ? '可执行文件' : '普通文件'}<br />文件 SHA-256：{entry.sha256}</p>
        {copyState !== 'idle' && <p role="status" className="mt-1 text-xs text-[#377968]">{copyState === 'copied' ? '已复制' : '复制未完成'}</p>}
      </header>
      <article className="artifact-document min-h-0 min-w-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8 sm:py-8" aria-busy={state.status === 'loading'}>
        {state.status === 'loading' && <p role="status"><LoaderCircle size={16} className="mr-2 inline animate-spin" aria-hidden="true" />正在读取并校验文件…</p>}
        {state.status === 'error' && <><p role="alert" className="text-rose-700">{state.message}</p><button type="button" className="artifact-tool" aria-label="重试读取文件" title="重试读取文件" onClick={onRetry}><RotateCw size={18} /></button></>}
        {state.status === 'ready' && (state.content.kind === 'binary' ? <p role="status">二进制文件不预览，仅展示元数据。请下载整工程查看。</p>
          : state.content.text === '' ? <p role="status">空文件（0 字节）。</p>
            : <ArtifactContent content={state.content.text} format={/\.(md|markdown)$/i.test(entry.path) ? 'markdown' : 'code'} />)}
      </article>
    </Dialog.Content>
  </Dialog.Portal>
}
