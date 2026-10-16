export type CheckOutput = {
  bytes?: number; sha256?: string; truncated?: boolean; complete?: boolean
  excerpt?: string; preview?: string; tail_excerpt?: string; preview_mode?: string
}

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
export const hasBrowserReceipt = (value: unknown): boolean => {
  const item = record(value)
  const report = record(item.browser_report), plan = record(item.browser_plan)
  return (item.name ?? item.tool) === 'browser_check' || item.check_kind === 'browser'
    || Object.keys(report).length > 0
    || typeof plan.entrypoint === 'string' && Array.isArray(plan.steps)
}

export const COMPACT_OUTPUT_LINES = 6

// Bound short-line logs as well as long single lines, preserving the tail's end.
function clip(text: string, limit: number | undefined, tail = false) {
  if (limit === undefined) return { text, clipped: false }
  const lines = text.split(/\r\n|\n|\r/)
  const selected = lines.length > COMPACT_OUTPUT_LINES ? (tail ? lines.slice(-COMPACT_OUTPUT_LINES) : lines.slice(0, COMPACT_OUTPUT_LINES)).join('\n') : text
  const chars = Array.from(selected)
  const clipped = lines.length > COMPACT_OUTPUT_LINES || chars.length > limit
  const saved = chars.length > limit ? (tail ? chars.slice(-limit) : chars.slice(0, limit)).join('') : selected
  return { text: !clipped ? saved : tail ? `（片段前部已省略）\n${saved}` : `${saved}\n（片段后部已省略）`, clipped }
}

export function outputPreview(value: unknown, limit?: number) {
  const output = record(value)
  const headTail = output.preview_mode === 'head_tail'
  const head = typeof value === 'string' ? value : !headTail && typeof output.preview === 'string' ? output.preview : typeof output.excerpt === 'string' ? output.excerpt
    : typeof output.preview === 'string' ? output.preview : undefined
  const tail = headTail && typeof output.tail_excerpt === 'string' ? output.tail_excerpt : undefined
  const incomplete = output.complete === false
  const parts = headTail
    ? [{ label: '开头（已保存片段）', text: head ?? '' }, { label: incomplete ? '已采集末尾（已保存片段）' : '末尾（已保存片段）', text: tail ?? '' }]
    : head ? [{ label: '已保存片段', text: head }] : []
  const shown = parts.map((part, index) => ({ ...part, ...clip(part.text, limit, headTail && index === 1) }))
  const clipped = shown.some(part => part.clipped)
  const emptyText = output.bytes === 0 && output.complete === true ? '无输出。'
    : head === '' ? '已保存的输出文本为空。'
    : output.bytes === 0 && incomplete ? '未采集到输出，采集尚未完成。'
    : typeof output.bytes === 'number' && output.bytes > 0 && typeof output.sha256 === 'string' ? '未保存输出正文，仅提供字节数与 SHA256。'
    : '输出正文未记录。'
  return {
    headTail, incomplete, clipped, emptyText,
    partial: headTail || output.truncated === true || clipped,
    parts: shown,
  }
}

export function outputSummaryLines(name: string, value: unknown, limit = 800, hideBody = false): string[] {
  if (value === undefined) return []
  const output = record(value), preview = outputPreview(value, limit)
  const lines: string[] = []
  if (!hideBody) {
    preview.parts.forEach((part, index) => {
      if (preview.headTail && index === 1) lines.push(`${name}：中间省略，非完整原文。`)
      lines.push(`${name}${preview.headTail ? ` · ${part.label}` : ''}：${part.text || '未记录该片段正文。'}`)
    })
    if (!preview.parts.length) lines.push(`${name}：${preview.emptyText}`)
  }
  if (preview.incomplete) lines.push(`${name}采集未完成。`)
  if (output.truncated === true) lines.push(`${name}已截断，当前展示不是完整输出。`)
  return lines
}
