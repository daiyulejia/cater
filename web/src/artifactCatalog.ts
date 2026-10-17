import type { Artifact } from './Artifact'
import { isTeamHost } from './team'
import type { TeamEvent, TeamState } from './team'

export type ArtifactCatalog = {
  kind: 'delivery' | 'current' | 'published' | 'unresolved'
  primary: Artifact[]
  history: Artifact[]
  later: Artifact[]
  deliverySeq?: number
  issue?: string
}

const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)

function latest(items: Artifact[]): Artifact[] {
  const files = new Map<string, Artifact>()
  for (const item of items) files.set(item.filename ? `file:${item.filename}` : `event:${item.seq}`, item)
  return [...files.values()].sort((a, b) => b.seq - a.seq)
}

// This organizes saved records; byte verification remains the server download's responsibility.
export function teamArtifactCatalog(team: TeamState, artifacts: Artifact[], events: TeamEvent[]): ArtifactCatalog {
  const all = [...artifacts].sort((a, b) => a.seq - b.seq)
  const trusted = events.filter(event => positive(event.seq) && isTeamHost(event)).sort((a, b) => a.seq - b.seq)
  const transition = trusted.filter(event => ['team.objective_updated', 'team.objective_completed'].includes(event.type)).at(-1)
  const boundary = trusted.filter(event => ['team.started', 'team.objective_updated'].includes(event.type)).at(-1)
  const fallback = (issue?: string): ArtifactCatalog => {
    const primary = latest(all), chosen = new Set(primary)
    return { kind: issue ? 'unresolved' : 'published', primary, history: all.filter(item => !chosen.has(item)).reverse(), later: [], issue }
  }
  if (team.status === 'done') {
    if (!transition || transition.type !== 'team.objective_completed' || transition.seq !== team.revision)
      return fallback('交付状态与文件记录尚未同步，以下仅为已发布版本。')
    const refs = transition.payload.artifacts
    if (!Array.isArray(refs)) return fallback('这次交付没有可核对的文件清单，以下仅为已发布版本。')
    const primary: Artifact[] = [], names = new Set<string>(), seqs = new Set<number>()
    for (const ref of refs) {
      if (!record(ref) || !positive(ref.seq) || ref.seq >= transition.seq || typeof ref.filename !== 'string' || !ref.filename.trim()
        || typeof ref.content_sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(ref.content_sha256))
        return fallback('交付文件清单不完整，未用最新发布替代交付版本。')
      const name = ref.filename.normalize('NFC').toLowerCase()
      const matches = all.filter(item => item.seq === ref.seq)
      if (names.has(name) || seqs.has(ref.seq) || matches.length !== 1)
        return fallback('交付文件引用重复或无法对应，未用最新发布替代交付版本。')
      const item = matches[0]
      const archive = item.format === 'archive' && record(item.archive) && item.archive.version === 1
        && Array.isArray(item.archive.files) && item.archive.files.length > 0
        && typeof item.content_bytes === 'number' && Number.isSafeInteger(item.content_bytes) && item.content_bytes > 0
      if (item.filename !== ref.filename || item.content_sha256 !== ref.content_sha256 || (typeof item.content !== 'string' && !archive) || item.content_error)
        return fallback('交付文件版本无法核对，未用最新发布替代交付版本。')
      if (boundary && item.seq <= boundary.seq && !(transition.payload.artifact_selection === 'explicit' && transition.payload.objective_seq === boundary.seq))
        return fallback('清单包含旧目标文件，但没有记录明确沿用；当前交付范围待核对。')
      names.add(name); seqs.add(ref.seq); primary.push(item)
    }
    const chosen = new Set(primary)
    return { kind: 'delivery', deliverySeq: transition.seq, primary,
      history: all.filter(item => !chosen.has(item) && item.seq < transition.seq).reverse(),
      later: all.filter(item => !chosen.has(item) && item.seq >= transition.seq).reverse() }
  }
  const scoped = transition?.type === 'team.objective_updated' && transition.seq === team.revision
    || team.revision === 0 && !transition && boundary?.type === 'team.started'
  if (!scoped || !boundary) return fallback()
  const primary = latest(all.filter(item => item.seq > boundary.seq)), chosen = new Set(primary)
  return { kind: 'current', primary, history: all.filter(item => !chosen.has(item)).reverse(), later: [] }
}
