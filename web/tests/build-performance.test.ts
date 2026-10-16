// @vitest-environment node
import { Buffer } from 'node:buffer'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import { expect, test, vi } from 'vitest'

test('production chunks stay within loading budgets with complete acyclic imports', async () => {
  const root = fileURLToPath(new URL('../', import.meta.url))
  const nodeEnv = process.env.NODE_ENV
  let result: Awaited<ReturnType<typeof build>>
  // Vite's production mode does not override Vitest's NODE_ENV=test.
  vi.stubEnv('NODE_ENV', 'production')
  try {
    result = await build({ root, mode: 'production', logLevel: 'silent', build: { write: false }, plugins: [{
      name: 'verify-production-environment',
      configResolved(config) {
        expect(config.mode).toBe('production')
        expect(config.isProduction).toBe(true)
      },
    }] })
  } finally {
    vi.unstubAllEnvs()
  }
  expect(process.env.NODE_ENV).toBe(nodeEnv)
  const outputs = Array.isArray(result) ? result : [result]
  const chunks = outputs.flatMap((output) => output.output).filter((item) => item.type === 'chunk')
  const entries = chunks.filter((chunk) => chunk.isEntry)

  const developmentModules = chunks.flatMap((chunk) => Object.keys(chunk.modules))
    .filter((id) => /[/\\]react(?:-dom)?[^/\\]*\.development\.js$|[/\\]jsx-dev-runtime\.js$/.test(id))
  expect(developmentModules, 'production output must not include React development runtimes').toEqual([])
  expect(entries).toHaveLength(1)
  console.info(`Production entry: ${Buffer.byteLength(entries[0].code)} / 150000 bytes (in-memory build)`)
  for (const chunk of chunks) {
    const budget = chunk.isEntry ? 150_000 : 500_000
    expect(Buffer.byteLength(chunk.code), `${chunk.fileName}: ${budget} byte budget`).toBeLessThanOrEqual(budget)
  }

  const byName = new Map(chunks.map((chunk) => [chunk.fileName, chunk]))
  const visited = new Set<string>()
  const visiting = new Set<string>()
  const visit = (name: string) => {
    expect(visiting.has(name), `static import cycle at ${name}`).toBe(false)
    if (visited.has(name)) return
    const chunk = byName.get(name)
    expect(chunk, `missing production chunk ${name}`).toBeDefined()
    visiting.add(name)
    for (const dependency of chunk!.imports) visit(dependency)
    visiting.delete(name)
    visited.add(name)
  }
  for (const chunk of chunks) visit(chunk.fileName)

  const graphVendor = chunks.find((chunk) => chunk.fileName.includes('graph-vendor'))
  expect(graphVendor, 'graph libraries must have an on-demand chunk').toBeDefined()
  visited.clear()
  visit(entries[0].fileName)
  expect(visited.has(graphVendor!.fileName), 'chat entry must not eagerly import graph libraries').toBe(false)
  const graph = chunks.find((chunk) => chunk.fileName.includes('TaskGraph-'))
  expect(graph, 'the graph view must be loaded through a dynamic import').toBeDefined()
  expect(entries[0].dynamicImports).toContain(graph!.fileName)
  for (const name of ['ClarificationCard', 'QuestionDetails', 'Artifact', 'MessageDetails']) {
    const component = chunks.find((chunk) => chunk.fileName.includes(`${name}-`))
    expect(component, `${name} must load on demand`).toBeDefined()
    expect(visited.has(component!.fileName), `${name} must not be statically reachable from the entry`).toBe(false)
    expect(entries[0].dynamicImports).toContain(component!.fileName)
  }
}, 30_000)
