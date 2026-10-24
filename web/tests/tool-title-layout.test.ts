// @vitest-environment node
import { mkdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { createServer } from 'vite'
import { expect, test } from 'vitest'
import { detail, event, leader, members, summaries } from './native-handoff.fixtures'

test.skipIf(process.env.CATER_WEB_BROWSER_TESTS !== '1')('交付依据动作在真实桌面和手机过程面板中显示中文且不冒充验收', async () => {
  const { chromium } = createRequire(new URL('../../tools/browser-check/package.json', import.meta.url))('playwright')
  const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)),
    logLevel: 'silent', server: { host: '127.0.0.1', port: 0, open: false } })
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined
  const errors: string[] = []
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    const current = detail([
      event(12, 'team.tool_started', { role: leader.id, run_id: 'run', name: 'read_handoff_sources', arguments: {} }),
      event(13, 'team.tool_finished', { role: leader.id, run_id: 'run', name: 'read_handoff_sources',
        started_seq: 12, result: { status: 'ok', sources: [] } }),
    ])
    for (const width of [390, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } })
      page.on('pageerror', error => errors.push(error.message))
      const methods: string[] = []
      await page.route('**/api/**', async route => {
        const request = route.request()
        const path = new URL(request.url()).pathname
        methods.push(request.method())
        const body = path === '/api/task-summaries' ? summaries([current])
          : path === '/api/organization' ? [leader, ...members]
          : path === '/api/organization/profile' ? { leader, cabinet: [], workers: members }
          : path === '/api/workspaces' ? [] : path === '/api/tasks/native-ui' ? current : undefined
        expect(body, path).toBeDefined()
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      })
      await page.goto(server.resolvedUrls!.local[0])
      if (width < 1024) await page.getByRole('button', { name: '打开成果与过程' }).click()
      const board = page.locator('section[aria-label="团队进度"]:visible')
      await board.waitFor()
      await board.locator('summary').filter({ hasText: '工具过程' }).click()
      const actions = board.getByLabel('团队工具过程')
      const content = await actions.textContent()
      expect(content).toContain('读取交付依据')
      expect(content).toContain('工具已完成')
      expect(content).not.toMatch(/read_handoff_sources|检查通过|已交付|验收通过/)
      const geometry = await actions.evaluate(element => ({ overflow: element.scrollWidth - element.clientWidth,
        width: element.getBoundingClientRect().width }))
      expect(geometry.width).toBeGreaterThan(150)
      expect(geometry.overflow).toBeLessThanOrEqual(1)
      if (process.env.CATER_WEB_QA_OUTPUT) {
        await mkdir(process.env.CATER_WEB_QA_OUTPUT, { recursive: true })
        await page.screenshot({ path: join(process.env.CATER_WEB_QA_OUTPUT, `tool-title-${width}.png`) })
      }
      expect(methods.every(method => method === 'GET')).toBe(true)
      await page.close()
    }
    expect(errors).toEqual([])
  } finally {
    await browser?.close()
    await server.close()
  }
}, 60_000)
