import { createContext, useContext, useEffect, useState, ReactNode } from "react"
import { getMe } from "@/lib/api"

interface AuthCtx {
  token: string | null
  email: string | null
  setToken: (t: string | null) => void
  logout: () => void
  loading: boolean
}

const C = createContext<AuthCtx | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => localStorage.getItem("token"))
  const [email, setEmail] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const setToken = (t: string | null) => {
    if (t) localStorage.setItem("token", t)
    else localStorage.removeItem("token")
    setTokenState(t)
  }

  const logout = () => setToken(null)

  useEffect(() => {
    if (!token) {
      setEmail(null)
      setLoading(false)
      return
    }
    getMe()
      .then(({ data }) => setEmail(data.email))
      .catch(() => setToken(null))
      .finally(() => setLoading(false))
  }, [token])

  return <C.Provider value={{ token, email, setToken, logout, loading }}>{children}</C.Provider>
}

export function useAuth() {
  const c = useContext(C)
  if (!c) throw new Error("useAuth outside provider")
  return c
}
