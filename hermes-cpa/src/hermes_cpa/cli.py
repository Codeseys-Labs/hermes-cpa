import argparse
import sys
from .controller import controller_from_profile

def build_parser(parser=None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(prog="hermes cpa", description="Manage CPA lifecycle and configuration")
    sub = parser.add_subparsers(dest="subcommand")

    sub.add_parser("status", help="Get CPA instance status")
    sub.add_parser("start", help="Start managed CPA instance")
    sub.add_parser("stop", help="Stop managed CPA instance")
    sub.add_parser("restart", help="Restart managed CPA instance")
    sub.add_parser("doctor", help="Run CPA diagnostic health checks")
    managed = sub.add_parser("setup", help="Consented managed setup: verify, prepare config with a fresh "
                             "public access key, start the owned CPA and broker, enroll the provider")
    source = managed.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", help="Local hermes-owner-v1 CPA 8.0.21 linux_amd64 runtime archive "
                        "(built by scripts/build_cpa_runtime.py)")
    source.add_argument("--download", action="store_true",
                        help="Fetch the fixed private candidate runtime asset with an authenticated "
                        "`gh` CLI; never the stock CPA release")
    managed.add_argument("--port", type=int, default=8318)
    managed.add_argument("--broker-port", type=int, default=8319,
                         help="Inference broker port; default profile only")
    managed.add_argument("--no-autostart", action="store_true",
                         help="Do not remember managed startup with Hermes")
    managed.add_argument("--no-enroll", action="store_true",
                         help="Skip broker-backed provider enrollment")
    managed.add_argument("--consent", action="store_true",
                         help="Create profile-scoped keys, managed config and owned processes")
    managed.add_argument("--isolated-authorization",
                         help="Explicit expiring disposable candidate permit, not release approval")

    setup = sub.add_parser("init", help="Offline native onboarding; no automatic CPA start")
    setup.add_argument("--archive", required=True, help="Local hermes-owner-v1 runtime archive")
    setup.add_argument("--port", type=int, default=8318)
    setup.add_argument("--consent", action="store_true", help="Create the managed config with a fresh public access key")
    setup.add_argument("--isolated-authorization", help="Explicit expiring disposable candidate permit, not release approval")

    autostart = sub.add_parser("autostart", help="Remember or forget managed startup with Hermes")
    choice = autostart.add_mutually_exclusive_group(required=True)
    choice.add_argument("--enable", action="store_true")
    choice.add_argument("--disable", action="store_true")
    autostart.add_argument("--consent", action="store_true", help="Required to enable")

    bootstrap = sub.add_parser("bootstrap-owner", help="Explicitly reconnect/create an idle owner; never starts CPA or clears recovery")
    bootstrap.add_argument("--consent", action="store_true")
    bootstrap.add_argument("--isolated-authorization", help="Disposable candidate permit, not release approval")
    recovery = sub.add_parser("recover", help="Explicit snapshot recovery; leaves CPA stopped")
    recovery.add_argument("--expected-revision", required=True)
    recovery.add_argument("--snapshot-revision", required=True)
    recovery.add_argument("--expected-generation", required=True, type=int)
    recovery.add_argument("--consent", action="store_true")

    enroll = sub.add_parser('provider-enroll', help='Discover and enroll broker-backed CPA provider; default model unchanged')
    enroll.add_argument('--base-url', required=True, help='Broker literal loopback endpoint ending /v1')
    enroll.add_argument('--control', required=True, help='Broker private supervisor.sock path')
    enroll.add_argument('--consent', action='store_true')

    for name in ('provider-disable', 'provider-remove'):
        action = sub.add_parser(name, help='Revoke enrolled provider access; preserve inference owner')
        action.add_argument('--consent', action='store_true')

    subscription = sub.add_parser('subscription-serve', help='Explicit default-owner inference broker; foreground, no CPA start')
    subscription.add_argument('--upstream', required=True, help='Literal loopback CPA HTTP origin')
    subscription.add_argument('--port', type=int, default=8319)
    subscription.add_argument('--consent', action='store_true')

    cfg = sub.add_parser("config", help="Read or update safe CPA configuration")
    cfg.add_argument("--strategy", choices=["round-robin", "fill-first"])
    cfg.add_argument("--retry", type=int)
    cfg.add_argument("--log", type=str, choices=["true", "false"])
    cfg.add_argument("--expected-revision", type=str, default="")
    cfg.add_argument("--expected-generation", type=int, required=False)
    cfg.add_argument("--restart-consent", action="store_true")

    return parser

def main(args=None):
    parser = build_parser()
    opts = args if isinstance(args, argparse.Namespace) else parser.parse_args(args)
    if opts.subcommand == 'provider-enroll':
        from .provider_onboarding import enroll
        return enroll(opts)
    if opts.subcommand in ('provider-disable', 'provider-remove'):
        from .provider_onboarding import retire
        return retire(opts)
    if opts.subcommand == 'subscription-serve':
        from .subscription_broker import serve_native
        return serve_native(opts)
    controller = controller_from_profile()

    if opts.subcommand == "setup":
        from .onboarding import SetupRefused, setup_managed
        try:
            outcome = setup_managed(controller.settings, controller.package_root, opts.archive,
                consent=opts.consent, port=opts.port, broker_port=opts.broker_port,
                autostart=not opts.no_autostart, download=opts.download,
                enroll=not opts.no_enroll, isolated_authorization=opts.isolated_authorization)
        except SetupRefused as exc:
            print(f"Setup refused ({exc.code}): {exc.message}")
            return 1
        except Exception:
            print("Setup stopped unexpectedly before completion. Run hermes cpa doctor before "
                  "retrying; do not repeat setup blindly.")
            return 1
        return _report_setup(outcome)

    if opts.subcommand == "init":
        from .onboarding import SetupRefused, initialize_managed
        try:
            initialized = initialize_managed(controller.settings, controller.package_root, opts.archive,
                consent=opts.consent, port=opts.port,
                isolated_authorization=opts.isolated_authorization)
            result = initialized.bootstrap_owner()
            if not result.get('ok'):
                print("Prepared but owner unavailable; do not repeat onboarding. Run doctor.")
                return 1
        except SetupRefused as exc:
            print(f"Onboarding refused ({exc.code}): {exc.message}")
            return 1
        except Exception:
            print("Onboarding refused or incomplete; mode remains external until preparation succeeds. Run doctor.")
            return 1
        print("Managed configuration prepared; owner idle. Explicit start is required.")
        return 0

    if opts.subcommand == "autostart":
        return _set_autostart(controller, enable=opts.enable, consent=opts.consent)

    if opts.subcommand == "bootstrap-owner":
        if not opts.consent:
            print("Owner bootstrap requires explicit --consent; no launch attempted.")
            return 1
        if opts.isolated_authorization:
            controller.isolated_authorization = opts.isolated_authorization
        result = controller.bootstrap_owner()
        if not result.get('ok'):
            print("Owner unavailable. Run doctor; existing recovery state was not cleared.")
            return 1
        print(f"Owner available. Generation: {result['generation']}. No CPA start requested; run doctor.")
        return 0

    if opts.subcommand == "recover":
        result = controller.recover_config(expected_revision=opts.expected_revision,
            snapshot_revision=opts.snapshot_revision, expected_generation=opts.expected_generation,
            native_consent=opts.consent)
        print("Recovered; CPA remains stopped. Explicit start required." if result.get('ok')
              else "Recovery refused or incomplete. Inspect revisions and run doctor; do not retry blindly.")
        return 0 if result.get('ok') else 1

    if opts.subcommand == "status":
        st = controller.get_status()
        snapshot = controller.snapshot()
        from .controller import profile_options
        print(f"Lifecycle: {st.lifecycle.value}")
        print(f"Management Ready: {st.management_ready}")
        print(f"Inference Configured: {st.inference_configured}")
        print(f"Owner Lifetime: {snapshot.get('lifetime') or 'none'}")
        print(f"Broker: {snapshot.get('broker') or 'none'}")
        print(f"Autostart: {profile_options(controller.settings.home).get('autostart') is True}")
        print(f"Revision: {controller.config_store.compute_digest()}")
        return 0

    if opts.subcommand == "start":
        ok = controller.start()
        print("Started" if ok else "Start failed")
        return 0 if ok else 1

    if opts.subcommand == "stop":
        ok = controller.stop()
        print("Stopped" if ok else "Stop failed")
        return 0 if ok else 1

    if opts.subcommand == "restart":
        ok = controller.restart()
        print("Restarted" if ok else "Restart failed")
        return 0 if ok else 1

    if opts.subcommand == "config":
        changes = {}
        if opts.strategy:
            changes["strategy"] = opts.strategy
        if opts.retry is not None:
            changes["request_retry"] = opts.retry
        if opts.log:
            changes["request_log"] = opts.log.lower() == "true"

        if changes:
            res = controller.apply_config(opts.expected_revision, changes, restart_consent=opts.restart_consent,
                                          expected_generation=opts.expected_generation)
            print(f"Applied: {res}")
            return 0 if res["ok"] else 1
        else:
            cfg = controller.get_config()
            print(f"Strategy: {cfg.strategy}")
            print(f"Request Retry: {cfg.request_retry}")
            print(f"Request Log: {cfg.request_log}")
            print(f"Revision: {cfg.revision}")
            return 0

    if opts.subcommand == "doctor":
        st = controller.get_status()
        print(f"CPA Version: {st.cpa_version}")
        print(f"Console Version: {st.console_version}")
        print(f"Lifecycle: {st.lifecycle.value}")
        print(f"Binary Exists: {controller.settings.binary_path.exists()}")
        print(f"Config Exists: {controller.settings.config_path.exists()}")
        try:
            from .installer import CpaInstaller
            runtime = CpaInstaller(controller.settings, controller.package_root).runtime_manifest()
            print(f"Runtime Integration: {runtime.data['integration']} "
                  f"(artifact pinned: {runtime.pinned})")
        except Exception:
            print("Runtime Integration: unavailable (package metadata missing or invalid)")
        recovery = controller.recovery_status()
        for key, label in [('owner_available', 'Owner Available'), ('generation', 'Generation'),
                           ('current_revision', 'Current Revision'), ('snapshot_revision', 'Snapshot Revision'),
                           ('journal_phase', 'Journal Phase'), ('recovery_required', 'Recovery Required')]:
            print(f"{label}: {recovery[key]}")
        if not recovery['owner_available'] and controller.mode == 'managed':
            print("Owner loss is not proof of stopped CPA. Explicit bootstrap-owner --consent, then doctor again before recover.")
        return 0

    parser.print_help()
    return 0


def _report_setup(outcome):
    """Print what is actually running after this command returns."""
    print("Prepared: pinned hermes-owner-v1 executable and CPA config; mode is managed. No private "
          "CPA key was created; the owner and broker use private owner-only channels.")
    print(f"Public access: a fresh public API key was generated into {outcome['config_path']} (mode "
          "0600, access.api-keys) and public management is disabled. Both are your configuration: "
          "view or change them in the CPAMC console or the config file; changing them never affects "
          "the private broker.")
    if outcome['owner'] == 'running' and outcome['lifetime'] == 'persistent':
        print("Owner: running with persistent lifetime. It keeps running after this command until "
              "`hermes cpa stop`, plugin disable/removal or a relevant policy change.")
    elif outcome['owner'] == 'running':
        print("Owner: running with plugin lifetime. It stops once no Hermes registration holds it.")
    else:
        print("Owner: unavailable; CPA was not started. Run hermes cpa doctor, then hermes cpa start.")
    print({'ready': f"CPA: ready on 127.0.0.1:{outcome['port']}.",
           'start_requested': "CPA: start requested; readiness not confirmed. Run hermes cpa status.",
           'start_failed': "CPA: start failed; run hermes cpa doctor.",
           'not_started': "CPA: not started."}[outcome['cpa']])
    print({'ready': f"Broker: ready at http://127.0.0.1:{outcome['broker_port']}/v1.",
           'disabled': "Broker: not hosted; only the default profile owns the inference broker.",
           }.get(outcome['broker'], "Broker: unavailable (port in use or another broker owns this "
                                    "home); never adopted. Provider not enrolled."))
    print({'enrolled': "Provider: custom:cpa enrolled through the broker; default model unchanged.",
           'failed': "Provider: enrollment failed; check the broker, then hermes cpa provider-enroll.",
           }.get(outcome['provider'], "Provider: not enrolled."))
    print("Next Hermes load: resumes managed services (autostart remembered)." if outcome['autostart']
          else "Next Hermes load: does not start managed services (autostart off).")
    if outcome.get('error'):
        print("Setup stopped after preparation; do not repeat setup. Run hermes cpa doctor.")
    ok = (outcome['owner'] == 'running' and outcome['cpa'] == 'ready' and
          outcome['provider'] != 'failed' and outcome['broker'] != 'unavailable')
    return 0 if ok else 1


def _set_autostart(controller, *, enable, consent):
    """Remember or forget startup with Hermes; this is a plugin policy write."""
    if controller.mode != 'managed':
        print("Autostart applies only to a managed profile; run hermes cpa setup first. Nothing changed.")
        return 1
    if enable:
        if not consent:
            print("Enabling autostart requires --consent. Nothing changed.")
            return 1
        from .host_compat import require_supported_host
        try:
            require_supported_host()
        except RuntimeError as exc:
            print(str(exc))
            return 1
    from hermes_cli.config import load_config, save_config
    cfg = load_config()
    options = cfg.setdefault('plugins', {}).setdefault('entries', {}).setdefault(
        'hermes-cpa', {}).setdefault('settings', {})
    if (options.get('autostart') is True) == enable:
        print(f"Autostart already {'enabled' if enable else 'disabled'}. Nothing changed.")
        return 0
    options['autostart'] = enable
    save_config(cfg)
    print("Autostart enabled: Hermes loads start managed services." if enable
          else "Autostart disabled: Hermes loads no longer start managed services.")
    print("Hermes records this as a plugin policy change, so a running managed owner is revoked "
          "and stops. Use hermes cpa start to start explicitly.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
