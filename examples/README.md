# Example provider specifications

Ready-made starting points for the file passed via `--provider-specification`
(or `$MOAT_INSTALL_PROVIDERS`). Copy one, adjust the endpoint, key variable,
and model list, and keep your real copy out of version control — actual
specifications are deployment-specific.

| File | Shows |
|---|---|
| `providers-minimal.json` | The minimum with an explicit catalog: one OpenAI-compatible gateway with bearer auth and a single model. |
| `providers-discovery.json` | **Discovery mode:** no `models`/`default_model` — moat-install queries `GET {base_url}/models` and configures the models the endpoint reports. |
| `providers.json` | Two providers (a bearer gateway and a header-auth Azure-APIM-style endpoint) using every field, including the optional `pi_api`, `opencode_npm`, `codex_disable_response_storage`, and `codex_profile`. |

The specification is validated strictly whenever `moat-install` runs —
unknown keys, missing fields, bad types, duplicate names, and a
`default_model` missing from an explicit `models` list all fail loudly with
the offending key named. A provider may omit `models` to have its catalog
discovered from the `/models` endpoint at run time. See the main README for
the full schema.
