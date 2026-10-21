import { expect, test } from 'vitest'
import { compareTeamSnapshots, resolveTeamActivity, resolveTeamTask } from '../src/team'
import type { TeamSnapshotClock, TeamState, TeamWithdrawal } from '../src/team'

const clock = (instance: string | undefined, revision: number, order: number,
  extra: Partial<TeamSnapshotClock> = {}): TeamSnapshotClock => ({
  version: 20, team: { revision: 10 }, observation_order: order,
  evaluated_at: 100, activity: { state: 'idle', active: false, revision, instance_id: instance }, ...extra,
})

test.each(['idle', 'queued', 'running', 'failed', 'completed'])('new instance %s replaces old higher activity revision without relying on server time', state => {
  const old = clock('old', 900, 1, { evaluated_at: 500 })
  const fresh = clock('new', 0, 2, { evaluated_at: 1 })
  fresh.activity = { ...fresh.activity!, state, active: ['queued', 'running'].includes(state) }
  expect(compareTeamSnapshots(fresh, old)).toBeGreaterThan(0)
  expect(compareTeamSnapshots(old, fresh)).toBeLessThan(0)
  expect(resolveTeamActivity(old.activity, fresh.activity, false, old, fresh)).toBe(fresh.activity)
  expect(resolveTeamActivity(fresh.activity, old.activity, false, fresh, old)).toBe(fresh.activity)
})

test('instance strings do not imply ordering or override a newer persistent event version', () => {
  const old = clock('z', 900, 1)
  const fresh = clock('a', 0, 2)
  expect(compareTeamSnapshots(fresh, old)).toBeGreaterThan(0)
  expect(compareTeamSnapshots({ ...old, version: 21 }, fresh)).toBeGreaterThan(0)
  expect(compareTeamSnapshots({ ...fresh, version: 19 }, old)).toBeLessThan(0)
  expect(compareTeamSnapshots({ ...old, team: { revision: 11 } }, fresh)).toBeGreaterThan(0)
  expect(compareTeamSnapshots({ ...fresh, observation_order: undefined }, { ...old, observation_order: undefined })).toBe(0)
})

test.each(['same', undefined])('same instance or legacy %s retains revision and lease ordering', instance => {
  const old = clock(instance, 1, 900)
  const fresh = clock(instance, 2, 1)
  expect(compareTeamSnapshots(fresh, old)).toBeGreaterThan(0)
  expect(compareTeamSnapshots(old, fresh)).toBeLessThan(0)
  expect(compareTeamSnapshots({ ...fresh, evaluated_at: 101 }, { ...fresh, observation_order: 900 })).toBeGreaterThan(0)
})

test('missing instance on one side is not a comparable activity revision', () => {
  expect(compareTeamSnapshots(clock('new', 0, 2), clock(undefined, 900, 1))).toBeGreaterThan(0)
  expect(compareTeamSnapshots(clock(undefined, 900, 1), clock('new', 0, 2))).toBeLessThan(0)
})

const withdrawal: TeamWithdrawal = { message_seq: 11, source_hash: 'a'.repeat(64), requester: 'manager', sender: 'manager',
  recipient: 'dev', leader_id: 'manager', revision: 10, reason: '已调整', run_id: 'withdraw-run', withdrawal_seq: 15 }
const team: TeamState = { enabled: true, objective: '当前目标', status: 'running', pending_count: 0, revision: 10,
  runs: [], assignments: [], checkpoint: '', withdrawals: [withdrawal] }

test('撤回前的迟到摘要不覆盖已含撤回记录的新详情', () => {
  const detail = { goal: team.objective, status: 'running', team }
  const before = structuredClone(detail)
  const stale = { goal: team.objective, status: 'running', version: 14, team: { ...team, withdrawals: [] } }
  expect(compareTeamSnapshots(stale, { version: 20, team })).toBeLessThan(0)
  expect(resolveTeamTask(detail, stale, 20)).toBe(detail)
  expect(resolveTeamTask(detail, { ...stale, version: 20, team }, 20)).toBe(detail)
  expect(detail).toEqual(before)
})

test('同目标精简摘要保留已有撤回，新目标同步期间不沿用旧撤回或从旧摘要恢复撤回', () => {
  const detail = { goal: team.objective, status: 'running', team }
  const compact = { goal: team.objective, status: 'running', version: 21,
    team: { enabled: true, status: 'running', pending_count: 0, revision: 10 } }
  expect(resolveTeamTask(detail, compact, 20)?.team?.withdrawals).toBe(team.withdrawals)
  const next = resolveTeamTask(detail, { ...compact, goal: '新目标', team: { ...compact.team, revision: 21 } }, 20)!
  expect(next.goal).toBe('新目标')
  expect(next.team?.withdrawals).toBeUndefined()
  expect(resolveTeamTask(next, { ...compact, team }, 21)).toBe(next)
  expect(team.withdrawals).toEqual([withdrawal])
})
