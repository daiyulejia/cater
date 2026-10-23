import { artifactReceiptContent, nativeArtifactReceiptStatus } from './conversationActivityState'
import { archiveEntries } from './archiveManifest'
import { artifactHistoryReceipt } from './artifactHistoryReceipt'
import type { TeamEvent } from './team'

export function NativeArtifactReceipt({ event }: { event: TeamEvent }) {
  const history = artifactHistoryReceipt(event)
  if (history) return <div key={JSON.stringify([event.seq, history])} aria-label="成果发布历史回执"
    className="mt-1 min-w-0 max-w-full space-y-2 [overflow-wrap:anywhere]">
    <p className="whitespace-pre-wrap font-semibold">成果发布历史 · {history.filename}</p>
    <p>本群同名发布元数据 · 本页 {history.versions.length} 条 / 共 {history.total} 条 · 所选成果 #{history.artifactSeq}</p>
    <p>仅按本群确切文件名列出发布记录；同名不代表同一目标或返修关系。</p>
    <p>未读取正文或当前磁盘文件；不代表复核通过、验证完成或任务交付。</p>
    <p>{history.hasMore ? `还有后续发布记录 · 本页末条 #${history.nextSeq}。` : '已到发布历史末页，无下一页。'}</p>
    <details>
      <summary className="cursor-pointer font-semibold">发布序号 · 本页 {history.versions.length} 条</summary>
      {history.versions.length ? <ul aria-label="本页发布序号" tabIndex={0}
        className="mt-1 max-h-48 min-w-0 max-w-full overflow-x-hidden overflow-y-auto">
        {history.versions.map(row => <li key={row.seq} className="py-1">发布版本 #{row.seq}</li>)}
      </ul> : <p>此游标之后没有发布记录。</p>}
    </details>
  </div>
  const status = nativeArtifactReceiptStatus(event)
  if (!['成果正文已读取', '成果本页已读取', '成果仅有元数据，正文不可读', '工程文件清单已读取',
    '工程文件正文已读取', '工程文件本页已读取', '工程二进制字节已核对'].includes(status)) return null
  const raw = event.payload.result as Record<string, unknown>
  const result = artifactReceiptContent(raw)
  const manifest = status === '工程文件清单已读取' ? archiveEntries(raw.archive) : null
  const member = raw.artifact_seq !== undefined
  const binary = status === '工程二进制字节已核对'
  const range = result.read_range as Record<string, unknown> | undefined
  const content = typeof result.content === 'string' ? Array.from(result.content) : undefined
  const binding = JSON.stringify([event.seq, result.seq, result.filename, result.content_sha256, raw.archive_sha256, range?.offset, range?.returned_chars])
  return <div key={binding} className="mt-1 min-w-0 space-y-2 [overflow-wrap:anywhere]">
    <p className="whitespace-pre-wrap font-semibold">{manifest || member ? '工程包' : '成果'}版本 #{result.seq as number}{typeof result.filename === 'string' ? ` · ${result.filename}` : ''}</p>
    {result.filename === undefined && <>
      {typeof result.title === 'string' && result.title.trim() && <p className="whitespace-pre-wrap">成果标题：{result.title}</p>}
      <p>文件名未提供。</p>
    </>}
    {manifest ? <>
      <p>本次只读取文件清单，未读取成员正文，也未在界面重新核对 ZIP 字节。</p>
      <details>
        <summary className="cursor-pointer font-semibold">工程文件清单 · {manifest.length} 份</summary>
        <ul aria-label="已读取的工程文件清单" className="mt-1 max-h-48 overflow-auto">
          {manifest.map(entry => <li key={entry.path} className="py-1">
            <code className="block whitespace-pre-wrap">{entry.path}</code>
            <span>{entry.bytes} 字节 · {entry.executable ? '可执行文件' : '普通文件'}</span>
          </li>)}
        </ul>
      </details>
    </> : binary ? <p>宿主已核对二进制字节与摘要；未解释或执行文件内容，不是文本正文读取。</p> : range ? <>
      <p>本页起点：{range.offset as number} · 本页 {range.returned_chars as number} 字符 · 全文共 {range.total_chars as number} 字符（Unicode 码点）</p>
      {range.has_more ? <p>还有后续正文 · 下一页起点 {range.next_offset as number}。</p> : <p>已到末页，无下一页。</p>}
      <p>{range.content_complete ? '本页覆盖全文。' : '本页未覆盖全文；末页不代表已读取全文。'}</p>
    </> : content ? <>
      <p>未分页正文回执 · 起点 0 · 返回 {content.length} 字符 · 全文共 {content.length} 字符（Unicode 码点）</p>
      <p>本次返回覆盖全文，无分页范围。</p>
    </> : <p>旧成果仅保存元数据，未保存可读取正文；无法确认正文范围或全文覆盖。</p>}
    <p>读取不代表复核通过、验证完成或任务交付。</p>
    <details>
      <summary className="cursor-pointer font-semibold">{manifest ? '工程包' : member ? '成员文件' : '全文'}指纹与大小</summary>
      <div className="mt-1 space-y-1">
        <p>{manifest ? '以下为发布记录中的 ZIP 指纹与大小，不代表本次读取所有成员。'
          : member ? '以下为完整成员文件的指纹与大小，不是本页；仅展示宿主回执，未在界面重新验证。'
          : '以下 SHA-256 与字节数属于完整原文，不是本页；仅展示记录值，未在此验证。'}</p>
        <p className="font-mono">全文 SHA-256：{result.content_sha256 as string ?? '未提供'}</p>
        <p>全文大小：{result.content_bytes === undefined ? '未提供' : `${result.content_bytes} 字节`}</p>
        {member && <p className="font-mono">所属工程包 SHA-256：{raw.archive_sha256 as string}</p>}
      </div>
    </details>
    {content && <details>
      <summary className="cursor-pointer font-semibold">成果正文 · {range ? '本页' : '本次返回'}纯文本</summary>
      <pre tabIndex={0} aria-label="成果读取正文" className="mt-2 max-h-64 min-w-0 overflow-auto whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]">{content.slice(0, 16000).join('') || '（空正文）'}</pre>
      {content.length > 16000 && <p>界面仅显示前 16000 字符，显示已截断；上述范围描述工具返回内容。</p>}
    </details>}
  </div>
}
