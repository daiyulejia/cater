import { render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import type { TeamEvent } from '../src/team'

function events(name: string, result: Record<string, unknown>): TeamEvent[] {
  const actor = { id: 'team', kind: 'system', role: 'team' }
  return [
    { seq: 10, type: 'team.tool_started', actor, payload: { role: 'dev', run_id: 'fixture',
      name, call_id: 'publish', arguments: { path: 'missing.html' } } },
    { seq: 11, type: 'team.tool_finished', actor, payload: { started_seq: 10, role: 'dev', run_id: 'fixture', name, result } },
  ]
}

test.each(['publish_artifact', 'publish_workspace_file'])('发布前已知拒绝用中文区分未开始：%s', name => {
  render(<TeamToolProgress events={events(name, { status: 'error', complete: true, outcome: 'not_started',
    code: 'source_file_unavailable', message: '没有读到完整源文件，未开始发布。' })} names={{ dev: '米寇' }} />)
  const row = within(screen.getByRole('list', { name: '团队工具过程', hidden: true })).getByRole('listitem', { hidden: true })
  expect(row.textContent).toContain('未开始发布 · @米寇')
  expect(row.textContent).toContain('没有读到完整源文件，未开始发布。')
  expect(row.textContent).not.toMatch(/工具已完成|工具结果待确认|source_file_unavailable|not_started/)
})

test.each([
  { status: 'error', complete: false, outcome: 'effects_unknown' },
  { status: 'error', outcome: 'not_started' },
  { status: 'error', complete: true, outcome: 'not_started', stderr: { complete: false } },
  { status: 'ok', complete: true, outcome: 'not_started' },
])('不完整或矛盾回执不能冒充已知发布前拒绝：%j', result => {
  const { container } = render(<TeamToolProgress events={events('publish_artifact', result)} names={{}} />)
  expect(container.textContent).not.toContain('未开始发布')
  expect(container.textContent).toContain('工具结果待确认')
})

test('历史失败与后续修正记录都保留，不覆盖成一次成功', () => {
  const first = events('publish_artifact', { status: 'error', complete: true, outcome: 'not_started',
    message: '文件名不适合落盘，未开始发布。' })
  const corrected = events('publish_artifact', { status: 'ok', artifact_seq: 15, filename: 'result.html' })
  corrected[0] = { ...corrected[0], seq: 14, payload: { ...corrected[0].payload, call_id: 'corrected' } }
  corrected[1] = { ...corrected[1], seq: 16, payload: { ...corrected[1].payload, started_seq: 14 } }
  render(<TeamToolProgress events={[...first, ...corrected]} names={{ dev: '米寇' }} />)
  const rows = within(screen.getByRole('list', { name: '团队工具过程', hidden: true })).getAllByRole('listitem', { hidden: true })
  expect(rows).toHaveLength(2)
  expect(rows[0].textContent).toContain('未开始发布')
  expect(rows[1].textContent).toContain('工具已完成')
  expect(rows[1].textContent).toContain('成果记录：#15')
})
