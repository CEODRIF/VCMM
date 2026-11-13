import { z } from "zod";

/**
 * Centralized API error handling.
 *
 * Routes catch unknown errors and funnel them through :func:`toErrorResponse`
 * so the client always gets a stable `{ "error": string }` shape — never
 * stack traces, never database internals, never credentials.
 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiErrorResponse {
  status: number;
  body: { error: string };
}

export function toErrorResponse(error: unknown): ApiErrorResponse {
  if (error instanceof ApiError) {
    return { status: error.status, body: { error: error.message } };
  }
  if (error instanceof z.ZodError) {
    const first = error.issues[0];
    const field = first?.path.length ? first.path.join(".") : "payload";
    return { status: 400, body: { error: `Invalid ${field}: ${first?.message ?? "bad value"}` } };
  }
  // Database/infrastructure failures: log the detail, return a generic 502/500.
  if (error instanceof Error) {
    console.error("[api] unexpected error:", error.message);
  }
  return { status: 500, body: { error: "Internal server error." } };
}
