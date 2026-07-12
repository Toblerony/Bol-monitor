import { FormEvent, useEffect, useState } from "react"
import { Globe, Play, Save, ShieldCheck } from "lucide-react"
import { getProxies, testProxies, updateProxies } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge, Spinner } from "@/components/ui/badge"
import { useToast } from "@/contexts/ToastContext"
import { apiErrorMessage } from "@/lib/utils"

interface ProxyTestRow {
  proxy: string
  ok: boolean
  message: string
}

export default function ProxiesPage() {
  const [useProxies, setUseProxies] = useState(false)
  const [proxyLines, setProxyLines] = useState("")
  const [proxyCount, setProxyCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResults, setTestResults] = useState<ProxyTestRow[]>([])
  const { toast } = useToast()

  async function load() {
    setLoading(true)
    try {
      const { data } = await getProxies()
      setUseProxies(data.use_proxies)
      setProxyLines(data.proxy_lines || "")
      setProxyCount(data.proxy_count || 0)
      setTestResults([])
    } catch (err) {
      toast(apiErrorMessage(err, "Failed to load proxies"), "error")
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function save(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    try {
      const { data } = await updateProxies({ use_proxies: useProxies, proxy_lines: proxyLines })
      setProxyCount(data.proxy_count || 0)
      toast(`Saved ${data.proxy_count} proxy line(s)`, "success")
    } catch (err) {
      toast(apiErrorMessage(err), "error")
    } finally {
      setSaving(false)
    }
  }

  async function runTest() {
    setTesting(true)
    setTestResults([])
    try {
      if (!proxyLines.trim()) {
        toast("Add proxy lines first, then save", "error")
        return
      }
      await updateProxies({ use_proxies: useProxies, proxy_lines: proxyLines })
      const { data } = await testProxies()
      setTestResults(data.results || [])
      const ok = (data.results || []).filter((r: ProxyTestRow) => r.ok).length
      toast(`Test done — ${ok}/${data.results?.length || 0} OK`, ok > 0 ? "success" : "error")
    } catch (err) {
      toast(apiErrorMessage(err, "Proxy test failed"), "error")
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh] text-muted-foreground">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold">Proxies</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Enable when bol.com blocks your IP. Disable to test with your direct connection.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Globe className="h-4 w-4" /> Proxy pool
          </CardTitle>
          <CardDescription>One proxy per line: host:port:username:password</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={save} className="space-y-4">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                checked={useProxies}
                onChange={(e) => setUseProxies(e.target.checked)}
              />
              Use proxies for sitemap scan &amp; stock checks
            </label>

            <textarea
              className="w-full min-h-[220px] rounded-lg border border-border bg-background px-3 py-2 text-sm font-mono"
              placeholder={"nl-proxy.example.com:12345:user:pass\nnl-proxy2.example.com:12345:user:pass"}
              value={proxyLines}
              onChange={(e) => setProxyLines(e.target.value)}
              spellCheck={false}
            />

            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={proxyCount > 0 ? "success" : "secondary"}>
                {proxyCount} proxy{proxyCount === 1 ? "" : "ies"} parsed
              </Badge>
              {useProxies && proxyCount === 0 && (
                <span className="text-xs text-warning">Add at least one proxy before starting monitor</span>
              )}
              {!useProxies && (
                <span className="text-xs text-muted-foreground">Proxies off — requests use your server IP</span>
              )}
            </div>

            <div className="rounded-lg border bg-muted/30 p-3 text-xs text-muted-foreground space-y-1">
              <p className="flex items-center gap-1.5 font-medium text-foreground">
                <ShieldCheck className="h-3.5 w-3.5" /> Auto rotation
              </p>
              <p>IP blocked (403/429), timeout, or &quot;ip-geblokkeerd&quot; → cooldown 120s → next proxy.</p>
              <p>Sitemap scans are low bandwidth (~every 5–15 min). Stock checks use more data (4–8s per online product).</p>
            </div>

            <div className="flex flex-wrap gap-2">
              <Button type="submit" disabled={saving}>
                {saving ? <Spinner className="h-4 w-4" /> : <Save className="h-4 w-4" />}
                Save proxies
              </Button>
              <Button type="button" variant="outline" onClick={runTest} disabled={testing}>
                {testing ? <Spinner className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                Test all (bol.com)
              </Button>
            </div>
          </form>

          {testResults.length > 0 && (
            <div className="mt-4 space-y-2">
              <p className="text-sm font-medium">Test results</p>
              {testResults.map((r) => (
                <div
                  key={r.proxy}
                  className="flex items-start justify-between gap-3 rounded-lg border px-3 py-2 text-sm"
                >
                  <span className="font-mono text-xs break-all">{r.proxy}</span>
                  <div className="text-right shrink-0">
                    <Badge variant={r.ok ? "success" : "destructive"}>{r.ok ? "OK" : "FAIL"}</Badge>
                    <p className="text-xs text-muted-foreground mt-1 max-w-[200px]">{r.message}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
