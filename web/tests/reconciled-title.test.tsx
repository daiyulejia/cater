import { render } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails, EventDetailsList } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'

const event = (name: string, changes: Record<string, unknown> = {}): DetailEvent => ({
  seq: 12, type: 'team.tool_reconciled', actor: { id: 'human', role: 'human', kind: 'human' },
  payload: { name, role: 'dev', run_id: 'run', call_id: 'call', started_seq: 10, started_hash: 'a'.repeat(64),
    invocation_stopped: true, outcome: 'effects_reviewed_no_replay',
    arguments: { input: 'PRIVATE_ARGUMENT' }, work_item_id: 'PRIVATE_WORK_ITEM', reason: '检查文件 <img src=x onerror=alert(1)>',
    extra: 'PRIVATE_EXTRA', ...changes },
})

test.each(['publish_artifact', 'publish_workspace_archive', 'browser_check', 'call_mcp_tool', 'unknown'])
  ('可识别人类回执保留未经验证的文本说明：%s', name => {
    const { container } = render(<EventDetails event={event(name)} />)
    expect(container.querySelector('h3')?.textContent).toBe('操作核对记录')
    expect(container.textContent).toContain('核对声明，需结合原始记录确认')
    expect(container.textContent).toContain('操作者说明，未经独立验证')
    expect(container.textContent).toContain('检查文件 <img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).not.toMatch(/PRIVATE_ARGUMENT|PRIVATE_EXTRA|PRIVATE_WORK_ITEM/)
    expect(container.textContent).not.toMatch(/实际影响已核对|发布影响已核对|后续不重放原请求|已停止/)
  })

test.each([
  { started_seq: null }, { started_seq: -1 }, { started_hash: 'bad' }, { role: '' },
  { run_id: null }, { call_id: '' }, { invocation_stopped: false }, { outcome: 'unknown' },
  { name: null }, { reason: '' },
])('来源结构缺失或冲突显示待核对：%j', changes => {
  const receipt = event('browser_check', changes)
  const { container } = render(<EventDetailsList events={[receipt]} />)
  expect(container.querySelector('h3')?.textContent).toBe('操作核对记录')
  expect(container.textContent).toContain('记录来源待核对')
  expect(container.textContent).not.toContain('操作者说明，未经独立验证')
  expect(container.textContent).not.toMatch(/PRIVATE_ARGUMENT|PRIVATE_EXTRA|实际影响已核对/)
})

test.each([
  { id: 'other', role: 'human', kind: 'human' }, { id: 'human', role: 'other', kind: 'human' },
  { id: 'human', role: 'human', kind: 'agent' },
])('非人类回执身份不展示说明：%j', actor => {
  const receipt = event('call_mcp_tool')
  receipt.actor = actor
  const { container } = render(<EventDetails event={receipt} />)
  expect(container.textContent).toContain('记录来源待核对')
  expect(container.textContent).not.toContain('检查文件')
  expect(container.textContent).not.toMatch(/PRIVATE_ARGUMENT|PRIVATE_EXTRA/)
})

test('展开完整操作者说明而不改变原事件', () => {
  const reason = '字'.repeat(3999) + '末'
  const receipt = event('publish_artifact', { reason })
  const { container } = render(<EventDetails event={receipt} />)
  const details = Array.from(container.querySelectorAll('details')).find(item => item.querySelector('summary')?.textContent === '操作者说明，未经独立验证')!
  expect(details.open).toBe(false)
  expect(details.querySelector('p')?.textContent).toBe(reason)
  expect(receipt.payload.reason).toBe(reason)
})
