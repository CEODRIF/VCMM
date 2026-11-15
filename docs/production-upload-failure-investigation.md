# TASK 017 — Production Upload Failure Investigation (Evidence Report)

**Date:** 2026-11-16 · **Branch:** `feat/task-017-e2e-reliability` · **Status:** investigation complete, minimal fix implemented, regression passing

Incident: a user uploaded a 250 MB MP4. The UI showed the "Job … is waiting for
its file / Retry upload" state; a subsequent **"Start Creating"** click displayed
**"Internal server error."**

---

## 1. Executive summary (root cause)

The incident is **two distinct failures** that the evidence cleanly separates:

**RC-1 — the 250 MB transfer failure** (the "waiting for its file" state).
The file is PUT **directly from the browser to private Supabase Storage** — the
application server is not in that data path. Every *code-level* size gate in the
entire chain is 2 GiB (see §4). The **only size-dependent gate in the whole chain
is the Supabase Storage bucket "Max file size"** — a production dashboard
setting. Our bucket migrations create `clipforge-sources` **without** a
`file_size_limit` (i.e. the project default applies). If the production project's
limit is below 250 MB, storage rejects the PUT (HTTP 413), the browser XHR
fails, and the form falls into the retry state — exactly the observed UI.
A plain network drop during the (long) 250 MB transfer produces the identical
state. **Owner action: check the bucket max file size in the Supabase dashboard
(§9, action 1).**

**RC-2 — the "Internal server error." on "Start Creating".** This string exists
in exactly **one place in the codebase**: the generic fallback of
`toErrorResponse` ([src/lib/api/errors.ts](../src/lib/api/errors.ts), line 46).
It is returned **only** when a route handler throws an exception that is not a
typed `ApiError`. Before this fix, *every* database or storage-API failure in the
three upload endpoints (job creation, authorize, complete) was wrapped in a raw
`Error` and therefore surfaced as this generic 500 — with the sanitized detail
written **only** to the server log as
`[api] unexpected error: <message>`. The database-level reproduction (§6) proves
that repeated "Start Creating" submissions are **not** rejected by any schema
constraint, so the failing call was a DB or storage-API failure whose exact
signature is in the production server logs (§9, action 2). The opacity itself —
no actionable message, no distinction between "nothing happened" and "it might
have happened" — is a confirmed product defect, now fixed (§7).

**Amplifying defect (RC-3, pre-existing, fixed on this branch):** every
"Start Creating" click created a **new** job, so each failed attempt leaked an
additional zombie `uploading` row (never claimed by the worker, no retention
deadline). The observed "Job … is waiting for its file" box *is* that zombie
state. Fixed by: server-side stale-upload reaper, a user cancel endpoint, and an
**idempotent resume** — "Start Creating" now re-uploads the already-waiting job
instead of creating another one.

---

## 2. Complete upload lifecycle (code trace)

```
upload-area.tsx (file pick, NO client size check)
        │  File object (250 MB) stays in the browser
        ▼
creation-form.tsx  handleSubmit()
        │  POST /api/v1/jobs {sourceType:"upload", fileName, clipCount, …}
        │  (no file bytes, no size in the body)
        ▼
POST /api/v1/jobs/route.ts → createJob() → insertJob()
        │  row: status='uploading', source_path='sources/<id>/reserved'
        ▼  201 {job:{id}}
creation-form.tsx  runUpload(jobId)
        │  POST /api/v1/jobs/<id>/upload/authorize {fileName, sizeBytes, mimeType}
        ▼
authorize/route.ts
        │  zod (fileName ≤255, sizeBytes int>0, MIME), extension/MIME class check,
        │  sizeBytes ≤ uploadConfig.maxUploadSizeBytes()  (default 2 GiB) → 413 typed
        │  server-generated path sources/<id>/original/<uuid>.mp4
        │  createSourceUploadUrl(path)  ← Supabase Storage API
        │  setUploadAuthorization()     ← guarded UPDATE (status='uploading')
        ▼  200 {uploadUrl, contentType, maxBytes, expiresInSeconds(900)}
browser  PUT <uploadUrl>  (250 MB, straight to Supabase Storage — NOT via the app)
        │  XHR: onprogress → UI %  ·  onload 2xx → success
        │  onload 4xx/5xx or onerror → failure  ◄── RC-1 lives HERE
        ▼
POST /api/v1/jobs/<id>/upload/complete {sizeBytes}
        │  ownership + status='uploading' + auth-TTL (410 if expired)
        │  verifySourceObject()  ← Storage list (metadata only) → 409 if missing
        │  size cross-checks (declared vs actual vs reported) → 400 typed
        │  markUploadComplete()  ← guarded UPDATE uploading → queued
        ▼  200 {enqueued:true}
        ▼
worker  claim_next_job → validating → ingestion (download from storage, ffprobe)
        → ready → transcribing → analyzing → rendering → completed
```

Key properties:
- The 250 MB touches **only two** size-aware points: the browser→storage PUT
  (storage project limit) and the worker download (`max_upload_size_bytes`,
  default 2 GiB — `worker/app/config/settings.py`).
- A failed PUT performs **zero database writes** — the job row is untouched.
  That is why a failed upload is invisible to Postgres and sits in
  `uploading` forever without a reaper/cancel (RC-3).
- The complete endpoint is the only place a "completed upload" is recorded, and
  it is gated on the real object existing with the right size — a failed upload
  can never be marked complete.

## 3. The observed UI strings — exact origin

| Observed text (owner) | Exact source |
|---|---|
| "The file was transferred directly to private storage and verified server-side." | `upload-area.tsx` line 121 — **static** footer, always visible on the Upload tab |
| "A Job is waiting for its file." / "A 'Retry upload' action requests fresh authorization." | `creation-form.tsx`, `canRetryUpload` block: `Job {id} is waiting for its file — retry re-requests a fresh authorization.` + "Retry upload" button |
| "Internal server error." | `toErrorResponse` generic 500 fallback — `src/lib/api/errors.ts` line 46 (the **only** occurrence in the codebase) |
| "Start Creating" | `creation-form.tsx` submit button label |

So the reported state = creation form with a job created, `runUpload()` returned
false, user stayed on the form; then a second "Start Creating" click produced a
500 body on one of `POST /api/v1/jobs`, `POST …/upload/authorize` or
`POST …/upload/complete` (the form renders each of their `error` fields verbatim).

## 4. Size-limit audit (250 MB vs every gate in the chain)

| Gate | Limit | 250 MB? | Evidence |
|---|---|---|---|
| Browser file picker / upload-area | none (no size check) | passes | `upload-area.tsx` (no size logic) |
| `POST /api/v1/jobs` body | 16 KB JSON cap (body is ~200 B) | passes | `security.ts` `bodyLimit` |
| `POST …/upload/authorize` size | `MAX_UPLOAD_SIZE_BYTES` default **2 GiB** → typed 413 otherwise | passes | `uploads.ts` line 37; authorize route size check |
| **Browser PUT → Supabase Storage** | **project/bucket "Max file size" (dashboard setting; NOT set by any migration)** | **UNKNOWN — owner must verify** | bucket insert in `20261115000001_phase3_storage_buckets.sql` has no `file_size_limit`; Supabase Storage supports up to 50 GB with resumable uploads, per-bucket max is configurable |
| `POST …/upload/complete` | equality checks only (typed 400) | passes | complete route |
| Worker source download | `max_upload_size_bytes` default **2 GiB** | passes | `worker/app/config/settings.py` |
| Worker source duration | 8 h default (duration, not size) | n/a | `uploads.ts` `maxSourceDurationSeconds` |
| Gemini analysis input | 2 GB default | passes | worker settings `gemini_video_max_upload_bytes` |

**Conclusion:** no code-level gate rejects 250 MB. The only size-dependent
failure point is the storage project setting (RC-1) — or the network. **Smaller
files below that limit are not affected by RC-1** (all other gates are
size-independent); RC-2 (masked 500s) was size-independent and could hit any
upload at any time.

## 5. The generic 500 — exact production mapping

`toErrorResponse` (`errors.ts`): `ApiError` → its status/message · `ZodError` →
400 · "not configured" marker → 503 · **anything else → `console.error("[api]
unexpected error:", message)` + 500 `{"error":"Internal server error."}`**.

Before this fix, these wrappers produced the raw `Error` (→ generic 500):

| Call site | Sanitized log signature (in production server logs) |
|---|---|
| `insertJob` | `Job creation failed: <pg code>` |
| `setUploadAuthorization` | `Upload authorization update failed: <pg code>` |
| `markUploadComplete` | `Upload completion update failed: <pg code>` |
| `findJobForSession` / `listJobsForSession` / `countClipsForJob` | `Job lookup/list/Clip count failed: <pg code>` |
| `createSourceUploadUrl` | `Signed upload URL creation failed: <storage message>` |
| `verifySourceObject` | `Storage verification failed: <storage message>` |

The exact failing step of the incident = the `[api] unexpected error: …` line in
the Vercel server logs at the click timestamp (§9 action 2). No secrets or
signed URLs are contained in these messages (Supabase error code/message text
only).

## 6. Reproduction (isolated environment, synthetic media — no production data)

All reproductions run on this branch in an isolated environment; the "250 MB"
file is simulated by its exact size in request bodies (`262_144_000` bytes) —
**no 250 MB file was ever uploaded**, and no third-party/production media was used.

**A. Database level — real Postgres engine (PGlite 16, all 16 migrations applied
cleanly), `tests/database/task17-upload-repro.test.ts` (5/5 pass):**
1. First "Start Creating": job1 `uploading` + authorization UPDATE (1 row) — accepted.
2. The failed PUT is invisible to the DB: job1 stays `uploading`, `expires_at` NULL, no locks — **the exact zombie state the user saw** ("waiting for its file").
3. Second "Start Creating": job2 inserted + authorized — **no constraint violation** → the "Internal server error." was NOT a job-state/DB-rejection issue.
4. Worker reaper (`reap_stale_uploading_jobs(1080)`) fails **only** the stale job1 → `failed` + user-facing `last_error` + retention armed; job2 untouched.
5. The retried upload (job2) finalizes normally → `queued` (guarded, idempotent).

**B. Route level — REAL code path (route → error mapper → service → real db/jobs
and storage/objects wrappers; only the Supabase transport faked),
`tests/task17-upload-failure-repro.test.ts` (14/14 pass):**

| Scenario (250 MB sizeBytes) | Before fix | After fix (verified) |
|---|---|---|
| Job creation, DB down | 500 "Internal server error." | **503** "The database is temporarily unavailable — your job was NOT created." |
| Authorize, storage down | 500 "Internal server error." (diagnostic leaked only to log) | **502** "Private storage is temporarily unavailable … Your file was NOT uploaded" (diagnostic stays in server log) |
| Authorize, DB down | 500 generic | **503** "… the upload authorization was NOT saved." |
| Complete, storage down | 500 generic | **502** "… could not be verified. The job was NOT completed." |
| Complete, object missing | 409 typed (already safe) | 409 "Uploaded object not found. Complete the file transfer first." |
| Complete, DB down | 500 generic | **503** "… the upload was NOT marked complete." |
| Happy path, 250 MB | 201/200/200 | 201/200/200 (unchanged — all typed gates accept 250 MB) |
| Browser PUT rejected 413 | "The storage transfer failed. Retry the upload." (opaque) | "Storage rejected the file as too large (HTTP 413) … Try a smaller file, or contact support to raise the limit." |
| PUT rejected 403 / network drop / 5xx | one generic string | distinct actionable messages ("fresh authorization", "connection dropped … NOT fully uploaded", "temporarily unavailable … NOT uploaded") |

**C. Security of the new messaging (verified in the same suite):** storage
response bodies are sanitized — JSON `message`/`error` field only, URLs stripped;
test feeds a body containing a signed-URL-like token and asserts it never
reaches the user-facing string.

## 7. Fixes implemented (minimal, on `feat/task-017-e2e-reliability`)

1. **`src/lib/db/jobs.ts`** — all 8 wrappers: raw `Error` → typed
   `ApiError(503)` with an explicit outcome ("was NOT created / saved / marked
   complete / cancelled") + `console.error` diagnostic. A failed operation can
   never be mistaken for a completed one.
2. **`src/lib/storage/objects.ts`** — `createSourceUploadUrl` /
   `verifySourceObject`: raw `Error` → typed `ApiError(502)` with explicit
   outcome + `console.error` diagnostic; missing object still → typed 409 at the
   complete route.
3. **`src/lib/api/storage-errors.ts`** (new) — `describeStorageTransferFailure`:
   interprets the PUT result (413/403/404/5xx/network) into actionable,
   sanitized messages.
4. **`src/components/create/creation-form.tsx`** —
   - PUT failures are interpreted (§6-B rows 8-9) instead of one opaque string;
   - **idempotent resume**: when a job is already waiting for its file,
     "Start Creating" (now labelled "Resume upload") re-runs the upload against
     THAT job (fresh authorization) instead of creating another zombie row;
   - authorize 409 (job reaped/cancelled meanwhile) drops the stale reference so
     the next click starts a fresh job — self-healing, no loops.
5. **Already on this branch (P3, same incident class):** stale-upload reaper
   (migration `20261131000000`, worker idle-cycle call, `upload_stale_age_seconds`
   = 1080 s > 900 s auth TTL), `POST /api/v1/jobs/[jobId]/cancel`
   (owner-only, `uploading → failed`, arms retention), duplicate-submission 409
   guard for URL sources, `ERROR_NO_CLIPS_TO_RENDER` message improvement.

Preserved by construction: direct-to-private-storage uploads (unchanged data
path), all upload-authorization safeguards (server-generated paths, 15-min TTL,
MIME/extension matching, rate limits, guarded state updates), provider
capability restrictions (untouched), no new dependencies, no schema change
beyond the additive reaper RPC.

## 8. Test results (exact)

| Layer | Result |
|---|---|
| Frontend unit + API + DB (vitest, 37 files) | **478 passed / 0 failed** (baseline 436 + 42 new TASK 017 tests) |
| TypeScript (`tsc --noEmit`) | **0 errors** |
| ESLint (`next lint --max-warnings 0`) | **0 warnings / 0 errors** |
| Production build (`next build`) | **success** (17 routes incl. new `/api/v1/jobs/[jobId]/cancel`) |
| PGlite migration consistency | 16/16 migrations apply to a clean DB; reaper function present; `task15-migration-order` updated 15→16 |
| Worker suite (pytest, incl. 6 synthetic E2E + reaper loop tests) | **1065 passed / 15 skipped / 0 failed** (272.93 s; skips = pre-existing env-dependent tests: optional mediapipe/tesseract not installed) |
| BLOCKED | Real 250 MB browser PUT against the production storage project — requires the owner's environment (§9); not fabricated |

## 9. Owner action items (production, no code change)

1. **Supabase dashboard → Storage → Buckets → `clipforge-sources` → "Max file
   size".** If it is below ~250 MB, raise it to ≥ 2 GiB (matches the app's
   `MAX_UPLOAD_SIZE_BYTES` default). This is the most likely RC-1 trigger.
2. **Vercel → Logs, at the incident timestamp:** search `[api] unexpected
   error:` — the signature identifies the exact failing call (§5 table).
3. **Inspect the affected session's jobs** (read-only): `status`, `created_at`,
   `upload_auth_expires_at`, `source_path`, `last_error`, `expires_at`. The
   zombies from the incident (status `uploading`) are auto-failed by the reaper
   once the branch is deployed, and can be cancelled immediately via the new
   endpoint.
4. **Storage check:** does `clipforge-sources/sources/<jobId>/original/` contain
   the expected object, and what is its size? (A partial/absent object = the
   PUT was rejected/dropped — consistent with RC-1.)
5. **Small-file smoke test in production** (e.g. 10 MB MP4): expected to
   complete end-to-end — confirms RC-1's size-specificity and rules out a
   systemic storage outage.

## 10. Security & production-boundary confirmation

- No production data was read, modified, or deleted; no production migration was
  applied (the reaper migration runs only in isolated PGlite test databases).
- No secrets, access tokens, or signed URLs appear in this report, in the code
  changes, or in test output (tests use `example.invalid` URLs; the sanitizer
  strips URLs from storage error bodies before any user-facing string).
- The user's uploaded file was never re-uploaded and is not deleted by anything
  in this branch (retention handles it only after expiry, as designed).
- No deployment, no merge — the branch stops at PR for human review.

## 11. Changed files (this investigation + fix)

```
src/lib/db/jobs.ts                          (8 wrappers → typed 503 + diagnostics)
src/lib/storage/objects.ts                  (2 wrappers → typed 502 + diagnostics)
src/lib/api/storage-errors.ts              (NEW — PUT failure interpretation, sanitized)
src/components/create/creation-form.tsx     (PUT interpretation, idempotent resume, 409 self-heal, label)
tests/task17-upload-failure-repro.test.ts  (NEW — 14 route-level repro tests, real code path)
tests/database/task17-upload-repro.test.ts (NEW — 5 PGlite DB-level repro tests)
tests/database/task15-migration-order.test.ts (15 → 16 migrations + reaper contract)
```
Plus the already-pushed P3 changes on the same branch (reaper migration,
worker wiring, cancel route, dedup guard, their tests).
