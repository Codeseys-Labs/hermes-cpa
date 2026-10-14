# Hermes CPA

Private staged integration of [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), its management console, and Hermes Agent. The agent plugin owns CPA and a local inference broker; the optional desktop component opens CPAMC in a docked workspace beside chat, using the same workspace mechanism as Monitoring Dashboard.

**Candidate, not a production release.** Stock Hermes does not provide the required host contract. Release acceptance remains false and the minimum qualified Desktop version remains unset. Source publication does not authorize installation into an existing live profile.

## Repository

- `hermes-cpa/`: unified directory-installable agent/desktop plugin and shipped console asset.
- `upstream-console/`: CPAMC 1.25.6 source with the paired Hermes sandbox transport.
- `host-integration/`: explicit host patch and per-file provenance against Hermes commit `0ed432d1871a9a9b169f5a778f6f9444fc13e0c1`.
- `scripts/`: console provenance, host-patch export and private-root qualification.
- `.pi-flow/`: execution contract and private GitHub tracker configuration.
- `qualification/`: pinned backend image, isolated Compose deployment and qualification regressions.

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

The dashboard reuses CPAMC's traffic chart, provider sparklines and success meters. Its passive projection covers **API-key usage entries**, not distinct accounts or global traffic: OAuth/auth-file activity is excluded. It separates lifetime totals from twenty ten-minute buckets, including the current incomplete interval. Missing data, no entries and a valid quiet window remain distinct. Provider labels are allowlisted; arbitrary names, keys and server timestamps do not reach the guest. A failed host status removes the chart and pauses polling until explicit refresh.

Credential import/editing, OAuth, quota mutation and plugin mutation parity remain deferred. The working console retains its supported read/inventory/configuration paths; it is not a claim that every upstream CPAMC feature is available.

## Persistent qualification deployment

This is a disposable candidate environment, not a live-profile installer. Run from the repository root in Linux/WSL with Docker Compose:

```sh
docker build -t hermes-cpa-qualification:dev -f qualification/Dockerfile .
export CPA_QUALIFICATION_IMAGE="$(docker image inspect hermes-cpa-qualification:dev --format '{{.Id}}')"
dc() { docker compose -p hermes-cpa-dev -f qualification/compose.yaml "$@"; }
q() { dc exec -T keepdev python /opt/q/scripts/qualification.py "$@"; }
dc up -d --wait
q permit --root /work/q --hours 4
q cpa --root /work/q -- setup \
  --archive /opt/q/cache/CLIProxyAPI_8.0.21_linux_amd64.tar.gz \
  --isolated-authorization /work/q/candidate-permit.json --consent
q smoke --root /work/q --managed
```

The image uses the public pinned Hermes baseline plus the checked-in patch, frozen host dependencies and checksum-verified CPA archive. Runtime is non-root, read-only except for a private named volume and temporary files, with no external network, published ports, host mounts or Docker socket. CPA, broker, backend and native CLI share one PID namespace; use `exec`, not a separate `compose run` container, for lifecycle commands.

Fresh boot prepares external mode only. The ordinary Docker healthcheck proves authenticated backend access, not managed CPA readiness; `smoke --managed` also requires CPA to be ready. A headless HTTP backend is not an agent-plugin registration lifetime: after its restart, use the explicit owner/start commands below. Remembered autostart applies when Hermes loads the agent plugin.

### Token persistence

`prepare` creates `ROOT/session-token` once, beneath a private Linux root, with no group/other access. `serve` reuses it and passes it to the backend through the child environment, never argv or stdout. Docker stores it at `/work/q/session-token` in `hermes-cpa-dev_data`; container replacement and source refresh preserve it. Do not copy it into an image, Git, a Compose envfile or a Windows plaintext file. Native Desktop verification loaded it into memory from WSL.

This session token is separate from CPA's management/inference credentials, the expiring candidate permit and the private hosted-selection cache. Repeated/concurrent healthchecks reuse one client and cached server-returned epoch. A lost selection cache while the backend is alive requires a backend restart, **not token rotation**. `smoke --expect-token-sha256 <previous-fingerprint>` verifies persistence without printing the token.

### Updating the maintained stack

Rebuild and export the new immutable image ID as above, then:

```sh
dc up -d --wait
q quiesce --root /work/q --consent
q refresh --root /work/q --source /opt/q/hermes-cpa --consent
q permit --root /work/q --hours 4
dc restart
dc up -d --wait
q cpa --root /work/q -- bootstrap-owner --consent \
  --isolated-authorization /work/q/candidate-permit.json
q cpa --root /work/q -- doctor
q cpa --root /work/q -- start
q smoke --root /work/q --managed
```

Image replacement alone does not update the installed plugin in the volume. Refresh verifies the installed inventory, refuses a live CPA owner, replaces the package outside plugin discovery, removes the old permit and invalidates backend health until restart—even for an identical package. It neither adopts modified files nor renews authorization. Permits must be explicitly issued for a positive duration of at most 24 hours. A managed owner captures an immutable wall/monotonic deadline: expiry denies broker admission, cuts active response budgets and stops its owned services. A renewed permit applies only after the owner is relaunched; it never extends a running owner.

For parallel workers, use the same immutable image with a different project name, such as `hermes-cpa-check-<id>`. Each project gets its own volume, home, token and PID namespace. Never share a writable qualification volume between running containers. Quiesce and remove only the explicitly disposable worker project with its own `down -v`; keep `hermes-cpa-dev` and its volume.

Native qualification uses the same CLI: `prepare --root <new-private-linux-directory> --source <package> --archive <pinned-archive>`, explicit `permit`, then `serve --root ... --host <patched-host> --port <unused-loopback-port>`. Use that host's Python 3.14 environment with its declared dependencies and bcrypt. Roots must be outside live Hermes homes and off Windows-backed filesystems. WSL loopback forwarding is still session-token authenticated.

### Qualification limits and recovery

Preparation and refresh fail closed after interruption; refresh is not a power-loss-atomic update across its directory renames. An interrupted preparation can leave an unmarked root; an interrupted refresh can leave a backup or missing package. Do not bypass the marker or silently adopt that state. For a fully disposable generated Docker project, inspect any state worth retaining before explicitly removing/recreating **that project's** volume; doing so destroys its token and configuration. Normal updates preserve the volume.

Known credential/runtime filenames are excluded case-insensitively from package copies and the Docker context. This is not a content scanner for secrets embedded in arbitrary source files. Container qualification does not certify stock Hermes, a packaged Windows release, paid inference, OAuth or credential migration.

### Registered-provider qualification

Use a disposable project on the same built image and a new root for every attempt:

```sh
pc() { docker compose -p hermes-cpa-check-provider -f qualification/compose.yaml "$@"; }
pq() { pc exec -T keepdev python /opt/q/scripts/qualification.py "$@"; }
pc up -d --wait
pq prepare --root /work/provider-check
pq permit --root /work/provider-check --hours 1
pc exec -T keepdev python /opt/q/qualification/provider_smoke.py \
  --root /work/provider-check --consent
pc down -v
```

The smoke requires a fresh prepared root, explicit permit and network-none namespace. It runs native setup, registered `custom:cpa` resolution, the real `key_cmd`, OpenAI SDK, broker and pinned CPA against a generated loopback upstream. It verifies ordinary/SSE inference, fresh grants, changed inventory without re-enrollment, and independent disable/remove denial without fallback traffic. Both successful and interrupted runs quiesce their owned root; failed/used roots are not silently retried. Retained roots contain generated credentials and must stay private.

Discovery keeps Hermes's existing one-hour fresh cache window and stale-while-revalidate behavior. The smoke uses the public one-second TTL override and real elapsed time, then checks the normal picker; it does not edit cache files or change the production TTL. Refresh Models remains the immediate explicit discovery path. This is runtime/SDK qualification, not a full agent/delegate turn or packaged Desktop certification.

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

For clean Python gates, derive a separate test image from the runtime image you just built. It adds the pinned host lock's `dev` and `test` groups, retains the qualification bcrypt dependency and copies only the allowlisted test context. It does not inherit a local venv or `.pth` paths:

```sh
docker build --build-arg QUALIFICATION_IMAGE=hermes-cpa-qualification:dev \
  -t hermes-cpa-qualification:tests -f qualification/Dockerfile.tests hermes-cpa/tests
docker run --rm --init --network none --read-only --tmpfs /tmp:rw,exec,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges hermes-cpa-qualification:tests
```

The default command runs the plugin suite. Override it for host suites under `/opt/q/host/tests` or the qualification unittest suite. The private executable tmpfs lets integration tests launch their verified CPA binaries; the root filesystem stays read-only. The test layer is not the maintained runtime deployment. Desktop boundary tests run from the checkout with the console's installed development dependencies and `HERMES_CPA_TEST_PYTHON` pointing to the qualified Python environment.

Observed in isolated qualification:

- Full plugin suite: 571 checks passed in one clean, frozen-dependency test-image run, including the real pinned CPA archive. The shutdown-escalation fixture synchronizes on installed signal handling rather than a fixed startup sleep.
- Adjacent host provider/discovery/validation suites: 424 checks passed in the clean, frozen-dependency test image.
- Desktop boundaries: all 135 checks passed in one native run. The two Python-backed checks used `HERMES_CPA_TEST_PYTHON` to select the qualified environment rather than bare system Python.
- Console: 1,575 tests, TypeScript/Vite build and lint passed (one existing Fast Refresh warning).
- Qualification CLI: 35 behavioral checks passed both natively and in the clean pinned image. They cover private roots, inventory tampering, repeated/concurrent selection, restart reconciliation, owner locks, restart-required refresh and sanitized failures.
- Registered-provider smoke: four generated upstream requests covered ordinary/SSE inference, new-model inference and a held-client re-enable control. Automatic/public-TTL and explicit discovery refreshed added/removed IDs without changing enrollment. Disable and remove each denied live captured grants, the held SDK and fresh resolution, with zero fallback requests.
- A real 30-second managed lease admitted a broker request before expiry, denied it afterward, closed both owned listeners and removed the control socket. Construction-time and live SIGTERM qualification checks reaped their child and quiesced the owner. Missing consent, networked execution, reused roots and an expired permit were refused; the expired setup created no profile credentials.
- Real pinned CPA made three generated loopback inference calls. The authenticated hosted projection and real Windows development Desktop showed three successes, one API-key usage entry, twenty buckets and the canonical `Other` fleet label, without the raw provider/key labels. The dock remained beside chat; reopening did not duplicate it; guest storage and external fetch remained blocked.
- A separate generated-REST fixture exercised the shipped guest and production bridge's active, quiet, no-entry and host-failure states, including paused polling and explicit-refresh recovery. It is not evidence of real authentication or CPA behavior.
- The maintained Docker stack survived image replacement and package refresh with the same session token and a changed, verified package digest. Health refused the refreshed package until backend restart. Two projects ran managed CPA simultaneously on the same image with distinct tokens/volumes; only the disposable worker stack was removed. Mixed-case generated secret canaries were absent from the built image.
- Earlier native integration also exercised a 68-second host confirmation, persisted configuration change and owned restart/reconciliation. The host patch applies against the pristine pinned baseline.

These are full plugin/console gates and targeted host suites, not one full upstream Hermes suite or packaged-release certification. Native Desktop verification used the existing qualification environment with a known foreign `.pth` dependency; final Python gates ran independently in the source-only, frozen-dependency Docker test image. No live subscription, paid model call, OAuth flow or real credential migration was exercised. Live Hermes and the independent CPA pilot were not modified.

Tracker: [integration map #1](https://github.com/baladithyab/hermes-cpa/issues/1), [host/package decision #2](https://github.com/baladithyab/hermes-cpa/issues/2), [deferred capability tranche #3](https://github.com/baladithyab/hermes-cpa/issues/3), [registered-provider qualification #4](https://github.com/baladithyab/hermes-cpa/issues/4).
