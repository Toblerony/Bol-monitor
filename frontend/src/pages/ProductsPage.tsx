import { useEffect, useMemo, useState } from "react"
import { ExternalLink, Package } from "lucide-react"
import { getProducts, type TrackedProduct } from "@/lib/api"
import { Card, CardContent } from "@/components/ui/card"
import { Badge, EmptyState, Spinner } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn, formatDate } from "@/lib/utils"

const statusVariant: Record<string, "secondary" | "warning" | "success" | "destructive"> = {
  offline: "secondary",
  online_oos: "warning",
  in_stock: "success",
  unknown: "secondary",
}

const statusLabel: Record<string, string> = {
  offline: "Offline",
  online_oos: "Online (OOS)",
  in_stock: "In stock",
  unknown: "Unknown",
}

const tabs = [
  { id: "all", label: "All" },
  { id: "in_stock", label: "In stock" },
  { id: "online_oos", label: "Online OOS" },
  { id: "offline", label: "Offline" },
] as const

export default function ProductsPage() {
  const [items, setItems] = useState<TrackedProduct[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<(typeof tabs)[number]["id"]>("all")

  useEffect(() => {
    const load = () =>
      getProducts()
        .then(({ data }) => setItems(data))
        .finally(() => setLoading(false))
    load()
    const t = setInterval(load, 8000)
    return () => clearInterval(t)
  }, [])

  const filtered = useMemo(
    () => (tab === "all" ? items : items.filter((p) => p.status === tab)),
    [items, tab],
  )

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length }
    for (const p of items) c[p.status] = (c[p.status] || 0) + 1
    return c
  }, [items])

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold">Tracked products</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Auto-discovered from profiles — online, offline, out-of-stock, and in-stock states
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        {tabs.map((t) => (
          <Button
            key={t.id}
            variant={tab === t.id ? "default" : "outline"}
            size="sm"
            onClick={() => setTab(t.id)}
          >
            {t.label}
            <span className="ml-1 opacity-70">({counts[t.id] ?? 0})</span>
          </Button>
        ))}
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>
      ) : filtered.length === 0 ? (
        <EmptyState icon={Package} title="No products in this view" description="Add profiles and start monitoring" />
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 border-b border-border">
                <tr>
                  <th className="text-left p-3 font-medium">Product</th>
                  <th className="text-left p-3 font-medium">Profile</th>
                  <th className="text-left p-3 font-medium">Price</th>
                  <th className="text-left p-3 font-medium">Status</th>
                  <th className="text-left p-3 font-medium">Last check</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((p) => (
                  <tr key={p.id} className="border-b border-border hover:bg-muted/20 transition-colors">
                    <td className="p-3 max-w-xs">
                      <a href={p.url} target="_blank" rel="noreferrer" className="font-medium text-primary hover:underline inline-flex items-center gap-1">
                        {p.title || p.url.slice(0, 55)}
                        <ExternalLink className="h-3 w-3 shrink-0 opacity-60" />
                      </a>
                      <p className="text-xs text-muted-foreground truncate mt-0.5">{p.categories || p.brand}</p>
                    </td>
                    <td className="p-3">{p.profile_name}</td>
                    <td className="p-3">{p.price_text ? `€${p.price_text}` : "—"}</td>
                    <td className="p-3">
                      <Badge variant={statusVariant[p.status] || "secondary"} className={cn(p.status === "in_stock" && "animate-pulse")}>
                        {statusLabel[p.status] || p.status}
                      </Badge>
                    </td>
                    <td className="p-3 text-muted-foreground text-xs">
                      {p.last_checked_at ? formatDate(p.last_checked_at) : "—"}
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
