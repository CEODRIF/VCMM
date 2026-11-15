/**
 * TASK 017 — interpreting browser → Supabase Storage transfer failures.
 *
 * Before this module, ANY non-2xx PUT surfaced as the single message
 * "The storage transfer failed. Retry the upload." — indistinguishable
 * between "file too large for the storage limit", "expired/rejected link"
 * and "storage outage". Each case needs a different user action, so the
 * status code is mapped to an actionable message.
 *
 * SANITIZATION: the response body is storage-generated error text; at most
 * its JSON `message`/`error` field is reused (trimmed, URL-free) so a
 * signed URL or token echoed by a proxy can never reach the UI or logs.
 */

function sanitizedSnippet(body: string | null | undefined): string | null {
  if (!body) return null;
  let candidate: string | null = null;
  try {
    const parsed = JSON.parse(body) as Record<string, unknown>;
    if (typeof parsed.message === "string") candidate = parsed.message;
    else if (typeof parsed.error === "string") candidate = parsed.error;
  } catch {
    candidate = body.slice(0, 160);
  }
  if (!candidate) return null;
  // Strip anything URL-shaped (signed URLs contain tokens — never echo).
  const cleaned = candidate.replace(/https?:\/\/\S+/gi, "").replace(/\s+/g, " ").trim();
  if (!cleaned) return null;
  return cleaned.slice(0, 140);
}

/**
 * Human-actionable description of a failed direct-to-storage transfer.
 *
 * @param status HTTP status of the PUT response; null when the XHR never
 *               got a response (network drop / abort by the transfer layer).
 * @param body   Raw response body (optional; sanitized before use).
 */
export function describeStorageTransferFailure(status: number | null, body?: string | null): string {
  const snippet = sanitizedSnippet(body);
  const detail = snippet ? ` (${snippet})` : "";

  if (status === null) {
    return "The network connection dropped during the transfer — your file was NOT fully uploaded. Retry the upload.";
  }
  if (status === 413) {
    return (
      "Storage rejected the file as too large (HTTP 413). This file exceeds the private-storage limit of this deployment." +
      " Try a smaller file, or contact support to raise the limit." +
      detail
    );
  }
  if (status === 401 || status === 403) {
    return (
      "The upload link was rejected by storage (HTTP " + status + ")." +
      " Use \"Retry upload\" to request a fresh authorization, then upload again." +
      detail
    );
  }
  if (status === 404) {
    return "The upload destination could not be found (HTTP 404). Retry the upload." + detail;
  }
  if (status >= 500) {
    return (
      "Private storage is temporarily unavailable (HTTP " + status + ")." +
      " Your file was NOT uploaded — retry in a moment." +
      detail
    );
  }
  return "The storage transfer failed (HTTP " + status + "). Your file was NOT uploaded — retry the upload." + detail;
}
