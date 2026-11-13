import "server-only";

/**
 * Bounded request-body reading (TASK 012, spec section 9/10).
 *
 * `request.json()` on a route handler has no size guarantee on every runtime
 * (self-hosted Node streams the body without a hard cap), so an attacker can
 * hold a worker in memory with a multi-GB "JSON" POST. These helpers read the
 * body as a stream and STOP as soon as the configured byte budget is exceeded,
 * failing with a typed 413 instead of buffering.
 */

export class BodyTooLargeError extends Error {
  constructor(maxBytes: number) {
    super(`Request body exceeds ${maxBytes} bytes.`);
    this.name = "BodyTooLargeError";
  }
}

/**
 * Reads and parses the JSON body of `request`, enforcing `maxBytes`.
 *
 * @throws {BodyTooLargeError} when the body (declared or actual) exceeds the
 *   budget — the stream is cancelled, nothing beyond the cap is buffered.
 * @throws {SyntaxError} when the (in-budget) body is not valid JSON or empty.
 */
export async function readJsonBody(request: Request, maxBytes: number): Promise<unknown> {
  // Fast path: a declared Content-Length above the budget never streams.
  const declared = Number(request.headers.get("content-length") ?? "");
  if (Number.isFinite(declared) && declared > maxBytes) {
    throw new BodyTooLargeError(maxBytes);
  }

  if (!request.body) {
    throw new SyntaxError("empty body");
  }

  const chunks: Uint8Array[] = [];
  let total = 0;
  const reader = request.body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        total += value.byteLength;
        if (total > maxBytes) {
          // Stop consuming: cancel the stream so the runtime can discard
          // the rest instead of buffering it.
          await reader.cancel().catch(() => undefined);
          throw new BodyTooLargeError(maxBytes);
        }
        chunks.push(value);
      }
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder().decode(bytes));
}
