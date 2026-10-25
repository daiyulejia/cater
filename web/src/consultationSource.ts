import { recordedArguments } from './auditArguments'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const revision = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const seq = (value: unknown): value is number => revision(value) && value > 0
const keys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every(key => allowed.includes(key))
const human = (event: TeamEvent) => event.actor.kind === 'human' && event.actor.id === 'human' && event.actor.role === 'human'
const agent = (event: TeamEvent) => event.actor.kind === 'agent' && text(event.actor.id) && text(event.actor.role)
const atomic = (value: unknown) => record(value) && Object.keys(value).length === 3
  && value.version === 1 && value.kind === 'atomic_local_events' && value.receipt === 'same_transaction'
const ordinary = (event: TeamEvent) => !('review_seq' in event.payload) && !('continue_assignment_seq' in event.payload)
  && (event.payload.parent_assignment_seq == null || seq(event.payload.parent_assignment_seq))

type Pair = { start: TeamEvent; finish: TeamEvent; run: TeamEvent; input: TeamEvent; args: Record<string, unknown>; result: Record<string, unknown> }

// The task API supplies a single-group snapshot. No network, current UI state, or role claims can repair a missing receipt.
export function consultationSources(events: readonly TeamEvent[]) {
  const ordered = [...events].sort((a, b) => a.seq - b.seq)
  const bySeq = new Map<number, TeamEvent>()
  let valid = true
  const groups = new Set<unknown>()
  for (const event of ordered) {
    if (!seq(event.seq) || bySeq.has(event.seq)) valid = false
    bySeq.set(event.seq, event)
    const group = (event as TeamEvent & { task_id?: unknown }).task_id
    if (group !== undefined) {
      if (!text(group)) valid = false
      groups.add(group)
    }
  }
  if (groups.size > 1) valid = false
  const hosts = ordered.filter(isTeamHost)
  const scopeAt = (before: number) => hosts.findLast(event => event.seq < before
    && ['team.objective_updated', 'team.objective_completed'].includes(event.type))?.seq ?? 0

  const runFor = (id: unknown, role: string, before: number) => {
    if (!valid || !text(id)) return undefined
    const rows = hosts.filter(event => event.type === 'team.run_started' && event.payload.run_id === id)
    if (rows.length !== 1) return undefined
    const run = rows[0], p = run.payload
    const input = seq(p.message_seq) ? bySeq.get(p.message_seq) : undefined
    const ends = hosts.filter(event => event.type === 'team.run_finished' && event.payload.run_id === id)
    if (p.role !== role || !revision(p.revision) || !input || input.type !== 'team.message_received'
      || input.payload.recipient !== role || !text(input.payload.text)
      || !(input.seq < run.seq && run.seq < before) || ends.length > 1
      || ends.some(end => end.seq <= run.seq || end.seq < before
        || ['role', 'message_seq'].some(key => end.payload[key] !== p[key]))) return undefined
    return { run, input }
  }

  const pairFor = (started: unknown): Pair | undefined => {
    const start = seq(started) ? bySeq.get(started) : undefined
    if (!valid || !start || !isTeamHost(start) || start.type !== 'team.tool_started') return undefined
    const p = start.payload
    if (!text(p.role) || !text(p.name) || !text(p.call_id) || !seq(p.message_seq) || !revision(p.revision)
      || !atomic(p.execution_contract)) return undefined
    const binding = runFor(p.run_id, p.role, start.seq)
    const rows = hosts.filter(event => event.type === 'team.tool_finished' && event.payload.started_seq === start.seq)
    if (!binding || rows.length !== 1) return undefined
    const finish = rows[0], f = finish.payload, args = recordedArguments(p.arguments), result = f.result
    if (binding.run.payload.message_seq !== p.message_seq || binding.run.payload.revision !== p.revision
      || !(start.seq < finish.seq) || !runFor(p.run_id, p.role, finish.seq)
      || ['name', 'role', 'run_id'].some(key => f[key] !== p[key])
      || ['call_id', 'message_seq', 'revision'].some(key => key in f && f[key] !== p[key])
      || !record(args) || !record(result) || result.status !== 'ok') return undefined
    return { start, finish, run: binding.run, input: binding.input, args, result }
  }

  const inputs = new Map<number, { input: TeamEvent; receiptSeq: number } | undefined>()
  const messageInput = (message: TeamEvent, before = Infinity): TeamEvent | undefined => {
    if (inputs.has(message.seq)) {
      const cached = inputs.get(message.seq)
      return cached && cached.receiptSeq < before ? cached.input : undefined
    }
    inputs.set(message.seq, undefined)
    if (!agent(message) || message.type !== 'team.message_received'
      || !(ordinary(message) || message.payload.kind === 'continuation' && !('review_seq' in message.payload)
        && seq(message.payload.continue_assignment_seq)
        && (message.payload.parent_assignment_seq == null || seq(message.payload.parent_assignment_seq)))
      || !revision(message.payload.revision) || !text(message.payload.text)) return undefined
    const receipts = hosts.filter(event => event.type === 'team.tool_finished' && record(event.payload.result)
      && event.payload.result.message_seq === message.seq)
    if (receipts.length !== 1) return undefined
    const pair = pairFor(receipts[0].payload.started_seq)
    if (!pair || pair.finish !== receipts[0]) return undefined
    const { start, finish, run, input, args, result } = pair, p = start.payload, m = message.payload
    if (!(start.seq < message.seq && message.seq < finish.seq) || p.role !== message.actor.id
      || p.revision !== m.revision || scopeAt(start.seq) !== p.revision
      || message.seq <= p.revision || ('recipient' in result && result.recipient !== m.recipient)
      || ('notified_only' in result && result.notified_only !== false)
      || ('already_reported' in result && result.already_reported !== false)
      || 'resolved_reply_to' in result) return undefined
    const body = args[p.name === 'delegate' || p.name === 'continue_work' ? 'instruction' : 'text']
    if (!text(body) || body.trim() !== m.text || !text(m.recipient) || m.recipient === message.actor.id) return undefined
    if (p.name === 'delegate') {
      if (!keys(args, ['recipient', 'instruction']) || args.recipient !== m.recipient
        || m.kind !== 'assignment' || m.reply_to != null) return undefined
    } else if (p.name === 'return_work') {
      if (!keys(args, ['text']) || m.kind !== 'report' || !seq(m.reply_to)) return undefined
    } else if (p.name === 'continue_work') {
      if (!keys(args, ['assignment_seq', 'instruction']) || !seq(args.assignment_seq)
        || m.kind !== 'continuation' || args.assignment_seq !== m.continue_assignment_seq || m.reply_to != null) return undefined
    } else if (p.name === 'send_message') {
      if (!keys(args, ['recipient', 'text', 'reply_to', 'notify_only', 'continue_assignment_seq']) || args.recipient !== m.recipient
        || ('notify_only' in args && args.notify_only !== false)
        || (m.kind === 'continuation' ? !seq(args.continue_assignment_seq) || args.continue_assignment_seq !== m.continue_assignment_seq
          || 'reply_to' in args || m.reply_to != null
          : 'continue_assignment_seq' in args || (m.kind === 'report' ? !seq(args.reply_to) || args.reply_to !== m.reply_to
            : m.kind !== 'message' || 'reply_to' in args || m.reply_to != null))) return undefined
    } else return undefined
    if (!(human(input) && input.payload.kind === 'message') && !messageInput(input, run.seq)) return undefined
    const question = human(input) ? questionContext(input, String(p.role), start.seq) : undefined
    if (human(input) && !question) return undefined
    const assignment = human(input) ? question?.assignment : activeAssignment(input, String(p.role), start.seq)
    if ((m.parent_assignment_seq ?? null) !== (assignment?.seq ?? null)
      || p.name === 'return_work' && (!assignment || assignment.seq !== m.reply_to)) return undefined
    if (m.kind === 'continuation') {
      const original = seq(m.continue_assignment_seq) ? bySeq.get(m.continue_assignment_seq) : undefined
      if (!original || original.actor.id !== message.actor.id || original.actor.role !== message.actor.role
        || original.payload.recipient !== m.recipient || !openRequest(original, start.seq)
        || !messageInput(original, start.seq)) return undefined
    }
    if (m.kind === 'report') {
      const request = seq(m.reply_to) ? bySeq.get(m.reply_to) : undefined
      if (!request || request.actor.id !== m.recipient || request.payload.recipient !== message.actor.id
        || !['assignment', 'message'].includes(String(request.payload.kind)) || request.payload.revision !== m.revision
        || !messageInput(request, start.seq)) return undefined
    }
    inputs.set(message.seq, { input, receiptSeq: finish.seq })
    return finish.seq < before ? input : undefined
  }

  const reported = (request: TeamEvent, before: number) => ordered.some(event => event.seq < before
    && event.seq > request.seq && event.type === 'team.message_received' && event.payload.kind === 'report'
    && event.payload.reply_to === request.seq && event.actor.id === request.payload.recipient
    && event.payload.recipient === request.actor.id && event.payload.revision === request.payload.revision
    && messageInput(event, before))
  const openRequest = (request: TeamEvent, before: number) => request.seq < before && agent(request)
    && request.type === 'team.message_received' && ordinary(request)
    && ['assignment', 'message'].includes(String(request.payload.kind)) && request.payload.revision === scopeAt(before)
    && !reported(request, before) && !hosts.some(event => event.seq < before && event.seq > request.seq
      && event.type === 'team.assignment_withdrawn' && event.payload.message_seq === request.seq
      && event.payload.revision === request.payload.revision)

  const questionContext = (input: TeamEvent, role: string, before: number): { assignment?: TeamEvent } | undefined => {
    const response = input.payload.questionnaire
    if (response != null && !record(response) || record(response) && !seq(response.question_seq)) return undefined
    const question = hosts.findLast(event => event.seq < input.seq && event.seq < before
      && event.type === 'team.questions_requested' && event.payload.role_id === role
      && (!record(response) || event.seq === response.question_seq))
    if (!question) return record(response) ? undefined : {}
    if (!record(response)) {
      const inputs = ordered.filter(event => event.seq > question.seq && event.seq < input.seq && human(event)
        && event.type === 'team.message_received' && event.payload.recipient === role)
      if (hosts.some(end => end.seq < input.seq && end.type === 'team.run_finished'
        && ['completed', 'waiting'].includes(String(end.payload.status)) && hosts.some(run => run.type === 'team.run_started'
          && run.seq < end.seq && run.payload.role === role && inputs.some(event => event.seq === run.payload.message_seq)
          && ['run_id', 'role', 'message_seq'].every(key => end.payload[key] === run.payload[key])))) return {}
    }
    const receipts = hosts.filter(event => event.seq < input.seq && event.seq < before && event.type === 'team.tool_finished'
      && record(event.payload.result) && event.payload.result.question_seq === question.seq)
    if (receipts.length !== 1) return undefined
    const pair = pairFor(receipts[0].payload.started_seq), q = question.payload
    if (!pair || pair.start.payload.name !== 'request_questions' || pair.start.payload.role !== role
      || !(pair.start.seq < question.seq && question.seq < pair.finish.seq)
      || q.message_seq !== pair.input.seq || q.run_id !== pair.start.payload.run_id || q.revision !== pair.start.payload.revision
      || !keys(pair.args, ['title', 'questions']) || !text(pair.args.title) || pair.args.title.trim() !== q.title
      || !Array.isArray(pair.args.questions) || !Array.isArray(q.questions)) return undefined
    if (human(pair.input) && !questionContext(pair.input, role, pair.start.seq)) return undefined
    const assignment = activeAssignment(pair.input, role, pair.start.seq)
    if ((assignment?.seq ?? null) !== (q.assignment_seq ?? null)) return undefined
    return { assignment: assignment && openRequest(assignment, before) ? assignment : undefined }
  }

  // Match the host's historical _waiting_assignment branches, authenticating every inherited link.
  const activeAssignment = (input: TeamEvent, role: string, before = Infinity): TeamEvent | undefined => {
    if (!valid || bySeq.get(input.seq) !== input || input.seq >= before || input.type !== 'team.message_received'
      || input.payload.recipient !== role) return undefined
    if (human(input)) return questionContext(input, role, before)?.assignment
    if (!messageInput(input, before)) return undefined
    const p = input.payload
    if (p.kind === 'continuation') {
      const original = seq(p.continue_assignment_seq) ? bySeq.get(p.continue_assignment_seq) : undefined
      return original && original.actor.id === input.actor.id && original.actor.role === input.actor.role
        && original.payload.recipient === role && openRequest(original, before) ? original : undefined
    }
    if (['assignment', 'message'].includes(String(p.kind))) return openRequest(input, before) ? input : undefined
    if (p.kind !== 'report') return undefined
    const child = seq(p.reply_to) ? bySeq.get(p.reply_to) : undefined
    const parent = child && seq(child.payload.parent_assignment_seq) ? bySeq.get(child.payload.parent_assignment_seq) : undefined
    return child && parent && child.actor.id === role && child.payload.recipient === input.actor.id
      && parent.payload.recipient === role && parent.seq < child.seq && openRequest(parent, before)
      && messageInput(child, before) && messageInput(parent, before) ? parent : undefined
  }

  const customerSource = (input: TeamEvent, role: string, before = Infinity, roleName?: string): TeamEvent | undefined => {
    if (!valid || bySeq.get(input.seq) !== input || input.seq >= before || input.type !== 'team.message_received'
      || input.payload.recipient !== role || !text(input.payload.text)) return undefined
    // Direct Human replies retain their existing lifetime and validation contract.
    if (human(input) && input.payload.kind === 'message') return input
    const currentRevision = scopeAt(before)
    let current: TeamEvent | undefined = input
    let sourceRole = roleName
    while (current && agent(current)) {
      const m = current.payload
      const request = seq(m.reply_to) ? bySeq.get(m.reply_to) : undefined
      if (m.kind !== 'report' || !ordinary(current)
        || !request || request.type !== 'team.message_received' || !ordinary(request)
        || !agent(request) || request.actor.id !== role || sourceRole !== undefined && request.actor.role !== sourceRole
        || !['assignment', 'message'].includes(String(request.payload.kind)) || request.seq >= current.seq
        || request.payload.recipient !== current.actor.id || m.recipient !== request.actor.id
        || !revision(m.revision) || m.revision !== currentRevision || request.payload.revision !== currentRevision
        || request.payload.parent_assignment_seq != null || !messageInput(current, before)) return undefined
      // Bind this consultation's sender to its answer, without freezing unrelated historical titles.
      sourceRole ??= request.actor.role
      current = messageInput(request, before)
      if (current && current.payload.recipient !== role) return undefined
    }
    return current && human(current) && current.payload.kind === 'message' && text(current.payload.text)
      && current.seq > currentRevision && !ordered.some(event => event.seq < before && event.seq > current!.seq
        && event.type === 'team.message_received' && human(event) && event.payload.recipient === role) ? current : undefined
  }

  const answerSource = (answer: TeamEvent, current = true): TeamEvent | undefined => {
    const a = answer.payload
    if (!valid || bySeq.get(answer.seq) !== answer || answer.type !== 'msg.said' || !agent(answer)
      || a.phase !== 'team' || !text(a.text) || a.response_kind !== 'reply' || a.audience !== 'customer'
      || a.recipient !== 'human' || !seq(a.reply_to)) return undefined
    const binding = runFor(a.run_id, answer.actor.id, answer.seq)
    if (!binding || binding.input.payload.kind !== 'report' || binding.run.payload.revision !== binding.input.payload.revision) return undefined
    let replyPair: Pair | undefined
    if ('customer_reply_tool' in a) {
      if (a.customer_reply_tool !== 'reply_to_customer') return undefined
      const receipts = hosts.filter(event => event.type === 'team.tool_finished' && record(event.payload.result)
        && event.payload.result.message_seq === answer.seq)
      if (receipts.length !== 1) return undefined
      const pair = pairFor(receipts[0].payload.started_seq)
      if (!pair || pair.start.payload.name !== 'reply_to_customer' || pair.start.payload.run_id !== a.run_id
        || pair.input !== binding.input || !(pair.start.seq < answer.seq && answer.seq < pair.finish.seq)
        || !keys(pair.args, ['text', 'evidence']) || !text(pair.args.text)
        || Array.from(pair.args.text).length > 32000 || pair.args.text.trim() !== a.text
        || ('reply_evidence_started_seq' in a && a.reply_evidence_started_seq !== pair.start.seq)) return undefined
      replyPair = pair
    } else if ('reply_evidence_started_seq' in a || hosts.some(event => event.type === 'team.tool_started'
      && event.seq < answer.seq && event.payload.run_id === a.run_id && event.payload.name === 'reply_to_customer')) return undefined
    const source = customerSource(binding.input, answer.actor.id, replyPair?.start.seq ?? answer.seq, answer.actor.role)
    if (!source || source.seq !== a.reply_to || replyPair && replyPair.result.reply_to !== source.seq
      || current && customerSource(binding.input, answer.actor.id, Infinity, answer.actor.role) !== source) return undefined
    return source
  }
  return { customerSource, answerSource, activeAssignment, messageInput }
}
