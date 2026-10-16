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
  exit_code?: number
  complete?: boolean
  truncated?: boolean
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

// The server owns source validation. Only read its saved completion, never a speaker's self-report.
export function nativeHandoff(event: TeamEvent): NativeHandoff | undefined {
  if (event.type !== 'team.objective_completed' || !isTeamHost(event)) return undefined
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
  }
  return value as NativeHandoff
}
