import { useEffect, useState } from "react"
import { Bell, ExternalLink } from "lucide-react"
import { getAlerts } from "@/lib/api"
import { Card, CardContent } from "@/components/ui/card"
import { Badge, EmptyState, Spinner } from "@/components/ui/badge"
import { formatDate } from "@/lib/utils"

interface AlertRow {
  id: number
  alert_type: string
  product_url: string
  product_title: string
  price_text: string | null
  telegram_ok: boolean
  discord_ok: boolean
  sent_at: string
}

function DeliveryBadges({ row }: { row: AlertRow }) {
  const any = row.discord_ok || row.telegram_ok
  if (!any) {
    return <Badge variant="destructive">Failed</Badge>
  }
  return (
    <div className="flex flex-wrap gap-1">
      {row.discord_ok && <Badge variant="success">Discord</Badge>}
      {row.telegram_ok && <Badge variant="success">Telegram</Badge>}
    </div>
  )
}

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const load = () =>
      getAlerts()
        .then(({ data }) => setAlerts(data))
        .finally(() => setLoading(false))
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold">Alerts</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Discord (default) and/or Telegram — <strong>New online</strong> and <strong>In stock</strong>
        </p>
      </div>

      <div className="grid sm:grid-cols-2 gap-4">
        <Card className="border-primary/20 bg-primary/5">
          <CardContent className="p-4">
            <p className="text-sm font-semibold text-primary">🟢 New online</p>
            <p className="text-xs text-muted-foreground mt-1">New matching product URL discovered or came back online</p>
          </CardContent>
        </Card>
        <Card className="border-success/20 bg-success/5">
          <CardContent className="p-4">
            <p className="text-sm font-semibold text-success">📦 In stock</p>
            <p className="text-xs text-muted-foreground mt-1">Product became available to buy on bol.com</p>
          </CardContent>
        </Card>
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>
      ) : alerts.length === 0 ? (
        <EmptyState icon={Bell} title="No alerts yet" description="Alerts appear here when monitoring finds matches" />
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 border-b border-border">
                <tr>
                  <th className="text-left p-3 font-medium">Type</th>
                  <th className="text-left p-3 font-medium">Product</th>
                  <th className="text-left p-3 font-medium">Price</th>
                  <th className="text-left p-3 font-medium">Sent</th>
                  <th className="text-left p-3 font-medium">Delivered</th>
                </tr>
              </thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.id} className="border-b border-border hover:bg-muted/20">
                    <td className="p-3">
                      <Badge variant={a.alert_type === "in_stock" ? "success" : "default"}>
                        {a.alert_type === "in_stock" ? "In stock" : "New online"}
                      </Badge>
                    </td>
                    <td className="p-3 max-w-sm">
                      <a href={a.product_url} target="_blank" rel="noreferrer" className="text-primary hover:underline inline-flex items-center gap-1">
                        {(a.product_title || a.product_url).slice(0, 70)}
                        <ExternalLink className="h-3 w-3 shrink-0" />
                      </a>
                    </td>
                    <td className="p-3">{a.price_text ? `€${a.price_text}` : "—"}</td>
                    <td className="p-3 text-muted-foreground text-xs">{formatDate(a.sent_at)}</td>
                    <td className="p-3">
                      <DeliveryBadges row={a} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
