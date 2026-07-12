import { FormEvent, useEffect, useState, type ReactNode } from "react"
import { Plus, Pencil, Trash2, Filter } from "lucide-react"
import { createProfile, deleteProfile, getProfiles, updateProfile, type Profile } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input, Label } from "@/components/ui/input"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge, EmptyState, Spinner } from "@/components/ui/badge"
import { useToast } from "@/contexts/ToastContext"
import { apiErrorMessage } from "@/lib/utils"

const empty = {
  name: "",
  title_keywords: "",
  category_keywords: "",
  exclude_keywords: "",
  price_min: "",
  price_max: "",
}

function splitKw(s: string) {
  return s.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean)
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div>
      <Label>{label}</Label>
      <div className="mt-1">{children}</div>
      {hint && <p className="text-xs text-muted-foreground mt-1">{hint}</p>}
    </div>
  )
}

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [form, setForm] = useState(empty)
  const [editId, setEditId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const { toast } = useToast()

  const load = () =>
    getProfiles()
      .then(({ data }) => setProfiles(data))
      .finally(() => setLoading(false))

  useEffect(() => {
    load()
  }, [])

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    try {
      const payload = {
        name: form.name,
        title_keywords: splitKw(form.title_keywords),
        category_keywords: splitKw(form.category_keywords),
        exclude_keywords: splitKw(form.exclude_keywords),
        price_min: form.price_min ? parseFloat(form.price_min) : null,
        price_max: form.price_max ? parseFloat(form.price_max) : null,
        is_enabled: true,
      }
      if (editId) await updateProfile(editId, payload)
      else await createProfile(payload)
      toast(editId ? "Profile updated" : "Profile created", "success")
      setForm(empty)
      setEditId(null)
      load()
    } catch (err) {
      toast(apiErrorMessage(err), "error")
    } finally {
      setSaving(false)
    }
  }

  async function onDelete(id: number) {
    if (!confirm("Delete this profile and its tracked products?")) return
    try {
      await deleteProfile(id)
      toast("Profile deleted", "success")
      load()
    } catch {
      toast("Delete failed", "error")
    }
  }

  function startEdit(p: Profile) {
    setEditId(p.id)
    setForm({
      name: p.name,
      title_keywords: p.title_keywords.join(", "),
      category_keywords: p.category_keywords.join(", "),
      exclude_keywords: p.exclude_keywords.join(", "),
      price_min: p.price_min != null ? String(p.price_min) : "",
      price_max: p.price_max != null ? String(p.price_max) : "",
    })
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-bold">Product profiles</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Category profile (e.g. Pokémon) discovers all new bol.com products in that category via sitemap + breadcrumb matching.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            {editId ? <Pencil className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
            {editId ? "Edit profile" : "New profile"}
          </CardTitle>
          <CardDescription>
            Category-only: leave title empty, set category e.g. &quot;Pokémon, Pokémon kaarten&quot; — all products in that bol category are tracked.
            Title + category: both must match (e.g. ETB + Pokémon).
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <Field label="Profile name">
              <Input placeholder="e.g. Pokémon ETB" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
            </Field>
            <Field label="Title keywords (optional)" hint="Product title filter — leave empty for whole category">
              <Input placeholder="Elite Trainer Box, ETB" value={form.title_keywords} onChange={(e) => setForm({ ...form, title_keywords: e.target.value })} />
            </Field>
            <Field label="Category keywords" hint="Bol breadcrumb path e.g. Pokémon kaarten — matches Je vindt dit artikel in + JSON-LD">
              <Input placeholder="Pokémon, Pokémon kaarten" value={form.category_keywords} onChange={(e) => setForm({ ...form, category_keywords: e.target.value })} />
            </Field>
            <Field label="Exclude keywords (optional)">
              <Input placeholder="Used, damaged" value={form.exclude_keywords} onChange={(e) => setForm({ ...form, exclude_keywords: e.target.value })} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Min price €">
                <Input type="number" step="0.01" value={form.price_min} onChange={(e) => setForm({ ...form, price_min: e.target.value })} />
              </Field>
              <Field label="Max price €">
                <Input type="number" step="0.01" value={form.price_max} onChange={(e) => setForm({ ...form, price_max: e.target.value })} />
              </Field>
            </div>
            <div className="flex gap-2">
              <Button type="submit" disabled={saving}>
                {saving ? <Spinner className="h-4 w-4" /> : null}
                {editId ? "Update profile" : "Create profile"}
              </Button>
              {editId && (
                <Button type="button" variant="outline" onClick={() => { setEditId(null); setForm(empty) }}>
                  Cancel
                </Button>
              )}
            </div>
          </form>
        </CardContent>
      </Card>

      {loading ? (
        <div className="flex justify-center py-12"><Spinner className="h-8 w-8" /></div>
      ) : profiles.length === 0 ? (
        <EmptyState icon={Filter} title="No profiles yet" description="Create a profile to start discovering products" />
      ) : (
        <div className="grid gap-4">
          {profiles.map((p) => (
            <Card key={p.id} className="hover:shadow-md transition-shadow">
              <CardContent className="p-5 flex flex-col sm:flex-row sm:items-center gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h3 className="font-semibold">{p.name}</h3>
                    <Badge variant={p.is_enabled ? "success" : "secondary"}>{p.is_enabled ? "Active" : "Off"}</Badge>
                    <Badge variant="secondary">{p.tracked_count} tracked</Badge>
                  </div>
                  <p className="text-sm text-muted-foreground mt-2">
                    <span className="font-medium text-foreground">Title:</span> {p.title_keywords.join(", ") || "—"}
                  </p>
                  <p className="text-sm text-muted-foreground mt-1">
                    <span className="font-medium text-foreground">Category:</span> {p.category_keywords.join(", ") || "—"}
                  </p>
                  {(p.price_min != null || p.price_max != null) && (
                    <p className="text-xs text-muted-foreground mt-1">
                      Price: €{p.price_min ?? "0"} – €{p.price_max ?? "∞"}
                    </p>
                  )}
                </div>
                <div className="flex gap-2 shrink-0">
                  <Button variant="outline" size="sm" onClick={() => startEdit(p)}><Pencil className="h-3.5 w-3.5" /> Edit</Button>
                  <Button variant="outline" size="sm" className="text-destructive border-destructive/30" onClick={() => onDelete(p.id)}>
                    <Trash2 className="h-3.5 w-3.5" /> Delete
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
