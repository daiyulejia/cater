import { describe, expect, test } from 'vitest'
import { usageSummary } from '../src/modelUsage'
import type { UsageCall, UsageGroup, UsageSource, UsageSummary } from '../src/modelUsage'
import type { TeamEvent } from '../src/team'

const event = (seq: number, payload: Record<string, unknown> = {}, type = 'team.model_called',
  actor: TeamEvent['actor'] = { id: 'team', kind: 'system', role: 'team' }): TeamEvent => ({ seq, type, actor, payload })
const reported = (seq: number, tokens: unknown, extra: Record<string, unknown> = {}) =>
  event(seq, { role: 'dev', tokens, token_usage: 'reported', ...extra })
const noDetails = { input: null, output: null, inputKind: null, cacheRead: null, cacheWrite: null, reasoning: null }
const noSizes = { prompt: null, history: null, tools: null, omittedGroups: null }
const invalid: unknown[] = [undefined, null, true, false, -1, 1.5, NaN, Infinity, -Infinity,
  Number.MAX_SAFE_INTEGER + 1, '0', '42', [], [1], {}, new Number(1)]

test('empty projection exports the UI contract without inventing zero totals', () => {
  const summary: UsageSummary = usageSummary([])
  const calls: UsageCall[] = summary.calls
  const groups: UsageGroup[] = summary.groups
  const source: UsageSource = 'unspecified'
  expect({ calls, groups, source }).toEqual({ calls: [], groups: [], source: 'unspecified' })
  expect(summary).toEqual({ calls: [], groups: [], reportedCalls: 0, reportedTokens: null,
    estimatedCalls: 0, estimatedTokens: null, unavailableCalls: 0, unspecifiedCalls: 0, detailCalls: 0, asOfSeq: 0 })
})

test('mixed historical goals count only host model receipts, including old explicit sources', () => {
  const events = [
    event(1, { revision: 1 }, 'team.objective_updated'),
    reported(2, 130, { driver: 'openai', usage: { total_tokens: 130, prompt_tokens: 100, completion_tokens: 30,
      prompt_tokens_details: { cached_tokens: 80 }, completion_tokens_details: { reasoning_tokens: 10 } } }),
    event(3, { tokens: 130, token_usage: 'reported' }, 'msg.said'),
    event(4, { tokens: 130, token_usage: 'reported' }, 'task.tokens'),
    event(5, { token_usage: 'reported', role: 'review', usage: { total_tokens: 20 } }),
    event(6, { revision: 2 }, 'team.objective_updated'),
    reported(7, 10),
    event(8, { role: 'dev', tokens: 9, token_usage: 'estimated' }),
    event(9, { role: 'dev', tokens: 0, token_usage: 'unavailable' }),
    event(10, { role: 'legacy', usage: { total_tokens: 7, input_tokens: 5, output_tokens: 2 } }),
    event(11, { role: 'missing' }),
    event(12, { token_usage: 'reported', tokens: 999 }, 'team.model_called', { id: 'dev', kind: 'agent', role: 'dev' }),
    event(90, { tokens: 100000, token_usage: 'reported', status: 'failed' }, 'team.run_finished'),
  ]
  const summary = usageSummary(events.reverse())
  expect(summary).toMatchObject({ reportedCalls: 3, reportedTokens: 160, estimatedCalls: 1, estimatedTokens: 9,
    unavailableCalls: 1, unspecifiedCalls: 2, detailCalls: 1, asOfSeq: 90 })
  expect(summary.calls.map(call => call.seq)).toEqual([2, 5, 7, 8, 9, 10, 11])
  expect(summary.calls[0]).toMatchObject({ driver: 'openai', tokens: 130, input: 100, output: 30,
    inputKind: 'prompt_inclusive', cacheRead: 80, cacheWrite: null, reasoning: 10 })
  expect(summary.calls[5]).toMatchObject({ source: 'unspecified', tokens: 7, input: 5, output: 2, inputKind: 'uncached' })
  expect(summary.groups).toEqual([
    { role: 'dev', calls: 4, reportedCalls: 2, reportedTokens: 140, estimatedCalls: 1, estimatedTokens: 9, unavailableCalls: 1, unspecifiedCalls: 0 },
    { role: 'review', calls: 1, reportedCalls: 1, reportedTokens: 20, estimatedCalls: 0, estimatedTokens: null, unavailableCalls: 0, unspecifiedCalls: 0 },
    { role: 'legacy', calls: 1, reportedCalls: 0, reportedTokens: null, estimatedCalls: 0, estimatedTokens: null, unavailableCalls: 0, unspecifiedCalls: 1 },
    { role: 'missing', calls: 1, reportedCalls: 0, reportedTokens: null, estimatedCalls: 0, estimatedTokens: null, unavailableCalls: 0, unspecifiedCalls: 1 },
  ])
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'other', kind: 'system', role: 'team' },
  { id: 'team', kind: 'system', role: 'other' }, { id: 'team', kind: undefined, role: 'team' },
])('rejects a forged host: %j', actor => {
  expect(usageSummary([event(9, { tokens: 10, token_usage: 'reported' }, 'team.model_called', actor)]))
    .toMatchObject({ calls: [], reportedTokens: null, asOfSeq: 9 })
})

describe('source and total validation', () => {
  test.each(['reported', 'estimated'])('%s accepts primary or legacy totals but never sums them', source => {
    const summary = usageSummary([
      event(1, { token_usage: source, tokens: 7, usage: { total_tokens: 7 } }),
      event(2, { token_usage: source, usage: { total_tokens: 8 } }),
      event(3, { token_usage: source, tokens: 9, usage: { total_tokens: '900' } }),
      event(4, { token_usage: source, tokens: '10', usage: { total_tokens: 10 } }),
    ])
    expect(summary.calls.map(call => [call.source, call.tokens])).toEqual([[source, 7], [source, 8], [source, 9], [source, 10]])
    expect(summary[source === 'reported' ? 'reportedTokens' : 'estimatedTokens']).toBe(34)
  })

  test.each(['reported', 'estimated', undefined])('conflicting valid totals are unusable: %s', source => {
    const summary = usageSummary([event(1, { token_usage: source, tokens: 0, usage: { total_tokens: 1, prompt_tokens: 1 } })])
    expect(summary.calls[0]).toMatchObject({ source: 'unavailable', tokens: null, ...noDetails })
    expect(summary).toMatchObject({ reportedTokens: null, estimatedTokens: null, detailCalls: 0 })
  })

  test.each(['unavailable', 'provider', 'Reported', '', null, true, 1, [], {}, ['reported']])('unknown or unavailable source never promotes zero: %j', source => {
    const summary = usageSummary([event(1, { token_usage: source, tokens: 0, usage: { total_tokens: 0, input_tokens: 0, output_tokens: 0 } })])
    expect(summary.calls[0]).toMatchObject({ source: 'unavailable', tokens: null, ...noDetails })
    expect(summary).toMatchObject({ unavailableCalls: 1, reportedCalls: 0, reportedTokens: null })
  })

  test('missing source preserves legacy totals and optional details without reporting them', () => {
    const summary = usageSummary([event(1, { tokens: 0 }), event(2, { tokens: 12 }),
      event(3, { usage: { total_tokens: 4, prompt_tokens: 3, completion_tokens: 1 } }), event(4)])
    expect(summary.calls.map(call => [call.source, call.tokens])).toEqual([
      ['unspecified', 0], ['unspecified', 12], ['unspecified', 4], ['unspecified', null],
    ])
    expect(summary.calls[2]).toMatchObject({ input: 3, output: 1 })
    expect(summary).toMatchObject({ unspecifiedCalls: 4, reportedCalls: 0, reportedTokens: null, estimatedTokens: null, detailCalls: 0 })
  })

  test.each(invalid.map(value => [value]))('rejects invalid primary and legacy totals: %j', value => {
    for (const payload of [{ tokens: value }, { usage: { total_tokens: value } }]) {
      const summary = usageSummary([event(1, { token_usage: 'reported', ...payload })])
      expect(summary.calls[0]).toMatchObject({ source: 'unavailable', tokens: null, ...noDetails })
      expect(summary).toMatchObject({ unavailableCalls: 1, reportedCalls: 0, reportedTokens: null })
    }
  })

  test('valid zero is real zero in both aggregates and all supported details', () => {
    const summary = usageSummary([reported(1, 0, { usage: { total_tokens: 0, prompt_tokens: 0, completion_tokens: 0,
      prompt_tokens_details: { cached_tokens: 0 }, prompt_cache_hit_tokens: 0, completion_tokens_details: { reasoning_tokens: 0 } } }),
    event(2, { token_usage: 'estimated', tokens: 0 }),
    reported(3, 0, { usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } })])
    expect(summary).toMatchObject({ reportedCalls: 2, reportedTokens: 0, estimatedCalls: 1, estimatedTokens: 0, detailCalls: 2 })
    expect(summary.calls[0]).toMatchObject({ tokens: 0, input: 0, output: 0, cacheRead: 0, reasoning: 0 })
    expect(summary.calls[2]).toMatchObject({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 })
  })
})

describe('protocol details', () => {
  test('Anthropic uncached input and cache details never alter the supplied total', () => {
    const summary = usageSummary([reported(1, 175, { usage: { total_tokens: 175, input_tokens: 20, output_tokens: 5,
      cache_read_input_tokens: 100, cache_creation_input_tokens: 50 } })])
    expect(summary.calls[0]).toMatchObject({ input: 20, inputKind: 'uncached', output: 5, cacheRead: 100, cacheWrite: 50, reasoning: null })
    expect(summary.reportedTokens).toBe(175)
  })

  test.each([
    [{ prompt_tokens_details: { cached_tokens: 80 }, prompt_cache_hit_tokens: 80 }, 80],
    [{ prompt_tokens_details: { cached_tokens: 80 } }, 80],
    [{ prompt_cache_hit_tokens: 70 }, 70],
    [{ prompt_tokens_details: { cached_tokens: 80 }, prompt_cache_hit_tokens: 70 }, null],
    [{ prompt_tokens_details: { cached_tokens: '80' }, prompt_cache_hit_tokens: 70 }, 70],
    [{ prompt_tokens_details: { cached_tokens: 80 }, prompt_cache_hit_tokens: true }, 80],
  ])('OpenAI cache read uses only valid consistent alternatives: %j', (details, cacheRead) => {
    const summary = usageSummary([reported(1, 110, { usage: { prompt_tokens: 100, completion_tokens: 10,
      completion_tokens_details: { reasoning_tokens: 5 }, cache_read_input_tokens: 999, cache_creation_input_tokens: 999, ...details } })])
    expect(summary.calls[0]).toMatchObject({ input: 100, output: 10, cacheRead, cacheWrite: null, reasoning: 5 })
    expect(summary.reportedTokens).toBe(110)
  })

  test.each([10, '10', null])('both protocol input keys suppress input and cache interpretation: %j', input => {
    const call = usageSummary([reported(1, 30, { usage: { prompt_tokens: 20, input_tokens: input, completion_tokens: 10,
      prompt_tokens_details: { cached_tokens: 5 }, cache_read_input_tokens: 7, cache_creation_input_tokens: 8 } })]).calls[0]
    expect(call).toMatchObject({ input: null, inputKind: null, output: 10, cacheRead: null, cacheWrite: null, tokens: 30 })
  })

  test('ambiguous output keys do not guess a protocol', () => {
    const summary = usageSummary([reported(1, 10, { usage: { completion_tokens: 3, output_tokens: 3 } })])
    expect(summary.calls[0].output).toBeNull()
    expect(summary.detailCalls).toBe(0)
  })

  test('cache-only details stay available without inflating input/output coverage', () => {
    const summary = usageSummary([
      reported(1, 20, { usage: { prompt_tokens_details: { cached_tokens: 4 } } }),
      reported(2, 20, { usage: { prompt_cache_hit_tokens: 5 } }),
      reported(3, 20, { usage: { cache_read_input_tokens: 6, cache_creation_input_tokens: 7 } }),
      reported(4, 20, { usage: { prompt_cache_hit_tokens: 5, cache_read_input_tokens: 6, cache_creation_input_tokens: 7 } }),
    ])
    expect(summary.calls.map(call => [call.cacheRead, call.cacheWrite])).toEqual([[4, null], [5, null], [6, 7], [null, null]])
    expect(summary).toMatchObject({ reportedTokens: 80, detailCalls: 0 })
    expect(summary.calls.every(call => call.input === null && call.inputKind === null && call.output === null)).toBe(true)
  })

  test.each(invalid.map(value => [value]))('rejects malformed numeric details: %j', value => {
    const summary = usageSummary([
      reported(1, 20, { usage: { prompt_tokens: value, completion_tokens: value, prompt_cache_hit_tokens: value,
        prompt_tokens_details: { cached_tokens: value }, completion_tokens_details: { reasoning_tokens: value } } }),
      reported(2, 20, { usage: { input_tokens: value, output_tokens: value, cache_read_input_tokens: value, cache_creation_input_tokens: value } }),
    ])
    for (const call of summary.calls) expect(call).toMatchObject(noDetails)
    expect(summary).toMatchObject({ reportedTokens: 40, detailCalls: 0 })
  })

  test('details cannot manufacture a total or turn an estimate into reported detail coverage', () => {
    const usage = { prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 2 } }
    const summary = usageSummary([event(1, { token_usage: 'reported', usage }), event(2, { usage }),
      event(3, { token_usage: 'estimated', tokens: 15, usage }), reported(4, 15),
      reported(5, 15, { usage: { prompt_tokens: 10 } }), reported(6, 15, { usage: { output_tokens: 5 } }),
      reported(7, 15, { usage: { completion_tokens_details: { reasoning_tokens: 2 } } })])
    expect(summary.calls.slice(0, 3).every(call => call.input === null && call.output === null)).toBe(true)
    expect(summary).toMatchObject({ reportedCalls: 4, reportedTokens: 60, estimatedCalls: 1, estimatedTokens: 15, detailCalls: 2 })
  })
})

describe('ordering, deduplication and overflow', () => {
  test('same seq with identical public values counts once regardless of key order or private payload', () => {
    const first = reported(2, 10, { usage: { prompt_tokens: 8, completion_tokens: 2 }, private: { thought: 'secret' } })
    const second = event(2, { usage: { completion_tokens: 2, prompt_tokens: 8 }, token_usage: 'reported', tokens: 10, role: 'dev' })
    const summary = usageSummary([first, reported(1, 3), second, structuredClone(first)])
    expect(summary.calls.map(call => call.seq)).toEqual([1, 2])
    expect(summary).toMatchObject({ reportedCalls: 2, reportedTokens: 13, detailCalls: 1 })
  })

  test.each([
    { tokens: 11 }, { role: 'review' }, { driver: 'other' }, { token_usage: 'estimated' },
    { usage: { input_tokens: 8, output_tokens: 2 } }, { request_sizes: { prompt_utf8_bytes: 5 } },
  ])('conflicting seq is one unavailable receipt independent of order: %j', conflict => {
    const first = reported(2, 10, { driver: 'openai', usage: { prompt_tokens: 8, completion_tokens: 2 } })
    const second = { ...first, payload: { ...first.payload, ...conflict } }
    const input = [first, second, first, reported(3, 4)]
    const summary = usageSummary(input)
    expect(summary).toEqual(usageSummary([...input].reverse()))
    expect(summary.calls).toHaveLength(2)
    expect(summary.calls[0]).toMatchObject({ source: 'unavailable', tokens: null, ...noDetails, requestSizes: noSizes })
    expect(summary).toMatchObject({ reportedCalls: 1, reportedTokens: 4, unavailableCalls: 1, detailCalls: 0 })
    expect(summary.calls[0].role).toBe(conflict.role ? 'unknown' : 'dev')
    expect(summary.calls[0].driver).toBe(conflict.driver ? undefined : 'openai')
  })

  test('a third matching copy cannot undo a conflict or restore conflicting metadata', () => {
    const events = [reported(1, 1, { role: 'a', driver: 'x' }), reported(1, 2, { role: 'b', driver: 'y' }),
      reported(1, 1, { role: 'a', driver: 'x' })]
    expect(usageSummary(events).calls).toEqual([{ seq: 1, role: 'unknown', source: 'unavailable', tokens: null, ...noDetails, requestSizes: noSizes }])
    expect(usageSummary(events)).toEqual(usageSummary([...events].reverse()))
  })

  test('unrelated events and forged actors at the same seq cannot duplicate a valid call', () => {
    const summary = usageSummary([reported(1, 5), event(1, { tokens: 999, token_usage: 'reported' }, 'msg.said'),
      event(1, { tokens: 999, token_usage: 'reported' }, 'team.model_called', { id: 'team', kind: 'agent', role: 'team' })])
    expect(summary.calls).toHaveLength(1)
    expect(summary).toMatchObject({ reportedCalls: 1, reportedTokens: 5, unavailableCalls: 0 })
  })

  test.each(['reported', 'estimated'])('overflow remains null after additional %s calls, including zero', source => {
    const summary = usageSummary([event(1, { role: 'large', token_usage: source, tokens: Number.MAX_SAFE_INTEGER }),
      event(2, { role: 'large', token_usage: source, tokens: 1 }), event(3, { role: 'large', token_usage: source, tokens: 0 }),
      event(4, { role: 'small', token_usage: source, tokens: 2 })])
    expect(summary[source === 'reported' ? 'reportedTokens' : 'estimatedTokens']).toBeNull()
    expect(summary.groups.find(group => group.role === 'large')?.[source === 'reported' ? 'reportedTokens' : 'estimatedTokens']).toBeNull()
    expect(summary.groups.find(group => group.role === 'small')?.[source === 'reported' ? 'reportedTokens' : 'estimatedTokens']).toBe(2)
    expect(summary[source === 'reported' ? 'reportedCalls' : 'estimatedCalls']).toBe(4)
    if (source === 'reported') expect(summary.groups.map(group => group.role)).toEqual(['small', 'large'])
  })

  test('safe maximum is valid and cross-role aggregate overflow does not corrupt group sums', () => {
    const summary = usageSummary([reported(1, Number.MAX_SAFE_INTEGER, { role: 'max' }), reported(2, 1, { role: 'one' })])
    expect(summary.reportedTokens).toBeNull()
    expect(summary.groups.map(group => group.reportedTokens)).toEqual([Number.MAX_SAFE_INTEGER, 1])
  })

  test('groups sort by reported tokens, with zero before null and stable role ties', () => {
    const summary = usageSummary([reported(1, 0, { role: 'zero' }), reported(2, 5, { role: 'b' }),
      reported(3, 5, { role: 'a' }), event(4, { role: 'estimate', token_usage: 'estimated', tokens: 1000 }),
      event(5, { role: '__proto__' }), event(6, { role: 'constructor' })])
    expect(summary.groups.map(group => group.role)).toEqual(['a', 'b', 'zero', '__proto__', 'constructor', 'estimate'])
  })

  test.each([0, ...invalid].map(value => [value]))('ignores invalid seq for calls and watermark: %j', seq => {
    const malformed = { ...reported(1, 99), seq } as unknown as TeamEvent
    const summary = usageSummary([malformed, reported(2, 1), event(7, {}, 'msg.said')])
    expect(summary.calls.map(call => call.seq)).toEqual([2])
    expect(summary.asOfSeq).toBe(7)
  })

  test('watermark includes every valid event, even when there are no model receipts', () => {
    expect(usageSummary([event(Number.MAX_SAFE_INTEGER, { provider_timestamp: Infinity }, 'msg.said')]))
      .toMatchObject({ calls: [], asOfSeq: Number.MAX_SAFE_INTEGER, reportedTokens: null })
  })
})

describe('request sizes and payload boundaries', () => {
  test('projects only request size whitelist, retaining explicit zeros and unavailable request measurements', () => {
    const summary = usageSummary([event(1, { token_usage: 'unavailable', request_sizes: {
      prompt_utf8_bytes: 0, history_json_bytes: 200, tools_json_bytes: 30, history_omitted_groups: 0,
      prompt: 999, total_bytes: 999, private: 'secret',
    } })])
    expect(summary.calls[0].requestSizes).toEqual({ prompt: 0, history: 200, tools: 30, omittedGroups: 0 })
    expect(summary.reportedTokens).toBeNull()
  })

  test.each(invalid.map(value => [value]))('rejects invalid request size values: %j', value => {
    const call = usageSummary([reported(1, 1, { request_sizes: { prompt_utf8_bytes: value, history_json_bytes: value,
      tools_json_bytes: value, history_omitted_groups: value } })]).calls[0]
    expect(call.requestSizes).toEqual(noSizes)
  })

  test.each([null, true, 1, 'secret', [], [{ total_tokens: 8 }]])('malformed nested objects do not throw or leak: %j', value => {
    const summary = usageSummary([reported(1, 8, { usage: value, request_sizes: value }),
      reported(2, 8, { usage: { prompt_tokens: 7, completion_tokens: 1, prompt_tokens_details: value, completion_tokens_details: value } }),
      event(3, value as unknown as Record<string, unknown>)])
    expect(summary.calls[0]).toMatchObject({ tokens: 8, ...noDetails, requestSizes: noSizes })
    expect(summary.calls[1]).toMatchObject({ input: 7, output: 1, cacheRead: null, reasoning: null })
    expect(summary.calls[2]).toMatchObject({ source: 'unspecified', tokens: null, role: 'unknown' })
  })

  test.each(['', '   ', null, [], 12, true])('invalid roles become unknown and invalid drivers are omitted: %j', value => {
    const call = usageSummary([reported(1, 1, { role: value, driver: value })]).calls[0]
    expect(call.role).toBe('unknown')
    expect(Object.hasOwn(call, 'driver')).toBe(false)
  })

  test('raw markup strings stay plain labels and private blobs never enter the projection', () => {
    const role = '<img src=x onerror=alert(1)>', driver = '<script>driver</script>'
    const summary = usageSummary([reported(1, 10, { role, driver, provider: 'secret', private_thinking: 'secret',
      usage: { prompt_tokens: '8', completion_tokens: '2', prompt: 8, output: 2,
        provider_private_blob: { input_tokens: 8, output_tokens: 2, thought: 'secret' } },
      request_sizes: { private_blob: 'secret', prompt: 100 } })])
    expect(summary.calls[0]).toEqual({ seq: 1, role, driver, source: 'reported', tokens: 10, ...noDetails, requestSizes: noSizes })
    expect(JSON.stringify(summary)).not.toContain('secret')
    expect(Object.hasOwn(summary.calls[0], 'payload')).toBe(false)
  })

  test('inherited properties cannot forge usage sources, totals, details or request measurements', () => {
    const payload = Object.create({ token_usage: 'reported', tokens: 999, role: 'forged' }) as Record<string, unknown>
    payload.usage = Object.create({ total_tokens: 20, prompt_tokens: 19, completion_tokens: 1 })
    payload.request_sizes = Object.create({ prompt_utf8_bytes: 100 })
    const summary = usageSummary([event(1, payload)])
    expect(summary.calls[0]).toMatchObject({ role: 'unknown', source: 'unspecified', tokens: null, ...noDetails, requestSizes: noSizes })
  })

  test('accepts frozen events and never mutates or retains references to their payloads', () => {
    const usage = Object.freeze({ total_tokens: 10, prompt_tokens: 8, completion_tokens: 2,
      prompt_tokens_details: Object.freeze({ cached_tokens: 3 }) })
    const sizes = Object.freeze({ prompt_utf8_bytes: 10, history_json_bytes: 20 })
    const events = [reported(2, 10, { usage, request_sizes: sizes }), reported(1, 0)]
    const before = structuredClone(events)
    events.forEach(item => { Object.freeze(item.actor); Object.freeze(item.payload); Object.freeze(item) })
    Object.freeze(events)
    const summary = usageSummary(events)
    expect(events).toEqual(before)
    expect(events.map(item => item.seq)).toEqual([2, 1])
    summary.calls[1].requestSizes.prompt = 900
    summary.calls[1].input = 900
    expect(usageSummary(events).calls[1]).toMatchObject({ input: 8, requestSizes: { prompt: 10, history: 20 } })
    expect(events).toEqual(before)
  })
})
