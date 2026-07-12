import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDate(date: string | Date) {
  return new Date(date).toLocaleString("nl-NL", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

export function formatIntervalRange(minSec: number, maxSec: number) {
  if (minSec === maxSec) return `${minSec}s`
  return `${minSec}–${maxSec}s (random)`
}

export function apiErrorMessage(error: unknown, fallback = "Request failed — try again."): string {
  if (typeof error === "object" && error !== null && "response" in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === "string") return detail
    if (Array.isArray(detail)) return detail.map(String).join(", ")
  }
  return fallback
}
