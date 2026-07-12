import { useCallback, useEffect, useRef, useState } from "react"
import { Search, Download, FileText, Trash2 } from "lucide-react"
import { getLogs, exportLogs, deleteLogs, deleteAllLogs } from "@/lib/api"
import type { ActivityLog } from "@/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent } from "@/components/ui/card"
import { Badge, Spinner, EmptyState } from "@/components/ui/badge"
import { formatDate } from "@/lib/utils"
import { useToast } from "@/contexts/ToastContext"

const levelColors: Record<string, "default" | "success" | "warning" | "destructive" | "secondary"> = {
  debug: "secondary",
  info: "default",
  warning: "warning",
  error: "destructive",
}

export default function LogsPage() {
  const [logs, setLogs] = useState<ActivityLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState("")
  const [category, setCategory] = useState("")
  const [level, setLevel] = useState("")
  const [loading, setLoading] = useState(true)
  const [deleting, setDeleting] = useState(false)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const { toast } = useToast()
  const loadSeq = useRef(0)

  const load = useCallback(async (opts?: { silent?: boolean }) => {
    const silent = opts?.silent ?? false
    const seq = ++loadSeq.current
    if (!silent) setLoading(true)
    try {
      const { data } = await getLogs({
        page,
        page_size: 50,
        search: search || undefined,
        category: category || undefined,
        level: level || undefined,
      })
      if (seq !== loadSeq.current) return
      setLogs(data.items)
      setTotal(data.total)
      if (!silent) setSelected(new Set())
    } finally {
      if (seq === loadSeq.current && !silent) setLoading(false)
    }
  }, [page, category, level, search])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") load({ silent: true })
    }
    const interval = window.setInterval(tick, 5000)
    return () => window.clearInterval(interval)
  }, [load])

  const toggleOne = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAllOnPage = () => {
    if (selected.size === logs.length && logs.length > 0) setSelected(new Set())
    else setSelected(new Set(logs.map((l) => l.id)))
  }

  const handleDeleteSelected = async () => {
    if (selected.size === 0) return
    if (!confirm(`Delete ${selected.size} selected log(s)?`)) return
    setDeleting(true)
    try {
      const { data } = await deleteLogs([...selected])
      toast(`Deleted ${data.deleted} log(s)`, "success")
      await load()
    } catch {
      toast("Failed to delete logs", "error")
    } finally {
      setDeleting(false)
    }
  }

  const handleDeleteAll = async () => {
    if (total === 0) return
    if (!confirm(`Delete ALL ${total} logs? This cannot be undone.`)) return
    setDeleting(true)
    try {
      const { data } = await deleteAllLogs()
      toast(`Deleted ${data.deleted} log(s)`, "success")
      setPage(1)
      await load()
    } catch {
      toast("Failed to delete all logs", "error")
    } finally {
      setDeleting(false)
    }
  }

  const handleExport = async () => {
    try {
      const { data } = await exportLogs()
      const url = window.URL.createObjectURL(new Blob([data]))
      const a = document.createElement("a")
      a.href = url
      a.download = "bol-logs.csv"
      a.click()
      toast("Logs exported", "success")
    } catch {
      toast("Export failed", "error")
    }
  }

  const totalPages = Math.ceil(total / 50)
  const allOnPageSelected = logs.length > 0 && selected.size === logs.length

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Activity Logs</h1>
          <p className="text-muted-foreground text-sm mt-1">Monitoring, scraper, Telegram, and system events</p>
        </div>
        <Button variant="outline" onClick={handleExport}><Download className="h-4 w-4" /> Export CSV</Button>
      </div>

      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-9"
            placeholder="Search logs..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && (setPage(1), load())}
          />
        </div>
        <select className="h-10 rounded-md border border-input bg-card px-3 text-sm" value={category} onChange={(e) => { setCategory(e.target.value); setPage(1) }}>
          <option value="">All Categories</option>
          <option value="monitoring">Monitoring</option>
          <option value="scraper">Scraper</option>
          <option value="notification">Notification</option>
          <option value="system">System</option>
          <option value="error">Error</option>
        </select>
        <select className="h-10 rounded-md border border-input bg-card px-3 text-sm" value={level} onChange={(e) => { setLevel(e.target.value); setPage(1) }}>
          <option value="">All Levels</option>
          <option value="debug">Debug</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="error">Error</option>
        </select>
        <Button onClick={() => { setPage(1); load() }}>Search</Button>
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>
      ) : logs.length === 0 ? (
        <EmptyState icon={FileText} title="No logs found" description="Start monitoring — activity will appear here" />
      ) : (
        <>
          <Card>
            <CardContent className="p-0">
              <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-border bg-muted/30">
                <div className="flex items-center gap-3">
                  <input type="checkbox" checked={allOnPageSelected} onChange={toggleAllOnPage} className="h-4 w-4 rounded accent-primary cursor-pointer" />
                  <span className="text-sm text-muted-foreground">Select all ({logs.length} on page)</span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {selected.size > 0 && (
                    <Button variant="destructive" size="sm" onClick={handleDeleteSelected} disabled={deleting}>
                      {deleting ? <Spinner /> : <Trash2 className="h-4 w-4" />}
                      Delete ({selected.size})
                    </Button>
                  )}
                  <Button variant="outline" size="sm" className="text-destructive border-destructive/30" onClick={handleDeleteAll} disabled={deleting || total === 0}>
                    Delete all
                  </Button>
                </div>
              </div>
              <div className="divide-y divide-border">
                {logs.map((log) => (
                  <div key={log.id} className="flex items-start gap-4 p-4 hover:bg-muted/30 transition-colors">
                    <input type="checkbox" checked={selected.has(log.id)} onChange={() => toggleOne(log.id)} className="h-4 w-4 mt-1 rounded accent-primary shrink-0 cursor-pointer" />
                    <Badge variant={levelColors[log.level] || "secondary"} className="mt-0.5 shrink-0">{log.level}</Badge>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge variant="secondary">{log.category}</Badge>
                        {log.source && <span className="text-xs text-muted-foreground">{log.source}</span>}
                        <span className="text-xs text-muted-foreground ml-auto">{formatDate(log.created_at)}</span>
                      </div>
                      <p className="text-sm mt-1">{log.message}</p>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2">
              <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
              <span className="text-sm text-muted-foreground">Page {page} of {totalPages} ({total} total)</span>
              <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Next</Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
