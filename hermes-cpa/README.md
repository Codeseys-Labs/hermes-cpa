# Hermes CPA Native Plugin

Standalone unified plugin for native CLI Proxy API (CPA) process lifecycle supervision and CPA Management Center (CPA-MC) desktop integration.

## Features
- Managed CPA child supervision with private Unix domain socket and lifetime advisory lock.
- Clean parent-death termination (`PR_SET_PDEATHSIG`) ensuring no unmanaged orphans.
- Opaque SandboxedFrame desktop integration with strict MessageChannel RPC protocol.
- Atomic config mutations with revision locking, fsync, and guarded rollback.

This is an isolated, unaccepted candidate, not a release or deployment approval.
Rollback can fail or stop on drift/revocation; it never guarantees restoration.
See [native recovery](RECOVERY.md) for observed doctor, lost-owner bootstrap and
explicit snapshot recovery commands, supported boundaries and remaining gaps.

## Native package loading

Directory installs use the root `__init__.py` and Hermes's profile-scoped
`hermes_plugins` namespace. The wrapper extends only its own package search
path; it does not change `sys.path` or alias the wheel's global `hermes_cpa`.
Internal modules use relative imports so two directory installations keep
separate state. Wheel installs expose `hermes_cpa:register` through the standard
`hermes_agent.plugins` entry-point group. The manifest's `entry_point` alone
was not a directory-loader entrypoint.

Registration is inert by default: an external/unconfigured installation starts
no owner, binary, management probe or credential resolution. Explicit
`hermes cpa setup --consent` prepares the pinned CPA release, generates separate
management/inference keys, starts the owner and broker, and enrolls `custom:cpa`
without changing the default model. It remembers managed autostart unless
`--no-autostart` is selected. Subsequent opted-in loads acquire scoped service
leases; unloading one host releases only its lease. Explicit stop, plugin disable,
removal or relevant policy revocation stops the owned services. Unrelated Hermes
settings do not stop them. External or unowned CPA is never adopted or stopped.

Directory installs include the desktop and console resources. Python wheels do
not; managed setup refuses an incomplete package. The scoped host patch runs
native CLI argument parsing and handlers in the isolated plugin child, including
normal help and foreground signal handling. Stock Hermes lacks this contract.
See the [repository installation guide](../README.md) for the pinned host patch.
Full upstream mutation parity and release/minimum-version acceptance remain open.

## Backend credential resolution

Authenticated hosted requests carry a pinned RequestScope into
CpaAdapter.from_scope through ProfileSecretResolver. Native CLI/owner calls pin
their explicit home. Each management read rebuilds that home's supported Hermes
secret scope; rotation does not require restarting the plugin. A missing value
fails closed rather than borrowing another profile's key or the process environment.
Native managed onboarding generates the management/inference keys via the host
credential writer; the console never receives them.

This is management-plane authentication (HERMES_CPA_MANAGEMENT_KEY), not worker
model-provider subscription authentication. The separate opt-in native subscription
broker implements the default-owner/all-local-workers decision; see
[subscription broker](SUBSCRIPTION-BROKER.md). Management keys are never used for
inference. No live credential migration is performed by this candidate.
