# Hermes CPA

Private staged integration of [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), its management console, and Hermes Agent. The agent plugin owns CPA and a local inference broker; the optional desktop component opens CPAMC in a docked workspace beside chat, using the same workspace mechanism as Monitoring Dashboard.

**Candidate, not a production release.** Stock Hermes does not provide the required host contract. Release acceptance remains false and the minimum qualified Desktop version remains unset. Source publication does not authorize installation into an existing live profile.

## Repository

- `hermes-cpa/`: unified directory-installable agent/desktop plugin and shipped console asset.
- `upstream-console/`: CPAMC 1.25.6 source with the paired Hermes sandbox transport.
- `host-integration/`: explicit host patch and per-file provenance against Hermes commit `0ed432d1871a9a9b169f5a778f6f9444fc13e0c1`.
- `scripts/`: console build/provenance and host-patch export.
- `.pi-flow/`: execution contract and private GitHub tracker configuration.

## Host and package installation

Use a disposable Linux/WSL qualification checkout and a separate `HERMES_HOME`. Do not apply the patch over an existing modified/live checkout. The patch is pinned to [this upstream Hermes revision](https://github.com/NousResearch/hermes-agent/commit/0ed432d1871a9a9b169f5a778f6f9444fc13e0c1).

```sh
git clone https://github.com/NousResearch/hermes-agent.git hermes-cpa-host
cd hermes-cpa-host
git checkout 0ed432d1871a9a9b169f5a778f6f9444fc13e0c1
git apply --check /path/to/hermes-cpa/host-integration/patches/0001-scoped-cpa-host-contract.patch
git apply /path/to/hermes-cpa/host-integration/patches/0001-scoped-cpa-host-contract.patch
```

Prepare that checkout using its own installation instructions. The contract adds authenticated request/selection/operation authority, zero-TTL request grants, scoped plugin-policy epochs, and native CLI execution within the isolated plugin host. A version string alone is insufficient: managed setup checks these capabilities before side effects.

Authenticate Git to the private repository as an authorized user. Install the package subdirectory through the patched Hermes CLI, pinning an exact published commit:

```sh
hermes plugins install 'https://github.com/baladithyab/hermes-cpa.git#hermes-cpa' \
  --ref <40-character-commit-sha> --no-deps --no-enable
```

This installs disabled without changing the shared Python environment. Review the installer security scan and dependency plan before enabling with `hermes plugins enable hermes-cpa`. Do not bypass scan or dependency-admission refusals. Dependencies are declared in `pyproject.toml` and `plugin.yaml`; bcrypt is required for managed onboarding. A Python wheel alone lacks the console resources and is not a complete managed installation.

Registration is inert until explicitly configured. For qualification, the supported managed entry point is:

```sh
hermes cpa setup --download --consent
# Or use a previously obtained, checksum-verified archive:
hermes cpa setup --archive /path/to/CPA_8.0.21_linux_amd64.tar.gz --consent
```

The candidate release gate intentionally refuses these commands without an explicit, expiring, home/package/manifest-bound isolated-validation permit (`--isolated-authorization`). The integration tests create such permits only in generated temporary homes. This is not a production-install workaround. Supported runtime pair: CPA 8.0.21, CPAMC 1.25.6, Linux amd64 backend.

## Managed behavior

Setup prepares separate management and inference keys, starts an exact-owned CPA process and inference broker, and enrolls `custom:cpa` with dynamic model discovery. It preserves the selected default model and unrelated provider configuration. No upstream provider credentials are imported automatically.

Managed autostart is remembered unless `--no-autostart` is supplied. Subsequent Hermes loads acquire scoped service leases; unloading one host releases its lease rather than killing services another host uses. Unrelated Hermes settings leave the owner running. Explicit stop, plugin disable/removal and relevant policy revocation stop only owned services. No system service is installed and external CPA instances are never adopted.

The broker supplies request-scoped grants through `key_cmd`; management credentials never enter the renderer or inference client. Model inventory revalidates through Hermes discovery/cache behavior, not an immediate push subscription. See [broker boundaries](hermes-cpa/SUBSCRIPTION-BROKER.md) and [recovery](hermes-cpa/RECOVERY.md).

## Desktop

Enable the optional desktop component on the patched host, then open **CPA Console**. Reopening focuses the existing docked workspace without replacing chat. The sandbox cannot use browser storage or direct network access. Supported configuration changes require a host-native diff and explicit restart confirmation; unchanged forms cannot restart CPA. Accepted operations reconcile instead of being replayed after an uncertain outcome.

Credential import/editing, OAuth, quota mutation and plugin mutation parity remain deferred. The working console retains its supported read/inventory/configuration paths; it is not a claim that every upstream CPAMC feature is available.

## Build and verification

```sh
cd upstream-console
bun install --frozen-lockfile
cd ..
python scripts/build_console.py --bun /path/to/bun
```

The builder records hashes for the published console source and shipped asset without changing release acceptance flags. To regenerate the host patch from a qualification checkout:

```sh
python scripts/export_host_patch.py --host /path/to/patched-host \
  --base-repo /path/to/hermes-git-checkout
```

Observed in the isolated integration work:

- Native behavioral suites: 457 passing checks across targeted runs, including actual hosted `hermes cpa setup`, the pinned CPA binary, broker enrollment and persistent ownership.
- Desktop behavior: 79 checks; console: 1,564 tests, TypeScript/Vite build and lint (one existing Fast Refresh warning).
- Host policy/config/publication: 88 checks; additional authority and CLI-boundary checks, including four session-argument regressions.
- Real Windows development Desktop connected to the isolated WSL backend: dock beside chat, sandbox restrictions, native confirmation held for 68 seconds, retry setting changed from 3 to 4, CPA restarted with the same owner, broker returned ready, reconciliation and disabled no-op review. The rebuilt shipped console was reopened and read the persisted value.
- The exported patch passes `git apply --check` against a pristine pinned host.

These are targeted runs, not one full upstream Hermes test suite or packaged-release certification. No live subscription, paid model call, OAuth flow or real credential migration was exercised. Live Hermes and the independent CPA pilot were not modified.

Tracker: [integration map #1](https://github.com/baladithyab/hermes-cpa/issues/1), [host/package decision #2](https://github.com/baladithyab/hermes-cpa/issues/2), [deferred capability tranche #3](https://github.com/baladithyab/hermes-cpa/issues/3).
