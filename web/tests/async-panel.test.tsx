import { act, fireEvent, render, screen } from '@testing-library/react'
import { lazy, useState } from 'react'
import { beforeEach, expect, test, vi } from 'vitest'
import { AsyncPanel } from '../src/AsyncPanel'
import { ReloadContext } from '../src/pageReloadContext'

beforeEach(() => { vi.spyOn(console, 'error').mockImplementation(() => {}) })
const broken = () => { throw new Error('private-token-and-path') }

test('功能异常保留相邻输入且不回显异常原文，不自动刷新', async () => {
  const reload = vi.fn(async () => '')
  function Chat() {
    const [value, setValue] = useState('未发送的内容')
    return <ReloadContext.Provider value={reload}><AsyncPanel fallback="正在读取"><Broken /></AsyncPanel>
      <input aria-label="聊天草稿" value={value} onChange={event => setValue(event.target.value)} /></ReloadContext.Provider>
  }
  const Broken = broken
  render(<Chat />)
  expect(screen.getByRole('alert').textContent).toContain('这部分暂时打不开')
  expect(screen.getByRole('alert').textContent).not.toMatch(/private-token|path|已结束|已暂停/)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '继续编辑也不会丢' } })
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('继续编辑也不会丢')
  expect(reload).not.toHaveBeenCalled()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '保留草稿并刷新' })))
  expect(reload).toHaveBeenCalledTimes(1)
})

test('真实lazy拒绝显示局部错误，其他功能继续渲染', async () => {
  const Missing = lazy(async () => { throw new TypeError('Failed to fetch dynamically imported module') })
  await act(async () => render(<><AsyncPanel fallback={<p>正在载入</p>}><Missing /></AsyncPanel><button>仍可编辑</button></>))
  expect(screen.getByRole('alert').textContent).toContain('这部分暂时打不开')
  expect(screen.getByRole('button', { name: '仍可编辑' })).toBeTruthy()
})

test('尚在加载时保留原等待内容，正常成功不显示错误', async () => {
  let finish!: (value: { default: () => React.ReactNode }) => void
  const Panel = lazy(() => new Promise<{ default: () => React.ReactNode }>(resolve => { finish = resolve }))
  render(<AsyncPanel fallback={<p role="status">正在载入</p>}><Panel /></AsyncPanel>)
  expect(screen.getByRole('status').textContent).toBe('正在载入')
  await act(async () => finish({ default: () => <p>实际内容</p> }))
  expect(screen.getByText('实际内容')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
})

test('刷新保存中不能双击重复动作，存储失败保留页面并允许再试', async () => {
  let done!: (value: string) => void
  const reload = vi.fn(() => new Promise<string>(resolve => { done = resolve }))
  const Broken = broken
  render(<ReloadContext.Provider value={reload}><AsyncPanel fallback={null}><Broken /></AsyncPanel></ReloadContext.Provider>)
  fireEvent.click(screen.getByRole('button'))
  expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button'))
  expect(reload).toHaveBeenCalledTimes(1)
  await act(async () => done('没有保存，尚未刷新。'))
  expect(screen.getByRole('status').textContent).toContain('尚未刷新')
  expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(false)
})

test('刷新回调抛异常也不暴露原文，缺少保存能力时不刷新', async () => {
  const Broken = broken
  const view = render(<ReloadContext.Provider value={async () => { throw new Error('secret') }}><AsyncPanel fallback={null}><Broken /></AsyncPanel></ReloadContext.Provider>)
  await act(async () => fireEvent.click(screen.getByRole('button')))
  expect(screen.getByRole('status').textContent).toBe('未能保存草稿，尚未刷新。')
  expect(view.container.textContent).not.toContain('secret')
  view.unmount()
  render(<AsyncPanel fallback={null}><Broken /></AsyncPanel>)
  await act(async () => fireEvent.click(screen.getByRole('button')))
  expect(screen.getByRole('status').textContent).toContain('当前无法保存草稿')
})

test('切换明确子视图key可恢复，与旧视图错误相互隔离', () => {
  const Broken = broken
  const view = render(<AsyncPanel fallback={null}><Broken key="old" /></AsyncPanel>)
  expect(screen.getByRole('alert')).toBeTruthy()
  view.rerender(<AsyncPanel fallback={null}><p key="new">新的正常视图</p></AsyncPanel>)
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('新的正常视图')).toBeTruthy()
})
