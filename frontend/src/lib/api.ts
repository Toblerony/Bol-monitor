import axios from "axios"
import { getApiBaseUrl, getHealthUrl, isLiveDeployment } from "@/lib/apiConfig"

const api = axios.create({ baseURL: getApiBaseUrl(), timeout: 30000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("token")
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401 && !err.config?.url?.includes("/auth/")) {
      localStorage.removeItem("token")
      if (!window.location.pathname.startsWith("/login") && !window.location.pathname.startsWith("/setup")) {
        window.location.href = "/login"
      }
    }
    return Promise.reject(err)
  },
)

export default api

export async function checkBackendHealth(): Promise<{
  ok: boolean
  status: string
  database: string
  ready: boolean
}> {
  const timeoutMs = isLiveDeployment ? 45000 : 12000
  try {
    const { data } = await axios.get(getHealthUrl(), { timeout: timeoutMs })
    const status = String(data?.status ?? "unknown")
    const database = String(data?.database ?? "unknown")
    const ready = Boolean(data?.ready)
    // Tolerant: Neon/monitor can briefly slow DB — only "offline" after real failures
    const ok =
      ready ||
      status === "healthy" ||
      database === "connected" ||
      status === "starting"
    return { ok, status, database, ready }
  } catch {
    return { ok: false, status: "offline", database: "offline", ready: false }
  }
}

/** @deprecated use checkBackendHealth */
export async function checkHealth(): Promise<boolean> {
  const h = await checkBackendHealth()
  return h.ok
}

export const getSetupStatus = () => api.get<{ needs_setup: boolean }>("/auth/setup-status")
export const completeSetup = (email: string, password: string) =>
  api.post("/auth/setup", { email, password })
export const login = (email: string, password: string) => api.post("/auth/login", { email, password })
export const getMe = () => api.get("/auth/me")

export const getStats = () => api.get("/dashboard/stats")
export const getDashboardCharts = () => api.get("/dashboard/charts")
export const getMonitoring = () => api.get("/monitoring/settings")
export const updateMonitoring = (data: Record<string, unknown>) => api.put("/monitoring/settings", data)
export const startMonitoring = () => api.post("/monitoring/start")
export const stopMonitoring = () => api.post("/monitoring/stop")

export const getProfiles = () => api.get("/profiles")
export const createProfile = (data: Record<string, unknown>) => api.post("/profiles", data)
export const updateProfile = (id: number, data: Record<string, unknown>) => api.put(`/profiles/${id}`, data)
export const deleteProfile = (id: number) => api.delete(`/profiles/${id}`)

export const getProducts = () => api.get("/products")
export const getAlerts = () => api.get("/alerts")
export const getLogs = (params: Record<string, unknown>) => api.get("/logs", { params })
export const exportLogs = () => api.get("/logs/export/csv", { responseType: "blob" })
export const deleteLogs = (ids: number[]) => api.delete("/logs", { data: { ids } })
export const deleteAllLogs = () => api.delete("/logs/all")
export const testTelegram = () => api.post("/telegram/test")
export const testDiscord = () => api.post("/discord/test")
export const getProxies = () => api.get("/proxies")
export const updateProxies = (data: Record<string, unknown>) => api.put("/proxies", data)
export const testProxies = () => api.post("/proxies/test")
export const getBolLoginStatus = () => api.get("/bol/login-status")
export const clearBolSession = () => api.post("/bol/clear-session")

export interface Profile {
  id: number
  name: string
  title_keywords: string[]
  category_keywords: string[]
  exclude_keywords: string[]
  price_min: number | null
  price_max: number | null
  is_enabled: boolean
  tracked_count: number
}

export interface TrackedProduct {
  id: number
  profile_id: number
  profile_name: string
  url: string
  title: string
  price_text: string | null
  status: string
  categories: string
  brand: string
  product_type: string
  alerted_online: boolean
  alerted_stock: boolean
  last_checked_at: string | null
}
