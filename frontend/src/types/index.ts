export interface ActivityLog {
  id: number
  category: string
  level: string
  message: string
  details: string | null
  source: string | null
  created_at: string
}

export interface PaginatedLogs {
  items: ActivityLog[]
  total: number
  page: number
  page_size: number
}

export interface DashboardStats {
  is_running: boolean
  bol_session_ok: boolean
  profiles_enabled: number
  tracked_products: number
  alerts_today: number
  by_status: Record<string, number>
}

export interface MonitoringSettings {
  is_enabled: boolean
  is_running: boolean
  last_scan_at: string | null
  bol_session_ok: boolean
  bol_session_message: string
  sitemap_scan_interval_sec: number
  poll_online_min: number
  poll_online_max: number
  poll_offline_min: number
  poll_offline_max: number
  alerts_new_online: boolean
  alerts_in_stock: boolean
  alerts_discord: boolean
  alerts_telegram: boolean
  discord_webhook_url: string
  telegram_bot_token: string
  telegram_chat_id: string
  use_proxies: boolean
  proxy_count: number
  tracked_count: number
  profile_count: number
}

export interface ChartDataPoint {
  date: string
  count: number
}

export interface DashboardCharts {
  products_per_day: ChartDataPoint[]
  alerts_per_day: ChartDataPoint[]
  online_alerts_per_day: ChartDataPoint[]
}
