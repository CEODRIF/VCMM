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
      className="inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
    >
      <LogOut size={14} /> Sign out
    </button>
  );
}
