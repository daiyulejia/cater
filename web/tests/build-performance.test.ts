// @vitest-environment node
import { Buffer } from 'node:buffer'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import { expect, test } from 'vitest'

test('production chunks stay within loading budgets with complete acyclic imports', async () => {
  const root = fileURLToPath(new URL('../', import.meta.url))
  const result = await build({ root, mode: 'production', logLevel: 'silent', build: { write: false } })
  const outputs = Array.isArray(result) ? result : [result]
  const chunks = outputs.flatMap((output) => output.output).filter((item) => item.type === 'chunk')
  const entries = chunks.filter((chunk) => chunk.isEntry)

  expect(entries).toHaveLength(1)
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
}, 30_000)
