"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { LogOut } from "lucide-react";

/**
 * Signs out by calling DELETE /api/access (server destroys the session row
 * and clears the HttpOnly cookie), then refreshes server components.
 */
export function SessionSignOut() {
  const router = useRouter();
  const [busy, setBusy] = React.useState(false);

  async function signOut() {
    setBusy(true);
    try {
      await fetch("/api/access", { method: "DELETE" });
    } finally {
      setBusy(false);
      router.refresh();
    }
  }

  return (
    <button
      type="button"
      onClick={signOut}
      disabled={busy}
      aria-label="Sign out"
      className="inline-flex h-9 items-center gap-1.5 rounded-xl px-3 text-[13px] font-medium text-muted-foreground transition-all duration-200 ease-premium hover:bg-accent hover:text-accent-foreground active:scale-[0.97] disabled:opacity-50 sm:text-sm"
    >
      <LogOut size={15} /> <span className="hidden sm:inline">Sign out</span>
    </button>
  );
}
