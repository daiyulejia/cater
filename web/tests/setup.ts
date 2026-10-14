import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn((media: string) => ({ media, matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
