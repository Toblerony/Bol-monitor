import { Navigate } from "react-router-dom"
import { useAuth } from "@/contexts/AuthContext"
import { FormEvent, useState } from "react"
import { login } from "@/lib/api"
import { Logo, BrandTitle } from "@/components/brand/Logo"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Input, Label } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/badge"

export default function LoginPage() {
  const { token, setToken } = useAuth()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [err, setErr] = useState("")
  const [loading, setLoading] = useState(false)

  if (token) return <Navigate to="/" replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    setErr("")
    try {
      const { data } = await login(email, password)
      setToken(data.access_token)
    } catch {
      setErr("Invalid email or password")
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-md shadow-lg">
        <CardHeader className="text-center space-y-3">
          <div className="flex justify-center">
            <Logo size={48} />
          </div>
          <BrandTitle />
          <CardTitle className="text-xl">Sign in</CardTitle>
          <CardDescription>Manage profiles, monitoring, and Telegram alerts</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            {err && <p className="text-destructive text-sm text-center">{err}</p>}
            <div>
              <Label>Email</Label>
              <Input type="email" className="mt-1" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div>
              <Label>Password</Label>
              <Input type="password" className="mt-1" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? <Spinner className="h-4 w-4" /> : null}
              {loading ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
