import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { InstanceInfo } from '../src/InstanceInfo'

window.sessionStorage.setItem('cater.access_token', 'instance-fixture-token')
const { default: App } = await import('../src/App')
window.sessionStorage.removeItem('cater.access_token')

const value = () => ({ schema_version: 1, instance_id: '1234567890abcdef1234567890abcdef', package_version: '0.1.0',
  backend_build_id: null, ui_version: 'a'.repeat(64), startup_recovery: false,
  listener: { host: '127.0.0.1', port: 8811 }, database: { kind: 'file', locator_id: 'b'.repeat(64) } })
const open = () => fireEvent.click(screen.getByRole('button', { name: '实例信息', exact: true }))
const dialog = () => within(screen.getByRole('dialog', { name: '当前连接服务' }))

test('入口按需首次读取，不因 render、关闭重开重取；不产生另一个入口或过度确认', async () => {
  const load = vi.fn(async () => value())
  const app = render(<InstanceInfo loadInstance={load} />)
  expect(load).not.toHaveBeenCalled()
  expect(screen.getAllByRole('button', { name: '实例信息', exact: true })).toHaveLength(1)
  open()
  await dialog().findByText('8811')
  expect(load).toHaveBeenCalledTimes(1)
  expect(dialog().getByText('未确认（服务未登记）')).toBeTruthy()
  expect(dialog().getByText('关闭', { exact: true })).toBeTruthy()
  expect(dialog().getByText('a'.repeat(64))).toBeTruthy()
  expect(dialog().getByText(/当前加载的页面脚本版本未核验/)).toBeTruthy()
  expect(dialog().getByText(/配置路径摘要不证明数据库相同或内容一致/)).toBeTruthy()
  expect(screen.getByRole('dialog').textContent).not.toMatch(/版本一致|已验证|同库证明|模型可用：是/)
  app.rerender(<InstanceInfo loadInstance={async () => value()} />)
  fireEvent.click(dialog().getByRole('button', { name: '关闭实例信息' }))
  open()
  expect(dialog().getByText('8811')).toBeTruthy()
  expect(load).toHaveBeenCalledTimes(1)
})

test('加载中关闭重开不重复并发请求，完成后仍显示该次结果', async () => {
  let resolve!: (value: unknown) => void
  const load = vi.fn(() => new Promise<unknown>(done => { resolve = done }))
  render(<InstanceInfo loadInstance={load} />)
  open()
  await waitFor(() => expect(load).toHaveBeenCalledTimes(1))
  expect(dialog().getByRole('status').textContent).toContain('正在读取')
  fireEvent.click(dialog().getByRole('button', { name: '关闭实例信息' }))
  open()
  await act(async () => resolve(value()))
  expect(dialog().getByText('8811')).toBeTruthy()
  expect(load).toHaveBeenCalledTimes(1)
})

test('ready 缓存有读取时间，显式刷新显示新实例标识，单次在途禁止并发而不自动重取', async () => {
  const previous = value()
  const nextId = 'fedcba0987654321fedcba0987654321'
  let resolve!: (value: unknown) => void
  const load = vi.fn().mockResolvedValueOnce(previous)
    .mockImplementationOnce(() => new Promise<unknown>(done => { resolve = done }))
  render(<InstanceInfo loadInstance={load} />)
  open()
  await dialog().findByText(previous.instance_id)
  expect(dialog().getByText(/^本页读取时间：/)).toBeTruthy()
  const refresh = dialog().getByRole('button', { name: '刷新实例信息' }) as HTMLButtonElement
  expect(refresh.title).toBe('刷新实例信息')
  fireEvent.click(refresh)
  fireEvent.click(refresh)
  expect(refresh.disabled).toBe(true)
  await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
  await act(async () => resolve({ ...value(), instance_id: nextId }))
  expect(dialog().getByText(nextId)).toBeTruthy()
  expect(dialog().queryByText(previous.instance_id)).toBeNull()
  expect(dialog().getByText(/^本页读取时间：/)).toBeTruthy()
  expect(refresh.disabled).toBe(false)
  fireEvent.click(dialog().getByRole('button', { name: '关闭实例信息' }))
  open()
  expect(dialog().getByText(nextId)).toBeTruthy()
  expect(load).toHaveBeenCalledTimes(2)
})

test.each([401, 404, 500])('HTTP %s 只在本入口显示未知，不泄漏原始错误，手动重试可恢复', async status => {
  const load = vi.fn().mockRejectedValueOnce(new Error(`HTTP ${status}: SECRET PATH AND TOKEN`)).mockResolvedValue(value())
  render(<InstanceInfo loadInstance={load} />)
  open()
  await dialog().findByText('实例信息未确认，读取失败或服务暂不支持。')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('dialog').textContent).not.toMatch(/SECRET|TOKEN|PATH/)
  fireEvent.click(dialog().getByRole('button', { name: '关闭实例信息' }))
  open()
  expect(load).toHaveBeenCalledTimes(1)
  fireEvent.click(dialog().getByRole('button', { name: '重试读取实例信息' }))
  await dialog().findByText('8811')
  expect(load).toHaveBeenCalledTimes(2)
})

test('同步抛错也只显示本地未知', async () => {
  render(<InstanceInfo loadInstance={() => { throw new Error('private-error') }} />)
  open()
  await dialog().findByText('实例信息未确认，读取失败或服务暂不支持。')
  expect(screen.getByRole('dialog').textContent).not.toContain('private-error')
})

const invalid: unknown[] = [null, [], 'raw', {},
  ...[{ schema_version: 2 }, { schema_version: '1' }, { instance_id: null }, { package_version: [] },
    { package_version: '\u0000private' }, { backend_build_id: 'unregistered' }, { backend_build_id: undefined },
    { ui_version: false }, { ui_version: 'not-a-hash' }, { ui_version: undefined }, { startup_recovery: 'false' },
    { listener: null }, { listener: { host: [], port: 8811 } }, { listener: { host: 'localhost', port: '8811' } },
    { listener: { host: 'localhost', port: 0 } }, { listener: { host: 'localhost', port: 65536 } },
    { listener: { host: 'localhost', port: 1.5 } }, { database: [] }, { database: { kind: 'other', locator_id: null } },
    { database: { kind: 'file', locator_id: 'b'.repeat(63) } }, { database: { kind: 'file' } },
    { database: { kind: 'memory', locator_id: 'b'.repeat(64) } }].map(patch => ({ ...value(), ...patch })),
]
test.each(invalid.map((item, index) => [index, item] as const))('未知/部分/恶意协议形状 %s 不当成已确认服务事实', async (_, item) => {
  render(<InstanceInfo loadInstance={async () => item} />)
  open()
  await dialog().findByText('实例信息未确认，读取失败或服务暂不支持。')
  expect(dialog().queryByText('实际监听端口')).toBeNull()
})

test.each(['file', 'memory', 'custom', 'unknown'])('合法未知信息与数据库来源 %s 不被 falsy 改写', async kind => {
  render(<InstanceInfo loadInstance={async () => ({ ...value(), ui_version: null, startup_recovery: true,
    listener: { host: '::1', port: 65535 }, database: { kind, locator_id: null },
    secret: 'MUST-NOT-RENDER', db_path: 'PRIVATE-DATABASE-PATH' })} />)
  open()
  await dialog().findByText('65535')
  expect(dialog().getByText('::1')).toBeTruthy()
  expect(dialog().getByText('开启')).toBeTruthy()
  expect(dialog().getByText('未确认', { exact: true })).toBeTruthy()
  expect(dialog().getByText('未提供')).toBeTruthy()
  expect(screen.getByRole('dialog').textContent).not.toMatch(/MUST-NOT-RENDER|PRIVATE-DATABASE-PATH/)
})

test('实际 App 全局入口复用认证 helper，只有按需 GET；401 不影响聊天或全局错误', async () => {
  window.sessionStorage.clear()
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status,
    headers: { 'Content-Type': 'application/json' } })
  let instanceStatus = 401
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url === '/api/instance') return json(instanceStatus === 200 ? value() : { error: 'SECRET' }, instanceStatus)
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'instance-task', goal: '核对实例入口', status: 'pending',
      status_label: '待开始', conversation_count: 0, latest_seq: 1, version: 1 }] })
    if (url === '/api/tasks/instance-task') return json({ task: { id: 'instance-task', goal: '核对实例入口', status: 'pending',
      acceptance: [], messages: [], assignments: [], deliverables: [] }, events: [], review: {} })
    if (url === '/api/organization') return json([])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] })
    throw new Error(`Unexpected ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '实例查看期间保留的草稿' } })
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/instance')).toHaveLength(0)
  open()
  await dialog().findByText('实例信息未确认，读取失败或服务暂不支持。')
  const instanceCalls = () => fetchMock.mock.calls.filter(([url]) => url === '/api/instance')
  expect(instanceCalls()).toHaveLength(1)
  const [path, init] = instanceCalls()[0]
  expect(path).toBe('/api/instance')
  expect(path).not.toContain('?')
  expect(new Headers(init?.headers).get('X-Cater-Access-Token')).toBe('instance-fixture-token')
  expect(init?.method ?? 'GET').toBe('GET')
  expect(screen.queryByRole('alert')).toBeNull()
  instanceStatus = 200
  fireEvent.click(dialog().getByRole('button', { name: '重试读取实例信息' }))
  await dialog().findByText('8811')
  fireEvent.click(dialog().getByRole('button', { name: '关闭实例信息' }))
  expect(input.value).toBe('实例查看期间保留的草稿')
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('核对实例入口')
  expect(fetchMock.mock.calls.every(([, request]) => !request?.method || request.method === 'GET')).toBe(true)
})
