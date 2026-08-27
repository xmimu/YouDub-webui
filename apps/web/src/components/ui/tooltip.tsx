"use client"

import { Tooltip } from "@base-ui/react/tooltip"
import { ReactNode } from "react"

import { cn } from "@/lib/utils"

function TooltipProvider({ children }: { children: ReactNode }) {
  return <Tooltip.Provider>{children}</Tooltip.Provider>
}

function TooltipTip({
  children,
  content,
  className,
}: {
  children: ReactNode
  content: string
  className?: string
}) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger render={<span />} className="inline-flex">
        {children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner sideOffset={6} align="center">
          <Tooltip.Popup
            className={cn(
              "max-w-[16rem] rounded-lg bg-foreground px-2.5 py-1.5 text-xs text-background shadow-lg",
              className,
            )}
          >
            {content}
          </Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  )
}

export { TooltipProvider, TooltipTip as Tooltip }
