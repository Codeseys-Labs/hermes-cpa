# Changelog

## Unreleased

- Recover failed console initialization with at most three document reads in total, the initial read plus two retries, and offer launcher-local recovery without reloading Desktop or Dashboard. Preserve captured selection fencing, document verification and explicit management unlock; never replay management operations or unknown outcomes.
- Corrected runtime release CI to request `blacksmith-4vcpu-ubuntu-2404` explicitly. Installing the Blacksmith app does not redirect `ubuntu-latest` jobs; manual dispatch remains build-and-verify only.
- Documented the tag-only release trigger and on-demand, nonpublishing verification path. Branch pushes and pull-request merges do not start CI.
- Added real workflow, Blacksmith verification, unlock, dashboard and credential-management screenshots to the README. UI captures use a network-isolated environment and a generated Claude account, not live credentials.

## preview-2026-10-16

- Prepared the Codeseys-Labs public preview, migrated the pinned runtime acquisition source and installation/tracker links, and retained the patched-host compatibility gates. This is not a standalone Windows installer.
- Hardened the pinned native dependency set and console lock, including Axios 1.20.0, React Router 7.18.2, go-git v6 alpha.5, x/net 0.60.0 and x/crypto 0.57.0. Preserved Git-store corruption detection by refreshing the updated storage cache before integrity checks; the existing corruption tests remain unchanged.
- Restored complete bundled license notices for the console and native runtime, filled missing lockfile integrity values, and made console builds reject affected build-only glob packages in the emitted bundle. Documented the remaining unimported legacy OpenPGP and build-only `braces` advisory scope.
- Made isolated-candidate authorization tests generate their own unaccepted package instead of depending on the shipped snapshot's acceptance state. Permit identity, expiry and captured-deadline checks remain covered without changing production authorization.
- Configured the loopback WSL Desktop backend to load its supported session-token override from an owner-only environment file. The same Desktop process recovered across two backend restarts without manual reauthentication; missing/wrong credentials remained denied. No unauthenticated token-scraping fallback was added.
- Moved fixed quota/profile-read consent into the explicit Unlock dialog for currently verified accounts. Removed the extra quota modal without granting arbitrary egress; added/replaced credentials still prompt. Live Antigravity plan and quota reads completed with zero extra approval dialogs.
- Added once-per-unlocked-session approval for fixed Codex, Claude and Antigravity quota/profile reads, bound to trusted credential identity and managed credential versions. Arbitrary network calls, writes, credit consumption, resets and plugin effects still prompt. Explicit unlock and revocation remain.
- Cached unchanged YAML policy projections while preserving identity/epoch revalidation, strict accepted-operation revocation and clock-rollback checks. Reduced pending-receipt poll waits without replaying operations.
- Measured the live Windows-to-WSL console: unlock to frame decreased from 3.50 s to 1.32 s, Auth Files navigation from 3.70 s to 1.23 s, and median three-run credential refresh from 4.62 s to 1.31 s. These are local observations, not performance guarantees.

## snapshot-2026-10-15

- Accepted the private patched-Hermes working snapshot, including WSL inference, 22 enrolled homes, three migrated OAuth accounts and live Codex quota display. Stock Hermes and generally packaged Windows releases remain unqualified.
- Fixed hot-mounted console route guard initialization, kept enrolled key helpers independent of ambient Hermes dependencies, and widened psutil admission to match Hermes core.
- Replaced the read-oriented hosted UI with the full pinned CPAMC 1.25.6 application in Agent Dashboard and the optional Desktop workspace, including configuration, provider keys, OAuth, credential lifecycle, quota and native-plugin interfaces.
- Made trusted unlock explicitly authorize user-managed CPA/provider credentials in the console while retaining Hermes session tokens and private owner/broker authority in the host. Mutations use reviewed, scoped execution and receipt reconciliation rather than replay.
- Moved managed control and inference to the checksum-pinned `hermes-owner-v1` runtime's private Unix channels. Public CPA settings no longer retarget the owner or enrolled broker; legacy private-key roots require an explicit transition rather than silent migration.
- Added captured Desktop plugin sessions, bounded resource admission/delivery, separate native-page presentation origins and incarnation-bound resource grants. Dashboard presentation supports loopback port remapping and configured HTTPS resource origins; external links require a fresh trusted user gesture.
- Added reproducible native runtime builds and fixed private-candidate acquisition through authenticated `gh`, with exact size/hash checks and no public-runtime fallback.
- Added generated native PKCE, callback, credential lifecycle and plugin-store/resource qualification. Fresh qualification roots can preserve the generated Hermes session token through stdin without copying configuration, credentials or permits.

- Separated native plugin resources from management execution and pinned declared management routes, fixed plugin quota requests and plugin OAuth starts to the installed plugin and registry revision, with atomic native dispatch assertions. Route-less plugin replacement invalidates these reviews; built-in OAuth stays on its native path.
- Kept uncertain console operations unknown and observed responses available or explicitly withheld after delivery failures; preserved terminal receipts without replay. OAuth starts now reserve tracking capacity before dispatch and retain observed states for cancellation even when their presentation data is rejected.
- Revalidated hosted execute grants before private reservations and owner ledger admission, without requiring host grants for native CLI operations.
- Kept each CPA child's creating thread alive until the child exits, so Linux parent-death protection no longer kills a successful restart when a transient console job finishes. Supervisor termination still kills its owned child.
- Gave hosted console replies a 4.5-second outer budget below host containment, without extending earlier deadlines or ordinary read limits. Lost resource replies retain conservative invocation metadata and unknown outcomes; observed native responses and definitive pre-dispatch refusals remain distinct, with no replay.
- Namespaced console owner operation IDs with a stable hash of the trusted document and guest request ID. Reopening cannot collide with retained receipts, while repeated same-document IDs retain owner deduplication. Guest response IDs, exact effect reviews and accepted-operation polling remain unchanged.
- Added explicit switching between CPAMC and an approved native page without reloading either document, plus an always-available trusted console lock.
- Decoded native resource content before Electron presentation with bounded, cancellable gzip, deflate, Brotli and zstd handling. Preserved repeated encoding-header order and aligned concatenated zstd frames for the pinned Electron runtime.
- Qualified the native Windows development UI with generated OAuth and API keys, exact native-page checks, private named-pipe control, pre-import session guards, clean-exit evidence and post-quiescence persistence checks. Guard diagnostics emit fixed redacted destination categories, never arbitrary hostnames.
- Revalidated native HTTP resource authority throughout delivery, including blocked sends. Revocation aborts incomplete delivery before releasing its buffer reservation; bytes already accepted by the transport cannot be recalled.
- Updated the maintained isolated stack to private-channel CPA in a fresh root, retaining the previous root and the original generated Hermes token. Image/root selection is stored separately from credentials; the explicit permit does not auto-renew.
- Serialized conflicting hosted management requests through preparation, approval, execution and terminal receipts. Parallel quota checks no longer invalidate sibling reviews or lock the console; known read-only routes still overlap, and every network request retains its own approval.

## Earlier staged integration — `2cc259e`

The full-console work above supersedes this candidate's mutation deferral and separate private CPA keys.

- Enforced captured candidate authorization across managed owners, broker grants and response budgets; expiry now stops owned services, and permit renewal cannot extend a running owner.
- Fixed callable-credential model discovery and prevented disabled/removed named providers from entering credential fallback. Preserved mode-specific validation when a callable credential fails.
- Added repeatable real `custom:cpa` qualification through the native runtime, broker and pinned CPA, with generated loopback inference, model refresh and independent retirement proofs.
- Added a separate frozen-dependency test image, avoiding local venv/path contamination without weakening home-I/O guards.
- Restored CPAMC traffic and provider-fleet presentation over a bounded, independently validated API-key usage projection. Kept OAuth exclusion, unavailable windows and valid zero traffic explicit; removed stale charts when host polling pauses.
- Added reproducible private-root qualification and a pinned, isolated Docker deployment with per-project volumes and persistent session tokens.
- Reused one hosted healthcheck selection across repeated/concurrent reads, enforced backend restart after package refresh, verified installed inventories and rejected duplicate backends.
- Excluded known credential/runtime artifacts from qualification copies and Docker build context, including mixed-case filenames.
- Exercised real loopback CPA traffic through the authenticated WSL backend and Windows Desktop, plus clean-image qualification boundaries; retained candidate release gates and explicit expiring permits.
- Added explicit checksum-pinned managed setup, separate management/inference credentials, persistent owned CPA and broker startup, provider enrollment and remembered opt-in autostart.
- Kept registration inert by default and incomplete/unsupported installations fail-closed before managed side effects.
- Added scoped service leases and per-plugin policy epochs so unrelated Hermes settings do not kill managed services while relevant revocation still does.
- Exported the pinned host contract, including per-request zero-TTL grants and hosted native CLI parsing/output/foreground signal support.
- Docked CPAMC beside chat, preserving other workspaces and focusing one existing console on repeated open.
- Paired the guest/host consent protocol with separate preview, human-review and commit deadlines; rejected stale scope/revision and avoided replay after an unknown outcome.
- Disabled unchanged configuration review, retained supported configuration/inventory paths and deferred credential/OAuth/quota/plugin mutation parity.
- Added behavioral regressions and exercised the real pinned CPA process, isolated Hermes CLI, WSL backend and Windows development Desktop with generated credentials.
- Removed obsolete private-path desktop probes; excluded local qualification state, credentials and dependencies from publication.

The earlier staged candidate had no release acceptance or live deployment. The private working snapshot above supersedes that state; a minimum generally supported Desktop version remains unset.
