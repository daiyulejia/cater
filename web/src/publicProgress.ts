import { isTeamHost } from './team'
import type { TeamActivity, TeamEvent } from './team'

export type PublicProgressRun = {
  external_started_seq: number; run_started_seq: number; role: string; run_id: string; message_seq: number
  revision: number; text: string; truncated: boolean; age_ms: number
}
export type PublicProgressSnapshot = { activity_id: string; instance_id: string; runs: PublicProgressRun[] }
export type PublicReport = PublicProgressRun & { expiresAt: number }
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0

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
    reports.set(row.run_id, { ...row, expiresAt } as PublicReport)
  }
  return reports
}
