import { useLayoutEffect, useRef, useState } from 'react'
import { Download, Files, LoaderCircle } from 'lucide-react'
import { request, RequestTimeoutError } from './request'
import { archiveEntries as entriesOf, portableArchivePart as portablePart } from './archiveManifest'

type ArchiveItem = {
  seq: number; filename?: string; format?: string; content_sha256?: string
  content_bytes?: number; archive?: unknown
}
type Props = { item: ArchiveItem; taskId?: string; accessToken?: string }
const digestPattern = /^[a-f0-9]{64}$/i
const integer = (value: unknown, max: number): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= max
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)

function safeFilename(value: unknown, seq: number) {
  return typeof value === 'string' && value === value.trim() && portablePart(value) &&
    /\.zip$/i.test(value) && new TextEncoder().encode(value).length <= 180
    ? value : `工程归档-${Number.isSafeInteger(seq) && seq > 0 ? seq : '文件'}.zip`
}

class DownloadError extends Error {}

export function ArchivePreview({ item, taskId, accessToken = '' }: Props) {
  // Replacing the scope also resets the fold, busy state and errors before paint.
  const scope = JSON.stringify([taskId, item.seq, accessToken, item.filename, item.content_bytes, item.content_sha256])
  return <ScopedArchivePreview key={scope} item={item} taskId={taskId} accessToken={accessToken} />
}

function ScopedArchivePreview({ item, taskId, accessToken }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const active = useRef(false)
  const pending = useRef<AbortController | null>(null)
  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false; pending.current?.abort() }
  }, [])
  const entries = entriesOf(item.archive)
  const filename = safeFilename(item.filename, item.seq)
  const validContent = integer(item.content_bytes, 65 * 1024 * 1024) && item.content_bytes > 0 &&
    typeof item.content_sha256 === 'string' && digestPattern.test(item.content_sha256)
  const available = typeof taskId === 'string' && Boolean(taskId.trim()) && Number.isSafeInteger(item.seq) && item.seq > 0 && validContent && entries !== null

  const download = async () => {
    if (!available || pending.current) return
    const controller = new AbortController()
    pending.current = controller
    const current = () => active.current && pending.current === controller && !controller.signal.aborted
    setBusy(true)
    setError('')
    try {
      const blob = await request(`/api/tasks/${encodeURIComponent(taskId!)}/artifacts/${item.seq}`, {
        signal: controller.signal, headers: { 'X-Cater-Access-Token': accessToken ?? '' },
      }, async response => {
        if (!response.ok) throw new DownloadError(response.status === 401 ? '下载需要有效的访问令牌，请检查后重试。'
          : response.status === 409 ? '归档与发布快照不一致，请刷新后重试。' : '归档下载失败，请稍后重试。')
        if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/zip') throw new DownloadError('归档响应格式异常，下载已停止。')
        const body = await response.blob()
        if (body.size !== item.content_bytes) throw new DownloadError('归档大小与发布记录不一致，下载已停止。')
        if (!globalThis.crypto?.subtle?.digest) throw new DownloadError('当前浏览器不支持 SHA-256 校验，无法下载归档。')
        const hash = await globalThis.crypto.subtle.digest('SHA-256', await body.arrayBuffer())
        const hex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('')
        if (hex !== item.content_sha256!.toLowerCase()) throw new DownloadError('归档 SHA-256 与发布记录不一致，下载已停止。')
        return body
      })
      if (!current()) return
      const url = URL.createObjectURL(blob)
      try {
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        document.body.append(link)
        try { link.click() } finally { link.remove() }
      } finally { window.setTimeout(() => URL.revokeObjectURL(url), 1000) }
    } catch (cause) {
      if (current() && !(record(cause) && cause.name === 'AbortError')) {
        setError(cause instanceof DownloadError ? cause.message : cause instanceof RequestTimeoutError ? '归档下载超时，请稍后重试。' : '归档下载未完成，请重试。')
      }
    } finally {
      if (current()) { pending.current = null; setBusy(false) }
    }
  }

  return <div className="mt-2 min-w-0 text-[11px] leading-5 text-[#52766d]">
    <div className="flex min-w-0 items-start gap-2">
      <details className="min-w-0 flex-1">
        <summary className="cursor-pointer"><Files size={13} className="mr-1 inline" aria-hidden="true" />{entries ? `工程文件 · ${entries.length} 份` : '工程文件 · 清单不可用'}</summary>
        {entries ? <ul aria-label="工程文件清单" className="mt-1 max-h-48 overflow-y-auto">
          {entries.map(entry => <li key={entry.path} className="min-w-0 py-1">
            <code className="block min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{entry.path}</code>
            <span>{entry.bytes} 字节 · {entry.executable ? '可执行文件' : '普通文件'}</span>
            <code className="block min-w-0 [overflow-wrap:anywhere]">SHA-256：{entry.sha256}</code>
          </li>)}
        </ul> : <p>归档清单异常，无法展示文件元数据。</p>}
        {validContent ? <p className="mt-1 min-w-0 [overflow-wrap:anywhere]">ZIP · {item.content_bytes} 字节<br />SHA-256：{item.content_sha256}</p> : <p>归档校验信息不完整，暂时无法下载。</p>}
      </details>
      <button type="button" className="artifact-tool shrink-0" disabled={!available || busy} aria-busy={busy} title={`下载 ${filename}`} aria-label={`下载 ${filename}`} onClick={() => void download()}>
        {busy ? <LoaderCircle size={16} className="animate-spin" aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}
      </button>
    </div>
    <p className="text-[#839087]">发布快照，不代表验收通过。</p>
    {error && <p role="alert" className="mt-1 text-rose-700">{error}</p>}
  </div>
}
