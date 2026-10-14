import { useState } from 'react'
import { Download, PackageCheck, LoaderCircle } from 'lucide-react'

type Evidence = { artifact_seq: number; quote: string }
type Result = { id: string; status: string; reason?: string; text?: string; evidence?: Evidence[]; source_quote?: string | null; receipt_seq?: number }
export type DeliveryState = {
  status: string; reason: string; digest?: string; reviewer?: string | null; review_seq?: number
  manifest?: { sources: { id: string; text: string }[]; files: { path: string; artifact_seq: number }[] }
  requirements?: Result[]; checks?: Result[]; local_checks?: Result[]
  handoff?: { summary: string; why: string; reproduce: string[]; limits: string[] }
}

const labels: Record<string, string> = {
  pending: '等待整体核对', reviewing: '正在整体核对', checking: '正在整体检查', uncertain: '复核结果待确认', accepted: '整体验收通过',
  stale: '需要重新核对', needs_evidence: '还需补齐依据', rework: '需要返工', invalid: '复核记录待补正', blocked: '停在检查点',
}

export function DeliveryPanel({ taskId, delivery, names, accessToken = '' }: {
  taskId: string; delivery?: DeliveryState; names: Record<string, string>; accessToken?: string
}) {
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState('')
  if (!delivery?.digest || !delivery.manifest) return null
  const accepted = delivery.status === 'accepted'
  const download = async () => {
    if (downloading) return
    setDownloading(true); setError('')
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/delivery`, {
        headers: accessToken ? { 'X-Cater-Access-Token': accessToken } : {},
      })
      if (!response.ok) throw new Error(response.status === 409 ? '交付版本已变化，请重新核对。' : '暂时无法取回交付包，请稍后重试。')
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url; link.download = 'cater-delivery.zip'; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '交付包下载失败。')
    } finally { setDownloading(false) }
  }
  return <section className="mb-5 border-b border-[#dce8e1] pb-5" aria-label="交付与复查">
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="flex items-center gap-1.5 text-sm font-semibold"><PackageCheck size={16} className="text-teal-700" />交付与复查</h2>
      {accepted && <button type="button" onClick={() => void download()} disabled={downloading} title="下载当前已验收的正文、清单和复查说明" aria-label="下载交付包" className="flex shrink-0 items-center gap-1.5 rounded-md bg-teal-700 px-2.5 py-1.5 text-xs font-semibold text-white disabled:opacity-60">
        {downloading ? <LoaderCircle size={14} className="animate-spin" /> : <Download size={14} />}交付包
      </button>}
    </div>
    <p className={`text-xs font-semibold ${accepted ? 'text-teal-800' : 'text-amber-800'}`}>{labels[delivery.status] ?? '状态待核对'}</p>
    <p className="mt-1 break-words text-xs leading-5 text-stone-600">{delivery.reason}</p>
    {delivery.reviewer && <p className="mt-2 text-xs text-stone-500">整体复核：{names[delivery.reviewer] ?? delivery.reviewer}</p>}
    {error && <p role="alert" className="mt-2 text-xs text-rose-700">{error}</p>}
    {delivery.local_checks?.map(check => <p key={check.id} className="mt-2 break-words text-xs leading-5 text-stone-600">
      {check.text ?? '整体行为检查'} · {check.status === 'passed' ? '通过' : check.status === 'failed' ? '未通过' : '待检查'}{check.receipt_seq ? ` · 回执 #${check.receipt_seq}` : ''}
    </p>)}
    {accepted && delivery.handoff && <div className="mt-3 space-y-3 text-xs leading-5 text-stone-600">
      <p className="break-words">{delivery.handoff.summary}</p>
      <details><summary className="cursor-pointer font-semibold">为什么这样做 · 如何复查</summary>
        <p className="mt-2 break-words">{delivery.handoff.why}</p>
        <ol className="mt-2 list-decimal space-y-1 pl-4">{delivery.handoff.reproduce.map((step, i) => <li key={i} className="break-words">{step}</li>)}</ol>
        {delivery.handoff.limits.length > 0 && <><p className="mt-2 font-semibold">未覆盖范围</p><ul className="mt-1 list-disc space-y-1 pl-4">{delivery.handoff.limits.map((limit, i) => <li key={i} className="break-words">{limit}</li>)}</ul></>}
      </details>
    </div>}
    {delivery.requirements && <details className="mt-3 text-xs leading-5 text-stone-600"><summary className="cursor-pointer font-semibold">逐条要求的核对依据</summary>
      <ul className="mt-2 space-y-3">{delivery.requirements.map(result => <li key={result.id} className="border-l-2 border-teal-200 pl-2">
        <p className="break-words">{delivery.manifest?.sources.find(source => source.id === result.id)?.text ?? result.id}</p>
        <p className="mt-1 break-words">{result.status === 'passed' ? '已核对' : result.status === 'superseded' ? '按后续要求调整' : result.status === 'failed' ? '未满足' : '依据不足'} · {result.reason}</p>
        {result.evidence?.map((e, i) => <blockquote key={i} className="mt-1 break-words text-stone-500">“{e.quote}” · 成果 #{e.artifact_seq}</blockquote>)}
        {result.source_quote && <blockquote className="mt-1 break-words text-stone-500">“{result.source_quote}”</blockquote>}
      </li>)}</ul>
    </details>}
  </section>
}
