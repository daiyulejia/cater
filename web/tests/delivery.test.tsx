import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { DeliveryPanel } from '../src/DeliveryPanel'
import type { DeliveryState } from '../src/DeliveryPanel'

const accepted: DeliveryState = {
  status: 'accepted', reason: '当前交付已通过', digest: 'fixture', reviewer: 'store_manager',
  manifest: { sources: [{ id: 'goal', text: '交付中文活动说明' }], files: [{ path: 'work-1/result.md', artifact_seq: 7 }] },
  requirements: [{ id: 'goal', status: 'passed', reason: '正文覆盖了活动目标', evidence: [{ artifact_seq: 7, quote: '活动说明' }] }],
  handoff: { summary: '活动说明整理好了', why: '先讲目标，再给可复查的安排', reproduce: ['核对活动日期'], limits: ['尚未核对场地容量'] },
}

type Receipt = NonNullable<NonNullable<DeliveryState['local_checks']>[number]['receipts']>[number]
const receipt = {
  receipt_seq: 41, attempt_seq: 39, at: '2026-10-15T09:00:00Z',
  command: ['node', 'check.mjs', '活动说明'], outcome: 'failed', exit_code: 1, duration_ms: 123,
  stdout: { bytes: 18, sha256: 'a'.repeat(64), truncated: false, complete: true },
  stderr: { bytes: 0, sha256: 'b'.repeat(64), truncated: false, complete: true },
  artifacts_unchanged: true, environment: 'temporary_delivery_copy_not_sandbox',
} satisfies Receipt

function withReceipts(receipts: Receipt[]): DeliveryState {
  return { ...accepted, status: 'rework', reason: '整体运行检查未通过', local_checks: [{ id: 'behavior', text: '整体运行检查', status: 'failed', receipt_seq: 41, receipts }] }
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(new Blob(['fixture-archive']), { headers: { 'Content-Type': 'application/zip' } })))
})

test('仅有效交付显示下载入口，并提供选择原因、复查步骤及未覆盖范围', () => {
  render(<DeliveryPanel taskId="task" delivery={accepted} names={{ store_manager: '店长' }} />)
  expect(screen.getByRole('button', { name: '下载交付包' })).toBeTruthy()
  expect(screen.getByText('整体复核：店长')).toBeTruthy()
  fireEvent.click(screen.getByText('为什么这样做 · 如何复查'))
  expect(screen.getByText('来源：整体复核人的说明；复查步骤不代表实际执行记录。')).toBeTruthy()
  expect(screen.getByText('先讲目标，再给可复查的安排')).toBeTruthy()
  expect(screen.getByText('核对活动日期')).toBeTruthy()
  expect(screen.getByText('尚未核对场地容量')).toBeTruthy()
  fireEvent.click(screen.getByText('逐条要求的核对依据'))
  expect(screen.getByText('交付中文活动说明')).toBeTruthy()
  expect(screen.getByText('“活动说明” · 成果 #7')).toBeTruthy()
})

test.each(['pending', 'reviewing', 'checking', 'uncertain', 'stale', 'invalid', 'needs_evidence', 'rework'])('状态 %s 不沿用旧交接或下载能力', (status) => {
  render(<DeliveryPanel taskId="task" delivery={{ ...accepted, status }} names={{}} />)
  expect(screen.queryByRole('button', { name: '下载交付包' })).toBeNull()
  expect(screen.queryByText('活动说明整理好了')).toBeNull()
})

test('按需展开当前波次的所有回执，失败与通过均可见，更新波次后不保留旧记录', () => {
  const passed: Receipt = { ...receipt, receipt_seq: 42, attempt_seq: 40, outcome: 'passed', exit_code: 0, duration_ms: 87 }
  const { rerender } = render(<DeliveryPanel taskId="task" delivery={withReceipts([receipt, passed])} names={{}} />)
  expect(screen.getByText('整体运行检查 · 未通过 · 回执 #41')).toBeTruthy()
  const summary = screen.getByText('实际检查记录 · 2 条')
  const details = summary.closest('details')!
  expect(details.open).toBe(false)
  expect(summary.getAttribute('aria-label')).toBe('整体运行检查的实际检查记录')
  fireEvent.click(summary)
  expect(details.open).toBe(true)
  expect(screen.getByText('来源：这次交付的实际执行记录')).toBeTruthy()
  const failedRecord = screen.getByRole('listitem', { name: '真实回执 #41' })
  const passedRecord = screen.getByRole('listitem', { name: '真实回执 #42' })
  expect(within(failedRecord).getByText('真实回执 #41 · 未通过')).toBeTruthy()
  expect(within(passedRecord).getByText('真实回执 #42 · 通过')).toBeTruthy()
  expect(within(failedRecord).getByText('退出码：1 · 用时：123 毫秒')).toBeTruthy()
  expect(within(passedRecord).getByText('退出码：0 · 用时：87 毫秒')).toBeTruthy()
  expect(failedRecord.textContent).toContain('尝试 #39 · 记录时间：2026-10-15T09:00:00Z')
  expect(within(failedRecord).getByText('检查环境：临时交付副本（非沙箱，已清理）')).toBeTruthy()
  expect(within(failedRecord).getByText('交付正文：未被修改')).toBeTruthy()
  fireEvent.click(summary)
  expect(details.open).toBe(false)
  expect(fetch).not.toHaveBeenCalled()

  const next = { ...accepted, local_checks: [{ id: 'behavior', text: '整体运行检查', status: 'passed', receipt_seq: 51, receipts: [{ ...passed, receipt_seq: 51, attempt_seq: 50 }] }] }
  rerender(<DeliveryPanel taskId="task" delivery={next} names={{}} />)
  fireEvent.click(screen.getByText('实际检查记录 · 1 条'))
  expect(screen.getByRole('listitem', { name: '真实回执 #51' })).toBeTruthy()
  expect(screen.queryByText('真实回执 #41 · 未通过')).toBeNull()
  expect(screen.queryByText('真实回执 #42 · 通过')).toBeNull()
})

test('实际命令保持 JSON 参数数组，HTML 输出仅显示文本，展开不会运行命令或暴露临时 cwd', () => {
  const command = ['node', '-e', 'console.log("活动说明")', 'a b', '$(echo bad); &', '<script>bad()</script>', 'line\nbreak', 'C:\\a b\\file']
  const stdout = '<script>bad()</script>\n<img src="x" onerror="bad()">'
  const stderr = '<b>失败详情</b>'
  const unsafe = { ...receipt, command, stdout: { ...receipt.stdout, excerpt: stdout, truncated: true }, stderr: { ...receipt.stderr, bytes: 17, excerpt: stderr }, cwd: 'C:\\Temp\\cater-delivery-check-deleted' }
  const { container } = render(<DeliveryPanel taskId="task" delivery={{ ...withReceipts([unsafe]), status: 'checking' }} names={{}} />)
  fireEvent.click(screen.getByText('实际检查记录 · 1 条'))
  const actualCommand = screen.getByLabelText('回执 #41 的实际命令参数数组')
  expect(actualCommand.tagName).toBe('PRE')
  expect(JSON.parse(actualCommand.textContent!)).toEqual(command)
  expect(screen.getByLabelText('标准输出（stdout）输出文本').textContent).toBe(stdout)
  expect(screen.getByLabelText('标准错误（stderr）输出文本').textContent).toBe(stderr)
  expect(container.querySelector('script, img, b')).toBeNull()
  expect(container.textContent).not.toContain(unsafe.cwd)
  expect(screen.queryByRole('button')).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

test.each([
  { output: receipt.stdout, message: '未保存输出正文，仅提供字节数与 SHA256。', capture: '完整', truncated: '否' },
  { output: receipt.stderr, message: '无输出。', capture: '完整', truncated: '否' },
  { output: { ...receipt.stderr, complete: false }, message: '未采集到输出，采集尚未完成。', capture: '未完成', truncated: '否' },
  { output: { ...receipt.stdout, complete: false, truncated: true }, message: '未保存输出正文，仅提供字节数与 SHA256。', capture: '未完成', truncated: '是' },
  { output: { ...receipt.stdout, excerpt: '' }, message: '已保存的输出文本为空。', capture: '完整', truncated: '否' },
])('无可显示输出正文时区分 $message（采集 $capture，截断 $truncated）', ({ output, message, capture, truncated }) => {
  render(<DeliveryPanel taskId="task" delivery={withReceipts([{ ...receipt, stdout: output }])} names={{}} />)
  fireEvent.click(screen.getByText('实际检查记录 · 1 条'))
  const group = within(screen.getByRole('group', { name: '标准输出（stdout）' }))
  expect(group.getByText(message)).toBeTruthy()
  expect(group.getByText(`标准输出（stdout） · ${output.bytes} 字节`)).toBeTruthy()
  expect(group.getByText(`SHA256：${output.sha256}`)).toBeTruthy()
  expect(group.getByText(`采集：${capture} · 预览截断：${truncated}`)).toBeTruthy()
  expect(group.queryByLabelText('标准输出（stdout）输出文本')).toBeNull()
})

test.each(['checking', 'uncertain'])('状态 %s 可以阅读已返回回执，尚未返回的检查继续待检查', (status) => {
  const delivery = withReceipts([{ ...receipt, outcome: 'timeout', exit_code: null, artifacts_unchanged: false }])
  render(<DeliveryPanel taskId="task" delivery={{ ...delivery, status,
    local_checks: [
      ...delivery.local_checks!,
      { id: 'pending', text: '第二项检查', status: 'unknown', receipts: [] },
    ],
  }} names={{}} />)
  expect(screen.getByText('第二项检查 · 待检查')).toBeTruthy()
  fireEvent.click(screen.getByText('实际检查记录 · 1 条'))
  expect(screen.getByText('真实回执 #41 · 超时')).toBeTruthy()
  expect(screen.getByText('退出码：无 · 用时：123 毫秒')).toBeTruthy()
  expect(screen.getByText('交付正文：发生变化')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '下载交付包' })).toBeNull()
  expect(screen.queryByText('来源：整体复核人的说明；复查步骤不代表实际执行记录。')).toBeNull()
})

test('旧回执缺少检查字段时明确显示未记录，不捏造命令、零用时或无输出', () => {
  const legacy: Receipt = { receipt_seq: 41, attempt_seq: 39, at: receipt.at }
  render(<DeliveryPanel taskId="task" delivery={withReceipts([legacy])} names={{}} />)
  fireEvent.click(screen.getByText('实际检查记录 · 1 条'))
  const record = screen.getByRole('listitem', { name: '真实回执 #41' })
  const row = within(record)
  expect(row.getByText('真实回执 #41 · 结果未记录')).toBeTruthy()
  expect(row.getByText('退出码：未记录 · 用时：未记录')).toBeTruthy()
  expect(row.getByText('检查环境：未记录')).toBeTruthy()
  expect(row.getByText('交付正文：未记录')).toBeTruthy()
  expect(row.getByText('实际命令未记录。')).toBeTruthy()
  expect(row.queryByLabelText('回执 #41 的实际命令参数数组')).toBeNull()
  for (const name of ['标准输出（stdout）', '标准错误（stderr）']) {
    const group = within(row.getByRole('group', { name }))
    expect(group.getByText(`${name} · 字节数未记录`)).toBeTruthy()
    expect(group.getByText('SHA256：未记录')).toBeTruthy()
    expect(group.getByText('采集：未记录 · 预览截断：未记录')).toBeTruthy()
    expect(group.getByText('输出正文未记录。')).toBeTruthy()
  }
  expect(row.queryByText('无输出。')).toBeNull()
  expect(record.textContent).not.toContain('0 毫秒')
  expect(record.textContent).not.toContain('0 字节')
  expect(fetch).not.toHaveBeenCalled()
})

test('部分旧输出摘要缺少完整性或字节数时保留已有信息，不误判无输出', () => {
  render(<DeliveryPanel taskId="task" delivery={withReceipts([{ ...receipt,
    stdout: { bytes: 0 }, stderr: { sha256: 'c'.repeat(64) }, duration_ms: 0,
  }])} names={{}} />)
  fireEvent.click(screen.getByText('实际检查记录 · 1 条'))
  expect(screen.getByText('退出码：1 · 用时：0 毫秒')).toBeTruthy()
  const stdout = within(screen.getByRole('group', { name: '标准输出（stdout）' }))
  expect(stdout.getByText('标准输出（stdout） · 0 字节')).toBeTruthy()
  expect(stdout.getByText('采集：未记录 · 预览截断：未记录')).toBeTruthy()
  expect(stdout.getByText('输出正文未记录。')).toBeTruthy()
  expect(stdout.queryByText('无输出。')).toBeNull()
  const stderr = within(screen.getByRole('group', { name: '标准错误（stderr）' }))
  expect(stderr.getByText('标准错误（stderr） · 字节数未记录')).toBeTruthy()
  expect(stderr.getByText(`SHA256：${'c'.repeat(64)}`)).toBeTruthy()
  expect(stderr.queryByText('无输出。')).toBeNull()
})

test.each([undefined, []])('兼容没有 receipts 或空 receipts 的旧数据，保留摘要与交接说明', (receipts) => {
  render(<DeliveryPanel taskId="task" delivery={{ ...accepted, local_checks: [
    { id: 'behavior', text: '整体运行检查', status: 'passed', receipt_seq: 12, receipts },
    { id: 'pending', status: 'unknown', receipt_seq: null },
  ] }} names={{}} />)
  expect(screen.getByText('整体运行检查 · 通过 · 回执 #12')).toBeTruthy()
  expect(screen.getByText('整体行为检查 · 待检查')).toBeTruthy()
  expect(screen.queryByText(/实际检查记录 ·/)).toBeNull()
  expect(screen.getByRole('button', { name: '下载交付包' })).toBeTruthy()
  fireEvent.click(screen.getByText('为什么这样做 · 如何复查'))
  expect(screen.getByText('核对活动日期')).toBeTruthy()
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
