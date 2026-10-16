import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ArtifactPreview } from '../src/Artifact'
import type { Artifact } from '../src/Artifact'

const summary = '这是完整的成果说明。'.repeat(60)
const artifact: Artifact = { seq: 7, title: '页面成果', summary, content: '不可被摘要替代的完整正文',
  filename: 'result.txt', format: 'text', source: 'external' }
const preview = (text: string, limit: number) => Array.from(text).slice(0, limit).join('') + '…'

test.each([48, 49])('侧栏摘要 %s 个码点的截断边界保持全文可达', length => {
  const text = '字'.repeat(length)
  render(<ArtifactPreview item={{ ...artifact, summary: text }} />)
  if (length === 48) {
    expect(screen.getByText(text)).toBeTruthy()
    expect(screen.queryByText('完整摘要')).toBeNull()
  } else {
    expect(screen.getByText(preview(text, 48))).toBeTruthy()
    const full = screen.getByText(text)
    expect(full.closest('details')!.open).toBe(false)
    fireEvent.click(screen.getByText('完整摘要'))
    expect(full.closest('details')!.open).toBe(true)
  }
})

test.each([96, 97])('阅读器摘要 %s 个码点的边界保持描述与全文', length => {
  const text = '字'.repeat(length)
  render(<ArtifactPreview item={{ ...artifact, summary: text }} />)
  fireEvent.click(screen.getByRole('button', { name: '查看正文' }))
  const reader = screen.getByRole('dialog', { name: artifact.title })
  const description = document.getElementById(reader.getAttribute('aria-describedby')!)!
  expect(description.textContent).toBe(length === 96 ? text : preview(text, 96))
  if (length === 96) expect(within(reader).queryByText('完整摘要')).toBeNull()
  else {
    fireEvent.click(within(reader).getByText('完整摘要'))
    expect(within(reader).getByText(text).closest('details')!.open).toBe(true)
  }
})

test('长摘要默认只显示预览，完整原文可展开且有高度边界', () => {
  render(<ArtifactPreview item={artifact} />)
  expect(screen.getByText(preview(summary, 48))).toBeTruthy()
  const full = screen.getByText(summary)
  const disclosure = full.closest('details')!
  expect(disclosure.open).toBe(false)
  expect(full.className).toContain('max-h-32')
  expect(full.className).toContain('overflow-y-auto')
  fireEvent.click(within(disclosure).getByText('完整摘要'))
  expect(disclosure.open).toBe(true)
  expect(full.textContent).toBe(summary)
  fireEvent.click(within(disclosure).getByText('完整摘要'))
  expect(disclosure.open).toBe(false)
})

test('阅读器描述有界且关联正确，完整摘要和正文分别可读', () => {
  render(<ArtifactPreview item={artifact} />)
  fireEvent.click(screen.getByRole('button', { name: '查看正文' }))
  const dialog = screen.getByRole('dialog', { name: artifact.title })
  const description = document.getElementById(dialog.getAttribute('aria-describedby')!)!
  expect(description.textContent).toBe(preview(summary, 96))
  const full = within(dialog).getByText(summary)
  expect(full.closest('details')!.open).toBe(false)
  fireEvent.click(within(dialog).getByText('完整摘要'))
  expect(full.closest('details')!.open).toBe(true)
  expect(within(dialog).getByText(artifact.content!)).toBeTruthy()
  expect(full.className).toContain('max-h-32')
})

test('短摘要不增加展开入口，多行原文保留但预览不铺成几十行', () => {
  const view = render(<ArtifactPreview item={{ ...artifact, summary: '简短说明' }} />)
  expect(screen.getByText('简短说明')).toBeTruthy()
  expect(screen.queryByText('完整摘要')).toBeNull()
  const multiline = '第一行\n\n第二行\r\n  第三行'
  view.rerender(<ArtifactPreview item={{ ...artifact, summary: multiline }} />)
  expect(screen.getByText('第一行 第二行 第三行', { exact: true, selector: 'p:not(details p)' })).toBeTruthy()
  const full = [...document.querySelectorAll('details p')].find(node => node.textContent === multiline)!
  expect(full.closest('details')!.open).toBe(false)
  fireEvent.click(screen.getByText('完整摘要'))
  expect(full.closest('details')!.open).toBe(true)
  expect(full.textContent).toBe(multiline)
})

test('预览按Unicode码点截取，不截断代理对，缺失旧摘要也不会使成果入口崩溃', () => {
  const emojis = '😀'.repeat(100)
  const view = render(<ArtifactPreview item={{ ...artifact, summary: emojis }} />)
  expect(screen.getByText('😀'.repeat(48) + '…')).toBeTruthy()
  view.rerender(<ArtifactPreview item={{ ...artifact, summary: undefined as unknown as string }} />)
  expect(screen.queryByText('完整摘要')).toBeNull()
  expect(screen.getByRole('button', { name: '查看正文' })).toBeTruthy()
})

test('成果版本变化后收起旧摘要，下载仍是完整正文', () => {
  let downloaded = ''
  vi.stubGlobal('Blob', class { constructor(parts: string[]) { downloaded = parts.join('') } })
  vi.stubGlobal('URL', class extends URL { static createObjectURL = () => 'blob:summary'; static revokeObjectURL = vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const view = render(<ArtifactPreview item={artifact} />)
  fireEvent.click(screen.getByText('完整摘要'))
  expect(screen.getByText(summary).closest('details')!.open).toBe(true)
  view.rerender(<ArtifactPreview item={{ ...artifact, seq: 8, summary: summary + '新版本' }} />)
  expect(screen.getByText(summary + '新版本').closest('details')!.open).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '下载 result.txt' }))
  expect(downloaded).toBe(artifact.content)
})

test('相同标题文件名不重复展示，不可打开的内部引用默认收起且保持纯文本', () => {
  const uri = 'javascript:alert(1)'
  const view = render(<ArtifactPreview item={{ ...artifact, title: 'result.txt', summary: '简短说明', uri }} />)
  expect(screen.getAllByText('result.txt')).toHaveLength(1)
  expect(screen.getByRole('button', { name: '下载 result.txt' })).toBeTruthy()
  const reference = screen.getByText(uri).closest('details')!
  expect(reference.open).toBe(false)
  fireEvent.click(within(reference).getByText('资源引用'))
  expect(reference.open).toBe(true)
  expect(screen.queryByRole('link', { name: '打开来源' })).toBeNull()
  view.rerender(<ArtifactPreview item={{ ...artifact, title: 'result.txt', summary: '简短说明' }} showSummary={false} />)
  expect(screen.getByText('result.txt')).toBeTruthy()
})
