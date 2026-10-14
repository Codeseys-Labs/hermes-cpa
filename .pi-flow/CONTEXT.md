# Hermes CPA integration

Connects Hermes Agent to CLIProxyAPI and exposes its management console through the Agent Dashboard and, optionally, Hermes Desktop.

## Language

**CPA**: CLIProxyAPI, the backend that routes inference requests to configured model providers.

**CPAMC**: CPA Management Center, the upstream configuration and monitoring interface. Once unlocked through the trusted launcher, it can manage CPA configuration, provider API keys, OAuth accounts and native plugins; its presentation is separate from the inference provider.

**Managed instance**: A CPA process installed and owned by the Hermes plugin after explicit user setup. An independently running CPA is an external instance and must never be adopted or terminated by the plugin.

**Inference broker**: The local backend that grants enrolled Hermes profiles access to CPA without transferring the managed instance's private owner authority to those profiles.

**Provider enrollment**: The explicit establishment of a broker-backed Hermes provider and model discovery for a profile. It does not authorize management-plane credential changes.

**Desktop half**: Optional trusted Hermes Desktop plugin code that hosts CPAMC and mediates authenticated, scoped backend operations. CPAMC guest code may receive user-managed credentials after trusted unlock, but never Hermes session tokens or private owner or broker authority.

**Docked console**: A CPAMC workspace tab beside chat, using the same workspace contribution mechanism as Monitoring Dashboard.

**User-managed credentials**: CPA public API keys, upstream provider API keys and OAuth account credentials that the user configures through CPAMC. They are distinct from the Hermes session token and the managed instance's private owner authority.

**Console lease**: A bounded authorization for one unlocked CPAMC document in a particular host session and plugin generation. Locking or retiring that session invalidates the document's authority.

**Native resource grant**: A separate, short-lived authorization for an exact native plugin resource and its presenting document. It binds the reviewed plugin incarnation and does not grant general console management authority.
