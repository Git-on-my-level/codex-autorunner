#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/smoke_destination_docker.sh --hub-root <path> --repo-id <repo_id> [options]

Options:
  --hub-root <path>     Hub root containing .codex-autorunner/manifest.yml
  --repo-id <repo_id>   Repo id from hub manifest
  --image <image>       Docker image to set (default: busybox:latest)
  --execute             Execute commands (default is dry-run)
  --help                Show this help

Behavior:
  - Safe by default: prints planned commands only.
  - With --execute:
    1) Set repo destination to docker
    2) Show effective destination
    3) Run a minimal ticket_flow bootstrap + start + status
    4) Capture evidence under:
       <hub_root>/.codex-autorunner/runs/destination-smoke-<timestamp>/
EOF
}

HUB_ROOT=""
REPO_ID=""
DOCKER_IMAGE="busybox:latest"
EXECUTE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hub-root)
      HUB_ROOT="${2:-}"
      shift 2
      ;;
    --repo-id)
      REPO_ID="${2:-}"
      shift 2
      ;;
    --image)
      DOCKER_IMAGE="${2:-}"
      shift 2
      ;;
    --execute)
      EXECUTE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$HUB_ROOT" || -z "$REPO_ID" ]]; then
  echo "Both --hub-root and --repo-id are required." >&2
  usage >&2
  exit 2
fi

if [[ "$DOCKER_IMAGE" == "" ]]; then
  echo "--image cannot be empty." >&2
  exit 2
fi

HUB_ROOT="$(cd "$HUB_ROOT" && pwd -P)"
MANIFEST_PATH="$HUB_ROOT/.codex-autorunner/manifest.yml"
if [[ ! -f "$MANIFEST_PATH" ]]; then
  echo "Manifest not found: $MANIFEST_PATH" >&2
  exit 1
fi

CAR_CMD=()
if command -v car >/dev/null 2>&1; then
  CAR_CMD=(car)
elif [[ -x ".venv/bin/python" ]]; then
  CAR_CMD=(".venv/bin/python" "-m" "codex_autorunner.cli")
elif command -v python3 >/dev/null 2>&1; then
  CAR_CMD=("python3" "-m" "codex_autorunner.cli")
else
  echo "Could not resolve CAR CLI command. Install 'car' or use a repo with .venv." >&2
  exit 1
fi

run_cmd() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$EXECUTE" -eq 1 ]]; then
    "$@"
  fi
}

run_capture() {
  local out_file="$1"
  shift
  printf '+'
  printf ' %q' "$@"
  printf ' > %q\n' "$out_file"
  if [[ "$EXECUTE" -eq 1 ]]; then
    "$@" >"$out_file"
  fi
}

run_capture_allow_fail() {
  local out_file="$1"
  local err_file="$2"
  local code_file="$3"
  shift 3
  printf '+'
  printf ' %q' "$@"
  printf ' > %q 2> %q (capture exit -> %q)\n' "$out_file" "$err_file" "$code_file"
  if [[ "$EXECUTE" -eq 1 ]]; then
    set +e
    "$@" >"$out_file" 2>"$err_file"
    local cmd_rc=$?
    set -e
    printf '%s\n' "$cmd_rc" >"$code_file"
    return "$cmd_rc"
  fi
}

repo_path_from_manifest() {
  local python_bin
  if [[ -x ".venv/bin/python" ]]; then
    python_bin=".venv/bin/python"
  else
    python_bin="python3"
  fi
  "$python_bin" - "$HUB_ROOT" "$REPO_ID" <<'PY'
from pathlib import Path
import sys
import yaml

hub_root = Path(sys.argv[1]).resolve()
repo_id = sys.argv[2]
manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
for repo in payload.get("repos", []):
    if isinstance(repo, dict) and repo.get("id") == repo_id:
        rel_path = repo.get("path")
        if isinstance(rel_path, str) and rel_path.strip():
            print((hub_root / rel_path).resolve())
            raise SystemExit(0)
raise SystemExit(f"Repo id not found in manifest: {repo_id}")
PY
}

if [[ "$EXECUTE" -eq 1 ]]; then
  REPO_PATH="$(repo_path_from_manifest)"
else
  REPO_PATH="<repo_path_from_manifest>"
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="$HUB_ROOT/.codex-autorunner/runs/destination-smoke-$TIMESTAMP"
RUN_START_LOG="$EVIDENCE_DIR/ticket_flow_start.txt"

echo "Hub root:   $HUB_ROOT"
echo "Repo id:    $REPO_ID"
echo "Repo path:  $REPO_PATH"
echo "Image:      $DOCKER_IMAGE"
echo "Execute:    $EXECUTE"
echo "CAR cmd:    ${CAR_CMD[*]}"
echo "Evidence:   $EVIDENCE_DIR"
echo

run_cmd "${CAR_CMD[@]}" hub destination set "$REPO_ID" docker \
  --image "$DOCKER_IMAGE" \
  --env "CAR_*" \
  --json \
  --path "$HUB_ROOT"

run_cmd "${CAR_CMD[@]}" hub destination show "$REPO_ID" --json --path "$HUB_ROOT"

run_cmd "${CAR_CMD[@]}" flow ticket_flow bootstrap --repo "$REPO_PATH" --hub "$HUB_ROOT"

if [[ "$EXECUTE" -eq 1 ]]; then
  mkdir -p "$EVIDENCE_DIR"
  printf '+'
  printf ' %q' "${CAR_CMD[@]}" flow ticket_flow start --repo "$REPO_PATH" --hub "$HUB_ROOT" --force-new
  printf '\n'
  "${CAR_CMD[@]}" flow ticket_flow start --repo "$REPO_PATH" --hub "$HUB_ROOT" --force-new \
    | tee "$RUN_START_LOG"
else
  run_cmd "${CAR_CMD[@]}" flow ticket_flow start --repo "$REPO_PATH" --hub "$HUB_ROOT" --force-new
fi

RUN_ID=""
if [[ "$EXECUTE" -eq 1 ]]; then
  RUN_ID="$(sed -n 's/^Started ticket_flow run: //p' "$RUN_START_LOG" | tail -n 1)"
  STATUS_STDOUT="$EVIDENCE_DIR/ticket_flow_status_initial.stdout"
  STATUS_STDERR="$EVIDENCE_DIR/ticket_flow_status_initial.stderr"
  STATUS_CODE="$EVIDENCE_DIR/ticket_flow_status_initial.exit_code"
  STATUS_RC=0
  if [[ -n "$RUN_ID" ]]; then
    run_capture_allow_fail \
      "$STATUS_STDOUT" \
      "$STATUS_STDERR" \
      "$STATUS_CODE" \
      "${CAR_CMD[@]}" flow ticket_flow status \
      --repo "$REPO_PATH" \
      --hub "$HUB_ROOT" \
      --run-id "$RUN_ID" \
      --json || STATUS_RC=$?
  else
    run_capture_allow_fail \
      "$STATUS_STDOUT" \
      "$STATUS_STDERR" \
      "$STATUS_CODE" \
      "${CAR_CMD[@]}" flow ticket_flow status \
      --repo "$REPO_PATH" \
      --hub "$HUB_ROOT" \
      --json || STATUS_RC=$?
  fi
  if [[ -s "$STATUS_STDOUT" ]]; then
    cat "$STATUS_STDOUT"
  fi
  if [[ "$STATUS_RC" -ne 0 ]]; then
    echo "Warning: ticket_flow status failed (exit=$STATUS_RC). Evidence captured at:"
    echo "  $STATUS_STDOUT"
    echo "  $STATUS_STDERR"
    echo "  $STATUS_CODE"
  fi

  run_cmd sh -c "find '$REPO_PATH/.codex-autorunner/runs' -maxdepth 3 -type f | sort > '$EVIDENCE_DIR/run_artifacts.txt'"
  if [[ -f "$REPO_PATH/.codex-autorunner/codex-autorunner.log" ]]; then
    run_cmd sh -c "tail -n 200 '$REPO_PATH/.codex-autorunner/codex-autorunner.log' > '$EVIDENCE_DIR/repo_log_tail.txt'"
  fi
  if [[ -f "$HUB_ROOT/.codex-autorunner/codex-autorunner-hub.log" ]]; then
    run_cmd sh -c "tail -n 200 '$HUB_ROOT/.codex-autorunner/codex-autorunner-hub.log' > '$EVIDENCE_DIR/hub_log_tail.txt'"
  fi

  run_capture "$EVIDENCE_DIR/destination_effective.json" \
    "${CAR_CMD[@]}" hub destination show "$REPO_ID" --json --path "$HUB_ROOT"
  STATUS_FINAL_STDOUT="$EVIDENCE_DIR/ticket_flow_status.json"
  STATUS_FINAL_STDERR="$EVIDENCE_DIR/ticket_flow_status.stderr"
  STATUS_FINAL_CODE="$EVIDENCE_DIR/ticket_flow_status.exit_code"
  STATUS_FINAL_RC=0
  if [[ -n "$RUN_ID" ]]; then
    run_capture_allow_fail \
      "$STATUS_FINAL_STDOUT" \
      "$STATUS_FINAL_STDERR" \
      "$STATUS_FINAL_CODE" \
      "${CAR_CMD[@]}" flow ticket_flow status \
      --repo "$REPO_PATH" \
      --hub "$HUB_ROOT" \
      --run-id "$RUN_ID" \
      --json || STATUS_FINAL_RC=$?
  else
    run_capture_allow_fail \
      "$STATUS_FINAL_STDOUT" \
      "$STATUS_FINAL_STDERR" \
      "$STATUS_FINAL_CODE" \
      "${CAR_CMD[@]}" flow ticket_flow status \
      --repo "$REPO_PATH" \
      --hub "$HUB_ROOT" \
      --json || STATUS_FINAL_RC=$?
  fi
  if [[ "$STATUS_FINAL_RC" -ne 0 ]]; then
    echo "Warning: final status capture failed (exit=$STATUS_FINAL_RC)."
  fi
fi

if [[ "$EXECUTE" -eq 0 ]]; then
  cat <<EOF

Dry-run complete.
No commands were executed.

To execute:
  scripts/smoke_destination_docker.sh --hub-root "$HUB_ROOT" --repo-id "$REPO_ID" --image "$DOCKER_IMAGE" --execute
EOF
else
  echo
  echo "Smoke run complete."
  echo "Evidence written to: $EVIDENCE_DIR"
fi
