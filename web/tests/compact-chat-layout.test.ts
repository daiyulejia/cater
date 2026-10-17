// @vitest-environment node
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { expect, test } from 'vitest'
import type { TaskDetail } from '../src/App'
import { detail, event, leader, members } from './native-handoff.fixtures'

const viewports = [
  { width: 1440, height: 900, minimum: 350 },
  { width: 390, height: 844, minimum: 280 },
  { width: 320, height: 900, minimum: 330 },
  { width: 320, height: 640, minimum: 150 },
  { width: 390, height: 480, minimum: 70 },
]

// Synthetic API fixtures: Chromium renders the actual app, with no external model or service.
function compactTasks(): TaskDetail[] {
  const tasks = ['整理周末读书会安排', '核对下周活动清单'].map((goal, index) => {
    const task = detail([], `compact-${index + 1}`)
    task.task.goal = goal
    task.task.status = 'running'
    task.task.team = { ...task.task.team!, objective: goal, status: 'running', revision: 0 }
    task.team = task.task.team
    task.events = [
      event(1, 'task.created', { goal }, { id: 'human', kind: 'human', role: 'human' }),
      event(2, 'team.message_received', { recipient: 'dev', kind: 'message', text: '请先整理时间和参与人数。' },
        { id: 'human', kind: 'human', role: 'human' }),
      event(3, 'team.run_started', { role: 'dev', message_seq: 2, revision: 0, run_id: 'dev-run' }),
      event(4, 'team.message_received', { recipient: 'test', kind: 'message', text: '请再核对场地与报名信息。' },
        { id: 'human', kind: 'human', role: 'human' }),
      event(5, 'team.run_started', { role: 'test', message_seq: 4, revision: 0, run_id: 'test-run' }),
    ].map(entry => ({ ...entry, ts: new Date().toISOString() }))
    task.activity = { active: true, state: 'running' }
    task.execution_control = { state: 'running', seq: null, reason: '' }
    task.workspace = { revision: 1, workspace_id: null, available: false, reason: 'unbound', pending: [] }
    task.background = { version: 1, source_task_id: `compact-${2 - index}`, source_goal: index ? '整理周末读书会安排' : '核对下周活动清单',
      source_seq: 1, source_hash: 'a'.repeat(64), captured_at: '2026-10-17T08:00:00Z',
      sources: [], artifacts: [], omitted_artifacts: 0 }
    return task
  })
  // The last summary is selected on a clean session.
  return tasks.reverse()
}

test.skipIf(process.env.CATER_WEB_BROWSER_TESTS !== '1')('真实 Chromium 在五种视口保留聊天空间、只读工具与手机展开选择', async () => {
  const { chromium } = createRequire(new URL('../../tools/browser-check/package.json', import.meta.url))('playwright')
  const server = await createServer({ root: fileURLToPath(new URL('../', import.meta.url)),
    cacheDir: fileURLToPath(new URL('../node_modules/.vite-compact-chat', import.meta.url)),
    logLevel: 'silent', server: { host: '127.0.0.1', port: 0, strictPort: false, open: false } })
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined
  const failures: string[] = []
  try {
    await server.listen()
    browser = await chromium.launch({ headless: true })
    for (const viewport of viewports) {
      const label = `${viewport.width}x${viewport.height}`
      const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, reducedMotion: 'reduce' })
      const requests: { path: string; method: string }[] = []
      const errors: string[] = []
      try {
        const page = await context.newPage()
        page.setDefaultTimeout(10_000)
        await page.addInitScript(() => {
          const ledger = { pending: 0, calls: [] as string[] }
          Object.assign(window, { __compactFetch: ledger })
          const fetch = window.fetch.bind(window)
          window.fetch = async (...args: Parameters<typeof window.fetch>) => {
            ledger.pending += 1
            ledger.calls.push(String(args[0]))
            try {
              const response = await fetch(...args)
              await response.clone().arrayBuffer()
              return response
            } finally { ledger.pending -= 1 }
          }
        })
        page.on('pageerror', (error: Error) => errors.push(error.message))
        const tasks = compactTasks()
        const selected = tasks.at(-1)!
        const other = tasks[0]
        const routes = new Map<string, () => unknown>([
          ['/api/task-summaries', () => ({ tasks: tasks.map(({ task, events, activity, execution_control }) => ({
            id: task.id, goal: task.goal, status: task.status, status_label: '进行中', conversation_count: events.length,
            latest_seq: events.at(-1)!.seq, version: events.length, team: task.team, activity, execution_control,
          })) })],
          ['/api/organization', () => [leader, ...members]],
          ['/api/organization/profile', () => ({ leader, cabinet: [], workers: members, mode: 'local_demo' })],
          ['/api/workspaces', () => []],
          ...tasks.map(task => [`/api/tasks/${task.task.id}`, () => task] as [string, () => unknown]),
        ])
        await context.route('**/api/**', async (route: { request: () => { url: () => string; method: () => string }; fulfill: (response: object) => Promise<void> }) => {
          const request = route.request()
          const path = new URL(request.url()).pathname
          requests.push({ path, method: request.method() })
          const allowed = request.method() === 'GET' && routes.has(path)
          await route.fulfill({ status: allowed ? 200 : 405, contentType: 'application/json',
            body: JSON.stringify(allowed ? routes.get(path)!() : { error: 'Unexpected fixture request' }) })
        })
        await page.goto(server.resolvedUrls!.local[0])
        const stream = page.getByLabel('聊天记录', { exact: true })
        const composer = page.getByRole('textbox', { name: '向团队发送消息', exact: true })
        await composer.waitFor()
        await expect.poll(() => stream.getAttribute('aria-busy')).toBe('false')
        await page.getByRole('region', { name: '当前工作进展' }).waitFor()
        await page.evaluate(() => document.fonts.ready)

        const geometry = async () => page.evaluate(() => {
          const stream = document.querySelector<HTMLElement>('.chat-stream')!
          const composer = document.querySelector<HTMLElement>('.composer-shell')!
          const rect = stream.getBoundingClientRect()
          return { chatHeight: rect.height, chatTop: rect.top, chatBottom: rect.bottom,
            composerTop: composer.getBoundingClientRect().top, composerBottom: composer.getBoundingClientRect().bottom,
            viewportHeight: innerHeight, overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth }
        })
        const bounds = await geometry()
        console.info(`[compact-chat] ${label} initial ${JSON.stringify(bounds)}`)
        expect.soft(bounds.chatHeight, `${label}: ${JSON.stringify(bounds)}`).toBeGreaterThanOrEqual(viewport.minimum)
        expect.soft(bounds.chatTop, label).toBeGreaterThanOrEqual(0)
        expect.soft(bounds.chatBottom, label).toBeLessThanOrEqual(bounds.composerTop + 1)
        expect.soft(bounds.composerBottom, label).toBeLessThanOrEqual(viewport.height + 1)
        expect.soft(bounds.overflow, label).toBeLessThanOrEqual(1)

        const checkTextBounds = async () => {
          const overflow = await page.evaluate(() => {
            const roots = document.querySelectorAll('.app-header, .task-sidebar-heading, .chat-pane, [role="dialog"]')
            const bad: string[] = []
            for (const root of roots) for (const node of root.querySelectorAll<HTMLElement>('button, input, textarea, p, h1, h2, label, span')) {
              const rect = node.getBoundingClientRect()
              if (!rect.width || !rect.height || getComputedStyle(node).visibility === 'hidden' || node.closest('.sr-only')) continue
              if (rect.bottom <= 0 || rect.top >= innerHeight) continue
              if (rect.left < -1 || rect.right > innerWidth + 1) bad.push(`${node.tagName}: ${node.getAttribute('aria-label') ?? node.textContent?.trim().slice(0, 60)} [${rect.left}, ${rect.right}]`)
              if (!node.children.length && node.textContent?.trim()) {
                const range = document.createRange()
                range.selectNodeContents(node)
                for (const text of range.getClientRects()) if (text.left < -1 || text.right > innerWidth + 1) bad.push(`text: ${node.textContent.slice(0, 60)}`)
              }
            }
            return bad
          })
          expect.soft(overflow, `${label}: text/control bounds`).toEqual([])
        }
        // Hit testing catches overflow clipping even when a popup's DOM rect looks correct.
        const checkReachable = async (selector: string) => {
          await page.locator(selector).click({ trial: true })
          const result = await page.locator(selector).evaluate((node: HTMLElement) => {
            const rect = node.getBoundingClientRect()
            const points = [[rect.left + 2, rect.top + 2], [rect.right - 2, rect.bottom - 2], [rect.left + rect.width / 2, rect.top + rect.height / 2]]
            const hits = points.map(([x, y]) => {
              const hit = document.elementFromPoint(x, y)
              return { x, y, matches: hit !== null && node.contains(hit), element: hit?.outerHTML.slice(0, 200) }
            })
            return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height,
              hit: hits.every(hit => hit.matches), hits }
          })
          expect.soft(result.left, `${label}: ${selector}`).toBeGreaterThanOrEqual(-1)
          expect.soft(result.right, `${label}: ${selector}`).toBeLessThanOrEqual(viewport.width + 1)
          expect.soft(result.top, `${label}: ${selector}`).toBeGreaterThanOrEqual(-1)
          expect.soft(result.bottom, `${label}: ${selector}`).toBeLessThanOrEqual(viewport.height + 1)
          expect.soft(result.width, selector).toBeGreaterThan(0)
          expect.soft(result.height, selector).toBeGreaterThan(0)
          expect.soft(result.hit, `${label}: ${selector} clipped/covered ${JSON.stringify(result)}`).toBe(true)
        }
        await checkTextBounds()
        const draft = '先保留这段草稿，待确认场地后再发。'
        await composer.fill(draft)
        const mobile = viewport.width < 640
        if (mobile) {
          const expand = page.getByRole('button', { name: '展开任务列表', exact: true })
          await expand.waitFor()
          expect(await expand.getAttribute('aria-expanded')).toBe('false')
          expect(await page.getByRole('heading', { name: /我的任务/ }).isVisible()).toBe(true)
          expect(await page.getByRole('button', { name: '查找任务', exact: true }).isVisible()).toBe(true)
          expect(await page.getByRole('navigation', { name: '任务列表' }).isVisible()).toBe(false)
          const filter = page.getByRole('button', { name: '筛选对话：全部', exact: true })
          await filter.click()
          const dialog = page.getByRole('dialog', { name: '筛选对话', exact: true })
          await dialog.waitFor()
          expect(await dialog.getByRole('heading', { name: '筛选对话', exact: true }).isVisible()).toBe(true)
          expect(await dialog.locator('input[type="radio"]').count()).toBe(4)
          for (const name of ['全部', '协作', '决策', '成果']) {
            expect(await dialog.getByRole('radio', { name, exact: true }).getAttribute('type')).toBe('radio')
          }
          expect(await dialog.getByRole('radio', { name: '全部', exact: true }).isChecked()).toBe(true)
          await checkTextBounds()
          // Choosing immediately unmounts the dialog; check() waits on the removed input.
          await dialog.getByRole('radio', { name: '协作', exact: true }).click()
          await expect.poll(() => dialog.isVisible()).toBe(false)
          const changedFilter = page.getByRole('button', { name: '筛选对话：协作', exact: true })
          await expect.poll(() => changedFilter.evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
          await changedFilter.click()
          expect(await dialog.getByRole('radio', { name: '协作', exact: true }).isChecked()).toBe(true)
          await page.setViewportSize({ width: 1440, height: 900 })
          expect(await dialog.isVisible(), `${label}: open filter survives desktop resize`).toBe(true)
          expect(await dialog.getByRole('heading', { name: '筛选对话', exact: true }).isVisible()).toBe(true)
          expect(await changedFilter.isVisible()).toBe(false)
          expect(await page.locator('button[aria-label="筛选对话：协作"]').evaluate((node: HTMLElement) =>
            getComputedStyle(node.parentElement!).display)).toBe('none')
          await page.keyboard.press('Escape')
          await expect.poll(() => dialog.isVisible()).toBe(false)
          const desktopCurrent = page.getByRole('group', { name: '对话筛选', exact: true }).getByRole('button', { name: '协作', exact: true })
          expect(await desktopCurrent.getAttribute('aria-pressed')).toBe('true')
          await expect.poll(() => desktopCurrent.evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
          await page.setViewportSize({ width: viewport.width, height: viewport.height })
          expect(await changedFilter.isVisible()).toBe(true)
          await changedFilter.click()
          await dialog.getByRole('radio', { name: '全部', exact: true }).click()

          const compactSummary = page.getByLabel('当前进展摘要', { exact: true })
          expect(await compactSummary.isVisible()).toBe(true)
          expect(await compactSummary.textContent()).toContain(`${members[1].name}正在处理最新消息`)
          expect(await compactSummary.textContent()).not.toContain(members[0].name)

          // Pause scheduled polling only during the two view-only clicks.
          await page.clock.install()
          await page.clock.pauseAt(new Date(Date.now() + 100))
          // Reset existing near-due polls before Playwright advances animation frames for clicks.
          const pendingFetches = () => page.evaluate(() =>
            (window as typeof window & { __compactFetch: { pending: number } }).__compactFetch.pending)
          const fetchCount = () => page.evaluate(() =>
            (window as typeof window & { __compactFetch: { calls: string[] } }).__compactFetch.calls.length)
          await expect.poll(pendingFetches).toBe(0)
          await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
          await expect.poll(pendingFetches).toBe(0)
          await page.evaluate(async () => { await Promise.resolve(); await Promise.resolve() })
          // Count at fetch initiation in the page, avoiding delayed route callbacks in Node.
          const count = await fetchCount()
          const expandProgress = page.getByRole('button', { name: '展开工作进展', exact: true })
          expect(await expandProgress.getAttribute('aria-expanded')).toBe('false')
          await expandProgress.click()
          expect(await fetchCount(), `${label}: progress expansion must not request`).toBe(count)
          const collapseProgress = page.getByRole('button', { name: '收起工作进展', exact: true })
          expect(await collapseProgress.getAttribute('aria-expanded')).toBe('true')
          // Previous 390x480 failure: stream y=207..247 covered collapse y=220..252.
          // Keep real hit testing and the original height targets for the anchored panel.
          await checkReachable('button[aria-label="收起工作进展"]')
          const panel = await page.locator('.activity-body').evaluate((node: HTMLElement) => {
            const rect = node.getBoundingClientRect()
            const root = node.closest('section')!.getBoundingClientRect()
            return { top: rect.top, bottom: rect.bottom, height: rect.height, rootTop: root.top,
              position: getComputedStyle(node).position, overflowY: getComputedStyle(node).overflowY,
              controls: node.querySelectorAll('button, input, textarea, a').length }
          })
          const expandedBounds = await geometry()
          console.info(`[compact-chat] ${label} expanded ${JSON.stringify({ ...expandedBounds, panel })}`)
          expect(panel.position).toBe('absolute')
          expect(panel.overflowY).toBe('auto')
          expect(panel.controls).toBe(0)
          expect(panel.top).toBeGreaterThanOrEqual(0)
          expect(panel.bottom).toBeLessThanOrEqual(panel.rootTop)
          expect(panel.height).toBeLessThanOrEqual(Math.min(240, viewport.height * 0.4) + 1)
          expect(expandedBounds.chatHeight).toBeGreaterThanOrEqual(bounds.chatHeight - 1)
          expect(expandedBounds.composerBottom).toBeLessThanOrEqual(viewport.height + 1)
          let collapseClicked = true
          try {
            await collapseProgress.click()
          } catch (error) {
            collapseClicked = false
            const expanded = await page.evaluate(() => {
              const selectors = ['.chat-stream', '.composer-shell', '[aria-label="当前工作进展"]', 'button[aria-label="收起工作进展"]']
              return selectors.map(selector => {
                const node = document.querySelector<HTMLElement>(selector)!
                const rect = node.getBoundingClientRect()
                const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
                return { selector, top: rect.top, bottom: rect.bottom, height: rect.height,
                  hit: hit?.outerHTML.slice(0, 300) }
              })
            })
            console.info(`[compact-chat] ${label} expanded failure ${JSON.stringify(expanded)}`)
            failures.push(`${label}: progress collapse click failed; ${JSON.stringify(expanded)}; ${String(error)}`)
            // Preserve the click failure; keyboard recovery lets the remaining tools be checked.
            await collapseProgress.focus()
            await page.keyboard.press('Space')
          }
          expect(await expandProgress.getAttribute('aria-expanded')).toBe('false')
          // Playwright advances its clock during failed click retries, allowing scheduled polls.
          if (collapseClicked) expect(await fetchCount(), `${label}: progress toggle must not request`).toBe(count)
          await page.clock.resume()
          await expandProgress.click()
          await page.locator('.activity-body').focus()
          await page.keyboard.press('Escape')
          await expect.poll(() => expandProgress.getAttribute('aria-expanded')).toBe('false')
          await expect.poll(() => expandProgress.evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
        } else {
          const filters = page.getByRole('group', { name: '对话筛选', exact: true })
          expect(await filters.isVisible()).toBe(true)
          expect(await filters.getByRole('button').allTextContents()).toEqual(['全部', '协作', '决策', '成果'])
          expect(await page.getByRole('button', { name: '筛选对话：全部', exact: true }).isVisible()).toBe(false)
          expect(await page.getByRole('navigation', { name: '任务列表' }).isVisible()).toBe(true)
        }

        for (const [triggerName, title, closeName] of [
          ['工作目录', '工作目录', '关闭工作目录'],
          ['查看对话来源', '对话来源', '关闭对话来源'],
          ['任务脉络', '任务脉络', '关闭任务脉络'],
        ]) {
          await checkReachable(`button[aria-label="${triggerName}"]`)
          await page.getByRole('button', { name: triggerName, exact: true }).click()
          const dialog = page.getByRole('dialog', { name: title, exact: true })
          await dialog.waitFor()
          await checkReachable(`button[aria-label="${closeName}"]`)
          await checkTextBounds()
          await dialog.getByRole('button', { name: closeName, exact: true }).click()
          await expect.poll(() => dialog.isVisible()).toBe(false)
        }
        await checkReachable('button[aria-label^="本次伙伴，"]')
        await page.getByRole('button', { name: /^本次伙伴，/ }).click()
        const partners = page.getByRole('dialog', { name: '本次伙伴', exact: true })
        await partners.waitFor()
        await checkReachable('button[aria-label="关闭本次伙伴"]')
        await partners.getByRole('button', { name: '关闭本次伙伴', exact: true }).click()
        await expect.poll(() => partners.isVisible()).toBe(false)
        await page.getByRole('button', { name: /^本次伙伴，/ }).click()
        await partners.getByRole('button', { name: '猫咖伙伴簿', exact: true }).click()
        const directory = page.getByRole('dialog', { name: '今天在店里', exact: true })
        await directory.waitFor()
        await checkReachable('button[aria-label="关闭伙伴页"]')
        await directory.getByRole('button', { name: '关闭伙伴页', exact: true }).click()
        await expect.poll(() => directory.isVisible()).toBe(false)
        await checkReachable('button[aria-label="更多任务操作"]')
        await page.getByRole('button', { name: '更多任务操作', exact: true }).click()
        const menu = page.getByRole('group', { name: '任务操作', exact: true })
        await menu.waitFor()
        await checkReachable('[aria-label="任务操作"]')
        await checkTextBounds()
        await page.keyboard.press('Escape')
        await expect.poll(() => menu.isVisible()).toBe(false)
        expect(await composer.inputValue()).toBe(draft)

        const beforeList = await geometry()
        if (mobile) {
          await page.getByRole('button', { name: '展开任务列表', exact: true }).click()
          const openList = await geometry()
          console.info(`[compact-chat] ${label} task-list-open ${JSON.stringify(openList)}`)
          expect(Math.abs(openList.chatHeight - beforeList.chatHeight), `${label}: task list must not displace chat`).toBeLessThanOrEqual(1)
          expect(Math.abs(openList.composerBottom - beforeList.composerBottom)).toBeLessThanOrEqual(1)
          expect(await page.locator('#task-list').evaluate((node: HTMLElement) => getComputedStyle(node).position)).toBe('absolute')
        }
        const pollVisible = async () => {
          const response = page.waitForResponse((response: { url: () => string; request: () => { method: () => string } }) =>
            new URL(response.url()).pathname === `/api/tasks/${selected.task.id}` && response.request().method() === 'GET')
          await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
          await response
          await expect.poll(() => stream.getAttribute('aria-busy')).toBe('false')
        }
        await pollVisible()
        if (mobile) expect(await page.getByRole('button', { name: '收起任务列表', exact: true }).getAttribute('aria-expanded')).toBe('true')
        const nav = page.getByRole('navigation', { name: '任务列表' })
        await nav.getByRole('button', { name: new RegExp(other.task.goal) }).click()
        await expect.poll(() => page.getByRole('heading', { name: other.task.goal, exact: true }).isVisible()).toBe(true)
        if (mobile) {
          expect(await page.getByRole('button', { name: '展开任务列表', exact: true }).getAttribute('aria-expanded')).toBe('false')
          expect(await nav.isVisible()).toBe(false)
          await expect.poll(() => page.getByRole('heading', { name: other.task.goal, exact: true })
            .evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
          await page.getByRole('button', { name: '展开任务列表', exact: true }).click()
        }
        await nav.getByRole('button', { name: new RegExp(selected.task.goal) }).click()
        await expect.poll(() => composer.inputValue()).toBe(draft)
        if (mobile) {
          // Polls preserve manual expansion; choosing a task explicitly closes the overlay.
          await expect.poll(() => page.getByRole('heading', { name: selected.task.goal, exact: true })
            .evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
          await pollVisible()
          expect(await page.getByRole('button', { name: '展开任务列表', exact: true }).getAttribute('aria-expanded')).toBe('false')
        }
        selected.events.push(
          event(6, 'team.run_finished', { role: 'dev', message_seq: 2, run_id: 'dev-run', status: 'failed' }),
          event(7, 'team.run_finished', { role: 'test', message_seq: 4, run_id: 'test-run', status: 'failed' }),
        )
        selected.activity = { active: false, state: 'completed' }
        await pollVisible()
        if (mobile) {
          const compactSummary = page.getByLabel('当前进展摘要', { exact: true })
          await expect.poll(() => compactSummary.textContent()).toContain(`${members[1].name}本轮处理未完成`)
          expect(await compactSummary.isVisible()).toBe(true)
          expect(await compactSummary.textContent()).not.toContain(members[0].name)
          expect(await page.getByLabel('2 项进展需核对', { exact: true }).isVisible()).toBe(true)
          expect(await page.getByRole('button', { name: '展开工作进展', exact: true }).getAttribute('aria-expanded')).toBe('false')
        }
        selected.execution_control = { state: 'paused', seq: 8, reason: '等场地确认后继续。' }
        selected.events.push(event(8, 'task.progress_reported', { text: '正在等待场地确认。' }))
        await pollVisible()
        const paused = page.getByRole('status', { name: '执行状态', exact: true })
        await expect.poll(() => paused.textContent()).toContain('已暂停后续执行')
        expect(await paused.isVisible()).toBe(true)
        await pollVisible()
        expect(await paused.isVisible()).toBe(true)
        expect(await composer.inputValue()).toBe(draft)
        const finalBounds = await geometry()
        console.info(`[compact-chat] ${label} paused ${JSON.stringify(finalBounds)}`)
        expect.soft(finalBounds.composerBottom, `${label}: paused composer`).toBeLessThanOrEqual(viewport.height + 1)
        expect.soft(finalBounds.overflow, `${label}: paused overflow`).toBeLessThanOrEqual(1)
        await checkTextBounds()
        expect((await page.screenshot()).byteLength).toBeGreaterThan(10_000)
      } catch (error) {
        failures.push(`${label}: ${error instanceof Error ? error.stack ?? error.message : String(error)}`)
      } finally {
        expect.soft(requests.filter(request => request.method !== 'GET'), `${label}: mutations`).toEqual([])
        expect.soft(requests.filter(request => !['/api/task-summaries', '/api/organization', '/api/organization/profile', '/api/workspaces', '/api/tasks/compact-1', '/api/tasks/compact-2'].includes(request.path)), `${label}: unexpected API`).toEqual([])
        expect.soft(errors, `${label}: page errors`).toEqual([])
        await context.close()
      }
    }
    expect(failures, 'Viewport interaction failures (height thresholds are intentionally fixed)').toEqual([])
  } finally {
    try { await browser?.close() } finally { await server.close() }
  }
}, 180_000)
