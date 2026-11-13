/**
 * ZIP entry filename sanitization (Phase 6 "Download all").
 *
 * Clip titles are user-adjacent (transcribed + title-generative), so the
 * entry name is built, not trusted: filesystem-forbidden characters and
 * control characters are removed, whitespace collapsed, length capped, and
 * a zero-padded index prefix guarantees uniqueness within the archive.
 * The result is a SINGLE path segment — no '/', no '..', no backslashes —
 * so path traversal is impossible by construction.
 */

const FORBIDDEN_RE = /[\\/:*?"<>|\u0000-\u001f\u007f]/g;
const MAX_TITLE_CHARS = 80;

/**
 * Strips everything that could escape a single path segment (path
 * separators, filesystem-forbidden characters, control characters),
 * collapses whitespace and caps the length. Returns "" when nothing
 * usable remains — callers decide the fallback.
 */
function cleanTitle(title: string): string {
  return title
    .replace(FORBIDDEN_RE, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_TITLE_CHARS)
    .trim();
}

/**
 * Safe single-segment filename for an INDIVIDUAL clip download — used as
 * the `download` parameter of the signed URL (Content-Disposition
 * attachment). Never contains a path separator, never empty.
 */
export function sanitizeMediaFilename(title: string, fallback = "clip.mp4"): string {
  const base = cleanTitle(title);
  return base.length > 0 ? `${base}.mp4` : fallback;
}

/**
 * Safe ZIP entry name: same rules, plus a zero-padded index prefix that
 * guarantees uniqueness inside the archive even when two titles sanitize
 * to the same string.
 */
export function sanitizeZipEntryName(title: string, index: number): string {
  const base = cleanTitle(title) || `clip-${index + 1}`;
  return `${String(index + 1).padStart(2, "0")}-${base}.mp4`;
}
