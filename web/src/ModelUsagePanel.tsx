import { Gauge } from 'lucide-react'
import type { TeamEvent } from './team'
import { usageSummary } from './modelUsage'
import type { UsageCall, UsageSource } from './modelUsage'

const format = (value: number) => value.toLocaleString('zh-CN')
const count = (value: number | null) => value === null ? '未记录' : format(value)
const sourceLabels: Record<UsageSource, string> = {
  reported: '已报告', estimated: '估算', unavailable: '用量未报告', unspecified: '来源未注明',
}

function CallDetails({ call }: { call: UsageCall }) {
  const fields: [string, number | null][] = [
    [call.inputKind === 'uncached' ? '输入（不含缓存）' : '输入（含缓存）', call.input],
    ['输出', call.output], ['缓存读取', call.cacheRead], ['缓存写入', call.cacheWrite], ['推理细项', call.reasoning],
  ]
  const available = fields.filter(([, value]) => value !== null)
  return available.length ? <dl className="mt-1 grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 text-[11px] text-[#6b8075]">
    {available.map(([label, value]) => <div key={label} className="contents"><dt>{label}</dt><dd className="text-right tabular-nums">{count(value)} token</dd></div>)}
  </dl> : <p className="mt-1 text-[11px] text-[#74897d]">未记录输入、输出明细。</p>
}

export function ModelUsagePanel({ events, names }: { events: TeamEvent[]; names: Record<string, string> }) {
  const summary = usageSummary(events)
  if (!summary.calls.length) return null
  const name = (id: string) => id === 'unknown' ? '未标记成员'
    : Object.hasOwn(names, id) && typeof names[id] === 'string' && names[id] ? names[id] : id
  const recent = summary.calls.slice(-8).reverse()
  const sized = [...summary.calls].reverse().find((call) => [call.requestSizes.prompt, call.requestSizes.history, call.requestSizes.tools, call.requestSizes.omittedGroups].some((value) => value !== null))
  return <section aria-label="本群模型用量" className="min-w-0 border-y border-[#e5d9cb] py-3 text-xs leading-5 [overflow-wrap:anywhere]">
    <div className="flex items-center justify-between gap-2">
      <h3 className="flex items-center gap-1.5 font-semibold text-[#6c614e]"><Gauge size={15} aria-hidden="true" />模型用量</h3>
      <span className="text-[11px] text-[#74897d]">本群累计 · 含历史目标</span>
    </div>
    <p aria-label="已报告总用量" className="mt-1 text-lg font-semibold leading-7 text-[#294a40] tabular-nums">
      {summary.reportedTokens === null ? summary.reportedCalls > 0 ? '总量超出可合计范围' : '暂无已报告用量' : <>{format(summary.reportedTokens)} <span className="text-xs font-normal text-[#52766d]">token</span></>}
    </p>
    <p className="text-[11px] text-[#74897d]">已记录 {format(summary.calls.length)} 次返回 · 其中 {format(summary.reportedCalls)} 次已报告 · 截至事件 #{summary.asOfSeq}</p>
    {(summary.unavailableCalls > 0 || summary.unspecifiedCalls > 0) && <p className="mt-1 text-[11px] text-[#a36045]">
      {summary.unavailableCalls > 0 && `${format(summary.unavailableCalls)} 次用量未报告`}
      {summary.unavailableCalls > 0 && summary.unspecifiedCalls > 0 && '；'}
      {summary.unspecifiedCalls > 0 && `${format(summary.unspecifiedCalls)} 次来源未注明`}
      ，未计入上方总量。
    </p>}
    {summary.estimatedCalls > 0 && <p className="mt-1 text-[11px] text-[#a36045]">另有估算：{summary.estimatedTokens === null ? '合计不可用' : `${format(summary.estimatedTokens)} token`} · {format(summary.estimatedCalls)} 次，不计入已报告总量。</p>}
    <details className="mt-2">
      <summary className="cursor-pointer font-semibold text-[#52766d]">成员用量与最近返回</summary>
      <p className="mt-2 text-[11px] text-[#74897d]">已报告返回中，{format(summary.detailCalls)} / {format(summary.reportedCalls)} 次有输入或输出明细。未报告不等于未消耗，失败或在途请求仍可能计费；实际费用以服务商账单为准。</p>
      <ul aria-label="按成员汇总的模型用量" className="mt-2 max-h-64 divide-y divide-[#e5e5dc] overflow-y-auto">
        {summary.groups.map((group) => <li key={group.role} className="min-w-0 py-2">
          <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-3"><strong className="min-w-0">@{name(group.role)}</strong><span className="tabular-nums">{group.reportedTokens === null ? group.reportedCalls > 0 ? '合计不可用' : '暂无已报告用量' : `${format(group.reportedTokens)} token`}</span></div>
          <p className="text-[11px] text-[#74897d]">{format(group.calls)} 次返回 · {format(group.reportedCalls)} 次已报告{group.unavailableCalls > 0 ? ` · ${format(group.unavailableCalls)} 次未报告` : ''}{group.unspecifiedCalls > 0 ? ` · ${format(group.unspecifiedCalls)} 次来源未注明` : ''}</p>
          {group.estimatedCalls > 0 && <p className="text-[11px] text-[#a36045]">另有估算 {group.estimatedTokens === null ? '合计不可用' : `${format(group.estimatedTokens)} token`} · {format(group.estimatedCalls)} 次</p>}
        </li>)}
      </ul>
      <h4 className="mt-3 font-semibold">最近返回 · {recent.length} 条</h4>
      <ol aria-label="最近模型返回" className="mt-1 max-h-80 divide-y divide-[#e5e5dc] overflow-y-auto">
        {recent.map((call) => <li key={call.seq} className="min-w-0 py-2">
          <p className="font-medium">@{name(call.role)} · {call.tokens === null ? '用量未报告' : `${format(call.tokens)} token`}</p>
          <p className="text-[11px] text-[#74897d]">事件 #{call.seq} · {sourceLabels[call.source]}</p>
          <CallDetails call={call} />
        </li>)}
      </ol>
      <p className="mt-2 text-[11px] text-[#74897d]">缓存与推理细项不再叠加到总量。缺少拆分的历史返回仍可计入已报告总量；更多记录保留在过程回放。</p>
      {sized && <div className="mt-3 border-t border-[#e5e5dc] pt-2">
        <h4 className="font-semibold">最近有记录的请求信息 · 事件 #{sized.seq}</h4>
        <dl className="mt-1 grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 text-[11px]">
          {([['提示正文', sized.requestSizes.prompt], ['本轮历史', sized.requestSizes.history], ['工具定义', sized.requestSizes.tools]] as const).map(([label, value]) => <div key={label} className="contents"><dt>{label}</dt><dd className="text-right tabular-nums">{count(value)}{value === null ? '' : ' 字节'}</dd></div>)}
        </dl>
        {sized.requestSizes.omittedGroups !== null && sized.requestSizes.omittedGroups > 0 && <p className="mt-1 text-[11px] text-[#74897d]">{format(sized.requestSizes.omittedGroups)} 组较早的完整交互未重复发送，原回执保留。</p>}
        <p className="mt-1 text-[11px] text-[#74897d]">这些是 UTF-8 / JSON 组成字节，不等于 token 或完整 HTTP 请求大小。</p>
      </div>}
    </details>
  </section>
}
