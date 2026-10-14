# Generated native resource fixture

A real native Go plugin (`hermes-resource-fixture`) for the pinned Hermes owner build
of CPA 8.0.21, plus the tools that build, install and exercise it. Generated data only:
no account, credential, public network or paid request. Nothing mocks the native
runtime, the owner console service or its grants.

| File | Role |
| --- | --- |
| `main.go`, `plugin.go`, `state.go`, `handlers.go`, `go.mod`, `assets/` | The plugin. Zero module dependencies (the plugin ABI is JSON over a C ABI), so it builds offline. |
| `build_fixture.py` | Builds both variants with the exact runtime settings, twice each with empty caches, and writes `fixture-manifest.json`. |
| `Dockerfile.fixture` | Runs `build_fixture.py` in the pinned Go 1.26.9 / gcc 12.2 / glibc 2.36 toolchain image. |
| `fixture_common.py`, `fixture_install.py` | Manifest/zip/registry helpers; `PluginRegistry` (loopback plugin store) and `install_fixture()` (real native install via the console). |
| `Dockerfile.smoke` | Smoke image: qualification image + this directory + `resource_smoke.py` + artifacts. |
| `../resource_smoke.py` | Real end-to-end smoke (runner and teardown identical to `provider_smoke`/`parity_smoke`). |
| `tls_ingress.py` | Loopback TLS reverse proxy + certificate generator for the real Dashboard HTTPS proof. |

## Build (exact)

Settings (same as `scripts/build_cpa_runtime.py`): Go `go1.26.9 linux/amd64` with
`GOTOOLCHAIN=local`, `GOOS=linux GOARCH=amd64 CGO_ENABLED=1`, `GOFLAGS=` `GOWORK=off`
`GOPROXY=off` `LC_ALL=C` `TZ=UTC`, `-trimpath -buildvcs=false -mod=readonly`,
`-buildmode=c-shared`, `-ldflags "-X main.generation=<g> -X main.pluginVersion=<v>"`.
Variants: `1.0.0` (generation `1`) and `2.0.0` (generation `2`): identical routes, different
native code and instance nonce. gcc 12.2 / glibc 2.36; only glibc libraries may be NEEDED.

```
docker buildx build -f qualification/native-resource-fixture/Dockerfile.fixture \
    --target artifact --output type=local,dest=./.verification/native-resource-fixture .
```

Output (keep out of the repo): `fixture-manifest.json` and
`hermes-resource-fixture_{1.0.0,2.0.0}_linux_amd64.so`. A local WSL run of the same script
(`python3 qualification/native-resource-fixture/build_fixture.py --go-root .verification/toolchains/go1.26.9 --output <dir>`)
needs gcc, readelf and the pinned Go; it is only byte-identical to the image build if the
compiler matches, so use the image output for qualification.

Stage into any qualification image under `/opt/q/cache/native-resource-fixture/` (the
manifest, both `.so` files) and `/opt/q/qualification/native-resource-fixture/` (this
directory; the manifest's `source_sha256` is verified against it, so a stale build is refused).

## Smoke

```
docker buildx build -f qualification/native-resource-fixture/Dockerfile.smoke \
    --build-arg CPA_QUALIFICATION_IMAGE=<immutable image id> \
    --build-context native-resource-fixture=./.verification/native-resource-fixture \
    -t hermes-cpa-resource-smoke .
RUN='docker run --rm --network none --user 10001:10001 --read-only --cap-drop ALL
     --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=256m
     -v <volume>:/work --entrypoint python hermes-cpa-resource-smoke'
$RUN /opt/q/scripts/qualification.py prepare --root /work/resource-check
$RUN /opt/q/scripts/qualification.py permit --root /work/resource-check --hours 1
docker run ... hermes-cpa-resource-smoke --root /work/resource-check --consent
```

A NEW root and explicit permit are required (the runner refuses anything beyond prepare +
permit, including prior failed runs). One JSON report (fixed check names, booleans,
counts) is printed; exit 0 only when every check passed. The root is retained for diagnosis.

Observed in order: native setup; `plugins.enabled` + a loopback `plugins.store-sources`
config transaction; real plugin-store install from the loopback registry (installed file
bytes compared with the build); native menu registered; resource review + exact consent +
single-use review + grant without a lease; entry page, relative assets, module assets,
genuine gzip bytes, exact binary; every native GET executed once (hit counters); the
counter effect exactly once (memory counter and the out-of-band log agree); duplicate and
encoded queries; sanitized request headers; forged response headers dropped (including a
forged `X-Hermes-Resource-Invoked: 0` that must not hide the invocation); redirects between
exact routes and three denials that report the effect truthfully; native HTTP 500/404,
plugin-error 502, an uncertain outcome (`OUTCOME_UNKNOWN`, `effect_invoked: true`, handler
completes once) and a response above the 32 MiB cap (`RESPONSE_TOO_LARGE`, `response_withheld`);
path/body denials; close, lock and new-document revocation; registry incarnation
replacement by an ordinary plugin reconfigure and by installing different native code behind
identical routes. Each revoked or replaced grant is shown to make zero further invocations.

## Console protocol the UI uses

All calls are the existing `POST /api/plugins/hermes-cpa/console/*` routes:

1. `plugin` `{document_id, lease_id, plugin_id, menu_index: 0}` returns a review whose
   `effects == ["plugin_code"]` and whose summary says GET is not read-only.
2. `plugin` `{document_id, lease_id, review_id, approved_effects: ["plugin_code"]}` returns
   `{kind: "native-resource", grant_id, entry_path, plugin_id, expires_at}` (no lease id).
3. `resource` `{document_id, grant_id, path, query: [[name, value], ...], accept?}` returns
   `{ok, status, headers, body_base64}`; errors are HTTP 409 with `error.code` and, for
   `resource`, `effect_invoked` and `response_withheld`.
4. `resource/close` `{document_id, grant_id}`.

Menu: label `Hermes Resource Fixture`, entry path
`/v0/resource/plugins/hermes-resource-fixture/panel`, menu index 0 (the only menu).

Error codes exercised: `RESOURCE_GONE`, `RESOURCE_REVOKED`, `RESOURCE_PATH_DENIED`,
`REDIRECT_DENIED`, `STALE_SCOPE`, `LEASE_EXPIRED`, `OUTCOME_UNKNOWN`, `RESPONSE_TOO_LARGE`,
`INVALID_INPUT`. A replayed single-use review is refused (the backend currently answers
`UNAVAILABLE`, not `REVIEW_STALE`).

## Routes (all GET, under `/v0/resource/plugins/hermes-resource-fixture`)

`/panel` (menu entry, HTML), `/app.js`, `/module.js`, `/static-dep.js`, `/dynamic.js`,
`/sw.js`, `/style.css`, `/logo.png` (16x16), `/form`; gzip: `/gzip-asset.js`,
`/gzip-module.js`, `/api/gzip`; `/api/state` (counters, not counted itself),
`/api/effect` (counted), `/api/query`, `/api/headers`, `/api/forge` (counted),
`/api/binary`, `/api/large?bytes=N` (counted), `/api/fail` 500, `/api/missing` 404,
`/api/error` (plugin RPC error, host 502), `/api/slow?ms=N` (counted, sleeps);
`/redirect/start` (relative `target?hop=1&dup=a&dup=b`), `/redirect/absolute`,
`/redirect/dotdot`, `/redirect/effect-start` (counted), `/redirect/target`, and the denied
`/redirect/outside`, `/redirect/protocol-relative`, `/redirect/unregistered`.
`/panel` itself sends hostile headers (`Set-Cookie`, `Refresh` to an outside URL, a
permissive `Content-Security-Policy`, `X-Frame-Options: DENY`, `Access-Control-Allow-*`);
a correct owner and presentation host drop them all.

## Out-of-band effect log

The plugin appends one JSON line per counted effect to
`<root>/.hermes/cpa/instances/default/hermes-resource-fixture-effects.jsonl` (set through
the `effect-log` plugin option): `{"event":"effect","seq","route","detail","generation",
"instance_nonce"}`, and `{"event":"finished",...}` when a slow effect completes. Read it
with `fixture_install.read_log()` / `effects()`.

## Browser markers (the panel)

`#fixture[data-state]` is `booting`, then `complete`; `data-failures` counts `fail`,
`data-leaks` counts `LEAK`; `document.title` becomes `fixture:complete:<failures>:<leaks>`.
`#fixture-report` holds the full JSON report. Each check sets `#fixture[data-check-<name>]`
and `<li id="check-<name>" data-result>`. Functional results are `pass`/`fail`; containment
results are `contained`/`LEAK`/`inconclusive` (an outside load is `contained` only with a real
CSP violation event, never a bare network failure). Expected on a correct host: every
functional check `pass`, every containment check `contained`, `data-leaks="0"`,
`data-failures="0"`, except `frame-isolation`/`storage-isolation` which are `inconclusive`
when the page is not framed (top-level).

Functional: `entry` (pathname exact), `script-relative`, `css-relative`, `image-relative`,
`module-root-relative` (root-relative module URL with a relative static import),
`module-dynamic` (root-relative dynamic `import()`), `gzip-script` (a `<script>` served with
`Content-Encoding: gzip`), `gzip-module` (a dynamically imported module served gzip),
`fetch-json`, `fetch-query` (duplicates and encodings), `fetch-binary`, `fetch-gzip`,
`redirect-relative`, `redirect-absolute-path`, `redirect-dot-segments`.
Containment: `headers-sanitized` (a fetch carrying forged `Authorization`/`X-Hermes-*`
headers; the plugin reports only header names, never values), `redirect-outside`,
`redirect-protocol-relative`, `redirect-unregistered`, `outside-fetch`, `outside-image`,
`outside-script`, `root-fallthrough`, `api-fallthrough`, `plugin-api-fallthrough`,
`management-fallthrough`, `unregistered-resource`, `frame-isolation`, `storage-isolation`,
`service-worker`. `data-gzip-script`, `data-module-static`, `data-module-dynamic` and
`data-gzip-module` are also set on `<html>` by the assets themselves.

Interactive (no automatic navigation or effect): `#effect-button` sets
`data-effect-state` (`running`/`done`), `data-effect-status` and `data-effect-count` (the
native counter value, or `error`); each click is exactly one native effect.
`#submit-get-form` (same-origin GET form, repeated and encoded values) lands on `/form`
(`#form-result[data-query]`); `#relative-link`, `#redirect-link` stay in origin.
Outside navigation buttons `#nav-location-outside`, `#nav-open-outside`, `#nav-link-outside`,
`#nav-link-blank-outside`, `#nav-form-outside`, `#nav-meta-outside` set
`data-nav-<location|open|link|link-blank|form|meta>` to `attempted`, then `stayed` after
750 ms if the page survived (contained). `data-nav-open="LEAK"` means a window opened.
Popups (`open`, `link-blank`) are refused by the sandbox (no `allow-popups`) and leave the page
alive. An attempt to navigate the guest frame itself (`location`, `link`, `meta`, `form`) is
refused by the grant origin's own policy: the wrapper's `frame-src` for `location`, `link` and
`meta`, the guest document's `form-action` for `form` (a GET form with one hidden field). Chromium
logs an enforced violation that names the destination (`Framing 'https://outside.invalid/'
violates ... "frame-src <origin>". The request has been blocked.`, with the path dropped, and
`Sending form data to 'https://outside.invalid/hermes-fixture-form?probe=1' violates ...
"form-action <origin>". The request has been blocked.`) and sends no document request. The page then
either stays or is replaced by Chromium's own error document (`chrome-error://chromewebdata/`),
which leaves no marker. In the pinned image `location`, `link` and `form` replace the page, and a
refused form logs both its `form-action` and the guest frame's `frame-src` refusal. Assert the
frame URL is that error document only together with the refusal that names the attempt's
destination, that no request reached the destination, and that the wrapper, host and native
effects are unchanged; reload only the exact approved entry URL before the next probe.

## Installing into a UI root

```python
sys.path.insert(0, '/opt/q/qualification/native-resource-fixture')
import fixture_install as fx
fixture = fx.load_manifest()                      # verifies manifest, libraries, source digest
with fx.PluginRegistry(fixture) as registry, ConsoleClient(root, host) as console:
    entry = fx.install_fixture(console, registry, fx.effect_log_path(root))
```

This enables plugins, registers the loopback store, performs the real store install, waits
for the exact version to register, and sets the effect-log option. The registry is only
needed during the install; the installed plugin keeps working afterwards. A later
`fx.install_version(console, registry, '2.0.0')` replaces the native code (new registry
incarnation, grants revoked).

## TLS ingress for the Dashboard HTTPS proof

`tls_ingress.py` (stdlib; see its docstring). Loopback only, one explicit upstream port,
original `Host` preserved, host allowlist (421 otherwise), nothing logged, bounded request
body/target/response and idle time, Upgrade tunnels supported.

```
python tls_ingress.py make-cert --cert C --key K --dns dashboard.fixture.invalid --dns '*.resource.fixture.invalid'
python tls_ingress.py serve --cert C --key K --upstream-port <hermes port> --listen-port 0 \
    --allow-host dashboard.fixture.invalid --allow-host-suffix .resource.fixture.invalid
```

`serve` prints `{"listening": {"port": N}, ...}`. Local HTTP presentation
(`p-<random>.localhost:<hermes port>`) needs no proxy.

## Known limits and risks

* Both generated libraries were built reproducibly in the pinned toolchain. Real native
  installation, resource delivery and replacement by version `2.0.0` passed isolated
  qualification; the separate Desktop/Dashboard surface matrix is tracked in the root README.
* Native replacement hot-loads a second Go `c-shared` runtime while the first stays loaded.
  That upstream design was exercised with the pinned build; it does not establish compatibility
  with arbitrary native plugins or other runtime versions.
* The oversize stage builds a 32 MiB + 1 response in the plugin (about 150 MB transient).
