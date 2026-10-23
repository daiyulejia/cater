import type { Artifact } from './Artifact'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'

export type ComparisonVersion = { item: Artifact & { filename: string; content: string }; objective: number }
export type ComparisonGroup = { filename: string; versions: ComparisonVersion[] }

export function comparisonCandidate(item: Artifact): item is ComparisonVersion['item'] {
  return Number.isSafeInteger(item.seq) && item.seq > 0 && typeof item.filename === 'string' && Boolean(item.filename.trim())
    && typeof item.content === 'string' && !item.content_error && [undefined, 'text', 'code', 'markdown'].includes(item.format)
    && item.content.length <= 65536 && (item.content_bytes === undefined || Number.isSafeInteger(item.content_bytes)
      && item.content_bytes >= item.content.length && item.content_bytes <= Math.min(65536, item.content.length * 3))
}

// The closed entry checks metadata only; complete byte validation happens on opening.
export function hasComparisonCandidates(items: Artifact[]): boolean {
  const counts = new Map<number, number>(), names = new Set<string>()
  for (const item of items) counts.set(item.seq, (counts.get(item.seq) ?? 0) + 1)
  for (const item of items) {
    if (!comparisonCandidate(item) || counts.get(item.seq) !== 1) continue
    if (names.has(item.filename!)) return true
    names.add(item.filename!)
  }
  return false
}

export function comparisonGroups(items: Artifact[], events: TeamEvent[]): ComparisonGroup[] {
  const counts = new Map<number, number>()
  for (const item of items) counts.set(item.seq, (counts.get(item.seq) ?? 0) + 1)
  const boundaries = events.filter(e => isTeamHost(e) && Number.isSafeInteger(e.seq) && e.seq > 0 && ['team.started', 'team.objective_updated'].includes(e.type))
    .map(e => e.seq).sort((a, b) => a - b)
  const groups = new Map<string, ComparisonVersion[]>()
  for (const item of [...items].sort((a, b) => a.seq - b.seq)) {
    if (!comparisonCandidate(item) || counts.get(item.seq) !== 1) continue
    const bytes = new TextEncoder().encode(item.content).length
    if (bytes > 65536 || item.content_bytes !== undefined && item.content_bytes !== bytes) continue
    const versions = groups.get(item.filename) ?? []
    versions.push({ item: { ...item, actor: typeof item.actor === 'string' ? item.actor : undefined,
      content_sha256: typeof item.content_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(item.content_sha256) ? item.content_sha256 : undefined } as ComparisonVersion['item'],
      objective: boundaries.filter(seq => seq < item.seq).at(-1) ?? 0 })
    groups.set(item.filename, versions)
  }
  return [...groups].filter(([, versions]) => versions.length >= 2).map(([filename, versions]) => ({ filename, versions }))
}

export function comparisonDefaults(groups: ComparisonGroup[], preferred: Artifact[], explicitFile = false) {
  const target = preferred.find(item => groups.some(group => group.filename === item.filename))
  const group = groups.find(group => group.filename === target?.filename) ?? groups[0]
  if (!group) return undefined
  const newer = group.versions.find(v => v.item.seq === target?.seq) ?? (!target && explicitFile ? group.versions.at(-1) : undefined)
  const older = newer && group.versions.filter(v => v.item.seq < newer.item.seq && v.objective > 0 && v.objective === newer.objective).at(-1)
  return { filename: group.filename, older: older?.item.seq ?? 0, newer: newer?.item.seq ?? 0 }
}
