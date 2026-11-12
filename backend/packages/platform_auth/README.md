# platform-auth

Framework-neutral backend SDK for Agent authentication against the unified platform.

The package owns stable identity/wire models, Entry authentication HTTP clients, the signed
Entry -> Agent identity protocol, and typed client/protocol errors. It does not depend on FastAPI
and does not own application repositories, local user directories, configuration objects, or
server-side authentication state.

Allowed responsibilities:

- call the platform authentication service over HTTP;
- serialize/deserialize authentication wire data;
- expose stable identity/context models and client errors;
- verify the signed identity envelope projected by Entry to an Agent backend.

```python
from platform_auth import PlatformAuthClient

client = PlatformAuthClient("https://aihub.example")
identity = await client.resolve_access_token(access_token, tenant_id="1")

# Privileged server-side session introspection. The SDK forwards the browser Cookie header
# without depending on Entry's configured cookie name.
context = await client.resolve_session(request.headers["cookie"])
credential = context.sso_credential
```
