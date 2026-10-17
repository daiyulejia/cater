import { expect, test } from 'vitest'
import { compareTeamSnapshots, resolveTeamActivity } from '../src/team'
import type { TeamSnapshotClock } from '../src/team'

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
