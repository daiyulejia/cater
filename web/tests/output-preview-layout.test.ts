// @vitest-environment node
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { expect, test } from 'vitest'

test.skipIf(process.env.CATER_WEB_BROWSER_TESTS !== '1')('紧凑首尾在160/390/1440宽度有界，短行和超长单行最后错误默认可见', async () => {
  const { chromium } = createRequire(new URL('../../tools/browser-check/package.json', import.meta.url))('playwright')
  let html = ''
  const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)), logLevel: 'silent',
    plugins: [{ name: 'output-preview-fixture', configureServer(server) {
      server.middlewares.use('/__output-preview', (_req, res) => {
        res.setHeader('Content-Type', 'text/html; charset=utf-8')
        res.end(`<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/src/index.css"></head><body>${html}</body></html>`)
      })
    } }],
    server: { host: '127.0.0.1', port: 0, strictPort: false, open: false } })
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined
  try {
    const { CommandOutputs } = await server.ssrLoadModule('/src/CommandOutputPreview.tsx')
    const lines = Array.from({ length: 100 }, (_, i) => `中文短行${i}`).join('\n')
    const output = (text: string) => ({ preview_mode: 'head_tail', excerpt: text, tail_excerpt: `${text}最后错误FATAL`, complete: false, truncated: true })
    html = renderToStaticMarkup(createElement('main', { style: { maxWidth: 360, margin: '8px auto', fontSize: 12, lineHeight: '20px' } },
      ...[lines, '超长中文单行'.repeat(3000)].map((text, i) => createElement('section', { key: i, 'data-case': i, style: { minWidth: 0, marginBottom: 20 } },
        createElement(CommandOutputs, { stdout: output(text), stderr: output(text), limit: 800 })))))
    await server.listen()
    browser = await chromium.launch({ headless: true })
    for (const width of [160, 390, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } })
      await page.route('**/*', async (route: { request: () => { url: () => string }; continue: () => Promise<void>; abort: () => Promise<void> }) => {
        const url = new URL(route.request().url())
        if (url.hostname !== '127.0.0.1' || url.pathname.startsWith('/api/')) return route.abort()
        return route.continue()
      })
      await page.goto(`${server.resolvedUrls!.local[0]}__output-preview`)
      await page.locator('pre').first().waitFor()
      const geometry = await page.locator('pre').evaluateAll((nodes: HTMLElement[]) => nodes.filter(node => !node.closest('details')).map(node => {
        const parent = node.parentElement!
        const bounds = parent.getBoundingClientRect()
        const tail = node.getAttribute('aria-label')!.includes('末尾')
        const range = document.createRange()
        range.selectNodeContents(node)
        if (node.firstChild) range.setStart(node.firstChild, Math.max(0, node.textContent!.length - 10))
        const end = range.getBoundingClientRect()
        return { tail, height: bounds.height, overflow: parent.scrollWidth - parent.clientWidth,
          direction: getComputedStyle(parent).flexDirection, endVisible: end.top >= bounds.top - 1 && end.bottom <= bounds.bottom + 1 }
      }))
      expect(geometry).toHaveLength(8)
      for (const item of geometry) {
        expect(item.height).toBeLessThanOrEqual(193)
        expect(item.overflow).toBeLessThanOrEqual(1)
        if (item.tail) {
          expect(item.direction).toBe('column-reverse')
          expect(item.endVisible).toBe(true)
        }
      }
      expect((await page.screenshot()).byteLength).toBeGreaterThan(1000)
      await page.close()
    }
  } finally {
    await browser?.close()
    await server.close()
  }
}, 60_000)
