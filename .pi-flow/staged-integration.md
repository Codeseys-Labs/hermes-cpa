# Staged integration execution contract

The user selected staged integration with a private GitHub tracker, then authorized taking over implementation and private publication. The Wayfinder map is https://github.com/baladithyab/hermes-cpa/issues/1. The host/package/ownership decision is https://github.com/baladithyab/hermes-cpa/issues/2. Full credential/OAuth/quota/plugin mutation parity remains deferred.

## Source ownership

New Git checkout: `/home/codeseys/.hermes/projects/hermes-cpa`.

`hermes-cpa/` is the unified agent/optional desktop package. `upstream-console/` contains the pinned upstream source and Hermes build. `host-integration/` records explicit compatibility changes against Hermes commit `0ed432d1871a9a9b169f5a778f6f9444fc13e0c1`; it must not silently install or patch the live runtime.

Preserve original `/home/codeseys/.hermes/projects/hermes-cpa-fidelity-r1` snapshots. Do not edit their canonical or separate repairs. Do not touch installed Hermes, live homes/config/credentials, systemd services, or the independent CPA pilot. GitHub repository visibility remains private. No commits, push, live install, external OAuth, or new spend by workers.

## Required behavior

After explicit trusted setup consent, Hermes can acquire the checksum-pinned CPA archive or use a local archive, prepare separate management/inference credentials, and establish an owned managed instance and broker-backed provider. Opt-in managed startup is remembered and follows native Hermes plugin lifetime; default/external registration remains inert. Shutdown must affect only exact-owned resources. Unsupported hosts must fail before credential/config writes or process launch. Per-profile grants remain fail-closed and models remain discovered through the inference broker, not the management guest.

An unrelated edit to Hermes config must not revoke a managed owner, while actual plugin disable/removal and relevant policy changes must revoke it. Do not weaken stale-request or ABA protections to fix this.

The optional desktop half uses Monitoring Dashboard's docked workspace mechanism, bounded host REST and an opaque sandbox. Integrate the understood consent phase repair with both ends of its wire protocol. Preserve current safe read/config functionality. Do not replace the console with placeholders or integrate the incomplete whole-port branch in a way that removes the working config/inventory routes.

## Shared interface

The parent owns `hermes-cpa/src/hermes_cpa/host_compat.py`. It provides `require_supported_host()` and `host_capabilities()`. A missing contract raises a sanitized actionable RuntimeError. Call the requirement before setup, provider enrollment, or opt-in managed startup, not before inert registration or status reads. The host patch will expose `CommandTokenSource.supports_single_request_grants = True` and existing scoped-request/operation APIs.

The backend worker owns native Python lifecycle, CLI, onboarding/provider code, plugin.yaml/pyproject.toml and backend tests. The desktop worker owns desktop/plugin.js, dashboard/manifest.json plus index.js, console transport/config UI and desktop/console tests. The parent owns host compatibility/export, root scripts/docs/tracker, final build/asset+manifest regeneration, verification and publication. Backend and desktop workers must not concurrently change README.md or generated console assets/compatibility hashes; send needed doc changes to parent.

## Verification ownership

Workers skip builds/lint/tests/formatters mid-flight. Write meaningful behavioral regressions and supply commands, but parent runs the combined checks once after integration. Parent's existing disposable reproductions already show old InstallPolicy revocation after unrelated config writes and installed-host reuse of zero-TTL grants. Any new permanent tests must exercise consumer-visible transitions, boundaries or failures, never source wording or implementation wiring.

Source publication is not acceptance of deferred full CPAMC parity. Keep compatibility/acceptance metadata truthful; do not make an untested release installable merely by flipping flags. Preserve an explicit generated-data validation permit until actual supported-host qualification is complete.
