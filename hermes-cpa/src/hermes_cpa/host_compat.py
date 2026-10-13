"""Validate the supported host contract before managed setup changes state."""
from importlib import import_module


HOST_CONTRACT_VERSION = 1


def host_capabilities():
    capabilities = {
        'single_request_grants': False,
        'authenticated_request_context': False,
        'selection_authority': False,
        'operation_authority': False,
        'plugin_policy_epochs': False,
        'hosted_native_cli': False,
    }
    try:
        source = import_module('agent.command_token_source').CommandTokenSource
        capabilities['single_request_grants'] = (
            getattr(source, 'supports_single_request_grants', False) is True
        )
    except (ImportError, AttributeError):
        pass
    try:
        context = import_module('hermes_cli.plugin_request_context')
        capabilities['authenticated_request_context'] = callable(
            getattr(context, 'authenticated_context', None)
        )
        capabilities['selection_authority'] = callable(
            getattr(context, 'bind_selection', None)
        )
    except ImportError:
        pass
    try:
        operations = import_module('hermes_cli.plugin_operation_authority').RequestOperations
        capabilities['operation_authority'] = all(
            callable(getattr(operations, method, None))
            for method in ('open', 'call', 'shutdown')
        )
    except (ImportError, AttributeError):
        pass
    try:
        epochs = import_module('hermes_cli.plugin_policy_epoch')
        capabilities['plugin_policy_epochs'] = all(
            callable(getattr(epochs, method, None))
            for method in ('read_policy_epoch', 'record_policy_write', 'record_policy_bytes')
        )
    except ImportError:
        pass
    try:
        host = import_module('hermes_cli.plugin_host').PluginHost
        capabilities['hosted_native_cli'] = (
            getattr(host, 'supports_hosted_native_cli', False) is True
        )
    except (ImportError, AttributeError):
        pass
    return {'version': HOST_CONTRACT_VERSION, **capabilities}


def require_supported_host():
    capabilities = host_capabilities()
    missing = [
        name for name, available in capabilities.items()
        if name != 'version' and available is not True
    ]
    if missing:
        raise RuntimeError(
            'Unsupported Hermes CPA host: ' + ', '.join(missing) + '. '
            'Install the documented host-integration contract before managed setup; '
            'no credentials, configuration, or processes have been changed.'
        )
    return capabilities
