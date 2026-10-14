import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { DeliveryPanel } from '../src/DeliveryPanel'
import type { DeliveryState } from '../src/DeliveryPanel'

const accepted: DeliveryState = {
  status: 'accepted', reason: '当前交付已通过', digest: 'fixture', reviewer: 'store_manager',
  manifest: { sources: [{ id: 'goal', text: '交付中文活动说明' }], files: [{ path: 'work-1/result.md', artifact_seq: 7 }] },
  requirements: [{ id: 'goal', status: 'passed', reason: '正文覆盖了活动目标', evidence: [{ artifact_seq: 7, quote: '活动说明' }] }],
  handoff: { summary: '活动说明整理好了', why: '先讲目标，再给可复查的安排', reproduce: ['核对活动日期'], limits: ['尚未核对场地容量'] },
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(new Blob(['fixture-archive']), { headers: { 'Content-Type': 'application/zip' } })))
})

test('仅有效交付显示下载入口，并提供选择原因、复查步骤及未覆盖范围', () => {
  render(<DeliveryPanel taskId="task" delivery={accepted} names={{ store_manager: '店长' }} />)
  expect(screen.getByRole('button', { name: '下载交付包' })).toBeTruthy()
  expect(screen.getByText('整体复核：店长')).toBeTruthy()
  fireEvent.click(screen.getByText('为什么这样做 · 如何复查'))
  expect(screen.getByText('先讲目标，再给可复查的安排')).toBeTruthy()
  expect(screen.getByText('核对活动日期')).toBeTruthy()
  expect(screen.getByText('尚未核对场地容量')).toBeTruthy()
  fireEvent.click(screen.getByText('逐条要求的核对依据'))
  expect(screen.getByText('交付中文活动说明')).toBeTruthy()
  expect(screen.getByText('“活动说明” · 成果 #7')).toBeTruthy()
})

test.each(['pending', 'reviewing', 'uncertain', 'stale', 'invalid', 'needs_evidence', 'rework'])('状态 %s 不沿用旧交接或下载能力', (status) => {
  render(<DeliveryPanel taskId="task" delivery={{ ...accepted, status }} names={{}} />)
  expect(screen.queryByRole('button', { name: '下载交付包' })).toBeNull()
  expect(screen.queryByText('活动说明整理好了')).toBeNull()
})

test('下载携带访问令牌而不放入地址，阻止并发点击', async () => {
  let done!: (response: Response) => void
  const fetch = vi.fn(() => new Promise<Response>(resolve => { done = resolve }))
  vi.stubGlobal('fetch', fetch)
  const create = vi.fn(() => 'blob:fixture')
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  render(<DeliveryPanel taskId="task" delivery={accepted} names={{}} accessToken="fixture-token" />)
  const button = screen.getByRole('button', { name: '下载交付包' })
  fireEvent.click(button); fireEvent.click(button)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch).toHaveBeenCalledWith('/api/tasks/task/delivery', { headers: { 'X-Cater-Access-Token': 'fixture-token' } })
  expect((button as HTMLButtonElement).disabled).toBe(true)
  done(new Response(new Blob(['fixture-archive'])))
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
})

test('服务拒绝过期下载时显示版本变化，不伪装成功', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('stale', { status: 409 })))
  render(<DeliveryPanel taskId="task" delivery={accepted} names={{}} />)
  fireEvent.click(screen.getByRole('button', { name: '下载交付包' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '交付版本已变化，请重新核对。')
})
