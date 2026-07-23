import { FormEvent, useEffect, useState } from "react"
import { ChevronDown, ChevronRight, Shield, Send, Trash2, MessageSquare } from "lucide-react"
import {
  clearBolSession,
  getBolLoginStatus,
  getMonitoring,
  testDiscord,
  testTelegram,
  updateMonitoring,
} from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input, Label } from "@/components/ui/input"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge, Spinner } from "@/components/ui/badge"
import { useToast } from "@/contexts/ToastContext"
import { apiErrorMessage } from "@/lib/utils"

export default function SettingsPage() {
  const [form, setForm] = useState<Record<string, string | boolean>>({})
  const [bolSession, setBolSession] = useState<{
    logged_in: boolean
    has_session: boolean
    has_file: boolean
    has_database: boolean
    message: string
  } | null>(null)
  const [clearing, setClearing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testingDiscord, setTestingDiscord] = useState(false)
  const [testingTelegram, setTestingTelegram] = useState(false)
  const { toast } = useToast()

  async function loadBolStatus() {
    const { data } = await getBolLoginStatus()
    setBolSession(data)
  }

  useEffect(() => {
    getMonitoring().then(({ data }) => {
      setForm({
        discord_webhook_url: data.discord_webhook_url || "",
        alerts_telegram: data.alerts_telegram ?? false,
        telegram_bot_token: data.telegram_bot_token || "",
        telegram_chat_id: data.telegram_chat_id || "",
        sitemap_scan_interval_sec: String(data.sitemap_scan_interval_sec),
        poll_online_min: String(data.poll_online_min),
        poll_online_max: String(data.poll_online_max),
        poll_offline_min: String(data.poll_offline_min),
        poll_offline_max: String(data.poll_offline_max),
      })
    })
    loadBolStatus()
  }, [])

  async function save(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    try {
      await updateMonitoring({
        discord_webhook_url: form.discord_webhook_url,
        alerts_discord: true,
        alerts_telegram: form.alerts_telegram,
        telegram_bot_token: form.telegram_bot_token,
        telegram_chat_id: form.telegram_chat_id,
        alerts_new_online: true,
        alerts_in_stock: true,
        sitemap_scan_interval_sec: parseFloat(String(form.sitemap_scan_interval_sec)),
        poll_online_min: Math.min(15, Math.max(5, parseFloat(String(form.poll_online_min)) || 5)),
        poll_online_max: Math.min(15, Math.max(5, parseFloat(String(form.poll_online_max)) || 10)),
        poll_offline_min: parseFloat(String(form.poll_offline_min)),
        poll_offline_max: parseFloat(String(form.poll_offline_max)),
      })
      toast("Settings saved", "success")
      const { data } = await getMonitoring()
      setForm((prev) => ({
        ...prev,
        poll_online_min: String(data.poll_online_min),
        poll_online_max: String(data.poll_online_max),
        sitemap_scan_interval_sec: String(data.sitemap_scan_interval_sec),
      }))
    } catch (err) {
      toast(apiErrorMessage(err), "error")
    } finally {
      setSaving(false)
    }
  }

  async function testDc() {
    setTestingDiscord(true)
    try {
      const { data } = await testDiscord()
      toast(data.ok ? "Discord test sent!" : data.message, data.ok ? "success" : "error")
    } catch {
      toast("Discord test failed", "error")
    } finally {
      setTestingDiscord(false)
    }
  }

  async function testTg() {
    setTestingTelegram(true)
    try {
      const { data } = await testTelegram()
      toast(data.ok ? "Telegram test sent!" : data.message, data.ok ? "success" : "error")
    } catch {
      toast("Telegram test failed", "error")
    } finally {
      setTestingTelegram(false)
    }
  }

  async function handleClearSession() {
    if (!confirm("Delete Bol session?\n\nRun login-bol.bat on your PC to sign in again.")) return
    setClearing(true)
    try {
      await clearBolSession()
      await loadBolStatus()
      toast("Session cleared — run login-bol.bat", "success")
    } catch {
      toast("Failed to clear session", "error")
    } finally {
      setClearing(false)
    }
  }

  const telegramEnabled = Boolean(form.alerts_telegram)

  return (
    <div className="space-y-6 max-w-3xl animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-muted-foreground text-sm mt-1">Discord alerts, optional Telegram, Bol session, monitoring speed</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base"><Shield className="h-4 w-4" /> Bol session</CardTitle>
          <CardDescription>Same DATABASE_URL on PC and Render — login once with login-bol.bat</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {bolSession ? (
            <>
              <p className={bolSession.has_session ? "text-success text-sm font-medium" : "text-warning text-sm font-medium"}>
                {bolSession.message}
              </p>
              <div className="flex flex-wrap gap-2">
                <Badge variant={bolSession.has_database ? "success" : "secondary"}>Database {bolSession.has_database ? "OK" : "missing"}</Badge>
                <Badge variant={bolSession.has_file ? "success" : "secondary"}>Local file {bolSession.has_file ? "OK" : "missing"}</Badge>
              </div>
            </>
          ) : (
            <Spinner />
          )}
          <ol className="text-xs text-muted-foreground list-decimal list-inside space-y-1 border rounded-lg p-3 bg-muted/30">
            <li>Paste Neon DATABASE_URL in backend/.env</li>
            <li>Double-click <strong>login-bol.bat</strong></li>
            <li>Log in to bol.com in Chromium</li>
            <li>Dashboard → Start monitoring</li>
          </ol>
          <Button variant="outline" size="sm" className="text-destructive border-destructive/30" onClick={handleClearSession} disabled={clearing}>
            {clearing ? <Spinner className="h-4 w-4" /> : <Trash2 className="h-4 w-4" />}
            Clear Bol session
          </Button>
        </CardContent>
      </Card>

      <Card className="border-primary/20">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <MessageSquare className="h-4 w-4" /> Discord alerts
            <Badge variant="success" className="text-[10px]">Default</Badge>
          </CardTitle>
          <CardDescription>
            Alerts: new online + in stock. Paste webhook URL below.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={save} className="space-y-4">
            <div>
              <Label>Webhook URL</Label>
              <Input
                className="mt-1 font-mono text-xs"
                placeholder="https://discord.com/api/webhooks/123456789/abcdef..."
                value={String(form.discord_webhook_url || "")}
                onChange={(e) => setForm({ ...form, discord_webhook_url: e.target.value })}
              />
            </div>
            <ol className="text-xs text-muted-foreground list-decimal list-inside space-y-1 border rounded-lg p-3 bg-muted/30">
              <li>Discord → left sidebar <strong>+</strong> → Create My Own → <strong>For me and my friends</strong></li>
              <li>Name your server (e.g. Bol Monitor) → Create</li>
              <li>Create a text channel (e.g. <strong>#alerts</strong>)</li>
              <li>Channel ⚙️ → <strong>Integrations</strong> → <strong>Webhooks</strong> → New Webhook</li>
              <li>Copy <strong>Webhook URL</strong> → paste above → Save → Send test</li>
            </ol>
            <Button type="button" variant="outline" size="sm" onClick={testDc} disabled={testingDiscord}>
              {testingDiscord ? <Spinner className="h-4 w-4" /> : <Send className="h-4 w-4" />}
              Send Discord test
            </Button>

            <div className="pt-4 border-t">
              <button
                type="button"
                className="flex w-full items-center gap-2 text-left text-sm font-medium hover:text-primary transition-colors"
                onClick={() => setForm({ ...form, alerts_telegram: !telegramEnabled })}
              >
                {telegramEnabled ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
                Telegram alerts (optional)
                {telegramEnabled && <Badge variant="secondary" className="text-[10px] ml-1">On</Badge>}
              </button>
              {!telegramEnabled && (
                <p className="text-xs text-muted-foreground mt-2">Click to enable and configure bot token + chat ID</p>
              )}
              {telegramEnabled && (
                <div className="mt-4 space-y-4 pl-1">
                  <div>
                    <Label>Bot token</Label>
                    <Input className="mt-1" value={String(form.telegram_bot_token || "")} onChange={(e) => setForm({ ...form, telegram_bot_token: e.target.value })} />
                  </div>
                  <div>
                    <Label>Chat ID</Label>
                    <Input className="mt-1" value={String(form.telegram_chat_id || "")} onChange={(e) => setForm({ ...form, telegram_chat_id: e.target.value })} />
                  </div>
                  <Button type="button" variant="outline" size="sm" onClick={testTg} disabled={testingTelegram}>
                    {testingTelegram ? <Spinner className="h-4 w-4" /> : <Send className="h-4 w-4" />}
                    Send Telegram test
                  </Button>
                </div>
              )}
            </div>

            <div className="pt-2 border-t">
              <CardTitle className="text-base mb-1">Speed</CardTitle>
              <p className="text-xs text-muted-foreground mb-3">
                Bol.com product pages are scraped as HTML (not Target Redsky). Sitemap finds new URLs; then each tracked product page is visited on a random interval.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <Label className="text-xs">Sitemap scan interval (sec)</Label>
                  <Input
                    type="number"
                    min={300}
                    max={900}
                    value={String(form.sitemap_scan_interval_sec || "")}
                    onChange={(e) => setForm({ ...form, sitemap_scan_interval_sec: e.target.value })}
                  />
                  <p className="text-[11px] text-muted-foreground mt-1">300–900 = every 5–15 minutes</p>
                </div>
                <div>
                  <Label className="text-xs">Product page visit min / max (sec)</Label>
                  <div className="flex gap-2">
                    <Input
                      type="number"
                      min={5}
                      max={15}
                      placeholder="5"
                      value={String(form.poll_online_min || "")}
                      onChange={(e) => setForm({ ...form, poll_online_min: e.target.value })}
                    />
                    <Input
                      type="number"
                      min={5}
                      max={15}
                      placeholder="10"
                      value={String(form.poll_online_max || "")}
                      onChange={(e) => setForm({ ...form, poll_online_max: e.target.value })}
                    />
                  </div>
                  <p className="text-[11px] text-muted-foreground mt-1">Random delay between visits — min 5, max 15</p>
                </div>
                <div className="sm:col-span-2">
                  <Label className="text-xs">Offline product poll min / max (sec)</Label>
                  <div className="flex gap-2">
                    <Input value={String(form.poll_offline_min || "")} onChange={(e) => setForm({ ...form, poll_offline_min: e.target.value })} />
                    <Input value={String(form.poll_offline_max || "")} onChange={(e) => setForm({ ...form, poll_offline_max: e.target.value })} />
                  </div>
                  <p className="text-[11px] text-muted-foreground mt-1">Slower checks when a page is offline / not found</p>
                </div>
              </div>
            </div>

            <Button type="submit" disabled={saving}>{saving ? <Spinner className="h-4 w-4" /> : null} Save settings</Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
