import { isTeamHost } from './team'
import type { TeamEvent } from './team'

export type UsageSource = 'reported' | 'estimated' | 'unavailable' | 'unspecified'

export type UsageCall = {
  seq: number
  role: string
  driver?: string
  source: UsageSource
  tokens: number | null
  input: number | null
  output: number | null
  inputKind: 'prompt_inclusive' | 'uncached' | null
  cacheRead: number | null
  cacheWrite: number | null
  reasoning: number | null
  requestSizes: { prompt: number | null; history: number | null; tools: number | null; omittedGroups: number | null }
}

export type UsageGroup = {
  role: string
  calls: number
  reportedCalls: number
  reportedTokens: number | null
  estimatedCalls: number
  estimatedTokens: number | null
  unavailableCalls: number
  unspecifiedCalls: number
}

export type UsageSummary = {
  calls: UsageCall[]
  groups: UsageGroup[]
  reportedCalls: number
  reportedTokens: number | null
  estimatedCalls: number
  estimatedTokens: number | null
  unavailableCalls: number
  unspecifiedCalls: number
  detailCalls: number
  asOfSeq: number
}

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const field = (value: Record<string, unknown>, key: string): unknown => Object.hasOwn(value, key) ? value[key] : undefined
const count = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
const label = (value: unknown): string | undefined => typeof value === 'string' && value.trim() ? value : undefined
const emptyDetails = () => ({ input: null, output: null, inputKind: null, cacheRead: null, cacheWrite: null, reasoning: null })
const emptySizes = () => ({ prompt: null, history: null, tools: null, omittedGroups: null })

function project(event: TeamEvent): UsageCall {
  const p = record(event.payload), usage = record(field(p, 'usage'))
  const primary = count(field(p, 'tokens')), legacy = count(field(usage, 'total_tokens'))
  const conflictingTotal = primary !== null && legacy !== null && primary !== legacy
  const total = conflictingTotal ? null : primary ?? legacy
  const rawSource = field(p, 'token_usage')
  const source: UsageSource = conflictingTotal ? 'unavailable' : rawSource === undefined ? 'unspecified'
    : total !== null && (rawSource === 'reported' || rawSource === 'estimated') ? rawSource : 'unavailable'
  const call: UsageCall = {
    seq: event.seq, role: label(field(p, 'role')) ?? 'unknown',
    ...(label(field(p, 'driver')) !== undefined ? { driver: label(field(p, 'driver')) } : {}),
    source, tokens: source === 'unavailable' ? null : total, ...emptyDetails(), requestSizes: emptySizes(),
  }
  const sizes = record(field(p, 'request_sizes'))
  call.requestSizes = {
    prompt: count(field(sizes, 'prompt_utf8_bytes')), history: count(field(sizes, 'history_json_bytes')),
    tools: count(field(sizes, 'tools_json_bytes')), omittedGroups: count(field(sizes, 'history_omitted_groups')),
  }
  if (total === null || (source !== 'reported' && source !== 'unspecified')) return call

  // Presence of both input keys is ambiguous, even if one value is malformed.
  const prompt = Object.hasOwn(usage, 'prompt_tokens'), input = Object.hasOwn(usage, 'input_tokens')
  if (prompt && !input) {
    call.input = count(field(usage, 'prompt_tokens'))
    call.inputKind = call.input === null ? null : 'prompt_inclusive'
  } else if (input && !prompt) {
    call.input = count(field(usage, 'input_tokens'))
    call.inputKind = call.input === null ? null : 'uncached'
  }
  const completion = Object.hasOwn(usage, 'completion_tokens'), output = Object.hasOwn(usage, 'output_tokens')
  if (completion !== output) call.output = count(field(usage, completion ? 'completion_tokens' : 'output_tokens'))
  const promptDetails = record(field(usage, 'prompt_tokens_details'))
  const cached = count(field(promptDetails, 'cached_tokens'))
  const cacheHit = count(field(usage, 'prompt_cache_hit_tokens'))
  const openAICache = Object.hasOwn(promptDetails, 'cached_tokens') || Object.hasOwn(usage, 'prompt_cache_hit_tokens')
  const anthropicCache = Object.hasOwn(usage, 'cache_read_input_tokens') || Object.hasOwn(usage, 'cache_creation_input_tokens')
  if (prompt && !input || !prompt && !input && openAICache && !anthropicCache) {
    call.cacheRead = cached !== null && cacheHit !== null && cached !== cacheHit ? null : cached ?? cacheHit
  } else if (input && !prompt || !prompt && !input && anthropicCache && !openAICache) {
    call.cacheRead = count(field(usage, 'cache_read_input_tokens'))
    call.cacheWrite = count(field(usage, 'cache_creation_input_tokens'))
  }
  call.reasoning = count(field(record(field(usage, 'completion_tokens_details')), 'reasoning_tokens'))
  return call
}

const newGroup = (role: string): UsageGroup => ({
  role, calls: 0, reportedCalls: 0, reportedTokens: null, estimatedCalls: 0, estimatedTokens: null,
  unavailableCalls: 0, unspecifiedCalls: 0,
})

function accumulate(group: UsageGroup, call: UsageCall) {
  group.calls++
  if ((call.source === 'reported' || call.source === 'estimated') && call.tokens !== null) {
    const calls = call.source === 'reported' ? 'reportedCalls' : 'estimatedCalls'
    const tokens = call.source === 'reported' ? 'reportedTokens' : 'estimatedTokens'
    // A null sum after the first valid call means overflow; it must stay null.
    group[tokens] = group[calls] === 0 ? call.tokens
      : group[tokens] === null ? null : count(group[tokens] + call.tokens)
    group[calls]++
  } else if (call.source === 'unspecified') group.unspecifiedCalls++
  else group.unavailableCalls++
}

export function usageSummary(events: TeamEvent[]): UsageSummary {
  const bySeq = new Map<number, UsageCall>()
  let asOfSeq = 0
  for (const event of events) {
    if (!event || typeof event !== 'object' || Array.isArray(event)) continue
    const seq = count(event.seq)
    if (seq === null || seq === 0) continue
    asOfSeq = Math.max(asOfSeq, seq)
    if (event.type !== 'team.model_called' || !event.actor || !isTeamHost(event)) continue
    const call = project(event), previous = bySeq.get(seq)
    if (!previous) bySeq.set(seq, call)
    else if (JSON.stringify(previous) !== JSON.stringify(call)) {
      // Compare only public projections. Conflicting receipts never choose a winner.
      bySeq.set(seq, {
        seq, role: previous.role === call.role ? call.role : 'unknown',
        ...(previous.driver !== undefined && previous.driver === call.driver ? { driver: call.driver } : {}),
        source: 'unavailable', tokens: null, ...emptyDetails(), requestSizes: emptySizes(),
      })
    }
  }
  const calls = [...bySeq.values()].sort((a, b) => a.seq - b.seq)
  const byRole = new Map<string, UsageGroup>(), totals = newGroup('unknown')
  let detailCalls = 0
  for (const call of calls) {
    const group = byRole.get(call.role) ?? newGroup(call.role)
    accumulate(group, call)
    accumulate(totals, call)
    byRole.set(call.role, group)
    if (call.source === 'reported' && (call.input !== null || call.output !== null)) detailCalls++
  }
  const groups = [...byRole.values()].sort((a, b) => {
    if (a.reportedTokens !== b.reportedTokens) {
      if (a.reportedTokens === null) return 1
      if (b.reportedTokens === null) return -1
      return b.reportedTokens - a.reportedTokens
    }
    return a.role < b.role ? -1 : a.role > b.role ? 1 : 0
  })
  return {
    calls, groups, reportedCalls: totals.reportedCalls, reportedTokens: totals.reportedTokens,
    estimatedCalls: totals.estimatedCalls, estimatedTokens: totals.estimatedTokens,
    unavailableCalls: totals.unavailableCalls, unspecifiedCalls: totals.unspecifiedCalls, detailCalls, asOfSeq,
  }
}
