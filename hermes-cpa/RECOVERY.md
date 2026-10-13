# Native recovery operator workflow (isolated candidate)

Status: implementation evidence, NOT an accepted release. Production compatibility
metadata remains fail-closed; this document does not authorize active installation,
real credentials, deployment, or changes to accepted release flags.

## Inspect without starting anything

Run `hermes cpa doctor` through the enabled native CLI registration, or, in the
isolated backend environment used by the retained tests,
`python -m hermes_cpa.cli doctor`. Doctor reports owner availability, current owner
generation, current configuration digest, snapshot digest, and a sanitized journal
phase. It never prints configuration contents, journal text, keys or raw errors.
Reads do not create/start an owner. A missing owner is NOT proof that CPA stopped.
The displayed digests are observations, not a durable authorization grant.

The module CLI workflow below is exercised by `owner-loss-recovery-1304.py` from
the repair workspace, using actual pinned CPA 8.0.21 and generated scoped secrets.
The `hermes cpa` wrapper follows existing registration, but this specific recovery
run tests module invocation, not a new claim of every supported host loader.

## Lost owner

Do not re-run `init` to repair an initialized profile: it deliberately refuses
existing configuration/keys, preventing silent rotation. Instead use
`python -m hermes_cpa.cli bootstrap-owner --consent` in the same pinned profile.
This explicitly reconnects or starts the idle owner; it does not start CPA, erase
an unfinished journal or infer recovery. An already running owner is reused, not
stopped. Without `--consent`, no launch is attempted.

For disposable unaccepted-candidate testing only, the command additionally takes
`--isolated-authorization /absolute/path/to/generated-permit.json`. The permit is
private, expiring and bound to the test home, package and manifest; it is not
production release approval. Do not copy a permit from another home or remove
release validation. Production metadata still refuses unaccepted installation.

Run doctor again after bootstrap: generation changes across owner incarnations.
An interrupted journal continues to block start and configuration mutations.
Old-generation recovery requests are rejected even if the configuration bytes
are unchanged. Explicit bootstrap is not cancellation or revocation of other
accepted work; no selection or credentials are retargeted.

## Explicit snapshot recovery

Review doctor output and the intended retained snapshot through authorized local
operator procedures. Submit all three observed values, never placeholders:

    python -m hermes_cpa.cli recover --expected-revision CURRENT_SHA256 --snapshot-revision SNAPSHOT_SHA256 --expected-generation GENERATION --consent

The private owner compares generation, both actual-byte digests, policy and
consent under its mutation gate, stops/reaps its own child, rechecks drift/policy,
restores exact snapshot bytes with private permissions, and leaves CPA stopped.
It does not restore the separate OAuth/auth directory or change Hermes provider
settings. Only a subsequent explicit `start` requests inference service startup.
Recovery is intentionally absent from guest and REST action schemas.

A refusal or transport failure is not permission to retry mutations blindly.
Run doctor/reconcile first. Missing, symlinked, oversized, malformed or externally
changed recovery inputs fail closed. Explicit policy revocation during restore
or journal commit cannot report successful recovery; CPA remains stopped and the
journal remains/reverts to a blocking recovery phase. A failed write/fsync is not
proof that replacement did not happen: inspect current digests again.

## Observed verification and limits

- Four retained red discovery/bootstrap tests became passing; doctor is read-only
  and sanitizes hostile phase text and unsafe snapshot paths.
- Real pinned CPA owner SIGKILL: parent-death termination and orphan reap, doctor,
  consent-only bootstrap, blocked start, stale generation rejection, exact-byte
  snapshot restoration/stopped state, and fresh explicit ready restart pass.
- Actual subprocess exits after durable prepared/stopped/applying/applied/committed
  phases verify reconstruction and recovery rules (five tests). These phase tests
  use a stopped transaction owner model, not five real-CPA crash executions.
- Real ordinary-pointer built Desktop edit/native-consent/restart/readback still
  passes; native CLI recovery after Desktop close leaves actual CPA stopped.
- The full disk-full/fsync/upgrade reconciliation corpus, Desktop-native recovery
  form, supported uninstall/second upgrade pair, final packaged proof and
  independent review are still incomplete. Do not treat this workflow as whole
  A11/A14/A15 acceptance or a claim of Windows/macOS support.
