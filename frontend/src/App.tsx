import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom"
import { AuthProvider, useAuth } from "@/contexts/AuthContext"
import { SetupProvider, useSetup } from "@/contexts/SetupContext"
import { ThemeProvider } from "@/contexts/ThemeContext"
import { ToastProvider } from "@/contexts/ToastContext"
import { MonitoringProvider } from "@/contexts/MonitoringContext"
import DashboardLayout from "@/components/layout/DashboardLayout"
import LoginPage from "@/pages/LoginPage"
import SetupPage from "@/pages/SetupPage"
import DashboardPage from "@/pages/DashboardPage"
import ProfilesPage from "@/pages/ProfilesPage"
import ProductsPage from "@/pages/ProductsPage"
import AlertsPage from "@/pages/AlertsPage"
import SettingsPage from "@/pages/SettingsPage"
import ProxiesPage from "@/pages/ProxiesPage"
import LogsPage from "@/pages/LogsPage"

function ProtectedShell() {
  const { token, loading } = useAuth()
  if (loading) return <div className="min-h-screen flex items-center justify-center text-muted-foreground">Loading…</div>
  if (!token) return <Navigate to="/login" replace />
  return (
    <MonitoringProvider>
      <DashboardLayout />
    </MonitoringProvider>
  )
}

function RoutesInner() {
  const { setupState } = useSetup()
  const loc = useLocation()

  if (setupState === "loading") {
    return <div className="min-h-screen flex items-center justify-center text-muted-foreground">Connecting…</div>
  }
  if (setupState === "needs" && loc.pathname !== "/setup") {
    return <Navigate to="/setup" replace />
  }

  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<ProtectedShell />}>
        <Route index element={<DashboardPage />} />
        <Route path="profiles" element={<ProfilesPage />} />
        <Route path="products" element={<ProductsPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="proxies" element={<ProxiesPage />} />
        <Route path="logs" element={<LogsPage />} />
      </Route>
      <Route path="*" element={<Navigate to={setupState === "needs" ? "/setup" : "/"} replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider>
            <SetupProvider>
              <RoutesInner />
            </SetupProvider>
          </AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </BrowserRouter>
  )
}
