import { useMemo, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { structuredPatch } from 'diff'
import type { StructuredPatch } from 'diff'
import type { Artifact } from './Artifact'
import { comparisonDefaults, comparisonGroups } from './artifactComparisonData'
import type { ComparisonGroup, ComparisonVersion } from './artifactComparisonData'
import type { TeamEvent } from './team'
import licenseUrl from './assets/jsdiff-LICENSE.txt?url&no-inline'
import './artifact-comparison.css'

export function textComparison(older: string, newer: string): { kind: 'equal' | 'limited' } | { kind: 'diff'; patch: StructuredPatch } {
  if (older === newer) return { kind: 'equal' }
  if (new TextEncoder().encode(older).length > 65536 || new TextEncoder().encode(newer).length > 65536
    || older.split('\n').length + newer.split('\n').length > 4000) return { kind: 'limited' }
  const patch = structuredPatch('older', 'newer', older, newer, undefined, undefined,
    { context: 3, ignoreWhitespace: false, stripTrailingCr: false, timeout: 80, maxEditLength: 1500 })
  return patch ? { kind: 'diff', patch } : { kind: 'limited' }
}

function ending(text: string) {
  const crlf = (text.match(/\r\n/g) ?? []).length
  const lf = (text.match(/\n/g) ?? []).length - crlf
  const cr = (text.match(/\r/g) ?? []).length - crlf
  return `${crlf ? `CRLF ${crlf} · ` : ''}${lf ? `LF ${lf} · ` : ''}${cr ? `CR ${cr} · ` : ''}${text.endsWith('\n') || text.endsWith('\r') ? '末尾有换行' : '末尾无换行'}`
}

function VersionInfo({ version, name, label, category }: { version: ComparisonVersion; name: string; label: string; category?: string }) {
  return <div className="comparison-version">
    <p className="font-semibold">{label} #{version.item.seq} · {name}</p>
    {category && <p className="font-semibold text-[#52766d]">{category}</p>}
    <p>{new TextEncoder().encode(version.item.content).length} 字节 · 最近目标记录 {version.objective ? `#${version.objective}` : '未确认'}</p>
    <p>{ending(version.item.content)}</p>
    <details><summary>发布记录指纹</summary><code>{version.item.content_sha256 || '未记录'}</code></details>
  </div>
}

function Patch({ patch }: { patch: StructuredPatch }) {
  const range = (label: string, start: number, count: number) => count ? `${label} ${start}${count > 1 ? `–${start + count - 1}` : ''} 行` : `${label} 无对应行`
  return <div aria-label="逐行差异">{patch.hunks.map((hunk, index) => {
    let oldLine = hunk.oldStart, newLine = hunk.newStart
    return <section className="comparison-hunk" key={index}>
      <h3>{range('旧版', hunk.oldStart, hunk.oldLines)} → {range('新版', hunk.newStart, hunk.newLines)}</h3>
      {hunk.lines.map((line, row) => {
        const marker = line[0]
        const oldNumber = marker === '-' || marker === ' ' ? oldLine++ : ''
        const newNumber = marker === '+' || marker === ' ' ? newLine++ : ''
        if (marker === '\\') return <p className="comparison-eof" key={row}>该行之后没有末尾换行</p>
        return <div className={`comparison-line ${marker === '+' ? 'comparison-add' : marker === '-' ? 'comparison-remove' : ''}`} key={row}>
          <span className="comparison-number" aria-label={oldNumber ? `旧版第${oldNumber}行` : undefined}>{oldNumber}</span>
          <span className="comparison-number" aria-label={newNumber ? `新版第${newNumber}行` : undefined}>{newNumber}</span>
          <span aria-label={marker === '+' ? '新增' : marker === '-' ? '移除' : '上下文'}>{marker}</span>
          <code>{line.slice(1).replace(/\r$/u, '')}{line.endsWith('\r') && <span className="comparison-cr"> ⏎CR</span>}</code>
        </div>
      })}
    </section>
  })}</div>
}

export function ArtifactComparison({ artifacts, events, ...props }: { artifacts: Artifact[]; events: TeamEvent[]; preferred: Artifact[]; names: Record<string, string>; categories?: Record<number, string> }) {
  const groups = useMemo(() => comparisonGroups(artifacts, events), [artifacts, events])
  return groups.length ? <ComparisonBody groups={groups} {...props} />
    : <p role="status" className="my-5 text-sm text-[#687e73]">没有两份可对照的完整同名文本，请核对正文与发布记录。</p>
}

function ComparisonBody({ groups, preferred, names, categories = {} }: { groups: ComparisonGroup[]; preferred: Artifact[]; names: Record<string, string>; categories?: Record<number, string> }) {
  const initial = comparisonDefaults(groups, preferred)!
  const [selection, setSelection] = useState(initial)
  const group = groups.find(g => g.filename === selection.filename)
  const older = group?.versions.find(v => v.item.seq === selection.older)
  const newer = group?.versions.find(v => v.item.seq === selection.newer)
  const valid = older && newer && older.item.seq < newer.item.seq
  const oldBody = valid ? older.item.content : undefined, newBody = valid ? newer.item.content : undefined
  const result = useMemo(() => oldBody !== undefined && newBody !== undefined ? textComparison(oldBody, newBody) : undefined, [oldBody, newBody])
  const name = (version: ComparisonVersion) => typeof names[version.item.actor ?? ''] === 'string'
    ? names[version.item.actor ?? ''] : version.item.actor ?? '发布者未记录'
  return <>
    <label className="comparison-select mt-4">文件<select value={group ? selection.filename : ''} onChange={e => {
      const chosen = groups.find(g => g.filename === e.target.value)
      const defaults = comparisonDefaults(chosen ? [chosen] : [], preferred, true)
      if (defaults) setSelection(defaults)
    }}><option value="">选择文件</option>{groups.map(g => <option key={g.filename}>{g.filename}</option>)}</select></label>
    <div className="comparison-selectors">
      <label className="comparison-select">旧版<select value={older?.item.seq ?? 0} onChange={e => setSelection(s => ({ ...s, older: Number(e.target.value) }))}>
        <option value={0}>选择比较基线</option>
        {group?.versions.filter(v => !newer || v.item.seq < newer.item.seq).map(v => <option key={v.item.seq} value={v.item.seq}>#{v.item.seq} · {name(v)}</option>)}
      </select></label>
      <ArrowRight className="comparison-arrow" size={16} aria-hidden="true" />
      <label className="comparison-select">新版<select value={newer?.item.seq ?? 0} onChange={e => {
        const next = Number(e.target.value)
        setSelection(s => ({ ...s, newer: next, older: s.older < next ? s.older : 0 }))
      }}><option value={0}>选择查看版本</option>{group?.versions.map(v => <option key={v.item.seq} value={v.item.seq}>#{v.item.seq} · {name(v)}</option>)}</select></label>
    </div>
    {!valid ? <p role="status" className="my-5 text-sm text-[#687e73]">请选择旧版与其后发布的新版。不同目标的版本不会自动配对。</p> : <>
      <div className="comparison-versions"><VersionInfo version={older} name={name(older)} label="旧版" category={categories[older.item.seq]} /><VersionInfo version={newer} name={name(newer)} label="新版" category={categories[newer.item.seq]} /></div>
      {older.objective !== newer.objective && <p role="status" className="comparison-notice">这两版的最近目标记录不同，仅作文本对照，不表示沿用或返修。</p>}
      {(!older.objective || !newer.objective) && <p role="status" className="comparison-notice">目标归属未确认，仅作文本对照。</p>}
      {older.item.content !== newer.item.content && older.item.content_sha256 && older.item.content_sha256 === newer.item.content_sha256
        && <p role="alert" className="comparison-notice">发布指纹相同，但正文不同；版本记录需要核对。</p>}
      {result?.kind === 'equal' ? <p role="status" className="comparison-notice">两版正文完全相同；发布事件和人员仍可能不同。</p>
        : result?.kind === 'diff' ? <Patch patch={result.patch} />
          : <p role="status" className="comparison-notice">差异较大，未生成完整逐行对照。两版完整原文保留在下方。</p>}
      <details className="comparison-originals"><summary>两版完整原文</summary>
        <h3>旧版 #{older.item.seq}</h3><pre aria-label="旧版完整原文"><code>{older.item.content}</code></pre>
        <h3>新版 #{newer.item.seq}</h3><pre aria-label="新版完整原文"><code>{newer.item.content}</code></pre>
      </details>
      <p className="mt-6 text-[11px] text-[#687e73]"><a href={licenseUrl} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">差异组件许可</a></p>
    </>}
  </>
}
