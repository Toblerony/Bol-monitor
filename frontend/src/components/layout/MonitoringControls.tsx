import { Play, Square } from "lucide-react"
import { useMonitoring } from "@/contexts/MonitoringContext"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export default function MonitoringControls({ compact = false }: { compact?: boolean }) {
  const { settings, toggling, startMonitoring, stopMonitoring } = useMonitoring()
  const isOn = settings?.is_running ?? false

  if (compact) {
    return (
      <div className="flex items-center gap-2">
        <StatusDot on={isOn} />
        {isOn ? (
          <Button variant="destructive" size="sm" onClick={stopMonitoring} disabled={toggling} className="gap-1.5">
            {toggling ? <Spinner className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5 fill-current" />}
            Stop
          </Button>
        ) : (
          <Button size="sm" onClick={startMonitoring} disabled={toggling} className="gap-1.5 bg-emerald-600 hover:bg-emerald-700 text-white">
            {toggling ? <Spinner className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5 fill-current" />}
            Start
          </Button>
        )}
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3">
      <StatusDot on={isOn} large />
      {isOn ? (
        <Button variant="destructive" size="lg" onClick={stopMonitoring} disabled={toggling} className="gap-2 min-w-[160px]">
          {toggling ? <Spinner /> : <Square className="h-4 w-4 fill-current" />}
          Stop monitoring
        </Button>
      ) : (
        <Button size="lg" onClick={startMonitoring} disabled={toggling} className="gap-2 min-w-[160px] bg-emerald-600 hover:bg-emerald-700 text-white">
          {toggling ? <Spinner /> : <Play className="h-4 w-4 fill-current" />}
          Start monitoring
        </Button>
      )}
    </div>
  )
}

function StatusDot({ on, large }: { on: boolean; large?: boolean }) {
  if (large) {
    return (
      <div className={cn("flex items-center justify-center rounded-full h-10 w-10", on ? "bg-emerald-500/15" : "bg-muted")}>
        <span className={cn("h-3 w-3 rounded-full", on ? "bg-emerald-500 animate-pulse" : "bg-red-500")} />
      </div>
    )
  }
  return (
    <div className="flex items-center gap-1.5 px-2 py-1 rounded-full bg-muted/50 border border-border">
      <span className={cn("h-2 w-2 rounded-full", on ? "bg-emerald-500 animate-pulse" : "bg-red-500")} />
      <span className="text-xs font-medium hidden sm:inline">{on ? "ON" : "OFF"}</span>
    </div>
  )
}
