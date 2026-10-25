const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)

export function recordedArguments(value: unknown): unknown {
  if (!record(value) || Object.keys(value).length !== 2 || !Object.keys(value).every(key => ['encoding', 'json'].includes(key))
    || value.encoding !== 'escaped_json' || typeof value.json !== 'string') return value
  try {
    let decodedKeys = 0
    const parsed: unknown = JSON.parse(value.json, (_key, item: unknown) => {
      if (typeof item === 'number' && !Number.isFinite(item)) throw new Error('Non-finite JSON number')
      if (record(item)) decodedKeys += Object.keys(item).length
      return item
    })
    // Tokenize every string, including values, so JSON.parse cannot silently discard duplicate keys.
    let recordedKeys = 0
    for (const token of value.json.matchAll(/"(?:\\.|[^"\\])*"/g)) {
      if (/^\s*:/.test(value.json.slice(token.index + token[0].length))) recordedKeys++
    }
    return recordedKeys === decodedKeys ? parsed : undefined
  } catch {
    return undefined
  }
}
