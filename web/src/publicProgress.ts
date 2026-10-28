import { isTeamHost, teamSpeaker } from './team'
import type { TeamActivity, TeamEvent } from './team'

type PublicTextSpan = { version: 1; start: number; end: number }
export type PublicProgressRun = {
  external_started_seq: number; run_started_seq: number; role: string; run_id: string; message_seq: number
  revision: number; text: string; truncated: boolean; age_ms: number; text_span?: PublicTextSpan
}
export type PublicProgressSnapshot = { activity_id: string; instance_id: string; runs: PublicProgressRun[] }
export type PublicReport = PublicProgressRun & { expiresAt: number; displaySegments: string[] }
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const textSpan = (value: unknown, length: number): value is PublicTextSpan => object(value) && value.version === 1
  && typeof value.start === 'number' && Number.isSafeInteger(value.start) && value.start >= 0
  && typeof value.end === 'number' && Number.isSafeInteger(value.end) && value.end > value.start
  && value.end - value.start === length
// In Unicode mode this rejects lone surrogates, while accepting valid astral scalars.
const scalarText = (text: string) => !/[\uD800-\uDFFF]/u.test(text)

// This is a display projection only: keep the accepted raw report and its original clock.
function displaySegments(row: PublicProgressRun, events: TeamEvent[]): string[] {
  const chars = Array.from(row.text), window = row.text_span
  if (!scalarText(row.text) || !textSpan(window, chars.length)) return [row.text]
  const covered: [number, number][] = []
  for (const event of events) {
    const p = event.payload, span = p.public_text_span
    // Host-persisted progress is authored by the member. actor.role is their
    // profession, not their id; bind actor.id to the already authenticated run.
    if (event.actor.kind !== 'agent' || event.actor.id !== row.role
      || typeof event.actor.role !== 'string' || !event.actor.role.trim()
      || event.type !== 'msg.said' || !positive(event.seq) || event.seq <= row.external_started_seq
      || p.phase !== 'team' || p.response_kind !== 'progress' || p.audience !== 'team'
      || p.run_id !== row.run_id || teamSpeaker(event) !== row.role || p.reply_to !== row.message_seq
      || p.revision !== row.revision || typeof p.text !== 'string' || !scalarText(p.text)
      || !object(span) || span.external_started_seq !== row.external_started_seq) continue
    const saved = Array.from(p.text)
    if (!textSpan(span, saved.length)) continue
    const start = Math.max(window.start, span.start), end = Math.min(window.end, span.end)
    if (start >= end || chars.slice(start - window.start, end - window.start).join('')
      !== saved.slice(start - span.start, end - span.start).join('')) continue
    covered.push([start, end])
  }
  let cursor = window.start
  const remaining: string[] = []
  for (const [start, end] of covered.sort((a, b) => a[0] - b[0])) {
    if (start > cursor) remaining.push(chars.slice(cursor - window.start, start - window.start).join(''))
    cursor = Math.max(cursor, end)
  }
  if (cursor < window.end) remaining.push(chars.slice(cursor - window.start).join(''))
  return remaining
}

export function publicReports(snapshot: unknown, receivedAt: number | undefined, activity: TeamActivity | undefined,
  events: TeamEvent[], revision: number, now: number): ReadonlyMap<string, PublicReport> {
  const reports = new Map<string, PublicReport>()
  if (!object(snapshot) || !activity?.active || activity.state !== 'running'
    || !activity.id || !activity.instance_id || snapshot.activity_id !== activity.id || snapshot.instance_id !== activity.instance_id
    || !Array.isArray(snapshot.runs) || snapshot.runs.length > 16 || receivedAt === undefined
    || !Number.isFinite(receivedAt) || receivedAt > now) return reports
  const hosts = events.filter(isTeamHost), bySeq = new Map(hosts.map(event => [event.seq, event]))
  for (const row of snapshot.runs) {
    if (!object(row) || !positive(row.external_started_seq) || !positive(row.run_started_seq) || !positive(row.message_seq)
      || typeof row.run_id !== 'string' || !row.run_id || typeof row.role !== 'string' || !row.role
      || row.revision !== revision || typeof row.text !== 'string' || !row.text.trim()
      || Array.from(row.text).length > 4000 || typeof row.truncated !== 'boolean'
      || typeof row.age_ms !== 'number' || !Number.isSafeInteger(row.age_ms) || row.age_ms < 0) continue
    const expiresAt = receivedAt + 60_000 - row.age_ms
    if (now >= expiresAt) continue
    const start = bySeq.get(row.run_started_seq), attempt = bySeq.get(row.external_started_seq)
    if (start?.type !== 'team.run_started' || attempt?.type !== 'team.external_run_started'
      || start.seq >= attempt.seq || attempt.payload.run_started_seq !== start.seq
      || [start, attempt].some(event => event.payload.run_id !== row.run_id
        || event.payload.role !== row.role || event.payload.message_seq !== row.message_seq)
      || hosts.some(event => event.seq > attempt.seq && (event.type === 'team.run_finished' && event.payload.run_id === row.run_id
        || event.type === 'team.external_run_finished' && event.payload.started_seq === attempt.seq))) continue
    const accepted = row as PublicProgressRun
    reports.set(row.run_id, { ...accepted, expiresAt, displaySegments: displaySegments(accepted, events) })
  }
  return reports
}
