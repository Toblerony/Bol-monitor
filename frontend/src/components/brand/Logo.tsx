export function Logo({ size = 36 }: { size?: number }) {
  return (
    <div
      className="rounded-lg bg-primary text-primary-foreground flex items-center justify-center font-bold shadow-sm"
      style={{ width: size, height: size, fontSize: size * 0.45 }}
    >
      B
    </div>
  )
}

export function BrandTitle() {
  return (
    <div>
      <p className="font-bold text-sm leading-tight">Bol Monitor</p>
      <p className="text-[10px] text-muted-foreground">Product alerts</p>
    </div>
  )
}
