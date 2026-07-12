import { createContext, useContext, useEffect, useState, ReactNode } from "react"
import { checkHealth, getSetupStatus } from "@/lib/api"

type SetupState = "loading" | "needs" | "done"

const C = createContext<{ setupState: SetupState }>({ setupState: "loading" })

export function SetupProvider({ children }: { children: ReactNode }) {
  const [setupState, setSetupState] = useState<SetupState>("loading")

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const ready = await checkHealth()
      if (!ready) {
        if (!cancelled) setSetupState("done")
        return
      }
      try {
        const { data } = await getSetupStatus()
        if (!cancelled) setSetupState(data.needs_setup ? "needs" : "done")
      } catch {
        if (!cancelled) setSetupState("done")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return <C.Provider value={{ setupState }}>{children}</C.Provider>
}

export function useSetup() {
  return useContext(C)
}
