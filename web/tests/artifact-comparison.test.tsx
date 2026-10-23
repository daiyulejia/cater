import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { structuredPatch } from 'diff'
import type { Artifact } from '../src/Artifact'
import { ArtifactComparison } from '../src/ArtifactComparison'
import { TeamArtifacts } from '../src/TeamArtifacts'
import type { TeamEvent, TeamState } from '../src/team'
import licenseUrl from '../src/assets/jsdiff-LICENSE.txt?url&no-inline'

vi.mock('diff', { spy: true })

const file = (seq: number, extra: Partial<Artifact> = {}): Artifact => ({
  seq, filename: 'app.html', title: 'app.html', summary: `Published ${seq}`,
  content: `shared\nversion ${seq}\n`, format: 'code', actor: `publisher-${seq}`, source: 'team',
  // Recorded fingerprints are fixtures, not verified content hashes.
  content_sha256: (seq % 16).toString(16).repeat(64), ...extra,
})
const event = (seq: number, type: string, payload: Record<string, unknown> = {}): TeamEvent => ({
  seq, type, payload, actor: { id: 'team', role: 'team', kind: 'system' },
})
const state = (extra: Partial<TeamState> = {}): TeamState => ({
  enabled: true, objective: 'Compare published files', status: 'done', revision: 20,
  runs: [], pending_count: 0, assignments: [], checkpoint: '', ...extra,
})
type Props = Parameters<typeof TeamArtifacts>[0]
const props = (extra: Partial<Props> = {}): Props => ({
  taskId: 'task-a', accessToken: 'token-a', team: state(),
  artifacts: [file(2), file(5), file(25)],
  events: [event(1, 'team.started'), event(20, 'team.objective_completed', {
    artifacts: [{ seq: 5, filename: 'app.html', content_sha256: file(5).content_sha256 }],
  })],
  names: { 'publisher-2': '早期作者', 'publisher-5': '交付作者', 'publisher-25': '后来作者' },
  ...extra,
})
const settle = async () => { await act(async () => { await vi.dynamicImportSettled() }) }
const open = async () => {
  fireEvent.click(screen.getByRole('button', { name: '对照已发布版本' }))
  await settle()
  return screen.getByRole('dialog', { name: '版本对照' })
}
const selector = (dialog: HTMLElement, name: '文件' | '旧版' | '新版') =>
  within(dialog).getByRole<HTMLSelectElement>('combobox', { name, exact: true })
const choose = (dialog: HTMLElement, name: '文件' | '旧版' | '新版', value: string) =>
  fireEvent.change(selector(dialog, name), { target: { value } })
const originals = (dialog: HTMLElement) => {
  const summary = within(dialog).getByText('两版完整原文')
  if (!summary.closest('details')?.open) fireEvent.click(summary)
  return {
    older: within(dialog).getByLabelText('旧版完整原文'),
    newer: within(dialog).getByLabelText('新版完整原文'),
  }
}
const expectNoResult = (dialog: HTMLElement) => {
  expect(within(dialog).getByRole('status').textContent).toContain('请选择旧版与其后发布的新版')
  expect(within(dialog).queryByLabelText('逐行差异')).toBeNull()
  expect(within(dialog).queryByText('两版完整原文')).toBeNull()
  expect(within(dialog).queryByText(/两版正文完全相同/)).toBeNull()
  expect(within(dialog).queryByText(/字节 · 最近目标记录/)).toBeNull()
}
const fetch = vi.fn()
beforeEach(() => {
  fetch.mockReset().mockRejectedValue(new Error('Unexpected comparison network request'))
  vi.stubGlobal('fetch', fetch)
})
afterEach(() => { expect(fetch).not.toHaveBeenCalled() })

test.each([
  { label: 'one version', artifacts: [file(2)] },
  { label: 'different filenames', artifacts: [file(2), file(5, { filename: 'other.html' })] },
  { label: 'missing content', artifacts: [file(2), file(5, { content: undefined })] },
  { label: 'content error', artifacts: [file(2), file(5, { content_error: 'invalid_encoding' })] },
  { label: 'incorrect byte count', artifacts: [file(2), file(5, { content_bytes: 1 })] },
  { label: 'unsupported format', artifacts: [file(2), file(5, { format: 'image' })] },
  { label: 'duplicate sequence', artifacts: [file(2), file(5), file(5)] },
])('launcher requires two complete same-name versions: $label', async ({ artifacts }) => {
  render(<TeamArtifacts {...props({ artifacts })} />)
  await settle()
  expect(screen.queryByRole('button', { name: '对照已发布版本' })).toBeNull()
  expect(screen.queryByRole('dialog', { name: '版本对照' })).toBeNull()
})

test('opens frozen delivery #5 against same-target #2, with selected refs and publishers, not later #25', async () => {
  render(<TeamArtifacts {...props()} />)
  const dialog = await open()
  expect(selector(dialog, '文件').value).toBe('app.html')
  expect(selector(dialog, '旧版').value).toBe('2')
  expect(selector(dialog, '新版').value).toBe('5')
  expect(within(selector(dialog, '旧版')).getByRole('option', { selected: true }).textContent).toBe('#2 · 早期作者')
  expect(within(selector(dialog, '新版')).getByRole('option', { selected: true }).textContent).toBe('#5 · 交付作者')
  expect(within(selector(dialog, '新版')).getByRole('option', { name: '#25 · 后来作者' })).toBeTruthy()
  expect(within(dialog).getByText('旧版 #2 · 早期作者')).toBeTruthy()
  expect(within(dialog).getByText('新版 #5 · 交付作者')).toBeTruthy()
  expect(within(dialog).getByText('本次交付文件')).toBeTruthy()
  expect(within(dialog).getAllByText(/字节 · 最近目标记录 #1/)).toHaveLength(2)
  for (const summary of within(dialog).getAllByText('发布记录指纹')) fireEvent.click(summary)
  expect(within(dialog).getByText(file(2).content_sha256!)).toBeTruthy()
  expect(within(dialog).getByText(file(5).content_sha256!)).toBeTruthy()
  const patch = within(dialog).getByLabelText('逐行差异')
  expect(within(patch).getByLabelText('移除')).toBeTruthy()
  expect(within(patch).getByLabelText('新增')).toBeTruthy()
  expect(within(patch).getByLabelText('上下文')).toBeTruthy()
  expect(within(patch).getByLabelText('旧版第2行')).toBeTruthy()
  expect(within(patch).getByLabelText('新版第2行')).toBeTruthy()
  const raw = originals(dialog)
  expect(raw.older.textContent).toBe(file(2).content)
  expect(raw.newer.textContent).toBe(file(5).content)
  expect(dialog.textContent).not.toContain('version 25')
  expect(within(dialog).getByText('已发布文本的差异，不代表返修关系或验收结论。')).toBeTruthy()
  choose(dialog, '新版', '25')
  expect(within(dialog).getByText('新版 #25 · 后来作者')).toBeTruthy()
  expect(within(dialog).getByText('交付后发布，不在本次交付包内')).toBeTruthy()
})

test('raw HTML and remote image markup stay inert in both the diff and full originals', async () => {
  const script = '<script>window.__comparisonExecuted = true</script>'
  const image = '<img src="https://remote.invalid/pixel" onerror="window.__comparisonExecuted = true">'
  const markdown = '![remote](https://remote.invalid/markdown-pixel)'
  const older = `${script}\nold\n`, newer = `${script}\n${image}\n${markdown}\n`
  render(<TeamArtifacts {...props({ artifacts: [file(2, { content: older }), file(5, { content: newer })] })} />)
  const dialog = await open()
  const patch = within(dialog).getByLabelText('逐行差异')
  expect(within(patch).getByText(script, { exact: true }).tagName).toBe('CODE')
  expect(within(patch).getByText(image, { exact: true }).tagName).toBe('CODE')
  expect(within(patch).getByText(markdown, { exact: true }).tagName).toBe('CODE')
  const raw = originals(dialog)
  expect(raw.older.textContent).toBe(older)
  expect(raw.newer.textContent).toBe(newer)
  expect(dialog.querySelector('script, img, iframe, object, embed, [onerror], [src]')).toBeNull()
  expect(patch.querySelector('a')).toBeNull()
  expect(raw.older.querySelector('a')).toBeNull()
  expect(raw.newer.querySelector('a')).toBeNull()
  const links = within(dialog).getAllByRole('link')
  expect(links).toHaveLength(1)
  expect(links[0].textContent).toBe('差异组件许可')
  expect(links[0].getAttribute('href')).toBe(licenseUrl)
  expect(links[0].getAttribute('rel')).toBe('noopener noreferrer')
  expect(Reflect.get(window, '__comparisonExecuted')).toBeUndefined()
})

test('file and version selectors recompute results, restrict older choices, and clear an invalid pair', async () => {
  const initial = props({ team: state({ revision: 24 }), events: [
    event(1, 'team.started'), event(22, 'team.objective_updated'), event(24, 'team.objective_completed', {
      artifacts: [{ seq: 5, filename: 'app.html', content_sha256: file(5).content_sha256 }],
      artifact_selection: 'explicit', objective_seq: 22,
    }),
  ] })
  render(<TeamArtifacts {...initial} artifacts={[
    ...initial.artifacts, file(6, { filename: 'notes.txt', content: 'identical\n' }),
    file(8, { filename: 'notes.txt', content: 'identical\n' }),
  ]} />)
  const dialog = await open()
  choose(dialog, '新版', '25')
  choose(dialog, '旧版', '5')
  expect(selector(dialog, '旧版').value).toBe('5')
  expect(originals(dialog).newer.textContent).toBe(file(25).content)
  expect(within(dialog).getByRole('status').textContent).toContain('最近目标记录不同，仅作文本对照')
  choose(dialog, '文件', 'notes.txt')
  expect(selector(dialog, '旧版').value).toBe('6')
  expect(selector(dialog, '新版').value).toBe('8')
  expect(within(dialog).getByRole('status').textContent).toBe('两版正文完全相同；发布事件和人员仍可能不同。')
  expect(within(dialog).queryByLabelText('逐行差异')).toBeNull()
  expect(originals(dialog).older.textContent).toBe('identical\n')
  expect(originals(dialog).newer.textContent).toBe('identical\n')
  expect(within(dialog).getByText('旧版 #6 · publisher-6')).toBeTruthy()
  choose(dialog, '新版', '6')
  expect(selector(dialog, '旧版').value).toBe('0')
  expect(within(selector(dialog, '旧版')).getAllByRole('option').map(option => option.getAttribute('value'))).toEqual(['0'])
  expectNoResult(dialog)
  choose(dialog, '新版', '8')
  choose(dialog, '旧版', '6')
  expect(within(dialog).getByRole('status').textContent).toContain('两版正文完全相同')
  choose(dialog, '文件', 'app.html')
  expect(selector(dialog, '旧版').value).toBe('2')
  expect(selector(dialog, '新版').value).toBe('5')
  expect(originals(dialog).newer.textContent).toBe(file(5).content)
  expect(dialog.textContent).not.toContain('identical')
})

test.each([
  { label: 'CRLF versus LF', older: 'line\r\n', newer: 'line\n', oldEnding: 'CRLF 1 · 末尾有换行', newEnding: 'LF 1 · 末尾有换行', eof: false },
  { label: 'missing final newline', older: 'line', newer: 'line\n', oldEnding: '末尾无换行', newEnding: 'LF 1 · 末尾有换行', eof: true },
])('preserves raw text and displays line-ending notes: $label', async ({ older, newer, oldEnding, newEnding, eof }) => {
  render(<TeamArtifacts {...props({ artifacts: [file(2, { content: older }), file(5, { content: newer })] })} />)
  const dialog = await open()
  expect(within(dialog).getByText(oldEnding, { exact: true })).toBeTruthy()
  expect(within(dialog).getByText(newEnding, { exact: true })).toBeTruthy()
  const patch = within(dialog).getByLabelText('逐行差异')
  expect(within(patch).getByLabelText('移除')).toBeTruthy()
  expect(within(patch).getByLabelText('新增')).toBeTruthy()
  if (eof) expect(within(patch).getByText('该行之后没有末尾换行')).toBeTruthy()
  else expect(within(patch).getByText('⏎CR', { exact: true })).toBeTruthy()
  const raw = originals(dialog)
  expect(raw.older.textContent).toBe(older)
  expect(raw.newer.textContent).toBe(newer)
})

test('matching recorded fingerprints do not hide different bodies', async () => {
  render(<TeamArtifacts {...props({ artifacts: [file(2), file(5, { content_sha256: file(2).content_sha256 })] })} />)
  const dialog = await open()
  expect(within(dialog).getByRole('alert').textContent).toBe('发布指纹相同，但正文不同；版本记录需要核对。')
  expect(within(dialog).getByLabelText('逐行差异').textContent).toContain('version 5')
  expect(within(dialog).queryByText(/两版正文完全相同/)).toBeNull()
})

test.each(['close button', 'Escape'])('%s closes the dialog, restores trigger focus and retains the surrounding draft', async method => {
  render(<><textarea aria-label="草稿" /><TeamArtifacts {...props()} /></>)
  const draft = screen.getByRole<HTMLTextAreaElement>('textbox', { name: '草稿' })
  fireEvent.change(draft, { target: { value: '保留未发送的想法' } })
  const trigger = screen.getByRole('button', { name: '对照已发布版本' })
  trigger.focus()
  const dialog = await open()
  expect(dialog.contains(document.activeElement)).toBe(true)
  if (method === 'Escape') fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
  else fireEvent.click(within(dialog).getByRole('button', { name: '关闭版本对照' }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '版本对照' })).toBeNull())
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect(screen.getByRole('textbox', { name: '草稿' })).toBe(draft)
  expect(draft.value).toBe('保留未发送的想法')
})

test('a new publish updates options without moving the user-selected versions', async () => {
  const initial = props()
  const view = render(<TeamArtifacts {...initial} />)
  const dialog = await open()
  choose(dialog, '新版', '25')
  choose(dialog, '旧版', '5')
  view.rerender(<TeamArtifacts {...initial} artifacts={[...initial.artifacts, file(30)]} />)
  await settle()
  expect(screen.getByRole('dialog', { name: '版本对照' })).toBe(dialog)
  expect(selector(dialog, '文件').value).toBe('app.html')
  expect(selector(dialog, '旧版').value).toBe('5')
  expect(selector(dialog, '新版').value).toBe('25')
  expect(within(selector(dialog, '新版')).getByRole('option', { name: '#30 · publisher-30' })).toBeTruthy()
  const raw = originals(dialog)
  expect(raw.older.textContent).toBe(file(5).content)
  expect(raw.newer.textContent).toBe(file(25).content)
  expect(dialog.textContent).not.toContain('version 30')
})

test.each(['task', 'revision', 'accessToken'])('switching %s closes the open comparison', async scope => {
  const initial = props()
  const view = render(<TeamArtifacts {...initial} />)
  await open()
  const next = { ...initial, ...(scope === 'task' ? { taskId: 'task-b' }
    : scope === 'accessToken' ? { accessToken: 'token-b' }
      : { team: state({ status: 'running', revision: 22 }), events: [...initial.events, event(22, 'team.objective_updated')] }) }
  view.rerender(<TeamArtifacts {...next} />)
  await settle()
  expect(screen.queryByRole('dialog', { name: '版本对照' })).toBeNull()
  expect(screen.queryByLabelText('逐行差异')).toBeNull()
  expect(screen.getByRole('button', { name: '对照已发布版本' })).toBeTruthy()
})

test.each([
  { label: 'older removed', seq: 2, replacement: undefined },
  { label: 'newer removed', seq: 5, replacement: undefined },
  { label: 'older missing body', seq: 2, replacement: file(2, { content: undefined }) },
  { label: 'newer non-string body', seq: 5, replacement: file(5, { content: 42 as unknown as string }) },
  { label: 'newer body error', seq: 5, replacement: file(5, { content_error: 'invalid_content' }) },
  { label: 'older byte mismatch', seq: 2, replacement: file(2, { content_bytes: 1 }) },
])('invalid selected content clears the previous result without silently choosing another version: $label', async ({ seq, replacement }) => {
  const initial = props({ artifacts: [file(2), file(5), file(25), file(30)] })
  const view = render(<TeamArtifacts {...initial} />)
  const dialog = await open()
  expect(within(dialog).getByLabelText('逐行差异').textContent).toContain('version 2')
  originals(dialog)
  const artifacts = initial.artifacts.flatMap(item => item.seq !== seq ? [item] : replacement ? [replacement] : [])
  view.rerender(<TeamArtifacts {...initial} artifacts={artifacts} />)
  await settle()
  expect(screen.getByRole('dialog', { name: '版本对照' })).toBe(dialog)
  expect(selector(dialog, seq === 2 ? '旧版' : '新版').value).toBe('0')
  expectNoResult(dialog)
  expect(dialog.textContent).not.toContain('version 2')
  expect(dialog.textContent).not.toContain('version 5')
  choose(dialog, '新版', '30')
  choose(dialog, '旧版', '25')
  const raw = originals(dialog)
  expect(raw.older.textContent).toBe(file(25).content)
  expect(raw.newer.textContent).toBe(file(30).content)
})

test('removing the entire selected filename clears its result while another comparable file remains', async () => {
  const remaining = [file(6, { filename: 'other.txt' }), file(8, { filename: 'other.txt' })]
  const initial = props({ artifacts: [file(2), file(5), ...remaining] })
  const view = render(<TeamArtifacts {...initial} />)
  const dialog = await open()
  originals(dialog)
  view.rerender(<TeamArtifacts {...initial} artifacts={remaining} />)
  await settle()
  expect(selector(dialog, '文件').value).toBe('')
  expectNoResult(dialog)
  choose(dialog, '文件', 'other.txt')
  expect(selector(dialog, '旧版').value).toBe('6')
  expect(selector(dialog, '新版').value).toBe('8')
  expect(originals(dialog).newer.textContent).toBe(file(8).content)
})

test('ineligible frozen delivery is not replaced by an eligible later publication', async () => {
  render(<TeamArtifacts {...props({ artifacts: [file(2), file(5, { content_bytes: 1 }), file(25)] })} />)
  const dialog = await open()
  expect(selector(dialog, '新版').value).toBe('0')
  expectNoResult(dialog)
  choose(dialog, '新版', '25')
  choose(dialog, '旧版', '2')
  expect(originals(dialog).newer.textContent).toBe(file(25).content)
  expect(within(dialog).getByText('交付后发布，不在本次交付包内')).toBeTruthy()
})

test('new objective without files does not automatically compare a historical pair', async () => {
  const initial = props()
  render(<TeamArtifacts {...initial} team={state({ status: 'running', revision: 30 })}
    events={[...initial.events, event(30, 'team.objective_updated')]} />)
  const dialog = await open()
  expect(selector(dialog, '旧版').value).toBe('0')
  expect(selector(dialog, '新版').value).toBe('0')
  expectNoResult(dialog)
})

test('same selected bodies in new API objects do not repeat patch calculation', async () => {
  const initial = props()
  const view = render(<TeamArtifacts {...initial} />)
  const dialog = await open()
  expect(originals(dialog).newer.textContent).toBe(file(5).content)
  vi.mocked(structuredPatch).mockClear()
  view.rerender(<TeamArtifacts {...initial} artifacts={initial.artifacts.map(item => ({ ...item }))}
    events={initial.events.map(item => ({ ...item }))} />)
  await settle()
  expect(vi.mocked(structuredPatch)).not.toHaveBeenCalled()
  expect(originals(dialog).newer.textContent).toBe(file(5).content)
})

test('malformed display metadata and inexact candidate bytes do not crash the comparison', () => {
  const object = {} as unknown as string
  const artifacts = [file(2, { filename: object }), file(5, { actor: object, content_sha256: object }), file(25)]
  const view = render(<ArtifactComparison artifacts={artifacts} events={[event(1, 'team.started')]}
    preferred={[file(25)]} names={{ 'publisher-25': object }} />)
  expect(screen.getByText('旧版 #5 · 发布者未记录')).toBeTruthy()
  expect(screen.getByText('新版 #25 · publisher-25')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
  view.rerender(<ArtifactComparison artifacts={[file(2, { content: '中', content_bytes: 2 }), file(5, { content: '文', content_bytes: 2 })]}
    events={[]} preferred={[]} names={{}} />)
  expect(screen.getByRole('status').textContent).toContain('没有两份可对照的完整同名文本')
})
