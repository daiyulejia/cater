import { receiptReadProvenance } from './receiptReadStatus'
import type { TeamEvent } from './team'

export function NativeReceiptRead({ event }: { event: TeamEvent }) {
  const read = receiptReadProvenance(event)
  if (!read) return null
  if (!read.valid) return <p>{read.errorNote ?? '已存回执读取待核对；无法确认本次查询的来源或返回范围。'}</p>
  const sources = read.sources ?? []
  return <div className="mt-1 min-w-0 space-y-2 [overflow-wrap:anywhere]">
    <p>本次读取已保存的观察记录，不重新执行原操作；不代表当前磁盘状态、重新核验、授权写入或验收通过。</p>
    {read.status === '文件读取来源范围不可查询' ? <p>当前工作目录绑定范围不可查询；空列表不表示没有历史读取，也不证明发生过解绑或权限变更。</p>
      : read.status === '当前筛选无匹配文件读取记录' ? <p>当前可查询范围及筛选条件下没有匹配记录，不证明文件从未读过。</p>
      : sources.length === 0 ? <p>当前筛选条件下没有匹配回执。</p> : null}
    {read.section === 'file_reads' && <p>仅列文件读取元数据，未读取原回执正文；原观察可能为节选，不补齐全文，首次或末次观察不代表基线或当前文件。</p>}
    {sources.some(source => source.kind === '未结算操作') && <p>未结算操作仅为已保存的状态记录，影响仍待核对；不代表操作已完成或允许重试。</p>}
    <p>{read.hasMore || read.pendingHasMore ? '还有后续记录，本页不代表全部历史。' : '本次查询已到末页，仅代表本次筛选范围。'}</p>
    <details key={JSON.stringify([event.seq, sources])}>
      <summary className="cursor-pointer font-semibold">来源事件序号 · 本页 {sources.length} 条</summary>
      <ul aria-label="已读取的来源事件序号" className="mt-1 max-h-48 overflow-auto">
        {sources.map(source => <li key={`${source.kind}:${source.seq}`}>{source.kind} #{source.seq}{source.relation ? ` · ${source.relation}` : ''}</li>)}
      </ul>
    </details>
  </div>
}
