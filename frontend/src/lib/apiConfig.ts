/** Local Vite dev vs Vercel production — both use /api and /health paths. */
export const isLiveDeployment = import.meta.env.PROD

export function getApiBaseUrl(): string {
  return import.meta.env.VITE_API_URL || "/api"
}

export function getHealthUrl(): string {
  const base = import.meta.env.VITE_API_URL
    ? import.meta.env.VITE_API_URL.replace(/\/api\/?$/, "")
    : ""
  return `${base}/health`
}
