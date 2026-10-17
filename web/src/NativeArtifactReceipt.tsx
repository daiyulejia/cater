import { nativeArtifactReceiptStatus } from './conversationActivityState'
import type { TeamEvent } from './team'

export function NativeArtifactReceipt({ event }: { event: TeamEvent }) {
  const status = nativeArtifactReceiptStatus(event)
  if (!['成果正文已读取', '成果本页已读取', '成果仅有元数据，正文不可读'].includes(status)) return null
  const result = event.payload.result as Record<string, unknown>
  const range = result.read_range as Record<string, unknown> | undefined
  const content = typeof result.content === 'string' ? Array.from(result.content) : undefined
  const binding = `${event.seq}:${result.seq}:${result.content_sha256}:${range?.offset}:${range?.returned_chars}`
  return <div key={binding} className="mt-1 min-w-0 space-y-2 [overflow-wrap:anywhere]">
    <p className="whitespace-pre-wrap font-semibold">成果版本 #{result.seq as number}{typeof result.filename === 'string' ? ` · ${result.filename}` : ''}</p>
    {result.filename === undefined && <>
      {typeof result.title === 'string' && result.title.trim() && <p className="whitespace-pre-wrap">成果标题：{result.title}</p>}
      <p>文件名未提供。</p>
    </>}
    {range ? <>
      <p>本页起点：{range.offset as number} · 本页 {range.returned_chars as number} 字符 · 全文共 {range.total_chars as number} 字符（Unicode 码点）</p>
      {range.has_more ? <p>还有后续正文 · 下一页起点 {range.next_offset as number}。</p> : <p>已到末页，无下一页。</p>}
      <p>{range.content_complete ? '本页覆盖全文。' : '本页未覆盖全文；末页不代表已读取全文。'}</p>
    </> : content ? <>
      <p>未分页正文回执 · 起点 0 · 返回 {content.length} 字符 · 全文共 {content.length} 字符（Unicode 码点）</p>
      <p>本次返回覆盖全文，无分页范围。</p>
    </> : <p>旧成果仅保存元数据，未保存可读取正文；无法确认正文范围或全文覆盖。</p>}
    <p>读取不代表复核通过、验证完成或任务交付。</p>
    <details>
      <summary className="cursor-pointer font-semibold">全文指纹与大小</summary>
      <div className="mt-1 space-y-1">
        <p>以下 SHA-256 与字节数属于完整原文，不是本页；仅展示记录值，未在此验证。</p>
        <p className="font-mono">全文 SHA-256：{result.content_sha256 as string ?? '未提供'}</p>
        <p>全文大小：{result.content_bytes === undefined ? '未提供' : `${result.content_bytes} 字节`}</p>
      </div>
    </details>
    {content && <details>
      <summary className="cursor-pointer font-semibold">成果正文 · {range ? '本页' : '本次返回'}纯文本</summary>
      <pre tabIndex={0} aria-label="成果读取正文" className="mt-2 max-h-64 min-w-0 overflow-auto whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]">{content.slice(0, 16000).join('') || '（空正文）'}</pre>
      {content.length > 16000 && <p>界面仅显示前 16000 字符，显示已截断；上述范围描述工具返回内容。</p>}
    </details>}
  </div>
}
