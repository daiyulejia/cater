import { nativeProjectHistoryReceiptStatus, projectHistoryArchiveReadEvent } from './conversationActivityState'
import { NativeArtifactReceipt } from './NativeArtifactReceipt'
import type { TeamEvent } from './team'
import { isOutputUtf8Validity, knownHistoryHandoffWarning, partialHandoff } from './nativeHandoff'
import { outputEncodingHint } from './outputPreview'

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const text = (value: unknown, limit = 1200): string => {
  if (typeof value !== 'string') return ''
  const points = Array.from(value)
  return points.slice(0, limit).join('') + (points.length > limit ? '\n（显示已截断）' : '')
}
const bodyClass = 'max-h-64 min-w-0 overflow-auto whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]'

function HistoryReferences({ value }: { value: unknown }) {
  if (!Array.isArray(value)) return null
  return <ul aria-label="历史文件引用" className="max-h-64 space-y-1 overflow-auto">{value.slice(0, 128).map((item, index) => {
    const ref = record(item)
    return <li key={index} className="min-w-0">
      <p>{text(ref.filename, 180)}{typeof ref.seq === 'number' && Number.isSafeInteger(ref.seq) && ref.seq > 0 ? ` · 文件事件 #${ref.seq}` : ''}</p>
      {typeof ref.content_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(ref.content_sha256) && <p className="font-mono">SHA-256：{ref.content_sha256}</p>}
    </li>
  })}{value.length > 128 && <li>文件引用显示已截断。</li>}</ul>
}

function DeliveryContent({ content, pageState }: { content: string; pageState?: { status: unknown; warning: unknown } }) {
  const pageWarning = pageState && knownHistoryHandoffWarning(pageState.status, pageState.warning)
  const incomplete = pageState && <>
    <p>历史交付说明状态不一致，请核对原记录。</p>
    {pageWarning && <p className="whitespace-pre-wrap">当时交付说明：{text(pageState.warning, 1000)}</p>}
  </>
  let parsed: unknown
  try { parsed = JSON.parse(content) } catch {
    return pageState ? incomplete : <pre className={bodyClass}>{text(content, 8000) || '（空正文）'}</pre>
  }
  const data = record(parsed)
  const mismatch = pageState !== undefined && (!pageWarning || data.handoff_status !== pageState.status || data.handoff_warning !== pageState.warning)
  const hasWarning = knownHistoryHandoffWarning(data.handoff_status, data.handoff_warning)
  const partial = !mismatch && data.handoff_status === 'omitted_invalid' ? partialHandoff(data.handoff_partial, data.handoff_warning) : undefined
  const originalHandoff = record(data.handoff)
  const handoff = record(mismatch ? undefined : partial ?? (data.handoff_status === 'omitted_invalid' ? undefined
    : data.handoff_status === 'references_omitted_invalid' ? { ...originalHandoff, evidence: [] } : originalHandoff))
  const fields = [['历史摘要', data.summary], ['当时目标', data.objective], ['店长当时验证说明', data.verification]]
  const recognized = fields.some(([, value]) => typeof value === 'string')
  if (!recognized && pageState) return incomplete
  if (!recognized) return <details>
    <summary className="cursor-pointer">其他历史数据 · 纯文本</summary>
    <pre className={`mt-1 ${bodyClass}`}>{text(content, 8000)}</pre>
  </details>
  return <div className="space-y-2">
    <dl className="space-y-2">{fields.filter(([, value]) => typeof value === 'string').map(([label, value]) => <div key={label as string}>
      <dt className="font-semibold">{label as string}</dt><dd className="max-h-48 overflow-auto whitespace-pre-wrap">{text(value, 2000)}</dd>
    </div>)}</dl>
    {mismatch && <p>历史交付说明状态不一致，请核对原记录。</p>}
    {(mismatch ? pageWarning : hasWarning) && <p className="whitespace-pre-wrap">当时交付说明：{text(mismatch ? pageState?.warning : data.handoff_warning, 1000)}</p>}
    {Object.keys(handoff).length > 0 && <details>
      <summary className="cursor-pointer">当时交接说明</summary>
      {text(handoff.why, 2000) && <p className="mt-1 whitespace-pre-wrap">原因：{text(handoff.why, 2000)}</p>}
      {(['changed', 'impact', 'alternatives', 'reproduce', 'limits'] as const).map((key, index) => {
        const values = handoff[key]
        if (!Array.isArray(values) || !values.some(value => typeof value === 'string')) return null
        return <div key={key} className="mt-2">
          <p className="font-semibold">{['当时改动', '当时影响', '当时取舍', '当时复查步骤', '当时未验证范围'][index]}</p>
          <ul className="max-h-48 space-y-1 overflow-auto">{values.slice(0, 12).map((value, offset) => typeof value === 'string' ? <li key={offset}>{text(value, 1000)}</li> : null)}</ul>
          {values.length > 12 && <p>交接条目显示已截断。</p>}
        </div>
      })}
      {Array.isArray(handoff.evidence) && handoff.evidence.length > 0 && <details className="mt-2">
        <summary className="cursor-pointer">当时引用摘录 · 历史记录</summary>
        <ul className="max-h-64 space-y-2 overflow-auto">{handoff.evidence.slice(0, 16).map((value, index) => {
          const ref = record(value)
          const validity = isOutputUtf8Validity(ref.output_utf8_valid) ? ref.output_utf8_valid : undefined
          return typeof ref.quote === 'string' ? <li key={index}>
            {typeof ref.source_seq === 'number' && Number.isSafeInteger(ref.source_seq) && ref.source_seq > 0 && <p>来源事件 #{ref.source_seq}</p>}
            <blockquote className="whitespace-pre-wrap">{text(ref.quote, 2000)}</blockquote>
            {(['stdout', 'stderr'] as const).map(stream => {
              const hint = outputEncodingHint(validity?.[stream])
              return hint && <p key={stream}>{stream === 'stdout' ? '标准输出' : '错误输出'}：{hint}</p>
            })}
          </li> : null
        })}</ul>
        {handoff.evidence.length > 16 && <p>引用摘录显示已截断。</p>}
      </details>}
    </details>}
    {Array.isArray(data.artifacts) && <details>
      <summary className="cursor-pointer">交付文件引用 · {data.artifacts.length} 份</summary>
      <HistoryReferences value={data.artifacts} />
    </details>}
  </div>
}

export function NativeProjectHistoryReceipt({ event }: { event: TeamEvent }) {
  const status = nativeProjectHistoryReceiptStatus(event)
  const archiveEvent = projectHistoryArchiveReadEvent(event)
  if (!archiveEvent && !['历史目录已读取', '历史交付说明已读取', '历史文件已读取'].includes(status)) return null
  const result = record(event.payload.result), index = result.section === 'index', source = record(result.source)
  const rows = index ? result.entries as Record<string, unknown>[] : []
  const content = typeof result.content === 'string' ? result.content : ''
  const fullDelivery = result.section === 'delivery' && result.offset === 0 && result.has_more === false
  const hasPageState = Object.hasOwn(result, 'handoff_status') || Object.hasOwn(result, 'handoff_warning')
  const pageState = hasPageState ? { status: result.handoff_status, warning: result.handoff_warning } : undefined
  const pageWarning = result.section === 'delivery' && !fullDelivery
    && knownHistoryHandoffWarning(result.handoff_status, result.handoff_warning)
  const end = (result.offset as number) + Array.from(content).length
  return <div className="mt-1 min-w-0 space-y-2 [overflow-wrap:anywhere]">
    {archiveEvent ? <>
      <p className="font-semibold">同项目历史工程包 · {text(source.filename, 180)}</p>
      <NativeArtifactReceipt event={archiveEvent} />
    </> : index ? <>
      {rows.length === 0 && <p>当前项目暂无历史交付。</p>}
      <ul aria-label="同项目历史交付" className="space-y-3">{rows.map(row => <li key={row.delivery_seq as number} className="min-w-0">
        <p className="whitespace-pre-wrap"><strong>历史摘要：</strong>{text(row.summary_excerpt, 640)}{row.summary_truncated === true && '（摘要节选）'}</p>
        <p className="whitespace-pre-wrap">当时目标：{text(row.objective_excerpt, 640)}{row.objective_truncated === true && '（目标节选）'}</p>
        <p>历史交付 #{row.delivery_seq as number} · 文件 {row.artifact_count as number} 份</p>
        {row.has_handoff_partial === true && <p>当时交付说明仅部分保存。</p>}
        {(row.artifacts as Record<string, unknown>[]).map(ref => <p key={ref.seq as number}>{text(ref.filename, 128)}</p>)}
        {row.artifacts_truncated === true && <p>仅列部分文件引用；完整引用见历史交付说明。</p>}
      </li>)}</ul>
      <p>本页 {rows.length} 条 · 共 {result.total as number} 条历史交付</p>
      {result.has_more === true && <p>还有历史目录 · 下一页上界 #{result.next_before_seq as number}（不含），本页不是全部。</p>}
    </> : <>
      {result.section === 'artifact' && text(source.filename, 180) && <p className="font-semibold">历史文件：{text(source.filename, 180)}</p>}
      {pageWarning && <p className="whitespace-pre-wrap">当时交付说明：{text(result.handoff_warning, 1000)}</p>}
      {result.section === 'delivery' && !fullDelivery && hasPageState && !pageWarning && <p>历史交付说明状态待核对。</p>}
      {fullDelivery ? <DeliveryContent key={event.seq} content={content} pageState={pageState} />
        : result.section === 'delivery' && hasPageState
          ? <p>此页仅为历史正文片段，无法单独核对交付说明；正文未显示，请结合完整记录核对。</p>
          : <pre className={bodyClass}>{text(content, 8000) || '（空正文）'}</pre>}
      <p>本页字符：{result.offset as number} 至 {end} · 历史正文共 {result.total_chars as number} 字符</p>
      {result.has_more === true ? <p>历史节选，非全文 · 下一页起点 {result.next_offset as number}。</p>
        : <p>{result.offset === 0 ? '已到历史正文末页。' : '末页节选，非全文。'}</p>}
    </>}
    <details key={`source:${event.seq}`}>
      <summary className="cursor-pointer">来源与范围</summary>
      <div className="mt-1 space-y-1 whitespace-pre-wrap">
        <p>历史持久版本，不代表当前目标、验收或磁盘状态；店长当时的验证说明不是独立实测。</p>
        <p>来源项目：{text(index ? result.project_id : source.project_id, 128)}</p>
        <p>目录版本：事件 #{result.as_of_seq as number}</p>
        <p className="font-mono">版本事件 SHA-256：{text(result.as_of_hash, 64)}</p>
        {(index ? rows : [source]).map((row, offset) => <div key={offset} className="mt-2 min-w-0">
          <p>来源任务：{text(row.task_id, 128)} · 历史交付 #{row.delivery_seq as number}</p>
          <p className="font-mono">交付事件 SHA-256：{text(row.delivery_hash, 64)}</p>
          {index ? <HistoryReferences value={row.artifacts} /> : <>
            {typeof source.artifact_seq === 'number' && <p>历史文件事件 #{source.artifact_seq}</p>}
            {text(source.author, 128) && <p>当时发布成员：{text(source.author, 128)}</p>}
            {typeof source.content_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(source.content_sha256) && <p className="font-mono">文件 SHA-256：{source.content_sha256}</p>}
            {archiveEvent && <p className="font-mono">文件事件 SHA-256：{text(source.event_hash, 64)}</p>}
          </>}
        </div>)}
      </div>
    </details>
  </div>
}
