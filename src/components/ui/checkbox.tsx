import * as React from "react";
import { Check } from "lucide-react";

import { cn } from "@/lib/utils";

export interface CheckboxProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {}

/**
 * Lightweight styled checkbox (native input underneath) -
 * shadcn/ui-compatible API surface without extra runtime dependencies.
 */
const Checkbox = React.forwardRef<HTMLInputElement, CheckboxProps>(
  ({ className, id, children, ...props }, ref) => (
    <label htmlFor={id} className={cn("group inline-flex cursor-pointer items-center gap-3", className)}>
      <span className="relative flex h-5 w-5 shrink-0 items-center justify-center">
        <input
          ref={ref}
          id={id}
          type="checkbox"
          className="peer h-5 w-5 cursor-pointer appearance-none rounded border border-input bg-background/60 transition-colors checked:border-primary checked:bg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          {...props}
        />
        <Check className="pointer-events-none absolute h-3.5 w-3.5 text-primary-foreground opacity-0 transition-opacity peer-checked:opacity-100" />
      </span>
      {children}
    </label>
  )
);
Checkbox.displayName = "Checkbox";

export { Checkbox };
