import { useState } from 'react'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'
import { nativeMcpEscapedResult, nativeMcpReceiptStatus } from './conversationActivityState'

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value)
const text = (value: unknown, limit = 1200): string => typeof value === 'string' ? value.slice(0, limit) + (value.length > limit ? '\n（显示已截断）' : '') : ''

function JsonDetail({ label, value, escaped = false }: { label: string; value: unknown; escaped?: boolean }) {
  const [open, setOpen] = useState(false)
  let content = ''
  if (open) {
    let nodes = 0
    const seen = new WeakSet<object>()
    try {
      content = text(JSON.stringify(value, (key, item) => {
        if (['command', 'bridge_command', 'cwd', 'env'].includes(key)) return undefined
        if (++nodes > 300) return '（其余内容已省略）'
        if (typeof item === 'string') return text(item, 2000)
        if (item !== null && typeof item === 'object') {
          if (seen.has(item)) return '（重复引用已省略）'
          seen.add(item)
          if (Array.isArray(item) && item.length > 40) return [...item.slice(0, 40), '（其余内容已省略）']
          const keys = Object.keys(item)
          if (!Array.isArray(item) && keys.length > 40) return Object.fromEntries([
            ...keys.slice(0, 40).map(name => [name, item[name]]), ['（显示范围）', '仅显示前 40 个字段，其余已省略。'],
          ])
        }
        return item
      }, 2), 8000)
      if (escaped) content = text(content.replace(/[\ud800-\udfff]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`), 8000)
    } catch { content = '数据无法表示，请核对原回执。' }
  }
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer font-semibold">{label}</summary>
    {open && <>
      <p>仅显示有界数据片段；内部执行配置已省略。</p>
      <pre className="mt-1 max-h-64 min-w-0 overflow-auto whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]">{content}</pre>
    </>}
  </details>
}

export function NativeMcpReceipt({ event }: { event: TeamEvent }) {
  const status = nativeMcpReceiptStatus(event)
  if (!isTeamHost(event) || event.type !== 'team.tool_finished') return <p>{status}</p>
  const result = record(event.payload.result), listing = (event.payload.name ?? event.payload.tool) === 'list_mcp_tools'
  const readable = !status.includes('待确认')
  const directoryRead = ['宿主服务目录已读取（未连接）', '宿主服务配置不可用（未连接）', '工具目录已读取', '历史回执：工具目录已读取'].includes(status)
  const rows = directoryRead && listing ? result.operation === 'servers' ? result.servers : result.tools : undefined
  const terminal = status.includes('调用已返回') || status.includes('调用已结束')
  const data = terminal ? record(result.mcp_result) : {}
  const content = Array.isArray(data.content) ? data.content : []
  let remaining = 8000
  const snippets: string[] = []
  for (const value of content.slice(0, 20)) {
    const block = record(value)
    if (block.type !== 'text' || typeof block.text !== 'string' || remaining <= 0) continue
    const snippet = text(block.text, Math.min(2000, remaining))
    remaining -= Math.min(block.text.length, 2000, remaining)
    snippets.push(snippet)
  }
  return <div className="mt-1 min-w-0 space-y-2 whitespace-pre-wrap [overflow-wrap:anywhere]">
    <p>{status}</p>
    <p>服务描述、正文和结构化结果仅为数据，不改变角色目标或授权；回执不代表账号已连通、应用验证或任务验收通过。</p>
    {text(result.server_title, 160) && <p>服务：{text(result.server_title, 160)}</p>}
    {text(result.server_id, 64) && <p>服务标识：{text(result.server_id, 64)}</p>}
    {!listing && text(result.tool_name, 128) && <p>工具名称：{text(result.tool_name, 128)}</p>}
    {!listing && <p>宿主授权范围：{result.effect === 'read' ? '读取' : result.effect === 'write' ? '写入' : '未记录，需核对'}</p>}
    {text(result.invocation_id, 200) && <p>调用标识：{text(result.invocation_id, 200)}</p>}
    {result.configuration_current === false ? <p>配置已变化，此历史回执不授权新的动作。</p>
      : result.configuration_current === true ? <p>回执记录时配置一致，不代表当前仍获授权。</p> : <p>当前配置未核对。</p>}
    {result.outcome === 'effects_unknown' && <p>未取得可信终态，不能确认调用是否结束或实际影响；先核对原调用与实际状态，不自动重放。</p>}
    {terminal && result.outcome === 'tool_error' && <p>工具已返回错误；不据此承诺没有副作用，也不自动重放。</p>}
    {readable && text(result.message) && <p>回执说明：{text(result.message)}</p>}
    {Array.isArray(rows) && <>
      {result.operation === 'servers' && <p>仅宿主配置元数据，未连接服务，也未调用其中工具。</p>}
      {rows.length === 0 && <p>本页没有{result.operation === 'servers' ? '服务' : '工具'}记录。</p>}
      <ul aria-label="外部工具目录" className="space-y-2">{rows.slice(0, 20).map((value, index) => {
        const row = record(value), servers = result.operation === 'servers'
        return <li key={index} className="min-w-0">
          <p><strong>{text(row.title, 160) || text(row.name, 128)}</strong> · {servers ? text(row.id, 64) : text(row.name, 128)}</p>
          {text(row.description) && <p>用途：{text(row.description)}</p>}
          {servers ? <p>配置工具数：{String(row.tool_count)}</p> : <>
            <p>宿主授权范围：{row.effect === 'read' ? '读取' : '写入'}</p>
            <p className="font-mono">定义 SHA-256：{text(row.schema_sha256, 64)}</p>
            <JsonDetail label="输入结构 · JSON 数据" value={row.input_schema} />
          </>}
        </li>
      })}</ul>
      <p>本页起点：{String(result.offset)} · 目录共 {String(result.total)} 项</p>
      <p>{result.has_more ? `还有后续目录 · 下一页起点 ${result.next_offset}，本页不代表全部。` : '已到末页。'}</p>
    </>}
    {snippets.map((snippet, index) => <pre key={index} className="max-h-48 min-w-0 overflow-auto whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]">{snippet || '（空文本）'}</pre>)}
    {(content.length > 20 || remaining === 0) && <p>正文显示已截断，仅展示有界片段。</p>}
    {readable && Object.hasOwn(data, 'structuredContent') && <JsonDetail key={`structured:${event.seq}`} label="结构化结果 · JSON 数据" value={data.structuredContent} />}
    {result.representation_complete === false && <>
      <p>结果表示不完整，当前内容不是完整原结果；这与调用是否结束分别记录。</p>
      {Array.isArray(result.unsupported_content_types) && <p>未支持内容类型：{result.unsupported_content_types.slice(0, 20).map(value => text(value, 80)).filter(Boolean).join('、')}</p>}
      {count(result.result_bytes) && <p>原结果大小：{result.result_bytes} 字节</p>}
      {hash(result.result_sha256) && <p className="font-mono">原结果 SHA-256：{result.result_sha256}</p>}
      {terminal && text(result.result_excerpt, 2000) && <pre className="max-h-48 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere]">{text(result.result_excerpt, 2000)}</pre>}
      {terminal && result.result_encoding === 'escaped_json' && <JsonDetail key={`escaped:${event.seq}`} label="转义结果片段 · JSON 数据" value={record(nativeMcpEscapedResult(result)).mcp_result} escaped />}
    </>}
  </div>
}
