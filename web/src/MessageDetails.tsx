import type { ChatItem } from './App'
import { nativeHandoff, nativeHandoffWarning } from './nativeHandoff'
import type { HandoffEvidence } from './nativeHandoff'
import { outputEncodingHint } from './outputPreview'
import { NativeCommandReceipts } from './NativeCommandReceipts'
import { DeliveryOmissionNote } from './DeliveryOmissionNote'
import { deliveryOmissionReason } from './deliveryOmission'

const sourceLabels = { artifact: '成果原文', tool_receipt: '历史工具回执', peer_review: '伙伴复核意见' }
const statusLabels: Record<string, string> = { ok: '正常返回', error: '返回错误', completed: '已结束', succeeded: '执行成功',
  passed: '检查通过', failed: '执行失败', approved: '伙伴同意', changes_requested: '伙伴建议修改', timeout: '超时', unavailable: '不可用' }

const toolLabels: Record<string, string> = { browser_check: '检查网页交互', workspace_read: '读取文件', workspace_write: '写入文件', workspace_run: '运行命令', workspace_list: '列出目录', workspace_search: '查找文件', read_artifact: '读取成果原文', load_tools: '载入工具说明' }
const outcomeLabels = { passed: '检查通过', failed: '执行失败', timeout: '超时', launch_error: '启动失败', effects_unknown: '执行影响待核实', not_started: '未开始执行' }

function SourceQuote({ source, name, reply = false }: { source: HandoffEvidence; name?: (id: string) => string; reply?: boolean }) {
  const nonzero = reply && source.exit_code !== undefined && source.exit_code !== 0
  return <li className={reply ? 'min-w-0 space-y-1 [overflow-wrap:anywhere]' : 'space-y-1'}>
    <p className="font-semibold">{sourceLabels[source.kind]} · 事件 #{source.source_seq}</p>
    {source.filename && <p>文件：{source.filename}</p>}
    {(source.role || source.tool || source.status) && <p>{[
      source.role && `伙伴：${name?.(source.role) ?? '协作成员'}`, source.tool && `工具：${Object.hasOwn(toolLabels, source.tool) ? toolLabels[source.tool] : '工作目录操作'}`,
      source.status && `记录状态：${Object.hasOwn(statusLabels, source.status) ? statusLabels[source.status] : source.status}`,
    ].filter(Boolean).join(' · ')}</p>}
    {source.exit_code !== undefined && <p className={nonzero ? 'font-semibold text-red-700' : undefined}>退出码：{source.exit_code}{nonzero ? ' · 命令未成功（非零退出）' : ''}</p>}
    {reply && source.outcome && <p className={source.outcome !== 'passed' ? 'font-semibold text-red-700' : undefined}>执行结果：{outcomeLabels[source.outcome]}</p>}
    {nonzero && <p>退出状态不表示引文中的每项业务断言都失败。</p>}
    {source.complete !== undefined && <p>{source.complete ? '回执采集完整' : '回执采集未完成'}</p>}
    {source.truncated === true && <p>回执正文仅保留部分内容。</p>}
    {(['stdout', 'stderr'] as const).map(stream => {
      const hint = outputEncodingHint(source.output_utf8_valid?.[stream])
      return hint && <p key={stream}>{stream === 'stdout' ? '标准输出' : '错误输出'}：{hint}</p>
    })}
    <blockquote className="whitespace-pre-wrap border-l-2 border-current pl-3">{source.quote}</blockquote>
    <details className="pt-1"><summary className="cursor-pointer">版本与事件标识</summary>
      {source.content_sha256 && <p>文件版本 SHA-256：{source.content_sha256}</p>}
      <p>来源事件哈希：{source.source_hash}</p>
    </details>
    {source.kind === 'tool_receipt' && <p>这是当时的工具记录，不证明文件后来未变化。</p>}
    {source.kind === 'peer_review' && <p>这是伙伴的复核意见，不代表工具执行或验收通过。</p>}
  </li>
}

function DetailList({ title, items, numbered = false }: { title: string; items: string[]; numbered?: boolean }) {
  if (!items.length) return null
  const List = numbered ? 'ol' : 'ul'
  return <section>
    <h3 className="font-semibold">{title}</h3>
    <List className={`mt-1 space-y-1 pl-5 ${numbered ? 'list-decimal' : 'list-disc'}`}>
      {items.map((text, index) => <li key={index} className="whitespace-pre-wrap">{text}</li>)}
    </List>
  </section>
}

export function MessageDetails({ item }: { item: ChatItem }) {
  const omissionReason = deliveryOmissionReason(item.evidence)
  const handoff = nativeHandoff(item.evidence)
  const handoffWarning = nativeHandoffWarning(item.evidence)
  const replyEvidenceWarning = item.replyEvidenceWarning
  const replyEvidence = replyEvidenceWarning ? undefined : item.replyEvidence
  if (replyEvidence) return <details className="message-details mt-3 min-w-0 border-t border-[#dfeae5] pt-2 text-xs text-[#52766d] [overflow-wrap:anywhere]">
    <summary className="cursor-pointer font-semibold">查看答复依据</summary>
    <div className="mt-2 space-y-3 leading-5">
      {item.details?.length ? <ul className="space-y-1.5">{item.details.map((detail, index) => <li key={index}>{detail}</li>)}</ul> : null}
      <DeliveryOmissionNote event={item.evidence} historical={item.historicalDelivery} />
      <p className="whitespace-pre-wrap">{replyEvidence.note}</p>
      <p>引文对应当时的工具回执，不代表答复观点正确，也不认证测试身份、覆盖范围或验收通过。</p>
      <ul className="space-y-3">{replyEvidence.sources.map((source, index) => <SourceQuote key={index} source={source} name={item.auditName} reply />)}</ul>
    </div>
  </details>
  if (!handoff && !handoffWarning && !replyEvidenceWarning && !item.details?.length && omissionReason === undefined) return null
  const human = item.kind === 'human'
  return <><details className={`message-details mt-3 min-w-0 border-t pt-2 text-xs ${human ? 'border-[#e99c80] text-[#fde9dd]' : 'border-[#dfeae5] text-[#52766d]'}`}>
    <summary className="cursor-pointer font-semibold">{handoff ? '为什么这样做 · 如何运行' : omissionReason !== undefined && !item.details?.length && !handoffWarning && !replyEvidenceWarning ? '交付文件说明' : '查看判断与依据'}</summary>
    {item.details?.length ? <ul className="mt-2 space-y-1.5 leading-5">{item.details.map((detail, index) => <li key={`${item.id}-${index}`}>{detail}</li>)}</ul> : null}
    <DeliveryOmissionNote event={item.evidence} historical={item.historicalDelivery} />
    {replyEvidenceWarning && <p className="mt-2 whitespace-pre-wrap leading-5 [overflow-wrap:anywhere]">{replyEvidenceWarning}</p>}
    {handoffWarning && <p className="mt-2 whitespace-pre-wrap leading-5 [overflow-wrap:anywhere]">可选交付说明：{handoffWarning}</p>}
    {handoff && <div className="mt-3 space-y-3 leading-5">
      <section><h3 className="font-semibold">为什么这样做</h3><p className="mt-1 whitespace-pre-wrap">{handoff.why}</p></section>
      <DetailList title="改动" items={handoff.changed} />
      <DetailList title="影响" items={handoff.impact} />
      <DetailList title="取舍与其他考虑" items={handoff.alternatives} />
      <NativeCommandReceipts event={item.evidence} historical={item.historicalDelivery} name={item.auditName} />
      <DetailList title={Object.hasOwn(handoff, 'command_receipts') ? '成员建议的运行与复查' : '运行与复查'} items={handoff.reproduce} numbered />
      {handoff.evidence.length > 0 && <section><h3 className="font-semibold">引用来源</h3>
        <ul className="mt-2 space-y-3">{handoff.evidence.map((source, index) => <SourceQuote key={index} source={source} name={item.auditName} />)}</ul>
      </section>}
    </div>}
  </details>
    {handoff && handoff.limits.length > 0 && <details key={`${item.id}-${item.evidence.seq}`} className="message-details mt-3 min-w-0 border-t border-[#dfeae5] pt-2 text-xs text-[#52766d]">
      <summary className="cursor-pointer font-semibold">使用边界 · {handoff.limits.length}项</summary>
      <div className="mt-2 space-y-2 leading-5">
        <p>{item.historicalDelivery ? '这是这次历史交付当时记录的使用边界，不代表当前版本。' : '使用前，可以先看看这些适用条件与尚未验证的范围。'}</p>
        <ul className="space-y-1 pl-5 list-disc">
          {handoff.limits.map((text, index) => <li key={index} className="whitespace-pre-wrap">{text}</li>)}
        </ul>
      </div>
    </details>}
  </>
}
