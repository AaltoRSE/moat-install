# Example provider specifications

Ready-made starting points for the file passed via `--provider-specification`
(or `$MOAT_INSTALL_PROVIDERS`). Copy one, adjust the endpoint, key variable,
and model list, and keep your real copy out of version control — actual
specifications are deployment-specific.

| File | Shows |
|---|---|
| `providers-minimal.json` | The minimum: one OpenAI-compatible gateway with bearer auth and a single model. |
| `providers.json` | Two providers (a bearer gateway and a header-auth Azure-APIM-style endpoint) using every field, including the optional `pi_api`, `opencode_npm`, and `codex_disable_response_storage`. |

The specification is validated strictly whenever `moat-install` runs —
unknown keys, missing fields, bad types, duplicate names, and a
`default_model` missing from `models` all fail loudly with the offending key
named. See the main README for the full schema.
