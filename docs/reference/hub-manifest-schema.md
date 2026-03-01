# Hub Manifest Destination Schema

Canonical reference for `repos[].destination` in hub manifests.

- Manifest path: `<hub_root>/.codex-autorunner/manifest.yml`
- Entry path: `repos[].destination`

## Destination Shape

`destination` is an object with a required `kind`:

- `kind: local`
  - No additional fields are required.
- `kind: docker`
  - Required: `image`
  - Optional: `container_name`, `profile`, `workdir`, `env_passthrough`, `env`, `mounts`

Docker field details:

- `profile`: optional string; only `full-dev` is supported.
- `workdir`: optional string path inside the container.
- `env_passthrough`: optional list of env wildcard patterns (example: `CAR_*`).
- `env`: optional explicit map of string keys to string values.
- `mounts`: optional list of objects with:
  - required `source` (host path) and `target` (container path)
  - optional `read_only` boolean (manifest canonical key)

Compatibility aliases accepted on API write:

- `mounts[].readOnly` and `mounts[].readonly` are normalized to `mounts[].read_only`.
- `envPassthrough` and `explicitEnv`/`explicit_env` aliases are normalized to canonical keys.

## `full-dev` Profile Contract

`full-dev` is the only supported profile and contributes defaults/preflight checks:

- Required binaries: `codex`, `opencode`, `python3`, `git`, `rg`, `bash`, `node`, `pnpm`
- Required auth files:
  - `${HOME}/.codex/auth.json`
  - `${HOME}/.local/share/opencode/auth.json`
- Default passthrough env:
  - `CAR_*`
  - `OPENAI_API_KEY`
  - `CODEX_HOME`
  - `OPENCODE_SERVER_USERNAME`
  - `OPENCODE_SERVER_PASSWORD`
- Default mounts:
  - `${HOME}/.codex` -> `${HOME}/.codex`
  - `${HOME}/.local/share/opencode` -> `${HOME}/.local/share/opencode`

## Contract Block (Drift Check Input)

This block is consumed by `scripts/check_destination_contract_drift.py`.

<!-- CAR_DESTINATION_CONTRACT:BEGIN -->
```yaml
supported_destination_kinds:
  - local
  - docker
docker:
  required_fields:
    - kind
    - image
  optional_fields:
    - container_name
    - mounts
    - env_passthrough
    - workdir
    - profile
    - env
  mount:
    required_fields:
      - source
      - target
    optional_fields:
      - read_only
    read_only_type: boolean
  env:
    key_type: string
    value_type: string
  profiles:
    supported:
      - full-dev
profiles:
  full-dev:
    required_binaries:
      - codex
      - opencode
      - python3
      - git
      - rg
      - bash
      - node
      - pnpm
    required_auth_files:
      - ${HOME}/.codex/auth.json
      - ${HOME}/.local/share/opencode/auth.json
    default_env_passthrough:
      - CAR_*
      - OPENAI_API_KEY
      - CODEX_HOME
      - OPENCODE_SERVER_USERNAME
      - OPENCODE_SERVER_PASSWORD
    default_mounts:
      - source: ${HOME}/.codex
        target: ${HOME}/.codex
        read_only: false
      - source: ${HOME}/.local/share/opencode
        target: ${HOME}/.local/share/opencode
        read_only: false
```
<!-- CAR_DESTINATION_CONTRACT:END -->
