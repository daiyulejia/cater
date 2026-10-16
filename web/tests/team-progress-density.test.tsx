import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { TeamProgressPanel } from '../src/TeamProgressPanel'
import type { TeamState } from '../src/team'

const team: TeamState = { enabled: true, objective: '完整目标与验收范围'.repeat(100), checkpoint: '已收到2条待处理消息',
  status: 'running', pending_count: 2, revision: 1, runs: [], assignments: [] }

test('目标与最近备注默认折叠，执行状态和待处理事实仍直接可见', () => {
  render(<TeamProgressPanel team={team} events={[]} names={{}} executionState="paused" />)
  expect(screen.getByLabelText('团队执行状态').textContent).toBe('已暂停后续执行')
  expect(screen.getByText('待处理消息：2')).toBeTruthy()
  for (const [title, text] of [['当前目标', team.objective], ['最近工作备注', team.checkpoint]]) {
    const paragraph = screen.getByText(text)
    const disclosure = paragraph.closest('details')!
    expect(disclosure.open).toBe(false)
    fireEvent.click(within(disclosure).getByText(title))
    expect(disclosure.open).toBe(true)
    expect(paragraph.textContent).toBe(text)
    expect(paragraph.className).toContain('max-h-48')
  }
  expect(screen.queryByText('交付说明')).toBeNull()
})

test('新目标版本重置展开状态，同目标备注不重复列出', () => {
  const view = render(<TeamProgressPanel team={team} events={[]} names={{}} />)
  fireEvent.click(screen.getByText('当前目标'))
  fireEvent.click(screen.getByText('最近工作备注'))
  view.rerender(<TeamProgressPanel team={{ ...team, revision: 5, objective: '新目标', checkpoint: '新备注' }} events={[]} names={{}} />)
  expect(screen.getByText('新目标').closest('details')!.open).toBe(false)
  expect(screen.getByText('新备注').closest('details')!.open).toBe(false)
  view.rerender(<TeamProgressPanel team={{ ...team, objective: '同一目标', checkpoint: '同一目标' }} events={[]} names={{}} />)
  expect(screen.queryByText('最近工作备注')).toBeNull()
  expect(screen.getAllByText('同一目标')).toHaveLength(1)
})
