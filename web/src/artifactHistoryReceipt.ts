import { isTeamHost } from './team'
import type { TeamEvent } from './team'

type RecordValue = Record<string, unknown>
export type ArtifactHistoryReceipt = {
  artifactSeq: number
  filename: string
  versions: { seq: number }[]
  total: number
  afterSeq: number
  nextSeq: number
  hasMore: boolean
}

const record = (value: unknown): RecordValue | undefined => value !== null && typeof value === 'object'
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
  ? value as RecordValue : undefined
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const positive = (value: unknown): value is number => count(value) && value > 0
const text = (value: unknown, max: number): value is string => typeof value === 'string' && Boolean(value.trim())
  && value.length <= max && new TextEncoder().encode(value).length <= max
const only = (value: RecordValue, keys: string[]) => Object.keys(value).every(key => keys.includes(key))
const rowKeys = ['seq', 'filename', 'actor', 'source', 'format', 'content_bytes', 'content_sha256', 'content_error']
const resultKeys = ['status', 'section', 'artifact_seq', 'filename', 'scope', 'versions', 'versions_total', 'has_more', 'next_seq']

export function isArtifactHistoryReceipt(event: TeamEvent): boolean {
  const args = event.payload.arguments as RecordValue | null | undefined
  const result = event.payload.result as RecordValue | null | undefined
  return args?.section !== undefined && args.section !== 'content'
    || result?.section !== undefined && result.section !== 'content'
    || ['after_seq', 'limit'].some(key => args && Object.hasOwn(args, key))
    || ['versions', 'versions_total'].some(key => result && Object.hasOwn(result, key))
}

export function artifactHistoryReceipt(event: TeamEvent): ArtifactHistoryReceipt | undefined {
  const actor = record(event.actor), payload = record(event.payload)
  if (!actor || !['id', 'kind', 'role'].every(key => Object.hasOwn(actor, key)) || !isTeamHost(event)
    || !positive(event.seq) || event.type !== 'team.tool_finished' || !payload
    || (payload.name ?? payload.tool) !== 'read_artifact'
    || ![undefined, 'ok', 'completed', 'succeeded'].includes(payload.status as string | undefined)) return
  const args = record(payload.arguments), result = record(payload.result)
  if (!args || !result || !only(args, ['artifact_seq', 'section', 'after_seq', 'limit'])
    || !only(result, [...resultKeys, 'complete', 'truncated'])
    || !resultKeys.every(key => Object.hasOwn(result, key))
    || args.section !== 'versions' || result.section !== 'versions' || result.status !== 'ok'
    || Object.hasOwn(result, 'complete') && result.complete !== true
    || Object.hasOwn(result, 'truncated') && result.truncated !== false
    || !positive(args.artifact_seq) || args.artifact_seq >= event.seq || result.artifact_seq !== args.artifact_seq
    || !text(result.filename, 4096) || result.scope !== 'same_filename_in_this_chat'
    || !positive(result.versions_total) || result.versions_total >= event.seq
    || !count(result.next_seq) || typeof result.has_more !== 'boolean' || !Array.isArray(result.versions)) return
  const after = Object.hasOwn(args, 'after_seq') ? args.after_seq : 0
  const limit = Object.hasOwn(args, 'limit') ? args.limit : 20
  if (!count(after) || !positive(limit) || limit > 50 || result.versions.length > limit) return
  const versions: { seq: number }[] = []
  let previous = after
  for (const value of result.versions) {
    const row = record(value)
    if (!row || !only(row, rowKeys) || !positive(row.seq) || row.seq <= previous || row.seq >= event.seq
      || row.filename !== result.filename
      || ['actor', 'source', 'content_error'].some(key => Object.hasOwn(row, key) && !text(row[key], 256))
      || Object.hasOwn(row, 'format') && !text(row.format, 64)
      || Object.hasOwn(row, 'content_bytes') && !count(row.content_bytes)
      || Object.hasOwn(row, 'content_sha256') && (typeof row.content_sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(row.content_sha256))) return
    versions.push({ seq: row.seq })
    previous = row.seq
  }
  const size = versions.length, total = result.versions_total, more = result.has_more
  const anchor = args.artifact_seq, containsAnchor = versions.some(row => row.seq === anchor)
  // A cursor is an event sequence, not an offset into this filename's history.
  const minimumBefore = anchor <= after ? 1 : 0
  if (result.next_seq !== previous || total < size + minimumBefore + (more ? 1 : 0)
    || more && size !== limit || !more && total - size > after
    || anchor > after && (!more || anchor <= previous) && !containsAnchor) return
  return { artifactSeq: anchor, filename: result.filename, versions, total, afterSeq: after,
    nextSeq: result.next_seq, hasMore: more }
}
