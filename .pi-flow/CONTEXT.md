# Hermes CPA integration

Connects Hermes Agent to CLIProxyAPI and optionally exposes its management console inside Hermes Desktop.

## Language

**CPA**: CLIProxyAPI, the backend that routes inference requests to configured model providers.

**CPAMC**: CPA Management Center, the upstream configuration and monitoring interface. Its desktop presentation is separate from the inference provider.

**Managed instance**: A CPA process installed and owned by the Hermes plugin after explicit user setup. An independently running CPA is an external instance and must never be adopted or terminated by the plugin.

**Inference broker**: The local backend that grants enrolled Hermes profiles access to CPA without copying the owner's inference key into those profiles.

**Provider enrollment**: The explicit establishment of a broker-backed Hermes provider and model discovery for a profile. It does not authorize management-plane credential changes.

**Desktop half**: Optional trusted Hermes Desktop plugin code that hosts CPAMC and performs authenticated, scoped backend operations. CPAMC guest code receives neither backend session tokens nor CPA keys.

**Docked console**: A CPAMC workspace tab beside chat, using the same workspace contribution mechanism as Monitoring Dashboard.
