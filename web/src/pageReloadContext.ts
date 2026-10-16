import { createContext } from 'react'

export const ReloadContext = createContext<(() => Promise<string>) | null>(null)
