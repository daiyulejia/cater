import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { TaskList } from '../src/TaskList'
import type { ExecutionControl } from '../src/executionControl'
import { executionLabel } from '../src/executionControl'
import type { TeamActivity, TeamSummary } from '../src/team'
import { teamStatus } from '../src/team'

const activityCss = readFileSync('src/TaskList.css', 'utf8')

type Row = {
  id: string; goal: string; status: string; status_label: string
  activity?: TeamActivity; team?: TeamSummary
  execution_control?: ExecutionControl
}
const team = (extra: Partial<TeamSummary> = {}): TeamSummary => ({
  enabled: true, status: 'running', pending_count: 0, revision: 10, runs: [], ...extra,
})
const row = (extra: Partial<Row> = {}): Row => ({
  id: 'cafe', goal: '核对猫咖菜单', status: 'running', status_label: '等待下一轮', ...extra,
})
const statusLabel = (task: Row) => executionLabel(task.execution_control?.state)
  || (task.team?.enabled ? teamStatus(task.team, task.activity) : task.status_label)
const props = { selectedId: 'cafe', drafts: {}, collapsed: false, searchOpen: true, onChoose: vi.fn(), statusLabel }
const entry = () => within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button')
const indicator = () => entry().querySelector('.task-list-activity')!

test.each<{ name: string; task: Row; state: string; label: string; moving?: boolean }>([
  { name: '真实运行', task: row({ activity: { active: true, state: 'running' }, team: team() }), state: 'running', label: '正在处理消息', moving: true },
  { name: '已排队且 active', task: row({ activity: { active: true, state: 'queued' }, team: team() }), state: 'queued', label: '消息处理已排队' },
  { name: '真实失败', task: row({ activity: { active: false, state: 'failed' }, team: team() }), state: 'failed', label: '执行失败，需跟进' },
  { name: '本轮等待', task: row({ activity: { active: false, state: 'completed' }, team: team({ runs: [{ status: 'waiting', revision: 10 }] }) }), state: 'waiting', label: '本轮结束，等待消息' },
  { name: '尚有待办', task: row({ activity: { active: false, state: 'idle' }, team: team({ pending_count: 2 }) }), state: 'waiting', label: '消息等待处理' },
  { name: '旧目标运行记录', task: row({ team: team({ runs: [{ status: 'running', revision: 9 }] }) }), state: 'idle', label: '任务待推进' },
  { name: '当前记录与消费者矛盾', task: row({ activity: { active: false, state: 'completed' }, team: team({ runs: [{ status: 'running', revision: 10 }] }) }), state: 'uncertain', label: '执行状态待核对' },
  { name: '缺少消费者但有当前运行记录', task: row({ team: team({ runs: [{ status: 'started', revision: 10 }] }) }), state: 'running', label: '正在处理消息', moving: true },
  { name: '失败后有待办', task: row({ team: team({ pending_count: 1, runs: [{ status: 'failed', revision: 10 }] }) }), state: 'failed', label: '处理失败，仍有待办' },
  { name: '处理受阻', task: row({ team: team({ runs: [{ status: 'blocked', revision: 10 }] }) }), state: 'blocked', label: '处理结果待核对' },
  { name: '已交付', task: row({ status: 'done', team: team({ status: 'done' }) }), state: 'done', label: '店长确认可交付' },
  { name: '交付后处理新消息', task: row({ status: 'done', activity: { active: true, state: 'running' }, team: team({ status: 'done' }) }), state: 'running', label: '正在处理后续消息', moving: true },
  { name: '交付后消息排队', task: row({ status: 'done', activity: { active: true, state: 'queued' }, team: team({ status: 'done' }) }), state: 'queued', label: '后续消息已排队' },
  { name: '已交付但操作待核对', task: row({ status: 'done', team: team({ status: 'done', effects_pending_count: 1 }) }), state: 'uncertain', label: '有操作影响待核对' },
  { name: '状态标记 running 但不 active', task: row({ activity: { active: false, state: 'running' }, team: team() }), state: 'idle', label: '任务待推进' },
  { name: 'active 不带执行状态', task: row({ activity: { active: true, state: '' }, team: team() }), state: 'uncertain', label: '任务待推进' },
  { name: '本轮中断不等于人类暂停', task: row({ team: team({ runs: [{ status: 'interrupted', revision: 10 }] }) }), state: 'uncertain', label: '本轮消息处理中断' },
  { name: '明确本轮取消事实', task: row({ activity: { active: false, state: 'completed' }, team: team({ runs: [{ status: 'cancelled', revision: 10 }] }) }), state: 'cancelled', label: '本轮消息处理已取消' },
  { name: '旧目标取消记录', task: row({ team: team({ runs: [{ status: 'cancelled', revision: 9 }] }) }), state: 'idle', label: '任务待推进' },
])('$name 只按实际字段展示', ({ task, state, label, moving = false }) => {
  render(<TaskList {...props} tasks={[task]} />)
  expect(indicator().getAttribute('data-state')).toBe(state)
  expect(within(entry()).getByText(label)).toBeTruthy()
  expect(indicator().querySelectorAll('.task-list-activity-paw')).toHaveLength(moving ? 2 : 0)
  expect(Boolean(indicator().querySelector('.task-list-activity-light'))).toBe(moving)
  expect(Boolean(indicator().querySelector('.lucide-cat'))).toBe(moving || state === 'idle')
})

test('普通任务也依据 activity，标题、编号、文案及选中态均不作为运行证据', () => {
  const task = row({ id: 'running-active', goal: '正在执行的猫咖菜单', status_label: '任务进行中' })
  const view = render(<TaskList {...props} tasks={[task]} selectedId={task.id} />)
  expect(indicator().getAttribute('data-state')).toBe('idle')
  expect(indicator().querySelector('.task-list-activity-paw')).toBeNull()
  expect(entry().getAttribute('aria-current')).toBe('page')
  view.rerender(<TaskList {...props} tasks={[{ ...task, status: 'done', activity: { active: true, state: 'running' }, status_label: '正在协作' }]} selectedId={null} />)
  expect(indicator().getAttribute('data-state')).toBe('running')
  expect(entry().getAttribute('aria-current')).toBeNull()
  expect(within(entry()).getByText('正在协作')).toBeTruthy()
})

test.each(['paused', 'cancelled'] as const)('%s 执行控制优先于仍在途的 running，不继续动画', state => {
  const task = row({ activity: { active: true, state: 'running' }, team: team(), execution_control: { state, seq: 12, reason: '' } })
  render(<TaskList {...props} tasks={[task]} />)
  expect(indicator().getAttribute('data-state')).toBe(state)
  expect(indicator().querySelector('.task-list-activity-paw')).toBeNull()
  expect(within(entry()).getByText(state === 'paused' ? '已暂停' : '已取消')).toBeTruthy()
})

test.each(['interrupted', 'cancelled'] as const)('当前真实活动优先于旧的 %s 运行终态', status => {
  const task = row({ activity: { active: true, state: 'running' }, team: team({ runs: [{ status, revision: 10 }] }) })
  render(<TaskList {...props} tasks={[task]} />)
  expect(indicator().getAttribute('data-state')).toBe('running')
  expect(indicator().querySelectorAll('.task-list-activity-paw')).toHaveLength(2)
  expect(within(entry()).getByText('正在处理消息')).toBeTruthy()
})

test.each(['', 'idle', 'completed', 'unknown'])('活跃消费者状态 %s 不可信时，历史记录不能确认当前运行或终止', state => {
  const view = render(<TaskList {...props} tasks={[]} />)
  for (const status of ['running', 'started', 'interrupted', 'cancelled', 'waiting', 'completed']) {
    const task = row({ activity: { active: true, state }, team: team({ runs: [{ status, revision: 10 }] }) })
    view.rerender(<TaskList {...props} tasks={[task]} />)
    expect(indicator().getAttribute('data-state')).toBe('uncertain')
    expect(indicator().querySelector('.task-list-activity-paw')).toBeNull()
    expect(indicator().querySelector('.lucide-pause, .lucide-circle-x')).toBeNull()
  }
})

test.each([null, -1, .5, NaN, Infinity])('操作记录计数 %s 不可信时，即使当前活动存活也不显示执行动画', effects_pending_count => {
  const task = row({ activity: { active: true, state: 'running' }, team: team({ effects_pending_count }) })
  render(<TaskList {...props} tasks={[task]} />)
  expect(indicator().getAttribute('data-state')).toBe('uncertain')
  expect(indicator().querySelector('.task-list-activity-paw')).toBeNull()
  expect(within(entry()).getByText('操作状态待核对')).toBeTruthy()
})

test('只有明确暂停控制才将中断运行展示为 paused，恢复控制不会把中断变成 running', () => {
  const task = row({ team: team({ runs: [{ status: 'interrupted', revision: 10 }] }) })
  const view = render(<TaskList {...props} tasks={[task]} />)
  expect(indicator().getAttribute('data-state')).toBe('uncertain')
  view.rerender(<TaskList {...props} tasks={[{ ...task, execution_control: { state: 'paused', seq: 11, reason: '' } }]} />)
  expect(indicator().getAttribute('data-state')).toBe('paused')
  expect(within(entry()).getByText('已暂停')).toBeTruthy()
  view.rerender(<TaskList {...props} tasks={[{ ...task, execution_control: { state: 'running', seq: 12, reason: '' } }]} />)
  expect(indicator().getAttribute('data-state')).toBe('uncertain')
  expect(within(entry()).getByText('本轮消息处理中断')).toBeTruthy()
})

test('可选 executionState 提供最新控制态，恢复后重新使用真实活动，DOM、草稿和导航保持稳定', () => {
  const task = row({ activity: { active: true, state: 'running' }, team: team() })
  const choose = vi.fn()
  const shared = { ...props, tasks: [task], drafts: { cafe: '未发送' }, onChoose: choose }
  const view = render(<TaskList {...shared} />)
  const button = entry()
  expect(indicator().getAttribute('data-state')).toBe('running')
  view.rerender(<TaskList {...shared} executionState={() => 'paused'} />)
  expect(entry()).toBe(button)
  expect(within(button).getByText('已暂停')).toBeTruthy()
  expect(indicator().getAttribute('data-state')).toBe('paused')
  view.rerender(<TaskList {...shared} executionState={() => 'running'} />)
  expect(entry()).toBe(button)
  expect(indicator().getAttribute('data-state')).toBe('running')
  expect(within(button).getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  expect(indicator().getAttribute('aria-hidden')).toBe('true')
  expect(button.querySelector('[aria-live], [role="status"], [role="progressbar"]')).toBeNull()
  expect(within(button).queryByRole('img', { name: /猫|执行|处理/ })).toBeNull()
  fireEvent.click(button)
  expect(choose).toHaveBeenCalledExactlyOnceWith('cafe')
})

test('固定尺寸和 reduced-motion 样式保留静态猫、双爪、灯及可读文字', () => {
  render(<TaskList {...props} tasks={[row({ activity: { active: true, state: 'running' }, team: team() })]} />)
  expect(activityCss).toMatch(/flex: 0 0 15px;[\s\S]*?width: 15px;[\s\S]*?height: 20px;/)
  expect(activityCss).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?animation: none;[\s\S]*?transform: none;[\s\S]*?opacity: 1;/)
  expect(activityCss).not.toMatch(/rotate\(/)
  expect(indicator().querySelectorAll('.task-list-activity-paw')).toHaveLength(2)
  expect(within(entry()).getByText('正在处理消息')).toBeTruthy()
})
