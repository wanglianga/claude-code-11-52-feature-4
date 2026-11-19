import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, JSONObj } from './api'

type Toast = { id: number; text: string; tone: 'ok' | 'bad' }

type AppCtx = {
  meta: JSONObj | null
  clock: JSONObj | null
  reloadMeta: () => Promise<void>
  refreshKey: number
  bump: () => void
  toast: (text: string, tone?: 'ok' | 'bad') => void
  run: <T = any>(p: Promise<T>, okText?: string) => Promise<T | null>
}

const Ctx = createContext<AppCtx | null>(null)

export function AppProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<JSONObj | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [toasts, setToasts] = useState<Toast[]>([])

  const toast = useCallback((text: string, tone: 'ok' | 'bad' = 'ok') => {
    const id = Date.now() + Math.random()
    setToasts((prev) => [...prev, { id, text, tone }])
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, tone === 'bad' ? 6000 : 3600)
  }, [])

  const reloadMeta = useCallback(async () => {
    const data = await api.get<JSONObj>('/api/meta')
    setMeta(data)
  }, [])

  const bump = useCallback(() => setRefreshKey((k) => k + 1), [])

  const run = useCallback(
    async <T,>(p: Promise<T>, okText?: string): Promise<T | null> => {
      try {
        const res = await p
        if (okText) toast(okText)
        return res
      } catch (err) {
        toast(err instanceof Error ? err.message : String(err), 'bad')
        return null
      }
    },
    [toast],
  )

  useEffect(() => {
    reloadMeta().catch((err) => toast(String(err), 'bad'))
  }, [reloadMeta, toast])

  const value = useMemo(
    () => ({ meta, clock: (meta?.clock as JSONObj) ?? null, reloadMeta, refreshKey, bump, toast, run }),
    [meta, reloadMeta, refreshKey, bump, toast, run],
  )

  return (
    <Ctx.Provider value={value}>
      {children}
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.tone === 'bad' ? 'bad' : ''}`}>
          {t.text}
        </div>
      ))}
    </Ctx.Provider>
  )
}

export function useApp(): AppCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useApp outside provider')
  return ctx
}
