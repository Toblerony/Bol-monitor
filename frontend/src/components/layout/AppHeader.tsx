import { Menu, Moon, Sun, LogOut } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { useTheme } from "@/contexts/ThemeContext"
import { useAuth } from "@/contexts/AuthContext"
import MonitoringControls from "@/components/layout/MonitoringControls"
import { Button } from "@/components/ui/button"

export default function AppHeader({ onMenuClick }: { onMenuClick: () => void }) {
  const { logout, email } = useAuth()
  const { resolvedTheme, setTheme } = useTheme()
  const navigate = useNavigate()

  const toggleTheme = () => setTheme(resolvedTheme === "dark" ? "light" : "dark")

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-card backdrop-blur-md shadow-[0_1px_4px_rgba(15,23,42,0.06)] dark:shadow-[0_1px_0_oklch(0.28_0.03_264),0_4px_16px_rgba(0,0,0,0.45)] dark:bg-card/98">
      <div className="flex items-center gap-2 px-4 py-3 lg:px-6">
        <button className="lg:hidden p-2 rounded-lg hover:bg-accent transition-colors" onClick={onMenuClick} aria-label="Open menu">
          <Menu className="h-5 w-5" />
        </button>
        <div className="hidden sm:block text-xs text-muted-foreground truncate max-w-[180px]">{email}</div>
        <div className="flex-1" />
        <MonitoringControls compact />
        <div className="h-6 w-px bg-border mx-1 hidden sm:block" />
        <Button
          variant="ghost"
          size="icon"
          onClick={toggleTheme}
          title={resolvedTheme === "dark" ? "Light Mode" : "Dark Mode"}
          className="rounded-lg shrink-0"
        >
          {resolvedTheme === "dark" ? (
            <Sun className="h-5 w-5 text-amber-400" />
          ) : (
            <Moon className="h-5 w-5 text-slate-600" />
          )}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          onClick={() => { logout(); navigate("/login") }}
          title="Logout"
          className="rounded-lg text-muted-foreground hover:text-destructive shrink-0"
        >
          <LogOut className="h-5 w-5" />
        </Button>
      </div>
    </header>
  )
}
