import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react"
import { getMonitoring, getStats, getBolLoginStatus, startMonitoring, stopMonitoring } from "@/lib/api"
import type { DashboardStats, MonitoringSettings } from "@/types"
import { useToast } from "@/contexts/ToastContext"
import { apiErrorMessage } from "@/lib/utils"

interface MonitoringContextType {
  settings: MonitoringSettings | null
  stats: DashboardStats | null
  loading: boolean
  toggling: boolean
  startMonitoring: () => Promise<void>
  stopMonitoring: () => Promise<void>
  refresh: () => Promise<void>
}

const MonitoringContext = createContext<MonitoringContextType | undefined>(undefined)

export function MonitoringProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<MonitoringSettings | null>(null)
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [toggling, setToggling] = useState(false)
  const { toast } = useToast()

  const refresh = useCallback(async () => {
    try {
      const [settingsRes, statsRes] = await Promise.all([getMonitoring(), getStats()])
      setSettings(settingsRes.data)
      setStats(statsRes.data)
    } catch {
      /* backend offline */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    const ms = settings?.is_running ? 8000 : 20000
    const interval = setInterval(refresh, ms)
    return () => clearInterval(interval)
  }, [refresh, settings?.is_running])

  const handleStart = async () => {
    setToggling(true)
    try {
      const { data: session } = await getBolLoginStatus()
      if (!session.has_session || !session.has_database) {
        toast(
          session.message ||
            "No Bol session saved. Open Settings → Login to Bol, then click Start.",
          "error",
        )
        await refresh()
        return
      }
      if ((settings?.profile_count ?? 0) === 0) {
        toast("Add at least one enabled product profile first.", "error")
        return
      }
      await startMonitoring()
      toast("Monitoring started", "success")
      await refresh()
    } catch (err: unknown) {
      toast(apiErrorMessage(err, "Start failed — Settings → Login to Bol first"), "error")
    } finally {
      setToggling(false)
    }
  }

  const handleStop = async () => {
    setToggling(true)
    try {
      await stopMonitoring()
      toast("Monitoring stopped", "info")
      await refresh()
    } catch {
      toast("Stop failed", "error")
    } finally {
      setToggling(false)
    }
  }

  return (
    <MonitoringContext.Provider
      value={{
        settings,
        stats,
        loading,
        toggling,
        startMonitoring: handleStart,
        stopMonitoring: handleStop,
        refresh,
      }}
    >
      {children}
    </MonitoringContext.Provider>
  )
}

export function useMonitoring() {
  const ctx = useContext(MonitoringContext)
  if (!ctx) throw new Error("useMonitoring must be used within MonitoringProvider")
  return ctx
}
