# Staged integration execution contract

The user selected staged integration with a private GitHub tracker and authorized implementation and private publication. The Wayfinder map is https://github.com/baladithyab/hermes-cpa/issues/1. The host/package/ownership decision is https://github.com/baladithyab/hermes-cpa/issues/2. The user now explicitly prioritizes full CPAMC configuration, API-key and OAuth/credential parity through the agent-owned instance and optional Desktop UI; https://github.com/baladithyab/hermes-cpa/issues/5 owns that contract. This supersedes the earlier mutation implementation deferral, not the restrictions on real accounts, credentials, live deployments or external provider authorization.

## Source ownership

New Git checkout: `/home/codeseys/.hermes/projects/hermes-cpa`.

`hermes-cpa/` is the unified agent/optional desktop package. `upstream-console/` contains the pinned upstream source and Hermes build. `host-integration/` records explicit compatibility changes against Hermes commit `0ed432d1871a9a9b169f5a778f6f9444fc13e0c1`; it must not silently install or patch the live runtime.

Preserve original `/home/codeseys/.hermes/projects/hermes-cpa-fidelity-r1` snapshots. Do not edit their canonical or separate repairs. Do not touch installed Hermes, live homes/config/credentials, systemd services, or the independent CPA pilot. GitHub repository visibility remains private. No commits, push, live install, external OAuth, or new spend by workers.

## Required behavior

After explicit trusted setup consent, Hermes can acquire the checksum-pinned private-channel CPA archive or use the verified local archive, generate user-managed public access configuration, and establish an owned managed instance and broker-backed provider. Managed control and inference use private Unix channels rather than public CPA keys. Opt-in startup follows native Hermes plugin lifetime; default/external registration remains inert. Shutdown must affect only exact-owned resources. Unsupported hosts must fail before configuration writes or process launch. Per-profile grants remain fail-closed and models remain discovered through the inference broker, not the management guest.

An unrelated edit to Hermes config must not revoke a managed owner, while actual plugin disable/removal and relevant policy changes must revoke it. Do not weaken stale-request or ABA protections to fix this.

The Agent Dashboard and optional Desktop half host the full pinned CPAMC application through bounded host REST and an opaque sandbox; Desktop uses Monitoring Dashboard's docked workspace mechanism. Trusted unlock permits the guest to manage ordinary user-owned CPA/provider keys and OAuth credentials, but not Hermes session tokens or private owner/broker authority. Native plugin pages use separate, incarnation-bound resource grants. Existing roots with legacy private CPA keys are not silently migrated; isolated transitions retain the old root and may explicitly preserve only the generated Hermes session token through stdin.

## Shared interface

The parent owns `hermes-cpa/src/hermes_cpa/host_compat.py`. It provides `require_supported_host()` and `host_capabilities()`. A missing contract raises a sanitized actionable RuntimeError. Call the requirement before setup, provider enrollment, or opt-in managed startup, not before inert registration or status reads. The host patch will expose `CommandTokenSource.supports_single_request_grants = True` and existing scoped-request/operation APIs.

The backend worker owns native Python lifecycle, CLI, onboarding/provider code, plugin.yaml/pyproject.toml and backend tests. The desktop worker owns desktop/plugin.js, dashboard/manifest.json plus index.js, console transport/config UI and desktop/console tests. The parent owns host compatibility/export, root scripts/docs/tracker, final build/asset+manifest regeneration, verification and publication. Backend and desktop workers must not concurrently change README.md or generated console assets/compatibility hashes; send needed doc changes to parent.

## Verification ownership

Workers skip builds/lint/tests/formatters mid-flight. Write meaningful behavioral regressions and supply commands, but parent runs the combined checks once after integration. Parent's existing disposable reproductions already show old InstallPolicy revocation after unrelated config writes and installed-host reuse of zero-TTL grants. Any new permanent tests must exercise consumer-visible transitions, boundaries or failures, never source wording or implementation wiring.

Source publication is not production or packaged-release acceptance. Keep compatibility/acceptance metadata truthful; do not make an untested release installable merely by flipping flags. Preserve an explicit generated-data validation permit until actual supported-host qualification is complete.

## Qualified private candidate — 2026-10-14

The full pinned CPAMC port passed generated-data qualification in native Windows Desktop, Linux Desktop and four Agent Dashboard origins: HTTP, remapped HTTP, authenticated HTTPS and HTTPS without forwarded-protocol headers. Dashboard and Desktop exercised OAuth and native API-key persistence; resource proofs covered opaque origins, native effects, navigation containment, concurrent console work and revocation during delivery. Runtime/provider proofs covered ordinary/SSE inference, model discovery without reenrollment, and disable/remove refusal without fallback. Root README records the exercised gates and limitations.

The maintained isolated `hermes-cpa-dev` stack uses `/work/q-parity`, retaining all of `/work/q` and its generated Hermes token. Image/root selection is in the ignored `.verification/maintained-compose.env`; no credential is stored there. Managed health passed after the transition. The explicit permit expires at 2026-10-14T18:37:14Z; expiry is intentional, not an instruction to renew automatically.

Private source/runtime publication remains subject to the parent's exact commit-message and candidate-publication approval. Source qualification does not change the Proposed ADR, unset minimum supported Desktop version, or unaccepted product-release metadata.
