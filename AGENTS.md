# AGENTS.md — moat-install

Instructions for an implementing agent working on **moat-install**: a Python
CLI that configures agentic coding harnesses ("harnesses") to talk to one or
more LLM providers ("profiles") defined in a **provider specification file**
(§5), given API keys.

The system described here is **implemented**; this document is its
authoritative description. Keep it in sync with the code: when you change the
architecture, update the matching section here. The design is **prescriptive**
— do not invent a different architecture. New facts (a harness changing its
config format, a new harness, a new provider) must be verified before coding:
ask the user whether they want to provide a link/reference before doing any
web research — never crawl or browse on your own initiative without
permission.

---

## 1. Goal

One command configures any subset of eight harnesses to talk to one or more
providers from the provider specification (§5), writing each harness's
**global user configuration** under a chosen **home base** directory. Keys are
supplied via environment variables or an interactive hidden prompt — never as
CLI arguments. Provider facts live in the specification file, not the code
base: adding or changing a provider never requires a code change.

### Runtime context — why a home base

The harnesses are not run directly on the host. They are launched through a
container runner ("moat") that starts each harness in a container (Apptainer or
Docker) and **bind-mounts the home base at the user's home-directory path inside
the container**. At runtime, the home base *is* `$HOME`: `<base>/.codex/config.toml`
is read as `~/.codex/config.toml`, `<base>/.bashrc` is `~/.bashrc`, and so on.
Design every write from that perspective:

- The home base is not a scratch or test directory — it is the harnesses' real
  runtime home.
- Host-side absolute paths of the base must never be baked into harness configs:
  inside the container the base lives at the container's home path, not at its
  host path. Use `$HOME`-style or config-file-relative references instead.
- Host environment variables are passed into containers by default, so a key
  exported on the host reaches the harness inside the container too (a second
  key-delivery channel — see §9).

## 2. Non-goals (do not build)

- Per-project configuration (only harness *global* config paths, rooted at home base).
- Installing or detecting harnesses. No `which`/PATH checks. `--harness` is the only filter.
- Cursor support (dropped).
- Keys passed as CLI parameters (forbidden outright — shell history / `ps` leakage).
- CI configuration.
- Automatic `git commit` (see §11).
- Creating or modifying environments/config for the container runner (that is the
  user's `env create -H <base>` step, done outside this script). This script only
  prepares the directory such an environment points at.

## 3. CLI specification

The `moat-install` command is provided via pyproject.toml `[project.scripts]`:

```toml
[project.scripts]
moat-install = "moat_install.cli:main"
```

All CLI code (argument parsing, orchestration, exit codes) lives in
`moat_install/cli.py` (`run()` returns the exit code, `main()` wraps it in
`sys.exit`); `moat_install/__main__.py` is a thin delegator so
`python -m moat_install` also works from a source checkout.

```
moat-install [--provider-specification=PATH] [--provider=NAME[,NAME…]]
             [--harness=NAME[,NAME…]] [--init-home-dir] [--home-base=PATH]
             [--no-verify] [--dry-run] [--verbose] [--debug] [--help]
```

| Flag | Semantics |
|---|---|
| `--provider-specification PATH` | Path to the JSON provider specification (§5). Wins over the `MOAT_INSTALL_PROVIDERS` environment variable. Missing/invalid file → usage error (exit 1). |
| `--provider` | Comma-separated subset of the providers defined in the specification. Default: all, in specification order. Order matters (see §5 default-model rule). Unknown name → usage error listing the valid names from the specification. |
| `--harness` | Comma-separated subset of `aider,claude,cline,codex,goose,oh-my-pi,opencode,pi`. Default: all eight. Validated against the adapter registry. |
| `--init-home-dir` | Use the user's real `$HOME` as home base — i.e. configure the host home for running harnesses directly on the host instead of inside containers. |
| `--home-base PATH` | Explicit home base; wins over `$MOAT_INSTALL_HOME_BASE` and over `--init-home-dir`. |
| `--no-verify` | Skip the `/models` endpoint validation. |
| `--dry-run` | Perform everything except writes: resolve paths, collect keys, run validation, run every adapter in dry-run mode (they load/merge but write nothing), and print the plan (home base; with `--verbose` also the masked env-file entries and the rc-file plan). Would-create/would-update statuses follow the `--verbose` rules. |
| `--verbose` | Print the full summary table (per harness × provider statuses, files, key mode, backups, parse check), warnings, manual-action notes, and the resolution plan (masked env-file entries, rc-file plan). Default output is the brief summary (§11). |
| `--debug` | Verbose trace of every decision: path resolution, merge steps, HTTP calls, file creation, backups, rc-file edits. Emitted through Python `logging` at DEBUG level on the `moat_install` logger (see §11 Logging); **all secrets must be masked** in debug output. |

### Key input rules

- For each selected provider, the key is taken from (in order):
  1. The provider's environment variable, if already set and non-empty.
  2. Interactive hidden prompt (`getpass.getpass`), labelled with the env var
     name and a `[leave empty to skip]` reminder.
- If stdin is not a TTY and the env var is missing → hard error for that provider
  (non-interactive mode must never hang).
- An empty answer at the interactive prompt **skips that provider**: it is not
  configured (no adapter entry, no validation, no env-file entry) and a note
  tells the user they can re-run the tool after retrieving the key. The next
  configured provider becomes the default. If every selected provider is
  skipped, nothing is written and the run exits 0.
- **Never** accept keys as flags/positional args. There is no `--key` option; do not add one.
- Keys must never appear in logs, debug output, error messages, or the summary —
  they are masked **fully** as `***` (no key material, not even last characters;
  `moat_install.home.mask_key` is the single masking helper).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including partial success where a provider's validation failed only with `--no-verify`… see §8) |
| 1 | Usage / argument error (including missing non-interactive key) |
| 2 | Missing dependency (see §10) |
| 3 | Provider validation failed (no `--no-verify`) |
| 4 | Config read/write/parse failure for at least one file |

## 4. Home base resolution

Resolve in this order (`moat_install.home.resolve_home_base`): `--home-base` →
`--init-home-dir` (`$HOME`) → `$MOAT_INSTALL_HOME_BASE` → if it is unset,
interactively prompt for the path (only when stdin is a TTY; otherwise a
usage error).

- Create the home base directory (and parents) if it does not exist (skipped in dry-run).
- All harness config paths are the harness's usual **global** config path, but
  relative to home base — see the per-harness table in §7. A container
  environment is later pointed at this directory and it becomes `$HOME` at
  runtime (§1), so the standard global locations relative to the base are
  exactly what the harnesses will read.
- **User-facing output:** every run starts with a fixed ASCII-art banner
  (`moat_install.cli._BANNER`) and a short description of what the tool does,
  including the empty-key-skip rule — purely presentational. The resolved home
  base is then announced (`Home base: …`) **before** the API-key prompts, so
  the user knows where the configuration will be written before entering any
  secrets; it is printed exactly once per run. After that, the
  *default* output is a brief description of what
  was configured — providers (default marked), one line per harness
  with the providers actually configured for it and the aggregate status, plus
  the env/rc file status lines. Warnings, manual-action notes, and the full
  per-provider table appear only with `--verbose` (a suppressed-count hint is
  printed in their place). Failures are always shown. Apart from the banner's
  brief mention that the tool is designed for the moat container runner (with
  the `--init-home-dir` escape hatch for direct host use), no output talks
  about containerization (the mechanics of §1/§9 are documented in README and
  code docstrings instead).
- Write shell startup files (`.bashrc`, `.zshrc`) inside the base, not on the
  host — at container runtime these are the `$HOME` startup files the harnesses'
  shells read.

## 5. Provider specification (`moat_install/providers.py`)

Provider facts are **not hard-coded** in the code base. They are read from a
JSON **provider specification file**, located via (in order of precedence,
`moat_install.cli.resolve_specification_path`):

1. `--provider-specification PATH`
2. the `MOAT_INSTALL_PROVIDERS` environment variable
3. neither set → usage error (exit 1) with a remedy message

`load_provider_specification(path)` parses and strictly validates the file and
returns a `ProviderRegistry` (`profiles` dict + `order` tuple). Validation is
strict so spec typos fail loudly: unknown keys (top level, provider, model)
are rejected; required fields must be present; `default_model` must be in
`models`; `auth_header` is required for `auth: "header"`; names must match
`^[a-z0-9][a-z0-9_-]*$` (they become TOML/JSON/YAML keys in harness configs);
duplicate names and empty model lists are rejected. All problems raise
`SpecificationError` with a message identifying the file and the offending
key (surfaced as exit code 1).

Schema (all fields shown; optional ones noted):

```json
{
  "providers": [
    {
      "name": "llm-gateway",
      "display_name": "Example LLM Gateway",
      "base_url": "https://llm-gateway.example/api/v1",
      "api_key_env": "LLM_GATEWAY_KEY",
      "auth": "bearer",
      "default_model": "example-org/model-a",
      "models": [
        {"id": "example-org/model-a", "display_name": "Model A",
         "context_window": 1048576, "max_output_tokens": 65536}
      ],
      "pi_api": "openai-completions",
      "opencode_npm": "@ai-sdk/openai-compatible",
      "codex_disable_response_storage": false
    }
  ]
}
```

- Required provider fields: `name`, `base_url`, `api_key_env`, `auth`
  (`bearer` or `header`), `models`, `default_model`. Optional:
  `display_name` (defaults to `name`), `auth_header` (required for `header`
  auth), `pi_api` (default `openai-completions`), `opencode_npm` (default
  `@ai-sdk/openai-compatible`), `codex_disable_response_storage` (default
  `false`).
- Required model fields: `id`, `display_name`, `context_window`,
  `max_output_tokens` (positive integers).
- The order of the `providers` array is the default selection order; the
  first entry becomes each harness's default provider (the default-model
  rule in §7).
- Example specification files are tracked in `examples/` and contain only
  generic placeholders (`example.org` domains, made-up model ids) — never real
  endpoints, institutional hostnames, or key material. Keep the shipped
  examples in sync when the schema changes, and keep them passing
  `load_provider_specification` validation. Real (deployment-specific)
  specifications live outside the repository.

No runtime `/models` discovery for catalog building; the catalog comes from
the specification.

**Default-model rule when both providers are configured into one harness:** the
provider listed first in `--provider` (or the default order when omitted) is the
harness's default provider; its `default_model` becomes the harness default
model (`adapter.default_selection` is the first selection). The other provider
is still fully configured and switchable — except in single-endpoint harnesses
(claude, aider, goose), where only the default is written and the rest are
reported as manual-action notes (§7).

## 6. Architecture

```
.
├── AGENTS.md
├── README.md               # user-facing docs
├── LICENSE                 # MIT
├── examples/               # tracked example provider specification files (generic placeholders) + README
├── pyproject.toml          # packaging: metadata, deps, dev extra, [project.scripts]; ruff/pytest/pyright config
├── .pre-commit-config.yaml
├── .secrets.baseline       # detect-secrets baseline used by the pre-commit hook (tracked)
├── .gitignore
├── moat_install/
│   ├── __init__.py
│   ├── cli.py              # CLI parsing, orchestration, exit codes (the [project.scripts] target)
│   ├── __main__.py         # thin delegator to cli.main() → python -m moat_install
│   ├── providers.py        # §5 spec-file loading/validation + profile dataclasses
│   ├── home.py             # home-base resolution, managed env file (incl. aliases), rc source lines, mask_key
│   ├── httpcheck.py        # /models validation (stdlib urllib; no requests)
│   ├── logging_setup.py    # logging standard: package logger, setup_logging(), get_logger()
│   ├── reporting.py        # final summary table (statuses/files/key mode)
│   ├── merge.py            # generic deep_merge + JSON/TOML load-serialize helpers
│   └── adapters/
│       ├── __init__.py     # registry: name → adapter class; build_adapters()
│       ├── _base.py        # Adapter ABC (§7 contract), ProviderSelection, AdapterReport
│       ├── _yaml.py        # ruamel.yaml round-trip helpers (aider/goose)
│       ├── pi.py           # PiFamilyAdapter base + PiAdapter (.pi/agent)
│       ├── oh_my_pi.py     # OhMyPiAdapter (.omp/agent), same schema as pi
│       ├── codex.py        # config.toml + derived moat-models.json catalog
│       ├── opencode.py     # opencode.json, `provider` (singular)
│       ├── claude.py       # settings.json env block
│       ├── aider.py        # .aider.conf.yml (YAML round-trip)
│       ├── goose.py        # .config/goose/config.yaml (YAML round-trip)
│       └── cline.py        # .cline/data/settings/providers.json
└── tests/
    ├── conftest.py         # tmp home-base fixture, HTTP mocking (mock_validate), key-masking checks
    ├── fixtures/           # sample pre-existing configs per harness
    ├── test_providers.py   # spec-file loading/validation
    ├── test_merge.py       # deep-merge semantics
    ├── test_home.py        # home-base resolution, env/rc file handling
    ├── test_httpcheck.py   # endpoint validation
    ├── test_logging_setup.py  # logging standard: setup, traces, secret masking
    ├── test_cli.py         # end-to-end CLI runs, exit codes, dry-run, secret masking
    └── adapters/           # test_<name>.py — one per adapter
```

**Registry pattern:** each adapter module registers its adapter class in
`ADAPTERS` (keyed by harness name). `cli.py` contains **zero** harness-specific
branches: it resolves selections, iterates the registry, aggregates env aliases
(`collect_env_aliases`), and collects reports. Adding a harness = adding one
adapter module + its test + one registry entry. Enforce this: if you find
yourself writing `if harness == "codex"` outside `adapters/`, restructure.

## 7. Adapter contract

Every adapter subclasses `Adapter` (ABC in `_base.py`) and sets `name`/`label`:

- **Input:** home base path, ordered list of `ProviderSelection(profile, key)`, dry-run flag.
- **Logging:** each adapter logs at DEBUG level through its per-harness child
  logger (`self.log`; `dbg()` is a thin wrapper) — config paths, backups,
  chmods, unchanged/created/updated files, and the aggregate status.
- **Steps (mandatory, in order):**
  1. Resolve the harness's global config path(s) under home base.
  2. Load existing file(s) if present (tomlkit for TOML — round-trips comments;
     stdlib `json` for JSON; `adapters/_yaml.py` for YAML — round-trips comments).
  3. Merge: add/update **only** the moat provider entries, model list, and
     default-model selection. Preserve every unrelated setting, other providers,
     and approval/safety policies. If a moat-written entry already exists,
     update it in place — never duplicate.
  4. Key handling per §9 (env-ref or literal, decided per harness).
  5. Serialize and write (atomic tmp+rename via `_write_text`, which also
     detects no-change → reports "unchanged" and skips the write entirely).
     Before the **first** modification of a pre-existing file in a run, create
     a timestamped backup `<name>.bak-YYYYMMDD-HHMMSS` next to it (`_backup`).
  6. Re-read and parse the written file to prove validity (post-write parse
     check). Failure → failed report → exit code 4 path.
  7. Call `_finalize_status` to derive the aggregate status (and, when nothing
     was written, downgrade per-provider statuses to `no-change`).
- **Path rule (hard constraint):** never serialize the host-side absolute path
  of the home base into a harness config. Inside the container the base is
  mounted at the home path, so host paths are invalid there. Reference companion
  files relative to the config file (e.g. Codex's `model_catalog_json =
  "moat-models.json"`) or via `$HOME`-style references only.
- **Output:** an `AdapterReport` dataclass: harness name, aggregate `status`
  (created/updated/no-change/failed), files created/updated/unchanged, backup
  paths, `key_mode` (`env-ref`/`literal`/`mixed`), `parse_check` result,
  per-provider `provider_status`, warnings, manual-action notes, `error`.
- **Env aliases:** adapters may declare `env_aliases(default_profile)` — extra
  managed env-file exports (§9) for harnesses that read ecosystem-standard
  variables. The CLI aggregates them before validation so dry-run plans and
  writes stay consistent.

### Per-harness facts (all verified — sources in each adapter's docstring)

Each adapter's docstring documents its harness's config mechanism, the verified
findings (with file paths/sources), and how the configuration reaches the
harness at container runtime (which env vars must be present at launch, and
whether they come from the rc-sourced env file, passed-through host env, or
literal-key mode):

| Harness | Config path under base | Mechanism | Key mode |
|---|---|---|---|
| **pi** | `.pi/agent/models.json` | `providers.<id>` with `baseUrl`, `api`, `apiKey` (`$ENV` interpolation), optional `headers`, `models[]` (agent dir defaults to `~/.pi/agent` — note: *not* `agents`) | env-ref |
| **oh-my-pi** | `.omp/agent/models.json` | pi fork; identical schema at the omp agent dir | env-ref |
| **codex** | `.codex/config.toml` + derived `.codex/moat-models.json` | `[model_providers.<id>]` (`wire_api = "responses"` for both profiles), `env_key`/`env_http_headers`; catalog JSON **derived at write time** from the profile models and referenced by bare filename; conservative `approval_policy = "on-request"` only on create; `disable_response_storage = true` when azure is the default and the key is absent | env-ref |
| **opencode** | `.config/opencode/opencode.json` | `provider` (singular) map; `npm` package per API flavour; `options.baseURL` + `apiKey` as `{env:VAR}` (azure: `apiKey: ""` + `options.headers`); per-model `limit` blocks; top-level `model` = `<provider>/<model>` | env-ref |
| **claude** | `.claude/settings.json` | `env` block; settings env **overrides** process env, so a stale `ANTHROPIC_AUTH_TOKEN` is removed (with warning) when delivering the token via the env file; one active endpoint — only the default provider is written | env-ref (gateway) / **literal** (azure: `ANTHROPIC_CUSTOM_HEADERS`, file chmod 0600) |
| **aider** | `.aider.conf.yml` | top-level `model: openai/<model-id>` + `openai-api-base` (openai-compatible flow); YAML merged with ruamel round-trip; single endpoint — default provider only | env-ref (`OPENAI_API_KEY` alias); azure default → warning (APIM header not expressible via env) |
| **goose** | `.config/goose/config.yaml` | `active_provider: openai` + `providers.openai.{enabled, model, configured}` + `OPENAI_BASE_URL` (goose normalizes `/v1`-suffixed URLs); **keys never in config.yaml**; single active provider — default only | env-ref (`OPENAI_API_KEY` alias; azure also `OPENAI_CUSTOM_HEADERS` alias) |
| **cline** | `.cline/data/settings/providers.json` | `{version: 1, lastUsedProvider, providers: {<instance>: {settings, updatedAt, tokenSource}}}`; both selected providers written as distinct `moat-<name>` instances (user instances preserved); built-in `openai-compatible` provider resolves `OPENAI_API_KEY` when no literal `apiKey` is stored; `updatedAt` only refreshed when settings actually change (idempotency) | env-ref (gateway) / **literal** (azure `headers`, file chmod 0600) → `mixed` when both |

## 8. Validation, idempotency, safety

- **Endpoint validation:** for each selected provider, call `GET {base_url}/models`
  with the key **once per provider** (never per harness), using stdlib `urllib`
  (`httpcheck.check_provider`). On failure: report the error (including the
  generic reachability hint — some endpoints are only reachable from a
  specific network or via VPN), skip that provider's
  configs, exit code 3 — unless `--no-verify`, in which case proceed with a
  warning. Respect `--dry-run` (validation allowed; writes forbidden). The
  dry-run plan is printed **before** validation, so a failure never hides what
  was already resolved. Validation runs on the host; containers inherit host
  networking, so a passing check implies in-container reachability too
  (network reachability requirements are deployment-specific).
  *Known caveat:* the Azure APIM returns 404 for unknown/invalid-key requests
  (it hides routes), so `GET /v1/openai/models` may fail even with a valid key;
  the 404 error message explains this and points at `--no-verify`.
- **Idempotency:** running twice with the same inputs must not duplicate
  providers, models, rc source lines, env-file header lines, or backups.
  Unchanged files are detected by content comparison and not rewritten; the
  second run reports `no-change` everywhere and creates zero backups.
- **Safety defaults are semantic content:** preserve user approval/safety
  settings (e.g. Codex `approval_policy`) on merge; when creating files from
  scratch, mirror the conservative values (Codex `approval_policy =
  "on-request"` — read-only-ish on shared HPC).
- **Dry-run output** must enumerate: home base and per-harness
  would-create/would-update statuses; the masked env-file entries (including
  `<derived>` aliases) and the rc-file plan appear only when `--verbose` is
  also given.

## 9. Key storage (per-harness reality check)

Each adapter declares its key mode (see the table in §7):

- **env-ref** — the config references the env var **name only** (`$VAR`,
  `{env:VAR}`, `env_key`, or by omitting the key so the harness resolves its
  standard variable at runtime); the key value is persisted exactly once in
  the managed env file.
- **literal** — the harness cannot use env indirection for that auth style
  (claude azure header, cline azure header): the adapter writes the literal
  key, chmods the file 0600, and the report/summary flags the file as
  containing a plaintext secret (`mixed` when a harness uses both modes).

**Managed env file:** `<base>/.config/moat-install/env.sh` with `export
VAR=value` lines for each selected provider's key, plus **derived aliases** —
double-quoted exports referencing managed variables, e.g.
`export OPENAI_API_KEY="$LLM_GATEWAY_KEY"` — for harnesses that resolve
ecosystem-standard variables (aider/goose/cline read `OPENAI_API_KEY`; goose
with a header-auth default also needs
`OPENAI_CUSTOM_HEADERS="Ocp-Apim-Subscription-Key=$AZURE_APIM_KEY"`).
Aliases expand at source time, so each key value is still persisted exactly
once. (Deliberately *not* under `.config/moat/` — that directory name belongs
to the container runner's own config and must not be polluted.) Unknown
pre-existing lines in the env file are preserved verbatim; the file is created
with mode 0600.

Rc integration: append an idempotently-guarded block to `.bashrc` and `.zshrc`
under home base (create if absent) — at container runtime these are the `$HOME`
startup files (§1):

```
# >>> moat-install >>>
. "$HOME/.config/moat-install/env.sh"
# <<< moat-install <<<
```

The `$HOME`-relative source line is correct in both contexts: on the host and
inside a container where the base is mounted as home.

**Key delivery into containers** (documented in each adapter's docstring and
the README, not printed to users;
don't overpromise):

1. *Startup files:* if the harness is launched via a shell inside the container,
   the rc block above exports the keys. Not guaranteed for harnesses exec'd
   directly without a sourcing shell — each adapter's docstring documents the
   reality for its harness.
2. *Passed-through host env:* the runner passes host environment variables into
   containers by default, so keys exported on the host also arrive in-container.
3. *Literal keys:* adapters in literal-key mode are immune to both caveats — one
   reason literal mode exists.

Check for the guard markers before appending. Keys enter this file from env vars
or the hidden prompt (§3); they are never echoed back.

## 10. Packaging & dependencies

The project is a proper installable package so end users can install it with
**pipx**:

- `pyproject.toml` carries full project metadata: `name`, `version`,
  `requires-python = ">=3.9"`, `dependencies = ["tomlkit>=0.11",
  "ruamel.yaml>=0.17"]` (loose pins), and `[project.scripts]` per §3. Build
  backend: hatchling (auto-discovers the `moat_install` package; no manifests
  to maintain).
- User installation: `pipx install <path-or-git-url>` — pipx creates an isolated
  venv, resolves the dependencies automatically, and puts `moat-install` on
  PATH. Verify the packaging during development with `uv tool install .` (or
  `pipx install .` / `pip install -e .` in a dev venv) followed by
  `moat-install --help`.
- Development dependencies live in the `dev` extra (`pytest`, `ruff`,
  `pyinstaller`): `pip install -e .[dev]` (or `uv pip install -e .[dev]`).
- Distribution for machines without pip/pipx: freeze a single-file executable
  with PyInstaller (`pyinstaller --onefile --name moat-install
  moat_install/__main__.py`; build on the target platform). The generated
  `*.spec` file and `build/`/`dist/` outputs are gitignored — always build via
  CLI flags. The provider specification is not bundled; it is passed at
  runtime.
- Runtime import guard: declared dependencies are resolved at install time, so
  they are normally guaranteed; keep the startup import check anyway as a
  safety net for source checkouts. If `tomlkit` or `ruamel.yaml` is missing,
  print a clear warning with the exact remedy (e.g. `pipx reinstall
  moat-install` / `pip install tomlkit ruamel.yaml`) and **exit with code 2**.
- No other third-party runtime deps (HTTP via stdlib `urllib`; YAML via
  `ruamel.yaml` because aider and goose need comment-preserving merges —
  justified in `adapters/_yaml.py` and both adapters' docstrings).

## 11. Coding standards & process rules

- **Type hints:** PEP 484 style throughout, including tests. `pyright` runs in
  pre-commit (configured in `pyproject.toml` with `venvPath`/`venv`); code must
  type-check cleanly.
- **Docstrings:** PEP 257 style on every module, class, and public function;
  each adapter's docstring documents its harness's config mechanism, the
  verified findings (with sources), and the container-runtime key-delivery
  reality.
- **Lint/format/secrets:** `ruff` (lint + format) plus pyright run via
  `pre-commit`, together with the base `pre-commit-hooks` (check-yaml,
  end-of-file-fixer, trailing-whitespace) and `detect-secrets` with the tracked
  baseline `.secrets.baseline` (refresh it with `detect-secrets scan --baseline
  .secrets.baseline` when secret-shaped strings are added intentionally).
  `pyproject.toml` carries the ruff/pytest/pyright config. Code must pass
  `pre-commit run --all-files` (ruff check, ruff format, pyright, hooks above).
- **Logging:** all debugging output goes through Python's `logging` module —
  never `print()` to stderr. Each module creates its logger once with
  `get_logger(__name__)` from `moat_install.logging_setup`; adapters get a
  per-harness child logger (`self.log`, e.g. `moat_install.adapters.pi`).
  `cli.run()` calls `setup_logging(args.debug)` once per run: `--debug` sets
  the `moat_install` logger to DEBUG (records prefixed `[debug] ` on stderr);
  without it the level is WARNING and debug records are dropped. File
  creation, backups, chmods, HTTP connections (method/URL/timeout/auth style —
  never headers), env/rc writes, and merge decisions are all DEBUG-traced.
  Secrets must never appear in log messages; `moat_install.home.mask_key` is
  the single masking helper, guarded by tests (`test_debug_masks_secrets`,
  `tests/test_logging_setup.py`).
- **Tests:** `pytest`, no network (mock `httpcheck` via the `mock_validate`
  fixture), no writes outside `tmp_path`:
  - `test_merge.py`: merge preserves unrelated keys/other providers; idempotent re-merge; moat entry updated in place.
  - Per-adapter (`tests/adapters/test_<name>.py`): fixture of a pre-existing config → assert merged output parses, preserves comments/unrelated settings, and contains expected provider/models/default; fresh home → created-from-scratch output matches the expected shape; idempotent second run → `no-change`, zero backups; dry-run → no writes.
  - `test_home.py`: home-base resolution precedence; env-file + rc guard idempotency; alias references (no duplicated secret values).
  - `test_httpcheck.py`: auth header construction; urllib mocking; error masking.
  - `test_cli.py`: flag parsing, validation errors, key-from-env vs prompt (mock `getpass`), startup banner + prompt skip hint, non-TTY without env var → error, empty prompt answer → provider skipped (next configured provider promoted to default; all skipped → nothing written, exit 0), exit codes (incl. 2 and 4), `--dry-run` writes nothing (fs snapshot), end-to-end full run into a temp home base with mocked HTTP → assert every file, the env file (incl. aliases), rc lines, the summary, and an idempotent re-run.
  - Secret-masking checks: `assert_no_secret_outside_env_file` (with an `exempt` list for deliberately-literal files such as cline's azure header).
- **Git discipline:** never `git commit` automatically. Make changes in the
  working tree and let the user review and commit.
- **Research etiquette:** before verifying any new external fact, ask the user
  whether they want to provide a link/reference. Do not web crawl or browse
  without explicit permission. If no link is provided and the fact cannot be
  verified offline (installed binaries/docs count as offline-verifiable), treat
  it as a research dead-end.
- **Stop-and-ask triggers:** a harness's config mechanism cannot be verified,
  a merge would need to destroy unrelated user data, or a requested behavior
  conflicts with this document.
- **Final summary output** (normal runs): the resolved home base is announced
  once, before the API-key prompts; *default* is then the brief summary —
  providers (default marked), one line per harness naming the
  providers actually configured for it and the aggregate status
  (created/updated/no-change/failed, `WOULD `-prefixed in dry-run; failures
  inline with the error), then the env-file line (status, path, and the names
  of the env vars it sets — values are never printed) and the rc-file lines
  (`.bashrc`/`.zshrc` created or modified in the home base), and a
  suppressed-count hint. `--verbose` prints the full table instead — one row
  per harness × provider with files, key mode, backup paths, parse-check
  result — plus the Warnings and Manual actions sections. No containerization
  details are printed.

## 12. Adding a harness or provider

1. **New harness:** add `moat_install/adapters/<name>.py` (subclass `Adapter`,
   set `name`, implement `run()`, declare `env_aliases` if it reads
   ecosystem-standard variables), document the verified config mechanism in the
   module docstring, register it in `adapters/__init__.py`, add
   `tests/adapters/test_<name>.py` plus a pre-existing-config fixture, and
   extend the table in §7 and the README.
2. **New provider:** no code change — add an entry to the provider
   specification file (§5). Only if a provider needs *new kinds* of metadata
   do you extend the `ProviderProfile`/`ModelInfo` dataclasses and the
   schema validation in `providers.py` (strict: new keys must be added to the
   allowed-key sets), plus a `test_providers.py` case. Adapters must never
   hard-code provider facts.
3. **Changing a config format:** re-verify the mechanism (per the research
   etiquette above), update the adapter + its fixture + tests, and keep the
   merge non-destructive (§7).
