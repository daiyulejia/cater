import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Download, GitCompareArrows, LoaderCircle, X } from 'lucide-react'
import type { Artifact } from './Artifact'
import type { TeamEvent, TeamState } from './team'
import { teamArtifactCatalog } from './artifactCatalog'
import { request } from './request'
import { hasComparisonCandidates } from './artifactComparisonData'
import { AsyncPanel } from './AsyncPanel'

const ArtifactPreview = lazy(() => import('./Artifact').then(module => ({ default: module.ArtifactPreview })))
const ArtifactComparison = lazy(() => import('./ArtifactComparison').then(module => ({ default: module.ArtifactComparison })))

function ComparisonEntry({ artifacts, events, preferred, names, categories }: { artifacts: Artifact[]; events: TeamEvent[]; preferred: Artifact[]; names: Record<string, string>; categories: Record<number, string> }) {
  const [open, setOpen] = useState(false)
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><button type="button" className="artifact-tool" title="对照已发布版本" aria-label="对照已发布版本"><GitCompareArrows size={17} aria-hidden="true" /></button></Dialog.Trigger>
    {open && <Dialog.Portal><Dialog.Overlay className="artifact-overlay" />
      <Dialog.Content className="artifact-reader comparison-panel overflow-y-auto p-5 sm:p-8">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0"><Dialog.Title className="text-lg font-bold">版本对照</Dialog.Title>
            <Dialog.Description className="mt-1 text-xs leading-5 text-[#687e73]">已发布文本的差异，不代表返修关系或验收结论。</Dialog.Description></div>
          <Dialog.Close asChild><button type="button" className="artifact-tool shrink-0" title="回到成果" aria-label="关闭版本对照"><X size={19} /></button></Dialog.Close>
        </div>
        <AsyncPanel fallback={<p role="status" className="mt-4 text-sm">正在读取版本…</p>}><ArtifactComparison artifacts={artifacts} events={events} preferred={preferred} names={names} categories={categories} /></AsyncPanel>
      </Dialog.Content>
    </Dialog.Portal>}
  </Dialog.Root>
}

function DeliveryDownload({ taskId, seq, accessToken }: { taskId: string; seq: number; accessToken: string }) {
  const scope = useMemo(() => ({ taskId, seq, accessToken }), [taskId, seq, accessToken])
  const active = useRef<{ scope: object; controller: AbortController } | undefined>(undefined)
  const [transfer, setTransfer] = useState<{ scope: object; busy: boolean; error: string }>()
  const current = transfer?.scope === scope ? transfer : undefined
  const busy = current?.busy ?? false, error = current?.error ?? ''
  useEffect(() => () => {
    if (active.current?.scope === scope) { active.current.controller.abort(); active.current = undefined }
  }, [scope])
  const download = async () => {
    if (active.current?.scope === scope) return
    active.current?.controller.abort()
    const controller = new AbortController()
    active.current = { scope, controller }
    setTransfer({ scope, busy: true, error: '' })
    let failure = ''
    try {
      const blob = await request(`/api/tasks/${encodeURIComponent(taskId)}/delivery?delivery_seq=${seq}`, {
        signal: controller.signal, headers: accessToken ? { 'X-Cater-Access-Token': accessToken } : {},
      }, async response => {
        if (!response.ok) throw new Error(response.status === 409 ? '交付版本已变化，请刷新后再下载。'
          : response.status === 401 ? '访问凭据已失效，请重新连接工作台。' : '暂时无法取回交付包，请稍后重试。')
        return response.blob()
      })
      if (active.current?.controller !== controller || controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      try {
        const link = document.createElement('a')
        link.href = url; link.download = 'cater-delivery.zip'; link.click()
      } finally { window.setTimeout(() => URL.revokeObjectURL(url), 1000) }
    } catch (cause) {
      failure = cause instanceof Error ? cause.message : '交付包下载未完成。'
    } finally {
      if (active.current?.controller === controller) { active.current = undefined; setTransfer({ scope, busy: false, error: failure }) }
    }
  }
  return <div className="min-w-0">
    <button type="button" disabled={busy} onClick={() => void download()} className="artifact-tool"
      title="下载这次交付的完整文件、清单与说明" aria-label="下载本次交付包">
      {busy ? <LoaderCircle size={17} aria-hidden="true" className="animate-spin" /> : <Download size={17} aria-hidden="true" />}
    </button>
    {busy && <span role="status" className="ml-1 text-[11px]">正在取回</span>}
    {error && <p role="alert" className="mt-2 text-xs leading-5 text-rose-700">{error}</p>}
  </div>
}

function ArtifactRows({ items, names, label, taskId, accessToken }: { items: Artifact[]; names: Record<string, string>; label: string; taskId: string; accessToken: string }) {
  return <Suspense fallback={<p role="status" className="text-xs text-[#52766d]">正在读取成果…</p>}>
    <ul aria-label={label} className="space-y-3">{items.map(item => <li key={item.seq} className="min-w-0 border-l-2 border-[#78a291] py-1 pl-3 [overflow-wrap:anywhere]">
      <p className="mb-1 text-[11px] text-[#728679]">{names[item.actor ?? ''] ?? '伙伴'} · 事件 #{item.seq}</p>
      <ArtifactPreview item={item} publisher={names[item.actor ?? ''] ?? item.actor ?? '伙伴'} taskId={taskId} accessToken={accessToken} />
    </li>)}</ul>
  </Suspense>
}

function History({ items, names, later = false, taskId, accessToken }: { items: Artifact[]; names: Record<string, string>; later?: boolean; taskId: string; accessToken: string }) {
  const [open, setOpen] = useState(false)
  if (!items.length) return null
  const title = later ? '交付后发布' : '历史发布'
  return <details className="mt-4 border-t border-[#e4e9e1] pt-3" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer text-xs font-semibold text-[#52766d]">{title} · {items.length} 份</summary>
    {open && <div className="mt-3 space-y-3">
      {later && <p className="text-[11px] leading-5 text-[#916949]">这些版本不在本次交付包内。</p>}
      <ArtifactRows items={items} names={names} label={title} taskId={taskId} accessToken={accessToken} />
    </div>}
  </details>
}

export function TeamArtifacts({ team, taskId, artifacts, events, names, accessToken = '' }: {
  team: TeamState; taskId: string; artifacts: Artifact[]; events: TeamEvent[]; names: Record<string, string>; accessToken?: string
}) {
  const catalog = teamArtifactCatalog(team, artifacts, events)
  const comparable = useMemo(() => hasComparisonCandidates(artifacts), [artifacts])
  const title = catalog.kind === 'delivery' ? '本次交付文件' : catalog.kind === 'current' ? '本轮发布' : '已发布版本'
  const categories = Object.fromEntries([...catalog.primary.map(item => [item.seq, title]),
    ...catalog.history.map(item => [item.seq, '历史发布']), ...catalog.later.map(item => [item.seq, '交付后发布，不在本次交付包内'])])
  return <section aria-label="成果版本" className="min-w-0 border-t border-[#eaded1] pt-4 [overflow-wrap:anywhere]">
    <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
      <h2 className="text-sm font-semibold">{title}<span className="ml-2 text-[11px] font-normal text-[#728679]">{catalog.primary.length} 份</span></h2>
      <div className="flex shrink-0 gap-1">
        {comparable && <ComparisonEntry key={`${taskId}:${team.revision}:${accessToken}`} artifacts={artifacts} events={events} preferred={catalog.primary} names={names} categories={categories} />}
        {catalog.kind === 'delivery' && catalog.primary.length > 0 && <DeliveryDownload key={`${taskId}:${catalog.deliverySeq}`} taskId={taskId} seq={catalog.deliverySeq!} accessToken={accessToken} />}
      </div>
    </div>
    <p className="mb-3 text-[11px] leading-5 text-[#728679]">{catalog.kind === 'delivery' ? `店长交付记录 #${catalog.deliverySeq} · 验证范围以交付说明及实际回执为准。`
      : catalog.kind === 'current' ? '本轮最新发布，尚未确认交付。' : '发布记录不等于当前交付版本。'}</p>
    {catalog.issue && <p role="alert" className="mb-3 text-xs leading-5 text-amber-800">{catalog.issue}</p>}
    {catalog.primary.length ? <ArtifactRows items={catalog.primary} names={names} label={title} taskId={taskId} accessToken={accessToken} />
      : <p className="text-xs leading-5 text-[#728679]">{catalog.kind === 'delivery' ? '这次交付没有附带文件。' : '本轮还没有发布成果。'}</p>}
    <History key={`${taskId}:${team.revision}:later`} items={catalog.later} names={names} taskId={taskId} accessToken={accessToken} later />
    <History key={`${taskId}:${team.revision}:history`} items={catalog.history} names={names} taskId={taskId} accessToken={accessToken} />
  </section>
}
