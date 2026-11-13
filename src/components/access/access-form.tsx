"use client";

import * as React from "react";
import { Eye, EyeOff, KeyRound, ShieldCheck } from "lucide-react";

import { appConfig } from "@/config/app";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

type FormState = "idle" | "submitted";

/**
 * Shared access-code entry (UI ONLY in Phase 1).
 *
 * SECURITY NOTE: no authentication is implemented in the browser. The code is
 * never compared against a value shipped to the client. Server-side
 * verification (hash comparison + session token) arrives in Phase 2 —
 * see docs/ROADMAP.md.
 */
export function AccessForm() {
  const [code, setCode] = React.useState("");
  const [visible, setVisible] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [state, setState] = React.useState<FormState>("idle");

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

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const problem = validate(code);
    if (problem) {
      setError(problem);
      setState("idle");
      return;
    }
    setError(null);
    // INTEGRATION POINT (Phase 2): POST /api/access with the code,
    // server compares against ACCESS_CODE_HASH and sets an httpOnly session
    // cookie. Nothing like that is sent or stored by the browser today.
    setState("submitted");
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
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? "access-code-error" : undefined}
                className={cn("pr-11", error && "border-destructive focus-visible:ring-destructive")}
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

          <Button type="submit" className="w-full">
            Unlock
          </Button>

          {state === "submitted" && (
            <div
              className="flex items-start gap-3 rounded-md border border-primary/30 bg-primary/10 p-3 text-sm text-accent-foreground"
              role="status"
            >
              <ShieldCheck size={18} className="mt-0.5 shrink-0 text-primary" />
              <p>
                Input validated. Server-side access verification is not enabled in Phase 1 —
                it will be implemented securely in Phase 2 (see docs/ROADMAP.md). You can
                explore the creation flow now.
              </p>
            </div>
          )}
        </form>
      </CardContent>
    </Card>
  );
}
