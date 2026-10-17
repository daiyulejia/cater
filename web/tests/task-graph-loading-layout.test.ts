// @vitest-environment node
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { expect, test } from 'vitest'
import { detail, event, leader, members } from './native-handoff.fixtures'

const artifacts = fileURLToPath(new URL('../../.cater/qa/frontend-flake-20261017/', import.meta.url))
const viewports = [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 900 }]
const privateError = 'PRIVATE_MODULE_ERROR_do_not_render_credential_fixture'
const graphModule = (path: string) => /\/src\/TaskGraph\.tsx$/.test(path) || /\/TaskGraph-[^/]+\.js$/.test(path)

test.skipIf(process.env.CATER_WEB_BROWSER_TESTS !== '1')('isolated Chromium keeps the graph shell styled and closable while its module waits or fails', async () => {
  const sourceRoot = fileURLToPath(new URL('../', import.meta.url))
  const overrideRoot = process.env.CATER_GRAPH_UI_ROOT
  if (overrideRoot && /^[a-z][a-z\d+.-]*:\/\//i.test(overrideRoot)) throw new Error('CATER_GRAPH_UI_ROOT must be a local directory')
  const root = overrideRoot ? resolve(sourceRoot, '..', overrideRoot) : sourceRoot
  if (root.split(/[\\/]/).some(segment => /^(static|\.vite)$/i.test(segment))) throw new Error('Graph UI root cannot target static or .vite')
  const runLabel = process.env.CATER_GRAPH_UI_LABEL ?? ''
  if (runLabel && !/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(runLabel)) throw new Error('CATER_GRAPH_UI_LABEL must be a filename-safe label')
  if (overrideRoot && !runLabel) throw new Error('A root override requires CATER_GRAPH_UI_LABEL to preserve source evidence')
  const prefix = runLabel ? `${runLabel}-` : ''
  const artifactPath = (name: string) => `${artifacts}/${prefix}${name}`
  const { chromium } = createRequire(new URL('../../tools/browser-check/package.json', import.meta.url))('playwright')
  const server = await createServer({ root, ...(overrideRoot ? { configFile: false } : {}),
    cacheDir: fileURLToPath(new URL('../node_modules/.vite-task-graph-loading', import.meta.url)),
    logLevel: 'silent', server: { host: '127.0.0.1', port: 0, strictPort: false, open: false } })
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined
  const failures: string[] = []
  const evidence: unknown[] = []
  try {
    await mkdir(artifacts, { recursive: true })
    await server.listen()
    const base = server.resolvedUrls!.local[0]
    const origin = new URL(base).origin
    browser = await chromium.launch({ headless: true })
    for (const viewport of viewports) for (const mode of ['gate', 'abort', 'private-error'] as const) {
      const label = `${viewport.width}x${viewport.height}-${mode}`
      const context = await browser.newContext({ viewport, reducedMotion: 'reduce', serviceWorkers: 'block' })
      const requests: { path: string; method: string; allowed: boolean }[] = []
      const external: string[] = []
      const writes: string[] = []
      const errors: string[] = []
      let moduleRequests = 0
      let documents = 0
      let released = false
      let release!: () => void
      const gate = new Promise<void>(resolve => { release = () => { released = true; resolve() } })
      const tasks = [detail([event(1, 'task.created', { goal: '另一组图谱测试' })], 'graph-other'),
        detail([event(1, 'task.created', { goal: '当前图谱测试' })], 'graph-selected')]
      tasks[0].task.goal = '另一组图谱测试'
      tasks[1].task.goal = '当前图谱测试'
      for (const task of tasks) task.task.team!.objective = task.task.goal
      const fixtures = new Map<string, unknown>([
        ['/api/task-summaries', { tasks: tasks.map(({ task, events, activity }) => ({ id: task.id, goal: task.goal,
          status: task.status, status_label: '已完成', conversation_count: events.length, latest_seq: 1,
          version: 1, team: task.team, activity })) }],
        ['/api/organization', [leader, ...members]],
        ['/api/organization/profile', { leader, cabinet: [], workers: members, mode: 'local_demo' }],
        ['/api/workspaces', []],
        ...tasks.map(task => [`/api/tasks/${task.task.id}`, task] as [string, unknown]),
      ])
      try {
        const page = await context.newPage()
        page.setDefaultTimeout(8_000)
        page.on('pageerror', (error: Error) => errors.push(error.message))
        // A single catch-all route prevents Vite's API proxy and external services from being reached.
        await context.route('**/*', async (route: {
          request: () => { url: () => string; method: () => string; resourceType: () => string }
          fulfill: (response: object) => Promise<void>; abort: (reason?: string) => Promise<void>; continue: () => Promise<void>
        }) => {
          const request = route.request()
          const url = new URL(request.url())
          if (request.method() !== 'GET') { writes.push(`${request.method()} ${url.pathname}`); await route.abort(); return }
          if (url.origin !== origin) { external.push(url.origin); await route.abort(); return }
          if (request.resourceType() === 'document') documents += 1
          if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/tasks/')) {
            const allowed = request.method() === 'GET' && fixtures.has(url.pathname)
            requests.push({ path: url.pathname, method: request.method(), allowed })
            await route.fulfill({ status: allowed ? 200 : 405, contentType: 'application/json',
              body: JSON.stringify(allowed ? fixtures.get(url.pathname) : { error: 'Unexpected fixture request' }) })
            return
          }
          if (graphModule(url.pathname)) {
            moduleRequests += 1
            await gate
            if (mode === 'abort') {
              await route.abort('failed')
            } else if (mode === 'private-error') {
              await route.fulfill({ contentType: 'text/javascript', body: `throw new Error('${privateError}')` })
            } else await route.continue()
            return
          }
          await route.continue()
        })
        await page.goto(base)
        const composer = page.getByRole('textbox', { name: '向团队发送消息', exact: true })
        await composer.waitFor()
        await expect.poll(() => page.getByRole('heading', { name: tasks[1].task.goal, exact: true }).isVisible()).toBe(true)
        const draft = `图谱加载期间保留草稿 ${label}`
        await composer.fill(draft)
        const trigger = page.getByRole('button', { name: '任务脉络', exact: true })
        const dialog = page.getByRole('dialog', { name: '任务脉络', exact: true })
        const close = dialog.getByRole('button', { name: '关闭任务脉络', exact: true })
        const checkShell = async () => {
          await dialog.waitFor({ timeout: 1500 })
          expect(await dialog.getByRole('heading', { name: '任务脉络', exact: true }).isVisible()).toBe(true)
          await close.click({ trial: true })
          const geometry = await dialog.evaluate((node: HTMLElement) => {
            const rect = node.getBoundingClientRect()
            const header = node.querySelector('header')!
            const button = node.querySelector<HTMLElement>('button[aria-label="关闭任务脉络"]')!
            const b = button.getBoundingClientRect()
            const h = header.getBoundingClientRect()
            const overlay = document.querySelector('.task-graph-overlay')!
            const o = overlay.getBoundingClientRect()
            const style = getComputedStyle(node)
            return { position: style.position, display: style.display, background: style.backgroundColor,
              left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
              headerHeight: h.height, closeWidth: b.width, closeHeight: b.height,
              closeInside: b.left >= rect.left && b.right <= rect.right && b.top >= rect.top && b.bottom <= rect.bottom,
              hit: button.contains(document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2)),
              overlayPosition: getComputedStyle(overlay).position,
              overlayCovers: o.left === 0 && o.top === 0 && o.right === innerWidth && o.bottom === innerHeight,
              overflow: document.documentElement.scrollWidth - innerWidth }
          })
          console.info(`[${prefix}graph-loading] ${label} ${JSON.stringify(geometry)}`)
          evidence.push({ label, stage: released ? 'resolved' : 'pending', geometry })
          expect(geometry.position).toBe('fixed')
          expect(geometry.display).toBe('flex')
          expect(geometry.background).toBe('rgb(255, 255, 255)')
          expect(geometry.left).toBeGreaterThanOrEqual(0)
          expect(geometry.top).toBeGreaterThanOrEqual(0)
          expect(geometry.right).toBeLessThanOrEqual(viewport.width)
          expect(geometry.bottom).toBeLessThanOrEqual(viewport.height)
          expect(geometry.headerHeight).toBeGreaterThanOrEqual(50)
          expect(geometry.closeWidth).toBeGreaterThanOrEqual(40)
          expect(geometry.closeHeight).toBeGreaterThanOrEqual(40)
          expect(geometry.closeInside).toBe(true)
          expect(geometry.hit).toBe(true)
          expect(geometry.overlayPosition).toBe('fixed')
          expect(geometry.overlayCovers).toBe(true)
          expect(geometry.overflow).toBeLessThanOrEqual(1)
        }
        await trigger.click()
        await expect.poll(() => moduleRequests).toBe(1)
        await checkShell()
        expect(released).toBe(false)
        expect(await dialog.getByRole('status').textContent()).toContain('正在读取任务脉络')
        await page.screenshot({ path: artifactPath(`graph-loading-${label}.png`) })
        await page.keyboard.press('Escape')
        await expect.poll(() => dialog.isVisible()).toBe(false)
        await expect.poll(() => trigger.evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
        expect(await composer.inputValue()).toBe(draft)
        await trigger.click()
        await checkShell()
        await close.click()
        await expect.poll(() => dialog.isVisible()).toBe(false)
        expect(await composer.inputValue()).toBe(draft)
        if (mode === 'gate') {
          const choose = async (goal: string) => {
            if (viewport.width < 640) await page.getByRole('button', { name: '展开任务列表', exact: true }).click()
            await page.getByRole('navigation', { name: '任务列表' }).getByRole('button', { name: new RegExp(goal) }).click()
            await expect.poll(() => page.getByRole('heading', { name: goal, exact: true }).isVisible()).toBe(true)
          }
          await choose(tasks[0].task.goal)
          const moduleResponse = page.waitForResponse((response: { url: () => string }) => graphModule(new URL(response.url()).pathname))
          release()
          await moduleResponse
          // Import resolution is observed by reopening the loaded graph, not a fixed sleep.
          expect(await dialog.isVisible()).toBe(false)
          await trigger.click()
          await dialog.getByRole('region', { name: '当前团队协作图', exact: true }).waitFor()
          await checkShell()
          await page.screenshot({ path: artifactPath(`graph-loaded-${label}.png`) })
          await close.click()
          await expect.poll(() => dialog.isVisible()).toBe(false)
          await choose(tasks[1].task.goal)
          expect(await composer.inputValue()).toBe(draft)
          expect(await dialog.isVisible()).toBe(false)
        } else {
          await trigger.click()
          release()
          await dialog.getByRole('alert').waitFor()
          await checkShell()
          expect(await dialog.getByRole('button', { name: '保留草稿并刷新', exact: true }).isVisible()).toBe(true)
          expect(await page.locator('body').textContent()).not.toContain(privateError)
          expect(await page.locator('body').textContent()).not.toContain('Failed to fetch dynamically imported module')
          // Observe a bounded quiet interval for accidental automatic recovery/reload.
          await page.waitForTimeout(500)
          expect(documents).toBe(1)
          expect(moduleRequests).toBe(1)
          await page.screenshot({ path: artifactPath(`graph-failed-${label}.png`) })
          await page.keyboard.press('Escape')
          await expect.poll(() => dialog.isVisible()).toBe(false)
          await expect.poll(() => trigger.evaluate((node: HTMLElement) => node === document.activeElement)).toBe(true)
          expect(await composer.inputValue()).toBe(draft)
          expect(documents).toBe(1)
        }
      } catch (error) {
        const page = context.pages()[0]
        if (page) {
          await page.screenshot({ path: artifactPath(`graph-probe-failure-${label}.png`) }).catch(() => {})
          console.info(`[${prefix}graph-loading] ${label} failure body: ${(await page.locator('body').innerText()).slice(0, 3000)}`)
        }
        failures.push(`${label}: ${error instanceof Error ? error.stack ?? error.message : String(error)}`)
      } finally {
        evidence.push({ label, moduleRequests, documents, requests, external, writes })
        expect.soft(writes, `${label}: no network writes`).toEqual([])
        expect.soft(requests.filter(request => request.method !== 'GET'), `${label}: no writes`).toEqual([])
        expect.soft(requests.filter(request => !request.allowed), `${label}: only fixture API`).toEqual([])
        expect.soft(external, `${label}: no external network`).toEqual([])
        expect.soft(mode === 'gate' ? errors : errors.filter(error => !(mode === 'private-error' ? error.includes(privateError) : /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(error))), `${label}: unexpected page errors`).toEqual([])
        release()
        try { await context.unrouteAll({ behavior: 'ignoreErrors' }) } finally { await context.close() }
      }
    }
    expect(failures, 'Graph loading controls and geometry must retain their original assertions').toEqual([])
  } finally {
    try { await browser?.close() } finally {
      try { await server.close() } finally {
        await writeFile(artifactPath('browser-graph-evidence.json'), JSON.stringify({ root, runLabel, failures, evidence }, null, 2))
      }
    }
  }
}, 180_000)
