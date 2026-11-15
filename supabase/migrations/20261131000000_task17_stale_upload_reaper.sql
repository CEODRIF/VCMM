-- =============================================================================
-- TASK 017 P3 — stale-upload reaper (zombie 'uploading' jobs)
-- =============================================================================
-- A job stuck in 'uploading' past its upload-authorization TTL can never be
-- finalized (POST /upload/complete rejects an expired authorization), and
-- claim_next_job never touches 'uploading' — without this reaper it is a
-- zombie forever: an unreachable row plus a possibly partially-uploaded
-- object with no retention deadline (the job never went terminal).
--
-- SAFETY: the reaper is only sound when max_age > UPLOAD_AUTH_TTL_SECONDS
-- (default 900 s; the worker default is 1080 s). At that point the client
-- CANNOT finalize the upload any more, so failing the job loses nothing the
-- user could still accomplish — and because the transition goes TERMINAL,
-- the Phase 6 retention trigger (jobs_apply_retention_policy) sets
-- expires_at automatically, which brings any partially uploaded source
-- object under the existing (manual) purge path.
--
-- The state transition itself (uploading -> failed) is already legal — the
-- transition trigger has permitted it since the initial schema (it was
-- added for exactly this kind of client-side abort).
--
-- Non-destructive and additive: one SECURITY DEFINER function. Nothing is
-- deleted. Idempotent (a second run reaps zero rows).
-- Applies AFTER 20261130000000. Never applied to production.
-- =============================================================================
create or replace function public.reap_stale_uploading_jobs(p_max_age_seconds integer)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    reaped integer;
begin
    if p_max_age_seconds is null or p_max_age_seconds < 60 then
        raise exception 'max age must be an integer of at least 60 seconds';
    end if;

    update public.jobs
       set status = 'failed',
           last_error = 'The upload did not finish in time and could not be '
                        'completed. Please upload the video again.',
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where status = 'uploading'
       and created_at < now() - make_interval(secs => p_max_age_seconds);

    get diagnostics reaped = row_count;
    return reaped;
end;
$$;

-- Restrict the reaper RPC: only the trusted service role (the public
-- default EXECUTE grant is revoked — anon must never be able to fail jobs,
-- same trust model as the Phase 3 queue RPCs).
revoke execute on function public.reap_stale_uploading_jobs(integer) from public;
grant execute on function public.reap_stale_uploading_jobs(integer) to service_role;

comment on function public.reap_stale_uploading_jobs(integer) is
    'TASK 017: atomically fails jobs stuck in ''uploading'' beyond p_max_age_seconds (must exceed the upload-authorization TTL). Returns the reaped row count. Called by the worker on idle poll cycles; the terminal transition arms the Phase 6 retention deadline.';
