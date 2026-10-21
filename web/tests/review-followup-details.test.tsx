import { render } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'

const fixture = (): DetailEvent => ({ seq: 80, type: 'team.review_followup_requested',
  actor: { id: 'team', kind: 'system', role: 'team' },
  payload: { review_seq: 20, message_seq: 21, model_call_seq: 78, role: 'test', run_id: 'RUN',
    unexpected: 'PRIVATE_EXTRA' } })

test('复核补登记提醒只展示中文关联事实，不冒充批准或新调用成功', () => {
  const { container } = render(<EventDetails event={fixture()} actorName={id => id === 'test' ? '三角' : id} />)
  const content = container.textContent ?? ''
  for (const text of ['复核结论补登记提醒', '已记录一次补登记提醒', '@三角', '复核请求20', '原复核委托21',
    '关联模型返回事件78', '不证明后续模型请求已经发出']) expect(content).toContain(text)
  expect(content).not.toContain('PRIVATE_EXTRA')
})

test.each(['actor', 'review', 'message', 'model', 'role', 'run', 'sequence'])('无效补登记提醒不背书：%s', field => {
  const value = fixture()
  if (field === 'actor') value.actor.kind = 'agent'
  else if (field === 'review') value.payload.review_seq = 21
  else if (field === 'message') value.payload.message_seq = 78
  else if (field === 'model') value.payload.model_call_seq = 80
  else if (field === 'role') value.payload.role = ''
  else if (field === 'run') value.payload.run_id = null
  else value.seq = Number.NaN
  const { container } = render(<EventDetails event={value} />)
  const content = container.textContent ?? ''
  expect(content).toContain('补登记提醒待核对')
  expect(content).not.toMatch(/已记录一次补登记提醒|PRIVATE_EXTRA/)
})
