# Changelog

## Unreleased — staged integration candidate

- Added explicit checksum-pinned managed setup, separate management/inference credentials, persistent owned CPA and broker startup, provider enrollment and remembered opt-in autostart.
- Kept registration inert by default and incomplete/unsupported installations fail-closed before managed side effects.
- Added scoped service leases and per-plugin policy epochs so unrelated Hermes settings do not kill managed services while relevant revocation still does.
- Exported the pinned host contract, including per-request zero-TTL grants and hosted native CLI parsing/output/foreground signal support.
- Docked CPAMC beside chat, preserving other workspaces and focusing one existing console on repeated open.
- Paired the guest/host consent protocol with separate preview, human-review and commit deadlines; rejected stale scope/revision and avoided replay after an unknown outcome.
- Disabled unchanged configuration review, retained supported configuration/inventory paths and deferred credential/OAuth/quota/plugin mutation parity.
- Added behavioral regressions and exercised the real pinned CPA process, isolated Hermes CLI, WSL backend and Windows development Desktop with generated credentials.
- Removed obsolete private-path desktop probes; excluded local qualification state, credentials and dependencies from publication.

Release acceptance and the qualified minimum Desktop version remain unset. No live deployment or upstream credential migration is included.
