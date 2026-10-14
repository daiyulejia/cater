import { useState } from 'react'
import type { ComponentProps } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { RulesPanel } from '../src/RulesPanel'

const project = { id: 'project-a', name: '作品集' }
const other = { id: 'project-b', name: '周报' }
const rule = { id: 'rule-1', revision: 1, text: '不要改动公共接口', scope: 'project', status: 'active', source: { kind: 'human', actor: 'human' }, seq: 1 }
let rules: typeof rule[]
let handler: (path: string, init?: RequestInit) => Promise<unknown> | undefined
let request: ReturnType<typeof vi.fn<ComponentProps<typeof RulesPanel>['request']>>

beforeEach(() => {
  rules = [rule]
  handler = () => undefined
  request = vi.fn(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const override = handler(path, init)
    if (override) return await override as T
    if (path === '/api/projects' && init?.method !== 'POST') return [project, other] as T
    if (path.startsWith('/api/rules') && init?.method !== 'POST') return { project_id: project.id, rules, enforcement: 'guidance_only' } as T
    if (path === '/api/rules' && init?.method === 'POST') return { id: 'new-rule', ...JSON.parse(String(init.body)) } as T
    throw new Error('Unexpected request: ' + path)
  })
})

function mount(props: Partial<ComponentProps<typeof RulesPanel>> = {}) {
  const callbacks = { onRecorded: vi.fn(), onProjectChange: vi.fn(async () => undefined), onUpdated: vi.fn() }
  const view = render(<RulesPanel request={request} taskId="task-a" projectId={project.id} draft="" busy={false} {...callbacks} {...props} />)
  return { ...view, ...callbacks }
}

const open = async () => { fireEvent.click(screen.getByTitle('项目与店规')); await screen.findByText(rule.text) }
const editor = () => screen.getByRole('textbox', { name: '店规内容' }) as HTMLTextAreaElement
const save = () => screen.getByRole('button', { name: '保存店规' }) as HTMLButtonElement

test('打开只读取店规，候选不会自动采用；明确采用才发出版本化请求', async () => {
  rules = [{ ...rule, status: 'candidate', source: { kind: 'agent_report', actor: 'dev' } }]
  handler = (path, init) => path === '/api/rules/rule-1' && init?.method === 'POST' ? Promise.resolve(rule) : undefined
  mount(); await open()
  expect(screen.getByText('待采用')).toBeTruthy()
  expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: '采用', exact: true }))
  await screen.findByText('已采用，后续调用会带上这条要求。')
  const write = request.mock.calls.find(([, init]) => init?.method === 'POST')!
  expect(JSON.parse(String(write[1]?.body))).toEqual({ revision: 1, text: rule.text, status: 'active', task_id: 'task-a' })
})

test('从聊天记录店规保留完整草稿，保存成功后才通知清理', async () => {
  const { onRecorded } = mount({ draft: '不要改动数据库结构' })
  fireEvent.click(screen.getByRole('button', { name: '记为店规' }))
  await screen.findByText(rule.text)
  expect(editor().value).toBe('不要改动数据库结构')
  expect(onRecorded).not.toHaveBeenCalled()
  fireEvent.click(save())
  await waitFor(() => expect(onRecorded).toHaveBeenCalledWith('不要改动数据库结构'))
  expect(request.mock.calls.some(([path, init]) => path === '/api/rules' && JSON.parse(String(init?.body)).scope === 'task')).toBe(true)
})

test('超长聊天不被截断且不能误保存；失败时编辑仍保留', async () => {
  const { onRecorded } = mount({ draft: '文'.repeat(2001) })
  fireEvent.click(screen.getByRole('button', { name: '记为店规' }))
  await screen.findByText(rule.text)
  expect(editor().value).toHaveLength(2001)
  expect(save().disabled).toBe(true)
  expect(screen.getByRole('alert').textContent).toContain('2001')
  fireEvent.change(editor(), { target: { value: '精简后的要求' } })
  handler = (path, init) => path === '/api/rules' && init?.method === 'POST' ? Promise.reject(new Error('unavailable')) : undefined
  fireEvent.click(save())
  await screen.findByText('这次没有保存成功，请稍后重试。')
  expect(editor().value).toBe('精简后的要求')
  expect(onRecorded).not.toHaveBeenCalled()
})

test('版本冲突保留编辑，展示新版并在明确核对后才允许保存', async () => {
  let writes = 0
  handler = (path, init) => {
    if (path !== '/api/rules/rule-1' || init?.method !== 'POST') return
    if (++writes === 1) {
      rules = [{ ...rule, revision: 2, text: '别人刚修改的要求' }]
      return Promise.reject(new Error('rule version changed; refresh before editing'))
    }
    return Promise.resolve(rule)
  }
  mount(); await open()
  fireEvent.click(screen.getByRole('button', { name: '修改店规 ' + rule.text }))
  fireEvent.change(editor(), { target: { value: '我的修改' } })
  fireEvent.click(save())
  await screen.findByText('已有新版本 v2 · 已采用')
  expect(editor().value).toBe('我的修改')
  expect(save().disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '基于新版保留我的修改' }))
  expect(editor().value).toBe('我的修改')
  fireEvent.click(save())
  await screen.findByText('已保存，后续调用将使用新的店规版本。')
  const post = request.mock.calls.filter(([, init]) => init?.method === 'POST').at(-1)!
  expect(JSON.parse(String(post[1]?.body)).revision).toBe(2)
})

test('关闭面板后不清空未保存内容，编辑时不允许关联另一个项目', async () => {
  mount({ projectId: undefined }); await open()
  fireEvent.click(screen.getByRole('button', { name: '修改店规 ' + rule.text }))
  fireEvent.change(editor(), { target: { value: '还没写完' } })
  expect((screen.getByRole('button', { name: '新建项目' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '关闭店规' }))
  await open()
  expect(editor().value).toBe('还没写完')
})

test('取消项目不能把原来的项目规则静默扩大为通用规则', async () => {
  function Harness() {
    const [selected, setSelected] = useState<typeof project | null>(project)
    return <RulesPanel request={request} projectId={selected?.id} draft="" busy={false} onProjectChange={async (value) => setSelected(value)} onRecorded={vi.fn()} onUpdated={vi.fn()} />
  }
  render(<Harness />); await open()
  fireEvent.change(editor(), { target: { value: '只针对作品集的要求' } })
  fireEvent.change(screen.getByLabelText('所属项目'), { target: { value: '' } })
  await waitFor(() => expect((screen.getByLabelText('所属项目') as HTMLSelectElement).value).toBe(''))
  expect((screen.getByLabelText('店规适用范围') as HTMLSelectElement).value).toBe('')
  expect(save().disabled).toBe(true)
})

test('保存期间防止重复写入，输入不会被迟到的成功响应清空', async () => {
  let finish!: (value: unknown) => void
  const pending = new Promise((resolve) => { finish = resolve })
  handler = (path, init) => path === '/api/rules' && init?.method === 'POST' ? pending : undefined
  mount({ draft: '待保存的要求' })
  fireEvent.click(screen.getByRole('button', { name: '记为店规' })); await screen.findByText(rule.text)
  const button = save()
  fireEvent.click(button); fireEvent.click(button)
  expect(editor().disabled).toBe(true)
  expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  await act(async () => finish(rule))
  expect(editor().value).toBe('')
})
