import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { ChatMarkdown } from '../src/ChatMarkdown'

test('伙伴回复呈现段落、强调、清单和代码，不展示原始标记', () => {
  render(<ChatMarkdown text={'**网页已保存**\n\n1. 打开 `index.html`\n2. 输入温度'} />)
  expect(screen.getByText('网页已保存').tagName).toBe('STRONG')
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(screen.getByText('index.html').tagName).toBe('CODE')
})

test('聊天不执行HTML、不自动加载图片、不保留危险链接', () => {
  const { container } = render(<ChatMarkdown text={'<script>alert(1)</script>\n\n![外部图片](https://example.com/image.png)\n\n[危险](javascript:alert(1))\n\n[文档](https://example.com/docs)'} />)
  expect(container.querySelector('script')).toBeNull()
  expect(container.querySelector('img')).toBeNull()
  expect(screen.queryByRole('link', { name: '危险' })).toBeNull()
  expect(screen.getByRole('link', { name: '文档' }).getAttribute('rel')).toBe('noopener noreferrer')
})
