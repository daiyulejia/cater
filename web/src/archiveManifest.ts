export type ArchiveEntry = { path: string; bytes: number; sha256: string; executable: boolean }
const integer = (value: unknown, max: number): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= max
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)

export function portableArchivePart(value: string) {
  const normalized = value.normalize('NFKC')
  return Boolean(normalized) && normalized === normalized.trim() && !['.', '..'].includes(normalized) &&
    !/[\\/:*?"<>|\p{C}]/u.test(normalized) && !/[. ]$/.test(normalized) &&
    !/^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9]|LPT[1-9])(?: *\.|$)/i.test(normalized)
}

export function archiveEntries(value: unknown): ArchiveEntry[] | null {
  if (!record(value) || value.version !== 1 || Object.keys(value).sort().join(',') !== 'files,total_bytes,version' ||
    !Array.isArray(value.files) || !value.files.length || value.files.length > 256 || !integer(value.total_bytes, 64 * 1024 * 1024)) return null
  const entries: ArchiveEntry[] = []
  const paths: string[] = []
  const spellings = new Map<string, string>()
  let total = 0
  for (const entry of value.files) {
    if (!record(entry) || Object.keys(entry).sort().join(',') !== 'bytes,executable,path,sha256' ||
      typeof entry.path !== 'string' || new TextEncoder().encode(entry.path).length > 4096 ||
      !entry.path.split('/').every(portableArchivePart) || !integer(entry.bytes, 32 * 1024 * 1024) ||
      typeof entry.sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(entry.sha256) || typeof entry.executable !== 'boolean') return null
    const path = entry.path.normalize('NFKC').toLowerCase()
    if (paths.some(previous => previous === path || previous.startsWith(path + '/') || path.startsWith(previous + '/'))) return null
    const parts = entry.path.split('/')
    for (let index = 1; index <= parts.length; index++) {
      const spelling = parts.slice(0, index).join('/')
      const key = spelling.normalize('NFKC').toLowerCase()
      if (spellings.has(key) && spellings.get(key) !== spelling) return null
      spellings.set(key, spelling)
    }
    paths.push(path)
    total += entry.bytes
    entries.push({ path: entry.path, bytes: entry.bytes, sha256: entry.sha256, executable: entry.executable })
  }
  return total === value.total_bytes ? entries : null
}
