import { isOutputUtf8Validity } from './nativeHandoff'
import type { HandoffEvidence } from './nativeHandoff'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'

export type ReplyEvidence = { version: 1; sources: (HandoffEvidence & { kind: 'tool_receipt' })[]; note: string }
export type ReplyEvidenceDetails = { evidence: ReplyEvidence; warning?: never } | { evidence?: never; warning: string }

const omittedWarning = '可选答复引用未通过校验，未保存；答复正文已发送，不代表内容已验证。'

const tools = new Set(['workspace_read', 'workspace_write', 'workspace_run', 'workspace_list', 'workspace_search', 'browser_check', 'read_artifact'])
const outcomes = new Set(['passed', 'failed', 'timeout', 'launch_error', 'effects_unknown', 'not_started'])
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const seq = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value)
const boundedText = (value: unknown, limit: number): value is string => text(value) && value.length <= limit * 2 && Array.from(value).length <= limit
const keys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every(key => allowed.includes(key))

function recordedArguments(value: unknown): unknown {
  if (!record(value) || Object.keys(value).length !== 2 || !keys(value, ['encoding', 'json'])
    || value.encoding !== 'escaped_json' || typeof value.json !== 'string') return value
  try {
    let decodedKeys = 0
    const parsed: unknown = JSON.parse(value.json, (_key, item: unknown) => {
      if (typeof item === 'number' && !Number.isFinite(item)) throw new Error('Non-finite JSON number')
      if (record(item)) decodedKeys += Object.keys(item).length
      return item
    })
    // Tokenize every string, including values, so JSON.parse cannot silently discard duplicate keys.
    let recordedKeys = 0
    for (const token of value.json.matchAll(/"(?:\\.|[^"\\])*"/g)) {
      if (/^\s*:/.test(value.json.slice(token.index + token[0].length))) recordedKeys++
    }
    return recordedKeys === decodedKeys ? parsed : undefined
  } catch {
    return undefined
  }
}

function normalized(value: unknown): value is ReplyEvidence {
  if (!record(value) || !keys(value, ['version', 'sources', 'note']) || value.version !== 1
    || !Array.isArray(value.sources) || value.sources.length < 1 || value.sources.length > 8
    || typeof value.note !== 'string' || value.note.length > 24000) return false
  const seen = new Set<string>()
  for (const source of value.sources) {
    if (!record(source) || !keys(source, ['source_seq', 'quote', 'kind', 'source_hash', 'filename', 'content_sha256',
      'role', 'tool', 'status', 'outcome', 'exit_code', 'complete', 'truncated', 'output_utf8_valid'])
      || !seq(source.source_seq) || !boundedText(source.quote, 2000) || source.kind !== 'tool_receipt'
      || !hash(source.source_hash) || !text(source.role) || !text(source.tool) || !tools.has(source.tool)
      || typeof source.status !== 'string' || !['ok', 'error', 'passed', 'failed'].includes(source.status)
      || ('outcome' in source && (typeof source.outcome !== 'string' || !outcomes.has(source.outcome)))
      || ('filename' in source && !text(source.filename)) || ('content_sha256' in source && !hash(source.content_sha256))
      || ('exit_code' in source && !Number.isSafeInteger(source.exit_code))
      || ['complete', 'truncated'].some(key => key in source && typeof source[key] !== 'boolean')
      || ('output_utf8_valid' in source && !isOutputUtf8Validity(source.output_utf8_valid))) return false
    const pair = JSON.stringify([source.source_seq, source.quote])
    if (seen.has(pair)) return false
    seen.add(pair)
  }
  return new TextEncoder().encode(JSON.stringify(value)).byteLength <= 24000
}

type ToolPair = { start: TeamEvent; finish: TeamEvent; input: TeamEvent; result: Record<string, unknown> }

// A snapshot is scoped to one conversation by the task API. Never resolve refs outside it.
export function replyEvidenceDetailsFor(events: readonly TeamEvent[]): ReadonlyMap<number, ReplyEvidenceDetails> {
  const replies = new Map<number, ReplyEvidenceDetails>()
  const bySeq = new Map<number, TeamEvent>()
  const runs = new Map<string, TeamEvent[]>()
  const endings = new Map<string, TeamEvent[]>()
  const finishes = new Map<number, TeamEvent[]>()
  const messages = new Map<number, TeamEvent[]>()
  for (const event of events) {
    if (!seq(event.seq) || bySeq.has(event.seq)) return replies
    bySeq.set(event.seq, event)
    if (isTeamHost(event) && event.type === 'team.run_started' && text(event.payload.run_id)) {
      const rows = runs.get(event.payload.run_id) ?? []
      rows.push(event)
      runs.set(event.payload.run_id, rows)
    }
    if (isTeamHost(event) && event.type === 'team.run_finished' && text(event.payload.run_id)) {
      const rows = endings.get(event.payload.run_id) ?? []
      rows.push(event)
      endings.set(event.payload.run_id, rows)
    }
    if (isTeamHost(event) && event.type === 'team.tool_finished' && seq(event.payload.started_seq)) {
      const rows = finishes.get(event.payload.started_seq) ?? []
      rows.push(event)
      finishes.set(event.payload.started_seq, rows)
    }
    if (event.type === 'msg.said' && seq(event.payload.reply_evidence_started_seq)) {
      const rows = messages.get(event.payload.reply_evidence_started_seq) ?? []
      rows.push(event)
      messages.set(event.payload.reply_evidence_started_seq, rows)
    }
  }
  const pairFor = (startSeq: unknown): ToolPair | undefined => {
    if (!seq(startSeq)) return undefined
    const start = bySeq.get(startSeq)
    if (!start || !isTeamHost(start) || start.type !== 'team.tool_started') return undefined
    const p = start.payload
    if (!text(p.run_id) || !text(p.role) || !text(p.name) || !text(p.call_id) || !seq(p.message_seq)
      || !Number.isSafeInteger(p.revision) || Number(p.revision) < 0) return undefined
    const runRows = runs.get(p.run_id), finishRows = finishes.get(start.seq)
    if (runRows?.length !== 1 || finishRows?.length !== 1) return undefined
    const run = runRows[0], finish = finishRows[0], input = bySeq.get(p.message_seq)
    const result = finish.payload.result
    if (!input || input.type !== 'team.message_received'
      || !(input.actor.kind === 'human' && input.actor.id === 'human' && input.actor.role === 'human'
        || input.actor.kind === 'agent' && text(input.actor.id) && text(input.actor.role)
        || isTeamHost(input) && input.payload.kind === 'recovery')
      || !text(input.payload.text) || input.payload.recipient !== p.role
      || !(input.seq < run.seq && run.seq < start.seq && start.seq < finish.seq)
      || ['run_id', 'role', 'message_seq'].some(key => run.payload[key] !== p[key])
      || endings.get(p.run_id)?.some(end => end.seq < finish.seq
        && end.payload.role === p.role && end.payload.message_seq === p.message_seq)
      || ['run_id', 'role', 'name'].some(key => finish.payload[key] !== p[key])
      || ('call_id' in finish.payload && finish.payload.call_id !== p.call_id)
      || !record(result) || (result.status !== 'ok' && result.status !== 'error')) return undefined
    return { start, finish, input, result }
  }
  for (const [startedSeq, rows] of messages) {
    if (rows.length !== 1) continue
    const message = rows[0], a = message.payload, pair = pairFor(startedSeq)
    if (!pair) continue
    const { start, finish, input, result } = pair
    const p = start.payload, contract = p.execution_contract
    // The HOST pair is already checked. Decoding an audit wrapper never grants citation authority.
    const args = result.reply_evidence_status === 'omitted_invalid' ? recordedArguments(p.arguments) : p.arguments
    if (p.name !== 'reply_to_customer' || result.status !== 'ok'
      || !record(contract) || Object.keys(contract).length !== 3 || contract.version !== 1
      || contract.kind !== 'atomic_local_events' || contract.receipt !== 'same_transaction'
      || !record(args) || !keys(args, ['text', 'evidence']) || !boundedText(args.text, 32000)
      || input.actor.id !== 'human' || input.actor.kind !== 'human' || input.actor.role !== 'human' || input.payload.kind !== 'message'
      || message.actor.kind !== 'agent' || message.actor.id !== p.role || !text(message.actor.role)
      || !(start.seq < message.seq && message.seq < finish.seq) || result.message_seq !== message.seq
      || result.reply_to !== input.seq || a.reply_to !== input.seq || a.run_id !== p.run_id
      || a.text !== args.text.trim() || a.phase !== 'team' || a.customer_reply_tool !== 'reply_to_customer'
      || a.audience !== 'customer' || a.recipient !== 'human' || a.response_kind !== 'reply') continue

    // Only the paired HOST receipt can omit evidence; raw refs and message claims are not evidence.
    if ('reply_evidence_status' in result || 'reply_evidence_warning' in result) {
      if (result.reply_evidence_status === 'omitted_invalid' && result.reply_evidence_warning === omittedWarning
        && !('reply_evidence' in result) && 'evidence' in args) replies.set(message.seq, { warning: omittedWarning })
      continue
    }
    if (!normalized(result.reply_evidence) || !Array.isArray(args.evidence)
      || args.evidence.length < 1 || args.evidence.length > 8) continue

    const refs: { source_seq: number; quote: string }[] = []
    let valid = true
    for (const ref of args.evidence) {
      if (!record(ref) || Object.keys(ref).length !== 2 || !keys(ref, ['source_seq', 'quote'])
        || !seq(ref.source_seq) || !boundedText(ref.quote, 2000)) { valid = false; break }
      const quote = ref.quote
      if (!refs.some(row => row.source_seq === ref.source_seq && row.quote === quote)) refs.push({ source_seq: ref.source_seq, quote })
    }
    const evidence = result.reply_evidence
    if (!valid || refs.length !== evidence.sources.length) continue
    // The HOST validates verbatim excerpts and metadata; the browser checks bindings, not hashes or execution claims.
    if (!evidence.sources.every((source, index) => {
      if (source.source_seq !== refs[index].source_seq || source.quote !== refs[index].quote || source.source_seq >= start.seq) return false
      const receipt = bySeq.get(source.source_seq)
      const origin = receipt && pairFor(receipt.payload.started_seq)
      return receipt?.type === 'team.tool_finished' && origin?.finish === receipt
        && tools.has(String(origin.start.payload.name)) && source.role === origin.start.payload.role
        && source.tool === origin.start.payload.name && hash(receipt.hash) && receipt.hash === source.source_hash
    })) continue
    replies.set(message.seq, { evidence })
  }
  return replies
}

export function replyEvidenceFor(events: readonly TeamEvent[]): ReadonlyMap<number, ReplyEvidence> {
  const replies = new Map<number, ReplyEvidence>()
  for (const [messageSeq, details] of replyEvidenceDetailsFor(events)) {
    if (details.evidence) replies.set(messageSeq, details.evidence)
  }
  return replies
}
