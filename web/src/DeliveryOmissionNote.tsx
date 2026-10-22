import type { TeamEvent } from './team'
import { deliveryOmissionReason } from './deliveryOmission'

export function DeliveryOmissionNote({ event, historical = false }: { event: TeamEvent; historical?: boolean }) {
  const reason = deliveryOmissionReason(event)
  if (reason === undefined) return null
  return <section className="mt-2 min-w-0 space-y-1 leading-5 [overflow-wrap:anywhere]">
    <h3 className="font-semibold">{historical ? '历史交付：本次未附已复核成果' : '本次未附已复核成果'}</h3>
    <p>店长说明</p>
    <p className="whitespace-pre-wrap">{reason}</p>
  </section>
}
