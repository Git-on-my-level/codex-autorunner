# MCP2 Agent Config

Source of truth untuk konfigurasi credential MCP2 agent.

## Files

- `credentials.template.json`: template konfigurasi.
- `connectors.map.json`: mapping integration ke MCP2 tool + kebutuhan stage SDLC.
- `validate-credentials.cjs`: validator config credential.
- `sdlc-orchestrator.cjs`: skeleton orchestrator SDLC.

## Usage

1. Copy `credentials.template.json` jadi `credentials.local.json`.
2. Isi `credential_fields` sesuai integrasi.
3. Set `status` ke `active` jika sudah valid.
4. Simpan `credentials.local.json` hanya di environment private.
5. Jalankan validator sebelum dipakai agent.

## Validation

```bash
cd /Users/sproutoffice/apps/codex-autorunner/mcp2-agent-config
node validate-credentials.cjs credentials.local.json
```

Untuk cek template:

```bash
node validate-credentials.cjs credentials.template.json
```

Strict mode (recommended for CI/CD, warning dianggap error):

```bash
node validate-credentials.cjs credentials.local.json --strict
```

## Agent Contract

- Agent baca file JSON dari path konsisten:
  - `/Users/sproutoffice/apps/codex-autorunner/mcp2-agent-config/credentials.local.json`
- Agent wajib menganggap field kosong sebagai `not configured`.
- Agent hanya pakai connector dengan `status = active`.
- Gunakan `schema_version` untuk menjaga kompatibilitas format.

## Run Orchestrator

```bash
cd /Users/sproutoffice/apps/codex-autorunner/mcp2-agent-config
node sdlc-orchestrator.cjs \
  --credentials credentials.local.json \
  --connectors connectors.map.json \
  --out artifacts \
  --project your-project-name \
  --strict
```

Output:
- `artifacts/pipeline-state.json`
- `artifacts/<stage>/contract.json`
- `artifacts/<stage>/artifact.md`

## Security

- Jangan commit `credentials.local.json`.
- Rotasi credential sesuai `meta.rotation_policy_days`.
- Scope akses harus minimal.
