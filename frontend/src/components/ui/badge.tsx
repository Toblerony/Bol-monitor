import * as React from "react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "./card"

export const Badge = ({
  className,
  variant = "default",
  ...props
}: React.HTMLAttributes<HTMLDivElement> & {
  variant?: "default" | "success" | "warning" | "destructive" | "secondary"
}) => {
  const variants = {
    default: "bg-primary/10 text-primary border-primary/20",
    success: "bg-success/10 text-success border-success/20",
    warning: "bg-warning/10 text-warning border-warning/20",
    destructive: "bg-destructive/10 text-destructive border-destructive/20",
    secondary: "bg-secondary text-secondary-foreground border-border",
  }
  return (
    <div
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold",
        variants[variant],
        className,
      )}
      {...props}
    />
  )
}

export const Spinner = ({ className }: { className?: string }) => (
  <div className={cn("h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent", className)} />
)

export const EmptyState = ({
  icon: Icon,
  title,
  description,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  description?: string
}) => (
  <div className="flex flex-col items-center justify-center py-16 text-center animate-fade-in">
    <div className="rounded-full bg-muted p-4 mb-4">
      <Icon className="h-8 w-8 text-muted-foreground" />
    </div>
    <h3 className="text-lg font-semibold">{title}</h3>
    {description && <p className="text-sm text-muted-foreground mt-1 max-w-sm">{description}</p>}
  </div>
)

export const StatCard = ({
  title,
  value,
  icon: Icon,
  to,
  className,
}: {
  title: string
  value: string | number
  icon: React.ComponentType<{ className?: string }>
  to?: string
  className?: string
}) => {
  const card = (
    <Card className={cn("animate-fade-in transition-all", to && "hover:shadow-md hover:border-primary/30 cursor-pointer group", className)}>
      <CardContent className="p-6">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-muted-foreground">{title}</p>
            <p className="text-2xl font-bold mt-1">{value}</p>
          </div>
          <div className="rounded-lg bg-primary/10 p-3">
            <Icon className="h-5 w-5 text-primary" />
          </div>
        </div>
      </CardContent>
    </Card>
  )
  if (to) return <Link to={to} className="block">{card}</Link>
  return card
}
