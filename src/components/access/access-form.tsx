"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, KeyRound, Loader2, LogIn } from "lucide-react";

import { appConfig } from "@/config/app";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

type FormStatus = "idle" | "submitting" | "error" | "success";

/**
 * Shared access-code entry — connected to the real server-side
 * authentication (POST /api/access) since Phase 2.
 *
 * SECURITY: the code is sent once to the server over HTTPS and verified
 * against a stored SHA-256 hash in constant time. The browser never compares
 * the code against anything it holds, and the resulting session lives in an
 * HttpOnly cookie that JavaScript cannot read. Attempts are rate-limited
 * server-side (5 per 15 minutes per IP).
 */
export function AccessForm() {
  const router = useRouter();
  const [code, setCode] = React.useState("");
  const [visible, setVisible] = React.useState(false);
  const [status, setStatus] = React.useState<FormStatus>("idle");
  const [error, setError] = React.useState<string | null>(null);

  function validate(nextCode: string): string | null {
    const trimmed = nextCode.trim();
    if (trimmed === "") return "Enter your access code.";
    if (trimmed.length < appConfig.access.codeMinLength) {
      return `The access code must be at least ${appConfig.access.codeMinLength} characters.`;
    }
    if (trimmed.length > appConfig.access.codeMaxLength) {
      return `The access code must be at most ${appConfig.access.codeMaxLength} characters.`;
    }
    return null;
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validate(code);
    if (problem) {
      setError(problem);
      setStatus("error");
      return;
    }

    setError(null);
    setStatus("submitting");
    try {
      const response = await fetch("/api/access", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code.trim() }),
      });
      const data = (await response.json().catch(() => null)) as { error?: string } | null;

      if (response.ok) {
        setStatus("success");
        router.push("/create");
        return;
      }
      if (response.status === 429) {
        setStatus("error");
        setError(data?.error ?? "Too many attempts. Please wait a few minutes.");
        return;
      }
      if (response.status === 503) {
        setStatus("error");
        setError("Access service is not configured on this deployment.");
        return;
      }
      setStatus("error");
      setError(data?.error ?? "Unable to verify the access code. Try again.");
    } catch {
      setStatus("error");
      setError("Network error. Check your connection and try again.");
    }
  }

  return (
    <Card className="card-surface mx-auto w-full max-w-md animate-fade-in">
      <CardHeader>
        <div className="mb-2 flex h-10 w-10 items-center justify-center rounded-lg bg-accent text-accent-foreground">
          <KeyRound size={18} />
        </div>
        <CardTitle>Enter your access code</CardTitle>
        <CardDescription>
          ClipForge AI is private. Use the shared access code to continue.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="access-code">Access code</Label>
            <div className="relative">
              <Input
                id="access-code"
                type={visible ? "text" : "password"}
                value={code}
                onChange={(event) => {
                  setCode(event.target.value);
                  if (error) setError(null);
                }}
                placeholder="••••••••"
                autoComplete="off"
                spellCheck={false}
                aria-invalid={status === "error" ? true : undefined}
                aria-describedby={error ? "access-code-error" : undefined}
                className={cn(status === "error" && "border-destructive focus-visible:ring-destructive")}
              />
              <button
                type="button"
                onClick={() => setVisible((value) => !value)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1.5 text-muted-foreground transition-colors hover:text-foreground"
                aria-label={visible ? "Hide access code" : "Show access code"}
              >
                {visible ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            {error && (
              <p id="access-code-error" className="text-sm text-destructive" role="alert">
                {error}
              </p>
            )}
          </div>

          <Button type="submit" className="w-full" disabled={status === "submitting"}>
            {status === "submitting" ? (
              <>
                <Loader2 size={16} className="animate-spin" /> Verifying…
              </>
            ) : (
              <>
                <LogIn size={16} /> Unlock
              </>
            )}
          </Button>

          <p className="text-xs text-muted-foreground">
            Verified server-side in constant time. Too many failed attempts are locked out
            temporarily.
          </p>
        </form>
      </CardContent>
    </Card>
  );
}
