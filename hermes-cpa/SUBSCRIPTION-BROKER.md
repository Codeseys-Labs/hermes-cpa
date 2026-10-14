# Opt-in subscription broker — isolated candidate, not release approval

Owner decision: default owns the inference subscription; all local live worker
profiles may consume it. This is not a management credential fallback. The new
broker is native backend code, never a renderer/guest API. The standalone
`subscription-serve` command does not start CPA or change model settings.
Explicit managed `hermes cpa setup --consent` starts CPA and the broker, enrolls
the provider and remembers autostart by default. Neither path migrates upstream
credentials or installs a system service.

Standalone external forwarding, separate from managed setup:

    python -m hermes_cpa.cli subscription-serve --upstream http://127.0.0.1:<fixture-port> --port 0 --consent
    python -m hermes_cpa.subscription_client --control <default-home>/cpa/run/subscription/supervisor.sock

The first is foreground, default profile only, literal loopback, and exits cleanly
on SIGTERM/SIGINT. Without consent it refuses. Production default broker port is
8319; tests ask the OS for an unused port. It never adopts another broker/socket.
The second prints JSON with a short-lived broker grant and expires_in=0, NOT the
upstream key. Treat grants as sensitive capabilities. Do not log them. Native
Hermes providers.<name>.key_cmd can invoke this helper with the consumer's bound
home; base_url is the printed broker URL including /v1. No static api_key/key_env
is needed in the consumer. The isolated host patch honors zero TTL by invoking
the helper per request; older hosts cache it and are not supported for this flow.
The [repeatable qualifier](../qualification/provider_smoke.py) exercises actual
resolve_runtime_provider -> key_cmd subprocess -> OpenAI SDK -> broker -> pinned
CPA 8.0.21 -> generated loopback upstream, including ordinary and SSE inference.

Observed native provider enrollment (isolated generated fixture):

    python -m hermes_cpa.cli provider-enroll --base-url http://127.0.0.1:<broker-port>/v1 --control <owner-home>/cpa/run/subscription/supervisor.sock --consent

This explicitly adds/refreshes only providers.cpa in the selected native profile,
using the installed helper's interpreter/source path and key_cmd (no static key).
It authenticates GET /v1/models through the broker, records every returned model
ID with discover_models enabled, preserves the default model and unrelated config,
and refuses a conflicting/disabled cpa entry or discovery failure. Select the
custom:cpa provider in native Hermes. The existing catalog cache automatically
revalidates on expiry (normal fresh window one hour); Refresh Models forces
discovery without reenrollment. The checked-in qualifier uses the public
one-second TTL override with real elapsed time, then verifies that the normal
picker consumes the refreshed cache. It does not edit cache files, change the
production TTL or rewrite the enrollment snapshot.

The qualifier verifies added and removed IDs, inference with the new model,
distinct request grants, and unchanged enrollment during discovery. Disable and
remove each start from independently proven live grants; a generated fixture
re-enable separates them. Held SDK calls, captured grants and fresh named-provider
resolution are denied afterward, including with a configured generated fallback.
The upstream receives no retirement or fallback traffic.

Earlier exploratory notes described two-profile delegate routing, a PTY picker
and full parent/delegate agent turns. Those artifacts are not part of the
checked-in qualifier and are not claimed as current repeatable evidence. The
current proof is the registered runtime/SDK path, not full agent or Desktop E2E.
Production installation remains unverified. Enrollment does not start the broker,
enable disabled providers, change upstream credentials, or migrate manual entries.

Enrollment publishes the permanent marker before replacing provider config.
Process termination after config commit therefore cannot reopen legacy admission
on later provider removal. A marker write failure publishes no provider. A crash
between marker and config deliberately leaves admission closed: recovery is not
automatic and the marker must not be casually removed to restore legacy access.
The config is checked for drift after discovery and again after marker I/O.
These are optimistic checks, NOT atomic CAS with arbitrary external config
writers, NOT a two-file transaction, and NOT power-loss durability proof.
Concurrent edits after the final check remain a qualification gap. Retirement
itself only replaces config while retaining the existing marker; there is no
second retirement write. Generated tests preserve retirement during discovery.

Retire access before uninstalling the plugin:

    python -m hermes_cpa.cli provider-disable --consent
    python -m hermes_cpa.cli provider-remove --consent

Disable retains a disabled entry; remove deletes only providers.cpa. Both preserve
an enrollment tombstone under cpa/provider-enrollment.json, so the legacy helper
cannot bypass removal. Enrolled helpers explicitly pin --home and --enrolled;
broker mint and HTTP admission re-read provider and plugin disable policy. Grants
bind the consumer config file identity, so edits/removal/replacement invalidate
captured grants, including disable/re-enable ABA. Missing helper source denies
admission. Do not delete the enrollment tombstone during provider removal. An
already dispatched inference is not cancelled by provider retirement.
Command-backed native providers skip legacy static credential pools. A failed
helper raises without substituting ambient credentials. Rotation invalidates old
grants (401); the next helper invocation obtains a new epoch-bound grant. Explicit
revoke() alone still permits fresh issuance; provider-disable is durable retirement.

Managed candidate owners capture one immutable authorization deadline, bounded
by wall and monotonic clocks, and share it with their broker. Expiry denies new
grants and HTTP admissions, bounds active response budgets, and stops the owned
CPA and broker. Failed streams do not receive successful terminal framing.
Changing or renewing the permit file does not extend a running owner; explicitly
relaunch the owner to capture a newly issued permit. No automatic renewal occurs.
Accepted releases have no candidate lease. Standalone external forwarding is not
a managed candidate owner and does not acquire one.

The managed broker uses the exact-owned CPA child's private inference channel.
It has no private CPA inference key to copy, rotate or expose. Changes to public
CPA keys, host, port, TLS or management settings do not retarget this channel or
change the enrolled broker endpoint.

The standalone external broker instead reads only `HERMES_CPA_INFERENCE_KEY`
from the explicit owner's local `.env` via the existing tokenizer. It does not
use ambient, management, another worker, external vault or managed-overlay
fallback. Source failure/removal denies admission. File identity plus key digest
advances its epoch, including A-B-A writes; invalid reads revoke existing grants.

Explicit revoke and process restart invalidate existing grants. Grants expire
after 30 seconds; signatures are process-private and never persist. Every
admission rechecks the grant, epoch and live consumer directory.
Private Unix control socket mode0600 and same-UID peer authentication are the
local authority boundary. A same-user process can choose any live worker because
the authorized mapping is all local workers, not mutually hostile OS principals.
Do not expose this socket or assume per-worker isolation against same-UID code.

The HTTP plane allows only GET `/v1/models`, POST `/v1/chat/completions` and
POST `/v1/responses`, and rejects browser-origin requests. Managed requests use
the private inference channel; standalone external forwarding substitutes its
owner's inference key server-side. Neither mode forwards client authorization
headers, follows redirects, uses ambient proxies, returns upstream error bodies
or retries requests. Admission is the linearization point: revocation prevents
future admissions but cannot unsend a request already dispatched. Consumer
request bytes are passed through.

Generated chunked HTTP checks exercise safe SSE byte preservation and, in
external-key mode, literal credential-echo refusal across chunk boundaries. That
mode holds at most one credential-length suffix and aborts on a literal key echo.
Failed, oversized or expired streams receive no terminating HTTP chunk, so the
client sees an incomplete response rather than successful EOF. Only fixed
JSON/SSE media types are returned. This is not a general filter for encoded
secrets or unknown upstream provider credentials.

Limits: 32 connection threads, 1MiB input, 8MiB response, 10s socket/inactivity
bounds, and a 300s total response-I/O deadline starting before connection setup,
capped by the captured candidate lease when present.
One timer per admitted request shuts down captured upstream/downstream sockets,
including trickling response headers/body and blocked slow-consumer writes. The
HTTP trace extension captures the upstream socket before headers arrive; connect
uses a bounded inactivity timeout and late connections are closed before dispatch.
Completed requests cancel their timer without touching replacement connections.
A separate 10s total ingress deadline now
shuts down trickling header/body sockets before authorization. Real socket
regressions verify EOF, no upstream dispatch and reclamation of all connection
slots using a shortened test budget. Credential/consumer filesystem resolution
now runs outside the authority lock with a 2s caller deadline and at most one
unresolved daemon job. Concurrent callers queue within their own 2s deadline. Timeout
invalidates outstanding grants; late resolver results cannot publish authority.
Revocation and broker shutdown do not wait for blocked filesystem resolution.
Broker shutdown also expires registered active response budgets outside the authority
lock, interrupting both captured sockets and reclaiming the response worker rather
than leaving authorized streams running until their normal 300s deadline. It does
not stop the independent upstream service. New response registration after closure
is refused; no request is replayed. Ingress-only readers retain their 10s bound.
This does not cancel an arbitrary blocked filesystem thread: its single slot
stays occupied until it returns, and the broker fails closed meanwhile.
Real generated socket regressions use shortened deadlines, reclaim every handler
slot and observe one upstream dispatch only. These are not live subscription tests.
Additional adversarial, streaming, cancellation, cross-process rotation/restart and
canary coverage is required. Literal active inference-key echoes are now refused;
encoded echoes and unknown upstream credentials remain unproven. Stale socket recovery after SIGKILL
is fail-closed, not automatically repaired. Coordinated upstream key rotation and
native explicit revoke UX are not implemented; no live keys were changed.

This feature does not satisfy all CPA acceptance rows or authorize deployment.
Only generated HTTP fixtures were used for broker inference; no paid model call
or real subscription credential was used. Original real pinned CPA/Desktop
evidence remains separate. Linux/WSL backend only; no Windows broker claim.
