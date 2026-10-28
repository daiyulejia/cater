import { isTeamHost } from './team'
import type { TeamEvent } from './team'

export type HandoffEvidence = {
  source_seq: number
  quote: string
  kind: 'artifact' | 'tool_receipt' | 'peer_review'
  source_hash: string
  filename?: string
  content_sha256?: string
  role?: string
  tool?: string
  status?: string
  outcome?: 'passed' | 'failed' | 'timeout' | 'launch_error' | 'effects_unknown' | 'not_started'
  exit_code?: number
  complete?: boolean
  truncated?: boolean
  output_utf8_valid?: Partial<Record<'stdout' | 'stderr', boolean | null>>
}

export type NativeHandoff = {
  version: 1
  why: string
  changed: string[]
  impact: string[]
  alternatives: string[]
  reproduce: string[]
  limits: string[]
  evidence: HandoffEvidence[]
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(text)
const PARTIAL_HANDOFF_WARNING = '仅保存部分内容；其他内容和引用未保存，请结合记录核对。'
const OMITTED_HANDOFF_WARNING = '可选交付说明未通过校验，未保存；主要交付结论与成果已保留。'
const OMITTED_REFERENCES_WARNING = '讲解已保留，引用核对失败未保存，不视为验证证据。'
const PARTIAL_FIELDS = ['alternatives', 'changed', 'evidence', 'impact', 'limits', 'reproduce', 'version', 'why']

function boundedPartialText(value: unknown, limit: number): value is string {
  if (typeof value !== 'string') return false
  const points = Array.from(value)
  const pythonSpace = (code: number) => (code >= 9 && code <= 13) || (code >= 28 && code <= 32)
    || code === 0x85 || code === 0xa0 || code === 0x1680 || (code >= 0x2000 && code <= 0x200a)
    || code === 0x2028 || code === 0x2029 || code === 0x202f || code === 0x205f || code === 0x3000
  if (!points.length || points.length > limit || pythonSpace(points[0].codePointAt(0)!)
    || pythonSpace(points[points.length - 1].codePointAt(0)!)) return false
  return points.every(point => {
    const code = point.codePointAt(0)!
    return code < 0xd800 || code > 0xdfff
  })
}

function partialJsonBytes(value: Record<string, unknown>): number {
  const body = JSON.stringify(value)
  let quoted = false, escaped = false, separators = 0
  for (const char of body) {
    if (quoted) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') quoted = false
    } else if (char === '"') quoted = true
    else if (char === ':' || char === ',') separators++
  }
  // Python's persisted JSON includes one space after each separator.
  return new TextEncoder().encode(body).length + separators
}

export function isOutputUtf8Validity(value: unknown): value is NonNullable<HandoffEvidence['output_utf8_valid']> {
  return record(value) && Object.entries(value).every(([key, state]) =>
    ['stdout', 'stderr'].includes(key) && (state === null || typeof state === 'boolean'))
}

export function partialHandoff(value: unknown, warning: unknown): NativeHandoff | undefined {
  if (warning !== PARTIAL_HANDOFF_WARNING || !record(value) || value.version !== 1
    || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(PARTIAL_FIELDS)
    || !boundedPartialText(value.why, 4000)
    || !['changed', 'impact', 'alternatives', 'reproduce', 'limits'].every((key) =>
      Array.isArray(value[key]) && value[key].length <= 12 && value[key].every((item: unknown) => boundedPartialText(item, 1000)))
    || !Array.isArray(value.evidence) || value.evidence.length !== 0 || partialJsonBytes(value) > 24000) return undefined
  return value as NativeHandoff
}

export function knownHistoryHandoffWarning(status: unknown, warning: unknown): boolean {
  return status === 'omitted_invalid' && (warning === PARTIAL_HANDOFF_WARNING || warning === OMITTED_HANDOFF_WARNING)
    || status === 'references_omitted_invalid' && warning === OMITTED_REFERENCES_WARNING
}

// Mirror the host's saved-status projection; fields omitted by the host are never evidence.
function savedStatus(payload: Record<string, unknown>): { handoff?: NativeHandoff; warning: string } | undefined {
  const status = payload.handoff_status, warning = payload.handoff_warning
  if (status === 'omitted_invalid' && !Object.hasOwn(payload, 'handoff')) {
    if (warning === OMITTED_HANDOFF_WARNING && !Object.hasOwn(payload, 'handoff_partial')) return { warning }
    const handoff = partialHandoff(payload.handoff_partial, warning)
    if (handoff) return { handoff, warning: PARTIAL_HANDOFF_WARNING }
  }
  if (status === 'references_omitted_invalid' && warning === OMITTED_REFERENCES_WARNING
    && !Object.hasOwn(payload, 'handoff_partial') && record(payload.handoff)) {
    const value = payload.handoff
    const prose = { version: value.version, why: value.why, changed: value.changed, impact: value.impact,
      alternatives: value.alternatives, reproduce: value.reproduce, limits: value.limits, evidence: [] }
    const handoff = partialHandoff(prose, PARTIAL_HANDOFF_WARNING)
    if (handoff) return { handoff, warning }
  }
  return undefined
}

// The server owns source validation. Only read its saved completion, never a speaker's self-report.
export function nativeHandoff(event: TeamEvent): NativeHandoff | undefined {
  if (event.type !== 'team.objective_completed' || !isTeamHost(event)) return undefined
  if (['handoff_status', 'handoff_warning', 'handoff_partial'].some(key => Object.hasOwn(event.payload, key)))
    return savedStatus(event.payload)?.handoff
  const value = event.payload.handoff
  if (!record(value) || value.version !== 1 || !text(value.why)
    || !['changed', 'impact', 'alternatives', 'reproduce', 'limits'].every((key) => strings(value[key]))
    || !Array.isArray(value.evidence)) return undefined
  for (const evidence of value.evidence) {
    if (!record(evidence) || !Number.isSafeInteger(evidence.source_seq) || Number(evidence.source_seq) <= 0
      || Number(evidence.source_seq) >= event.seq || !text(evidence.quote) || !text(evidence.source_hash)
      || typeof evidence.kind !== 'string' || !['artifact', 'tool_receipt', 'peer_review'].includes(evidence.kind)
      || ['filename', 'content_sha256', 'role', 'tool', 'status'].some((key) => key in evidence && !text(evidence[key]))
      || ('exit_code' in evidence && !Number.isSafeInteger(evidence.exit_code))
      || ('complete' in evidence && typeof evidence.complete !== 'boolean')) return undefined
    if ('truncated' in evidence && typeof evidence.truncated !== 'boolean') return undefined
    if ('output_utf8_valid' in evidence && !isOutputUtf8Validity(evidence.output_utf8_valid)) return undefined
  }
  return value as NativeHandoff
}

export function nativeHandoffWarning(event: TeamEvent): string | undefined {
  if (event.type !== 'team.objective_completed' || !isTeamHost(event)) return undefined
  return savedStatus(event.payload)?.warning
}
