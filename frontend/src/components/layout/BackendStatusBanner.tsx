import { useCallback, useEffect, useRef, useState } from "react"
import { AlertTriangle, RefreshCw } from "lucide-react"
import { checkBackendHealth } from "@/lib/api"
import { isLiveDeployment } from "@/lib/apiConfig"
import { Button } from "@/components/ui/button"

const FAILS_BEFORE_BANNER = 8
const POLL_MS = 30000
const GRACE_MS = 25000

export default function BackendStatusBanner() {
  const [online, setOnline] = useState(true)
  const [checking, setChecking] = useState(false)
  const failCountRef = useRef(0)
  const mountedAtRef = useRef(Date.now())
  const checkingRef = useRef(false)

  const ping = useCallback(async () => {
    if (checkingRef.current) return
    checkingRef.current = true
    setChecking(true)
    try {
      const health = await checkBackendHealth()
      if (health.ok) {
        failCountRef.current = 0
        setOnline(true)
        return
      }
      failCountRef.current += 1
      const pastGrace = Date.now() - mountedAtRef.current > GRACE_MS
      if (pastGrace && failCountRef.current >= FAILS_BEFORE_BANNER) {
        setOnline(false)
      }
    } finally {
      checkingRef.current = false
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    mountedAtRef.current = Date.now()
    failCountRef.current = 0
    setOnline(true)
    ping()
    const id = window.setInterval(ping, POLL_MS)
    return () => window.clearInterval(id)
  }, [ping])

  if (online) return null

  return (
    <div className="mb-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm">
      <div className="flex items-start gap-2 text-destructive">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
        <div>
          <p className="font-semibold">Backend not connected</p>
          <p className="text-xs text-muted-foreground mt-0.5">
            {isLiveDeployment
              ? "Render may be waking up — wait 30–60 seconds, then Retry."
              : "Start the backend (port 8003) with startall.bat, then Retry."}
          </p>
        </div>
      </div>
      <Button variant="outline" size="sm" onClick={ping} disabled={checking} className="shrink-0">
        <RefreshCw className={`h-3.5 w-3.5 ${checking ? "animate-spin" : ""}`} />
        Retry
      </Button>
    </div>
  )
}
