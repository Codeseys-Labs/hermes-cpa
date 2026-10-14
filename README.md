# Hermes CPA

Private staged integration of [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), its full pinned management console, and Hermes Agent. The agent plugin owns CPA and a local inference broker. CPAMC is available through the Agent Dashboard without Desktop; the optional desktop component opens the same console in a docked workspace beside chat, using the Monitoring Dashboard workspace mechanism.

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
hermes cpa setup --archive /path/to/CLIProxyAPI_8.0.21_hermes-owner-v1_linux_amd64.tar.gz --consent
```

The candidate release gate intentionally refuses these commands without an explicit, expiring, home/package/manifest-bound isolated-validation permit (`--isolated-authorization`). The integration tests create such permits only in generated temporary homes. This is not a production-install workaround. The pinned pair is CPA 8.0.21 with the `hermes-owner-v1` integration and CPAMC 1.25.6, on Linux amd64. A stock CPA archive is not interchangeable. `--download` uses authenticated `gh` to fetch the exact private candidate asset and verifies its size and SHA-256; it never falls back to a public binary.

## Managed behavior

Setup generates a user-managed public API key, leaves public management disabled, starts an exact-owned CPA process and inference broker, and enrolls `custom:cpa` with dynamic model discovery. Private Unix control and inference channels are independent of public CPA host, port, TLS and key settings. Setup preserves the selected default model and unrelated provider configuration; it does not import upstream credentials automatically.

Managed autostart is remembered unless `--no-autostart` is supplied. Subsequent Hermes loads acquire scoped service leases; unloading one host releases its lease rather than killing services another host uses. Unrelated Hermes settings leave the owner running. Explicit stop, plugin disable/removal and relevant policy revocation stop only owned services. No system service is installed and external CPA instances are never adopted.

The broker supplies request-scoped grants through `key_cmd`; neither its signing authority nor private owner authority enters the renderer or inference client. Model inventory revalidates through Hermes discovery/cache behavior, not an immediate push subscription. Use Refresh Models for immediate discovery. See [broker boundaries](hermes-cpa/SUBSCRIPTION-BROKER.md) and [recovery](hermes-cpa/RECOVERY.md).

## Agent Dashboard and optional Desktop

Open **CPA Console** in the Agent Dashboard, or enable the optional Desktop component and open its docked workspace. Reopening the Desktop contribution focuses the existing workspace without replacing chat. Unlock through the trusted launcher; CPAMC cannot unlock itself.

The unlocked document runs the full pinned upstream application, not the earlier read-oriented replacement:

| Area | Integration |
| --- | --- |
| Configuration and providers | Full pinned configuration UI, public API keys, upstream provider settings, model mappings and native validation; reviewed changes use revision checks. |
| OAuth and credentials | Native authorization start/status/callback/cancellation; credential import, export, enable/disable, metadata editing, refresh and deletion. |
| Usage, logs and quota | Pinned upstream views and operations through the scoped management transport; effectful requests require trusted review. |
| Native plugins | Registry/install/configuration and native extension operations, bound to the reviewed plugin incarnation. Plugin pages receive separate resource grants. |

CPAMC is a privileged credential editor after unlock: user-managed CPA/provider keys and OAuth account files may enter that document. Hermes session tokens, private owner authority and broker signing authority never do. Browser storage and direct guest network access remain blocked.

Mutations require trusted review. Accepted operations reconcile by receipt rather than being replayed after an uncertain reply. Locking, host/profile retirement and generation changes end the document's authority. Native plugin GET resources can have effects and are also single-attempt; an uncertain response is not reported as “not invoked.”

Desktop external links use the native opener. Dashboard links display their exact URL in a trusted prompt and require a fresh user gesture before opening a separate page. Imports are bounded at 10 MiB and exports at 32 MiB. Native plugin pages cannot inherit Hermes cookies, authentication headers or the console bridge.

While a native page is open, **Show CPA Console** / **Show plugin resource** switches the visible pane without reloading either document or renewing its grant. **Lock CPA Console** remains available in the trusted header and revokes both documents' authority. A closed session is terminal: use **Reload**, then unlock a new session. Accepted operations with uncertain outcomes remain uncertain; reloading does not replay them.

Native plugins execute code inside CPA. The isolated web presentation does **not** sandbox that backend code; install only plugins you trust.

## Persistent qualification deployment

This is a disposable candidate environment, not a live-profile installer. Run from the repository root in Linux/WSL with Docker Compose:

```sh
docker buildx build -f qualification/Dockerfile.runtime --target artifact \
  --output type=local,dest=.verification/runtime-build-output .
docker build --build-context cpa-runtime=.verification/runtime-build-output \
  -t hermes-cpa-qualification:dev -f qualification/Dockerfile .
export CPA_QUALIFICATION_IMAGE="$(docker image inspect hermes-cpa-qualification:dev --format '{{.Id}}')"
dc() { docker compose -p hermes-cpa-dev -f qualification/compose.yaml "$@"; }
q() { dc exec -T keepdev python /opt/q/scripts/qualification.py "$@"; }
dc up -d --wait
q permit --root /work/q --hours 4
q cpa --root /work/q -- setup \
  --archive /opt/q/cache/CLIProxyAPI_8.0.21_hermes-owner-v1_linux_amd64.tar.gz \
  --isolated-authorization /work/q/candidate-permit.json --consent
q smoke --root /work/q --managed
```

The image uses the public pinned Hermes baseline plus the checked-in patch, frozen host dependencies and the reproducibly built, checksum-pinned private-channel CPA runtime. Runtime is non-root, read-only except for a private named volume and temporary files, with no external network, published ports, host mounts or Docker socket. CPA, broker, backend and native CLI share one PID namespace; use `exec`, not a separate `compose run` container, for lifecycle commands.

Fresh boot prepares external mode only. The ordinary Docker healthcheck proves authenticated backend access, not managed CPA readiness; `smoke --managed` also requires CPA to be ready. A headless HTTP backend is not an agent-plugin registration lifetime: after its restart, use the explicit owner/start commands below. Remembered autostart applies when Hermes loads the agent plugin.

### Token persistence

`prepare` creates `ROOT/session-token` once, beneath a private Linux root, with no group/other access. `serve` reuses it and passes it to the backend through the child environment, never argv or stdout. The default Docker root is `/work/q` in `hermes-cpa-dev_data`; `CPA_QUALIFICATION_ROOT` selects another root in that same volume. Container replacement and ordinary source refresh preserve the token. Do not copy it into an image, Git, a Compose envfile or a Windows plaintext file.

This token authenticates Hermes, not CPA. It is separate from user-managed CPA/provider credentials, the expiring candidate permit and the private hosted-selection cache. Repeated/concurrent healthchecks reuse one client and cached server-returned epoch. A lost selection cache while the backend is alive requires a backend restart, **not token rotation**. `smoke --expect-token-sha256 <previous-fingerprint>` verifies persistence without printing the token.

For an explicit fresh-root transition, `prepare --session-token-stdin` accepts the retained generated token through stdin and writes it privately into the new root. It refuses an existing target root. Preserve the old root; do not reuse its permit or silently migrate its CPA configuration and credentials.

The maintained `hermes-cpa-dev` transition was exercised on 2026-10-14: `/work/q` was retained and `/work/q-parity` received only its generated Hermes token. Authenticated managed health passed with the original token fingerprint. The local, ignored `.verification/maintained-compose.env` records the selected image and root, not credentials; use it with `docker compose --env-file .verification/maintained-compose.env -p hermes-cpa-dev -f qualification/compose.yaml ...`. Its explicit four-hour permit expires at **2026-10-14 18:37:14 UTC** and will not renew itself. The enrolled provider currently has no real accounts or models configured.

### Updating the maintained stack

For an ordinary source update with unchanged runtime pins and authority format, quiesce through the running old image first, then replace the image and refresh the installed package. The example uses the default root; use the selected `CPA_QUALIFICATION_ROOT` consistently if it differs.

```sh
q quiesce --root /work/q --consent
dc up -d --wait
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

The private-channel transition from the earlier read-oriented candidate is **not** an ordinary refresh. Existing CPA configuration or legacy `HERMES_CPA_MANAGEMENT_KEY` / `HERMES_CPA_INFERENCE_KEY` entries make new setup refuse rather than reuse or delete them. Quiesce and stop the old container, retain its entire root, and prepare a distinct root with the new image. Transfer only the generated Hermes token through `prepare --session-token-stdin`, then select the new root with `CPA_QUALIFICATION_ROOT`, explicitly issue a fresh permit and run setup. Never mount the retained volume writable in two running containers.

For parallel workers, use the same immutable image with a different project name, such as `hermes-cpa-check-<id>`. Each project gets its own volume, home, token and PID namespace. Never share a writable qualification volume between running containers. Quiesce and remove only the explicitly disposable worker project with its own `down -v`; keep `hermes-cpa-dev` and its volume.

Native qualification uses the same CLI: `prepare --root <new-private-linux-directory> --source <package> --archive <pinned-archive>`, explicit `permit`, then `serve --root ... --host <patched-host> --port <unused-loopback-port>`. Use that host's Python 3.14 environment with its declared dependencies and bcrypt. Roots must be outside live Hermes homes and off Windows-backed filesystems. WSL loopback forwarding is still session-token authenticated.

### Qualification limits and recovery

Preparation and refresh fail closed after interruption; refresh is not a power-loss-atomic update across its directory renames. An interrupted preparation can leave an unmarked root; an interrupted refresh can leave a backup or missing package. Do not bypass the marker or silently adopt that state. For a fully disposable generated Docker project, inspect any state worth retaining before explicitly removing/recreating **that project's** volume; doing so destroys its token and configuration. Normal updates preserve the volume.

Known credential/runtime filenames are excluded case-insensitively from package copies and the Docker context. This is not a content scanner for secrets embedded in arbitrary source files. Generated qualification does not certify stock Hermes, a packaged Windows release, paid inference, real provider authorization or live credential migration.

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

### Desktop and Dashboard qualification

Build the [generated native fixture](qualification/native-resource-fixture/README.md#build-exact), then derive the OAuth and UI images from the immutable backend image:

```sh
docker build -f qualification/Dockerfile.oauth \
  --build-arg CPA_QUALIFICATION_IMAGE="hermes-cpa-qualification:dev@$CPA_QUALIFICATION_IMAGE" \
  -t hermes-cpa-qualification:oauth .
oauth_image="$(docker image inspect hermes-cpa-qualification:oauth --format '{{.Id}}')"
docker build -f qualification/Dockerfile.ui --target qualified \
  --build-arg CPA_OAUTH_QUALIFICATION_IMAGE="hermes-cpa-qualification:oauth@$oauth_image" \
  --build-context native-resource-fixture=.verification/native-resource-fixture \
  -t hermes-cpa-qualification:ui .
ui_image="$(docker image inspect hermes-cpa-qualification:ui --format '{{.Id}}')"
mode=http
run_id="$(date +%s)"
docker run --rm --init --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 1024 --memory 5g --shm-size 512m \
  --tmpfs /tmp:rw,exec,mode=1777,size=2g \
  -v "hermes-cpa-ui-${mode}-${run_id}:/work" --entrypoint python "$ui_image" \
  /opt/q/qualification/run_ui.py --root "/work/ui-${mode}" --mode "$mode" --consent
```

The other modes are `desktop`, `http-remap`, `https` and `https-bare`. Use a fresh volume/root for every attempt. Each run prepares generated accounts, grants an explicit two-hour permit, drives the actual UI and quiesces its owner. Evidence remains under the root's `ui-results`. The OAuth/UI images trust a short-lived generated authority and must never replace the maintained deployment image.

For native Windows, use Windows Node (tested with 24.15.0), the immutable UI image ID and a local Windows Desktop checkout with matching dependencies, Electron 40.10.2 and staged `dist/native/win32-x64` dependencies. Set `$repo` to this repository's Windows-accessible path, `$windowsDesktop` to that checkout's `apps/desktop`, and `$freshRoot` to a nonexistent directory beneath an existing private local directory:

```powershell
node "$repo\qualification\windows_parity.cjs" --image $uiImage `
  --windows-desktop $windowsDesktop --root $freshRoot --consent
```

The runner stages the image's current Desktop bundle and plugin into the fresh root. Its loopback relay uses `docker exec` into a separate network-none backend; no Docker port is published. Generated Hermes authentication stays in memory and child environments, not Windows files or argv. Session network and OS-opener guards are installed before Desktop import; they do not claim to intercept arbitrary Node main-process networking. The result records clean native exit, final persisted-key observation, exact resource checks and bounded text-log scans. Screenshots remain in `$freshRoot/evidence`. This is a development proof, not packaged-release certification.

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

Observed in the current isolated qualification:

| Check | Evidence |
| --- | --- |
| Plugin backend | 974 tests passed in the clean frozen-dependency image, including real native lifecycle paths and repeated content-encoding headers. |
| Targeted host Python suites | 340 earlier checks passed; the final HTTP delivery/resource subset passed 32 checks, including revocation while a send is blocked, expiry, selection retirement and one-time buffer release. |
| CPAMC | 1,570 tests, lint, TypeScript and Vite build passed. |
| Trusted launchers | 28 boundary/lifecycle tests passed. |
| Qualification tooling | 39 checks passed, including preserved-token preparation and deployment lifecycle refusals. |
| Native runtime | Two clean-cache builds produced identical archive and executable hashes; Go vet and the owner/API/plugin-host/server suites passed. |
| Registered provider | Native setup, real Hermes resolution/key command, broker and CPA reached a generated upstream for ordinary/SSE and newly discovered-model inference. Disable/remove denied held grants and clients with zero fallback traffic. |
| OAuth and credentials | Real native PKCE against generated loopback endpoints, native and pasted callbacks, invalid/reused-state refusal, cancellation, enable/disable, metadata editing, explicit refresh, export/import/delete and a 10 MiB upload passed. |
| Native resources | Real plugin-store installation, relative assets, gzip/binary responses, effectful timeout/oversize outcomes, no replay, document/lock revocation and actual native code replacement passed. |
| Desktop host boundaries | Full Desktop typecheck, 109 Electron tests, 29 renderer tests, 7 Dashboard tests and 24 dependency-contract checks passed. Native resource decoding covers gzip, zlib/raw deflate, Brotli, zstd and stacked encodings with bounded output and cancellation. |
| Native Windows development UI | Optional opt-in, generated OAuth login and credential listing, native API-key save and reopen, all 30 native-page checks, grant revocation and acknowledged clean exit passed. The saved key remained after backend quiescence; process and Windows text-log scans found none of the checked host secrets. |
| Windows qualification containment | Actual Electron `net.fetch` was allowed to the owned relay but denied to two non-relay loopback destinations before Desktop import or Playwright routing; the listening canary accepted zero connections. Terminal protocol overflow and wrong-nonce socket closure passed. |
| Linux Desktop UI | Full generated OAuth/configuration flow, one docked workspace, native-page isolation, counted effects, same-document navigation, live pane switching and lock revocation passed. Two pinned host-owned catalog embeds were refused before networking; guest requests received no exemption. |
| Agent-only Dashboard UI | HTTP, remapped HTTP, authenticated HTTPS and HTTPS without forwarded-protocol headers passed. HTTP also exercised generated OAuth, credential visibility, native API-key save and persistence after lock/reload. Every origin exercised native navigation containment, concurrent console work, slow-effect revocation and close during an unread 24 MiB response. |

These are generated-data candidate checks, not one full upstream Hermes suite or packaged-release certification. No real subscription account, paid model call, external provider authorization or live credential migration was exercised. Live Hermes and the independent CPA pilot were not modified.

Tracker: [integration map #1](https://github.com/baladithyab/hermes-cpa/issues/1), [host/package decision #2](https://github.com/baladithyab/hermes-cpa/issues/2), [registered-provider qualification #4](https://github.com/baladithyab/hermes-cpa/issues/4), [full CPAMC parity #5](https://github.com/baladithyab/hermes-cpa/issues/5).
