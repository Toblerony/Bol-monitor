import { FormEvent, useState } from "react"
import { useNavigate } from "react-router-dom"
import { completeSetup } from "@/lib/api"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input, Label } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/badge"

export default function SetupPage() {
  const nav = useNavigate()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [confirm, setConfirm] = useState("")
  const [err, setErr] = useState("")
  const [loading, setLoading] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (password !== confirm) {
      setErr("Passwords do not match")
      return
    }
    setLoading(true)
    try {
      await completeSetup(email, password)
      nav("/login")
    } catch {
      setErr("Setup failed")
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-md shadow-lg">
        <CardHeader>
          <CardTitle>First-time setup</CardTitle>
          <CardDescription>Create your admin account</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            {err && <p className="text-destructive text-sm">{err}</p>}
            <div>
              <Label>Admin email</Label>
              <Input type="email" className="mt-1" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div>
              <Label>Password (min 6)</Label>
              <Input type="password" className="mt-1" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={6} />
            </div>
            <div>
              <Label>Confirm password</Label>
              <Input type="password" className="mt-1" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
            </div>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? <Spinner className="h-4 w-4" /> : null}
              {loading ? "Saving…" : "Save and continue"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
