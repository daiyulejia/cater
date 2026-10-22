import { isTeamHost } from './team'
import type { TeamEvent } from './team'

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object'
  && !Array.isArray(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value))

export function deliveryOmissionReason(event: TeamEvent): string | undefined {
  if (!record(event) || !['actor', 'type', 'seq', 'payload'].every(key => Object.hasOwn(event, key))
    || event.type !== 'team.objective_completed' || !Number.isSafeInteger(event.seq) || event.seq <= 0
    || !record(event.actor) || !['id', 'kind', 'role'].every(key => Object.hasOwn(event.actor, key)) || !isTeamHost(event)
    || !record(event.payload) || !Object.hasOwn(event.payload, 'omitted_approved_artifacts_reason')) return undefined
  const reason = event.payload.omitted_approved_artifacts_reason
  // Match the backend's character limit, including supplementary Unicode characters.
  return typeof reason === 'string' && reason.replace(/[\u001c-\u001f\u0085]/g, ' ').trim().length > 0
    && Array.from(reason).length <= 1000 && !reason.includes('\0')
    && !/[\uD800-\uDFFF]/u.test(reason)
    ? reason : undefined
}
