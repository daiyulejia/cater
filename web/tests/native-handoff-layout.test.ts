// @vitest-environment node
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { expect, test } from 'vitest'
import { completion, detail, handoff, leader, members, summaries } from './native-handoff.fixtures'

test.skipIf(process.env.CATER_WEB_BROWSER_TESTS !== '1').each([false, true])('真实浏览器在移动和桌面宽度支持独立边界入口、键盘开合与长文换行，引用省略=%s', async omitted => {
  // Reuse the repository's browser-check runtime without adding a web dependency.
  const { chromium } = createRequire(new URL('../../tools/browser-check/package.json', import.meta.url))('playwright')
  const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)),
    logLevel: 'silent', server: { host: '127.0.0.1', port: 0, strictPort: false, open: false } })
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const saved = handoff()
    saved.evidence[0].filename = `${'long-file-name-'.repeat(30)}.html`
    saved.evidence[0].quote = `<script>${'unbroken-quote'.repeat(50)}</script>\n  精确摘录。  `
    saved.limits = ['手机端离线场景尚未验证。', `  https://example.invalid/${'unbroken-limit'.repeat(70)}\n<script>仅为原文</script>  `]
    // The host strips list items and bounds them before saving a reference-omitted handoff.
    if (omitted) saved.limits[1] = `https://example.invalid/${'unbroken-limit'.repeat(55)}\n<script>仅为原文</script>`
    const tasks = [detail([completion(10, undefined), completion(20, saved)])]
    delete tasks[0].events[0].payload.handoff
    const warning = '讲解已保留，引用核对失败未保存，不视为验证证据。'
    if (omitted) Object.assign(tasks[0].events[1].payload,
      { handoff_status: 'references_omitted_invalid', handoff_warning: warning })
    const errors: string[] = []
    for (const width of [320, 390, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } })
      page.on('pageerror', (error: Error) => errors.push(error.message))
      const requests: { path: string; method: string }[] = []
      await page.route('**/api/**', async (route: { request: () => { url: () => string; method: () => string }; fulfill: (response: object) => Promise<void> }) => {
        const request = route.request()
        const path = new URL(request.url()).pathname
        requests.push({ path, method: request.method() })
        const body = path === '/api/task-summaries' ? summaries(tasks)
          : path === '/api/organization' ? [leader, ...members]
          : path === '/api/organization/profile' ? { leader, cabinet: [], workers: members }
          : path === '/api/workspaces' ? []
          : path === '/api/tasks/native-ui' ? tasks[0] : undefined
        expect(body, `Unexpected API request: ${path}`).toBeDefined()
        await route.fulfill({ status: body ? 200 : 404, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
      })
      await page.goto(server.resolvedUrls!.local[0])
      const fold = page.locator('.message-details').filter({ hasText: '为什么这样做 · 如何运行' })
      const summary = fold.locator(':scope > summary')
      await summary.waitFor()
      expect(await fold.getAttribute('open')).toBeNull()
      expect(await page.locator('.delivery-history').getAttribute('open')).toBeNull()
      const limitsSummary = page.locator('summary').filter({ hasText: /^使用边界 · 2项$/ })
      const limitsFold = limitsSummary.locator('..')
      expect(await limitsSummary.count()).toBe(1)
      expect(await limitsSummary.isVisible()).toBe(true)
      expect(await limitsFold.getAttribute('open')).toBeNull()
      expect(await page.locator('.delivery-history').locator('summary').filter({ hasText: /^使用边界 · / }).count()).toBe(0)
      await summary.focus()
      await page.keyboard.press('Tab')
      expect(await limitsSummary.evaluate((element: HTMLElement) => element === document.activeElement)).toBe(true)
      expect(await limitsSummary.evaluate((element: HTMLElement) => getComputedStyle(element).outlineWidth)).toBe('2px')
      await page.keyboard.press('Enter')
      expect(await limitsFold.evaluate((element: HTMLDetailsElement) => element.open)).toBe(true)
      expect(await fold.getAttribute('open')).toBeNull()
      expect(await limitsFold.locator('li').allTextContents()).toEqual(saved.limits)
      await page.keyboard.press('Space')
      expect(await limitsFold.evaluate((element: HTMLDetailsElement) => element.open)).toBe(false)
      expect(await limitsSummary.evaluate((element: HTMLElement) => element === document.activeElement)).toBe(true)
      await limitsSummary.click()
      await summary.click()
      expect(await fold.evaluate((element: HTMLDetailsElement) => element.open)).toBe(true)
      expect(await fold.locator('details').count()).toBe(omitted ? 0 : 3)
      for (const metadata of await fold.locator('details').all()) expect(await metadata.getAttribute('open')).toBeNull()
      expect(await fold.locator('ol > li').allTextContents()).toEqual(saved.reproduce)
      for (const limit of saved.limits) expect(await fold.textContent()).not.toContain(limit)
      expect(await limitsFold.locator('button, a, script, iframe, [role="alert"]').count()).toBe(0)
      const limitsGeometry = await limitsFold.evaluate((element: HTMLElement) => {
        const rect = element.getBoundingClientRect()
        return { left: rect.left, right: rect.right, viewport: innerWidth,
          overflow: element.scrollWidth - element.clientWidth, wrapping: getComputedStyle(element).overflowWrap,
          whitespace: getComputedStyle(element.querySelector('li')!).whiteSpace }
      })
      expect(limitsGeometry.left).toBeGreaterThanOrEqual(0)
      expect(limitsGeometry.right).toBeLessThanOrEqual(limitsGeometry.viewport)
      expect(limitsGeometry.overflow).toBeLessThanOrEqual(1)
      expect(limitsGeometry.wrapping).toBe('anywhere')
      expect(limitsGeometry.whitespace).toBe('pre-wrap')
      if (omitted) {
        expect(await fold.locator('blockquote').count()).toBe(0)
        expect(await fold.textContent()).toContain(warning)
        expect(await fold.textContent()).toContain(saved.why)
      } else expect(await fold.locator('blockquote').first().textContent()).toBe(saved.evidence[0].quote)
      expect(await fold.locator('button, a, script, iframe').count()).toBe(0)
      const geometry = await fold.evaluate((element: HTMLElement) => {
        const bubble = element.closest('.message-bubble') as HTMLElement
        const rect = element.getBoundingClientRect()
        return { width: rect.width, left: rect.left, right: rect.right, viewport: innerWidth,
          overflow: element.scrollWidth - element.clientWidth, bubbleOverflow: bubble.scrollWidth - bubble.clientWidth,
          wrapping: getComputedStyle(element).overflowWrap, steps: getComputedStyle(element.querySelector('ol')!).listStyleType }
      })
      expect(geometry.width).toBeGreaterThan(120)
      expect(geometry.left).toBeGreaterThanOrEqual(0)
      expect(geometry.right).toBeLessThanOrEqual(geometry.viewport)
      expect(geometry.overflow).toBeLessThanOrEqual(1)
      expect(geometry.bubbleOverflow).toBeLessThanOrEqual(1)
      expect(geometry.wrapping).toBe('anywhere')
      expect(geometry.steps).toBe('decimal')
      expect((await page.screenshot()).byteLength).toBeGreaterThan(10_000)
      const mutations = requests.filter((request) => request.method !== 'GET')
      expect(mutations).toEqual([])
      expect(requests.every((request) => ['/api/task-summaries', '/api/organization', '/api/organization/profile', '/api/workspaces', '/api/tasks/native-ui'].includes(request.path))).toBe(true)
      await page.close()
    }
    expect(errors).toEqual([])
  } finally {
    await browser?.close()
    await server.close()
  }
}, 60_000)
