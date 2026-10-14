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
}, 30_000)
