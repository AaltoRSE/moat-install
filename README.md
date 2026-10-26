# moat-install

**moat-install** is a companion tool for moat that makes it easy to
configure the agentic coding harnesses present in moat's default image —
**pi, oh-my-pi, Codex, OpenCode, Claude Code, aider, goose, and cline**.

You describe your LLM providers once in a small JSON file (endpoint URL, API-key
variable, model list); one command then writes each harness's global
configuration under a chosen **home base** directory. Adding or changing a
provider never requires a code change.

## Quick start

```sh
moat-install --provider-specification providers.json --home-base ~/moat-home
```

The tool will:

1. read the provider specification,
2. take each provider's API key from its environment variable, or ask for it
   with a hidden prompt (keys are never CLI arguments),
3. check that each endpoint is reachable (`--no-verify` to skip),
4. write every harness's configuration into the home base, plus a keyed env
   file that `.bashrc`/`.zshrc` in the base source.

Re-running with the same inputs changes nothing: existing files are backed up
once before their first modification, unrelated settings are preserved, and
unchanged files are not rewritten.

## The home base

moat launches each harness in a container and mounts the home base at the
user's home-directory path inside it. The files moat-install writes into the
base are therefore the harnesses' real global configuration at runtime —
`<base>/.codex/config.toml` *is* `~/.codex/config.toml` inside the container.
Host-side paths are never written into any config.

Pick the base with `--home-base PATH` (or `$MOAT_INSTALL_HOME_BASE`, or an
interactive prompt). To configure the real `$HOME` instead — for running
harnesses directly on the host — use `--init-home-dir`.

## Providers

Providers come from a JSON **provider specification file**, located via (in
order of precedence):

1. `--provider-specification PATH`
2. the `MOAT_INSTALL_PROVIDERS` environment variable
3. neither set → usage error

Ready-made examples are in [`examples/`](examples/) — start from
`providers-minimal.json` (one OpenAI-compatible gateway) or `providers.json`
(two providers using every field). The full schema:

```json
{
  "providers": [
    {
      "name": "my-gateway",
      "display_name": "My OpenAI-compatible Gateway",
      "base_url": "https://gateway.example.org/api/v1",
      "api_key_env": "MY_GATEWAY_API_KEY",
      "auth": "bearer",
      "default_model": "acme/model-7b",
      "models": [
        {"id": "acme/model-7b", "display_name": "Acme 7B",
         "context_window": 131072, "max_output_tokens": 8192}
      ],
      "pi_api": "openai-completions",
      "opencode_npm": "@ai-sdk/openai-compatible",
      "codex_disable_response_storage": false
    }
  ]
}
```

- Required provider fields: `name`, `base_url`, `api_key_env`, `auth`
  (`bearer` or `header`), `models`, `default_model`; `auth_header` is
  required for `header` auth. The remaining fields are optional.
- Validation is strict: unknown keys, missing fields, bad types, duplicate
  names, and a `default_model` missing from `models` are usage errors that
  name the offending key.
- The order of the `providers` array is the default order: the first entry
  becomes each harness's default provider; later entries are still fully
  configured where the harness supports multiple endpoints.

> Some gateways are only reachable from an internal network or via VPN.

## Keys

- Keys are taken from the provider's environment variable if set, otherwise
  via an interactive hidden prompt. **Keys are never accepted as CLI
  arguments.** If stdin is not a TTY and the env var is missing, that is a
  hard error.
- An empty answer at the prompt skips that provider (the rest proceed); a
  note explains that re-running the tool after retrieving the key will
  configure it. If every key is left empty, nothing is written.
- Each key is persisted exactly once, in the managed env file
  `<base>/.config/moat-install/env.sh` (mode 0600), which is sourced by a
  guarded block appended to `.bashrc` and `.zshrc` in the base. A few
  harnesses cannot use env-var indirection and get the literal key written
  into their config instead — those files are chmod 0600 and flagged in the
  `--verbose` warnings.
- Key values never appear in the output: entries are listed by variable name
  only, fully masked as `***`.

## Supported harnesses

| Harness | Config written under the base |
|---|---|
| `pi` | `.pi/agent/models.json` |
| `oh-my-pi` | `.omp/agent/models.json` |
| `codex` | `.codex/config.toml` + derived `.codex/moat-models.json` |
| `opencode` | `.config/opencode/opencode.json` |
| `claude` | `.claude/settings.json` (`env` block) |
| `aider` | `.aider.conf.yml` |
| `goose` | `.config/goose/config.yaml` |
| `cline` | `.cline/data/settings/providers.json` |

Select a subset with `--harness=pi,codex,…`. Moat-written entries are merged
into existing configs: unrelated settings, other providers, and safety
policies are preserved; YAML configs keep their comments. When a harness
supports only one active endpoint (claude, aider, goose), only the default
provider is written and the rest are reported as manual-action notes.

## Command-line reference

```
moat-install [--provider-specification=PATH] [--provider=NAME[,NAME…]]
             [--harness=NAME[,NAME…]] [--init-home-dir] [--home-base=PATH]
             [--no-verify] [--dry-run] [--verbose] [--debug] [--help]
```

| Flag | Meaning |
|---|---|
| `--provider-specification PATH` | Path to the JSON provider specification (wins over `$MOAT_INSTALL_PROVIDERS`) |
| `--provider` | Comma-separated subset of the providers in the specification (default: all, in specification order) |
| `--harness` | Comma-separated subset of the harnesses (default: all) |
| `--home-base PATH` | Home base directory; wins over `$MOAT_INSTALL_HOME_BASE` and `--init-home-dir` |
| `--init-home-dir` | Use the real `$HOME` as home base |
| `--no-verify` | Skip the endpoint reachability check |
| `--dry-run` | Perform everything except writes; print the plan |
| `--verbose` | Print the full summary table, warnings, and manual-action notes |
| `--debug` | Verbose decision trace on stderr (all secrets masked) |

By default the run prints a brief summary: one line per harness with the
providers configured for it and the overall status, plus the env-file and
rc-file lines. `--verbose` replaces it with the full per-provider table.
A failed endpoint check skips that provider's configs and exits with code 3
(see below) unless `--no-verify` is given.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Usage / argument error (including missing non-interactive key) |
| 2 | Missing dependency |
| 3 | Provider validation failed |
| 4 | Config read/write/parse failure for at least one file |

## Install

```sh
pipx install <path-or-git-url>   # or: pip install .
moat-install --help
```

For machines where you cannot install Python packages, the tool can be frozen
into a single executable with PyInstaller (`pyinstaller --onefile --name
moat-install moat_install/__main__.py`); build on the platform you target.
The specification file is not bundled — pass it at runtime.

## Development

```sh
uv venv .venv && uv pip install -e .[dev]
.venv/bin/pytest                # tests (no network; HTTP is mocked)
pre-commit run --all-files      # ruff + pyright
```

## Safety notes

- The endpoint check runs on the host; containers inherit host networking, so
  a passing check implies in-container reachability.
- Codex configs are created with `approval_policy = "on-request"`; existing
  safety settings are never overwritten.
- Files containing plaintext secrets (the env file always; claude's and
  cline's configs in literal-key mode) are written with mode 0600.
