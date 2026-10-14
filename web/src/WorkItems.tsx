import { CheckCircle2, ChevronRight, Circle, CircleAlert, Clock3, GitBranch, LoaderCircle } from 'lucide-react'

export type WorkState = {
  plan_seq: number | null
  stage_accepted: boolean
  scope_current?: boolean
  planning?: { summary: string; coverage: { source_id: string; item_ids: string[]; superseded_by: string | null; reason: string; quote: string }[] } | null
  items: {
    id: string; owner: string; reviewer: string | null; objective: string; status: string; reason: string
    depends_on: string[]
    criteria: { id: string; text: string; kind: string }[]
    checks: { id: string; status: string; reason: string; quote?: string; artifact_seq?: number; receipt_seq?: number }[]
    submission: { seq: number; artifact_seqs: number[]; reused_from?: number } | null
  }[]
}

const labels: Record<string, string> = {
  ready: '可以开工', running: '正在执行', submitted: '等待验收', accepted: '阶段通过',
  blocked: '等待前置', stale: '需要重新核对', uncertain: '结果待核实', rework: '需要返工', planned: '已安排',
}

export function WorkItems({ work, names }: { work?: WorkState | null; names: Record<string, string> }) {
  if (!work?.items.length) return null
  const accepted = work.items.filter((item) => item.status === 'accepted').length
  const titles = Object.fromEntries(work.items.map((item, index) => [item.id, `工作 ${index + 1}`]))
  return <section className="mb-5 border-b border-[#dce8e1] pb-5" aria-label="工作项与验收">
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="flex items-center gap-1.5 text-sm font-semibold"><GitBranch size={16} className="text-teal-700" /> 工作进展</h2>
      <span className="text-xs text-stone-500">{accepted} / {work.items.length} 项通过</span>
    </div>
    {work.scope_current === false && <p className="mb-3 text-xs leading-5 text-amber-800">方向或店规已更新，旧结果暂不放行。</p>}
    {work.planning && <details className="mb-3 text-xs leading-5 text-stone-600">
      <summary className="cursor-pointer font-semibold">要求与安排</summary>
      <p className="mt-2 break-words">{work.planning.summary}</p>
      <ul className="mt-2 space-y-2">{work.planning.coverage.map((entry) => <li key={entry.source_id} className="border-l-2 border-teal-200 pl-2">
        <blockquote className="break-words">“{entry.quote}”</blockquote>
        <p className="break-words">{entry.superseded_by ? '已按后续要求调整' : entry.item_ids.map((id) => titles[id] ?? id).join('、')} · {entry.reason}</p>
      </li>)}</ul>
    </details>}
    <ol className="divide-y divide-[#e6e4db]">
      {work.items.map((item, index) => {
        const Icon = item.status === 'accepted' ? CheckCircle2 : item.status === 'running' ? LoaderCircle
          : item.status === 'submitted' || item.status === 'blocked' ? Clock3
            : item.status === 'uncertain' || item.status === 'stale' ? CircleAlert : Circle
        return <li key={item.id} className="py-3 first:pt-0">
          <details className="group">
            <summary className="flex cursor-pointer list-none items-start gap-2 text-xs [&::-webkit-details-marker]:hidden">
              <Icon size={16} className={`mt-0.5 shrink-0 ${item.status === 'accepted' ? 'text-teal-700' : 'text-amber-700'}`} />
              <div className="min-w-0 flex-1">
                <span className="flex flex-wrap justify-between gap-1"><strong>{index + 1}. {names[item.owner] ?? item.owner}</strong><span className="text-stone-500">{labels[item.status] ?? '状态待核对'}</span></span>
                <p className="mt-1 line-clamp-2 break-words leading-5 text-stone-600 group-open:line-clamp-none">{item.objective}</p>
              </div><ChevronRight size={14} className="mt-1 shrink-0 text-stone-400 group-open:rotate-90" />
            </summary>
            <div className="ml-6 mt-3 space-y-2 text-xs leading-5 text-stone-600">
              <p>复核：{item.reviewer ? (names[item.reviewer] ?? item.reviewer) : '尚未安排独立复核人'}</p>
              {item.depends_on.length > 0 && <p>前置：{item.depends_on.map((id) => titles[id] ?? id).join('、')}</p>}
              {item.reason && <p className="break-words text-amber-800">{item.reason}</p>}
              {item.submission && <p>成果记录：{item.submission.artifact_seqs.map((seq) => `#${seq}`).join('、')}</p>}
              {item.submission?.reused_from && <p className="text-teal-800">沿用候选成果 #{item.submission.reused_from}，本轮验收独立记录。</p>}
              <ul className="space-y-2">
                {item.criteria.map((criterion) => {
                  const result = item.checks.find((check) => check.id === criterion.id)
                  return <li key={criterion.id} className="border-l-2 border-teal-200 pl-2">
                    <p className="break-words">{criterion.text}</p>
                    <p className="text-stone-500">{criterion.kind === 'local_check' ? '实际检查' : '独立审查'} · {result?.status === 'passed' ? '通过' : result?.status === 'failed' ? '未通过' : '待核验'}</p>
                    {result && <p className="break-words">{result.reason}</p>}
                    {result?.quote && <blockquote className="mt-1 break-words text-stone-500">“{result.quote}”</blockquote>}
                    {result?.receipt_seq && <p>检查回执 #{result.receipt_seq}</p>}
                  </li>
                })}
              </ul>
            </div>
          </details>
        </li>
      })}
    </ol>
    {work.stage_accepted && <p className="mt-3 text-xs leading-5 text-teal-800">阶段验收已通过，尚不代表整体交付完成。</p>}
  </section>
}
