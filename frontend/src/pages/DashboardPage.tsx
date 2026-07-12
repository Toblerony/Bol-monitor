import { Package, Bell, Filter, Activity, Clock, ChevronRight, RefreshCw, Shield } from "lucide-react"
import { Link } from "react-router-dom"
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, BarChart, Bar } from "recharts"
import { useMonitoring } from "@/contexts/MonitoringContext"
import { getDashboardCharts } from "@/lib/api"
import type { DashboardCharts } from "@/types"
import { useEffect, useState } from "react"
import { StatCard, Spinner } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { formatDate, formatIntervalRange } from "@/lib/utils"
import { cn } from "@/lib/utils"

function InfoCard({
  to,
  icon: Icon,
  label,
  value,
  iconClass,
}: {
  to: string
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string | number
  iconClass?: string
}) {
  return (
    <Link to={to}>
      <Card className="hover:shadow-md hover:border-primary/30 transition-all cursor-pointer group">
        <CardContent className="p-5 flex items-center gap-4">
          <div className={cn("h-11 w-11 rounded-xl flex items-center justify-center", iconClass || "bg-muted")}>
            <Icon className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <p className="text-xs text-muted-foreground font-medium uppercase tracking-wide">{label}</p>
            <p className="text-sm font-semibold mt-0.5">{value}</p>
          </div>
          <ChevronRight className="h-4 w-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
        </CardContent>
      </Card>
    </Link>
  )
}

export default function DashboardPage() {
  const { stats, settings, loading } = useMonitoring()
  const [charts, setCharts] = useState<DashboardCharts | null>(null)

  useEffect(() => {
    getDashboardCharts().then(({ data }) => setCharts(data)).catch(() => {})
  }, [])

  if (loading && !stats) {
    return <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>
  }

  const byStatus = stats?.by_status ?? {}

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-muted-foreground text-sm mt-1">Bol.com monitoring & Telegram alerts</p>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard title="Profiles active" value={stats?.profiles_enabled ?? 0} icon={Filter} to="/profiles" />
        <StatCard title="Tracked products" value={stats?.tracked_products ?? 0} icon={Package} to="/products" />
        <StatCard title="Alerts today" value={stats?.alerts_today ?? 0} icon={Bell} to="/alerts" />
        <StatCard title="In stock" value={byStatus.in_stock ?? 0} icon={Activity} to="/products" />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <InfoCard
          to="/settings"
          icon={Shield}
          label="Bol session"
          value={stats?.bol_session_ok ? "Connected" : "Run login-bol.bat"}
          iconClass={stats?.bol_session_ok ? "bg-success/10 text-success" : "bg-warning/10 text-warning"}
        />
        <InfoCard
          to="/settings"
          icon={Clock}
          label="Last sitemap scan"
          value={settings?.last_scan_at ? formatDate(settings.last_scan_at) : "Not yet"}
          iconClass="bg-muted text-muted-foreground"
        />
        <InfoCard
          to="/settings"
          icon={RefreshCw}
          label="Poll speed (online)"
          value={formatIntervalRange(settings?.poll_online_min ?? 4, settings?.poll_online_max ?? 8)}
          iconClass="bg-primary/10 text-primary"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Link to="/products">
          <Card className="hover:shadow-md hover:border-primary/20 transition-all cursor-pointer h-full">
            <CardHeader className="pb-2 flex flex-row items-center justify-between">
              <CardTitle className="text-base">Products discovered per day</CardTitle>
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={charts?.products_per_day ?? []}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} tickFormatter={(v) => v.slice(5)} />
                  <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                  <Tooltip contentStyle={{ borderRadius: 8, border: "1px solid var(--color-border)" }} />
                  <Area type="monotone" dataKey="count" stroke="var(--color-primary)" fill="var(--color-primary)" fillOpacity={0.12} strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </Link>

        <Link to="/alerts">
          <Card className="hover:shadow-md hover:border-primary/20 transition-all cursor-pointer h-full">
            <CardHeader className="pb-2 flex flex-row items-center justify-between">
              <CardTitle className="text-base">Telegram alerts per day</CardTitle>
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={charts?.alerts_per_day ?? []}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} tickFormatter={(v) => v.slice(5)} />
                  <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                  <Tooltip contentStyle={{ borderRadius: 8, border: "1px solid var(--color-border)" }} />
                  <Bar dataKey="count" fill="var(--color-primary)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </Link>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Products by status</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            {Object.entries(byStatus).map(([k, v]) => (
              <span key={k} className="px-3 py-1 rounded-full bg-secondary text-sm">
                {k.replace("_", " ")}: <strong>{v}</strong>
              </span>
            ))}
            {Object.keys(byStatus).length === 0 && (
              <span className="text-muted-foreground text-sm">No products tracked yet — add profiles and start monitoring</span>
            )}
          </div>
        </CardContent>
      </Card>

      <div className="rounded-xl border border-primary/20 bg-primary/5 p-4 text-sm">
        <strong>Two alert types:</strong> 🟢 <em>New online</em> when a matching product goes live (even OOS).
        📦 <em>In stock</em> when add-to-cart becomes available.
      </div>
    </div>
  )
}
