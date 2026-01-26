#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-.codex-autorunner}"

if [ ! -d "$ROOT" ]; then
  echo "Missing CAR artifact directory: $ROOT" >&2
  exit 1
fi

shopt -s nullglob

human_total() {
  local total
  total=$(du -ch "$@" | tail -n 1 | awk '{print $1}')
  printf "%s" "$total"
}

print_group() {
  local label="$1"
  shift
  local paths=("$@")

  if [ ${#paths[@]} -eq 0 ]; then
    printf "%-24s %s\n" "$label" "0B"
    return
  fi

  printf "%-24s %s\n" "$label" "$(human_total "${paths[@]}")"
}

add_if_exists() {
  local out_array="$1"
  local path="$2"
  if [ -e "$path" ]; then
    eval "$out_array+=(\"$path\")"
  fi
}

echo "CAR artifact sizes under $ROOT"
print_group "Total" "$ROOT"

echo
echo "Buckets"

main_logs=("$ROOT"/codex-autorunner.log*)
print_group "Main logs" "${main_logs[@]}"

# Use a temporary variable to check for existence before accessing the array
# This avoids "unbound variable" errors under set -u when no matches are found.
run_logs_glob="$ROOT/runs/*.log"
run_logs=($run_logs_glob)
if [ "${run_logs[0]}" != "$run_logs_glob" ]; then
  print_group "Run logs" "${run_logs[@]}"
else
  printf "%-24s %s\n" "Run logs" "0B"
fi

uploads=("$ROOT"/uploads)
if [ -d "$uploads" ]; then
  print_group "Uploads" "${uploads[@]}"
else
  printf "%-24s %s\n" "Uploads" "0B"
fi

static_cache=("$ROOT"/static-cache)
print_group "Static cache" "${static_cache[@]}"

server_logs=("$ROOT"/codex-server.log)
print_group "Server log" "${server_logs[@]}"

context_cache=("$ROOT"/github_context)
if [ -d "$context_cache" ]; then
  print_group "GitHub context" "${context_cache[@]}"
else
  printf "%-24s %s\n" "GitHub context" "0B"
fi

work_docs=()
add_if_exists work_docs "$ROOT/workspace/active_context.md"
add_if_exists work_docs "$ROOT/workspace/decisions.md"
add_if_exists work_docs "$ROOT/workspace/spec.md"
add_if_exists work_docs "$ROOT/ABOUT_CAR.md"
add_if_exists work_docs "$ROOT/config.yml"
add_if_exists work_docs "$ROOT/state.sqlite3"
add_if_exists work_docs "$ROOT/telegram_state.sqlite3"
add_if_exists work_docs "$ROOT/github.json"
add_if_exists work_docs "$ROOT/tickets"
print_group "Work docs/state" "${work_docs[@]}"

echo
echo "Largest files"
find "$ROOT" -type f -print0 \
  | xargs -0 du -h \
  | sort -h \
  | tail -n 8

echo
echo "Housekeeping"

config_path="$ROOT/config.yml"
python_bin=""
if [ -x ".venv/bin/python" ]; then
  python_bin=".venv/bin/python"
fi

if [ -n "$python_bin" ]; then
  if hk_output=$(
    "$python_bin" - <<'PY' 2>/dev/null
from pathlib import Path

try:
    from codex_autorunner.core.config import load_config
except Exception:
    raise SystemExit(1)

cfg = load_config(Path("."))
hk = cfg.housekeeping
print(f"enabled={hk.enabled}")
print(f"interval_seconds={hk.interval_seconds}")
print(f"min_file_age_seconds={hk.min_file_age_seconds}")
print(f"dry_run={hk.dry_run}")
print(f"rule_count={len(hk.rules)}")
PY
  ); then
    hk_enabled=$(printf "%s\n" "$hk_output" | awk -F= '$1=="enabled"{print $2}')
    hk_interval=$(printf "%s\n" "$hk_output" | awk -F= '$1=="interval_seconds"{print $2}')
    hk_min_age=$(printf "%s\n" "$hk_output" | awk -F= '$1=="min_file_age_seconds"{print $2}')
    hk_dry_run=$(printf "%s\n" "$hk_output" | awk -F= '$1=="dry_run"{print $2}')
    hk_rule_count=$(printf "%s\n" "$hk_output" | awk -F= '$1=="rule_count"{print $2}')

    printf "%-24s %s\n" "Enabled" "${hk_enabled:-unknown}"
    printf "%-24s %s\n" "Interval seconds" "${hk_interval:-unknown}"
    printf "%-24s %s\n" "Min file age seconds" "${hk_min_age:-unknown}"
    printf "%-24s %s\n" "Dry run" "${hk_dry_run:-unknown}"
    printf "%-24s %s\n" "Rule count" "${hk_rule_count:-0}"
    printf "%-24s %s\n" "Config source" "$config_path"
    exit 0
  fi
fi

if [ -f "$config_path" ]; then
  hk_enabled=$(awk 'found && $1=="enabled:" {print $2; exit} $1=="housekeeping:" {found=1}' "$config_path")
  hk_interval=$(awk 'found && $1=="interval_seconds:" {print $2; exit} $1=="housekeeping:" {found=1}' "$config_path")
  hk_min_age=$(awk 'found && $1=="min_file_age_seconds:" {print $2; exit} $1=="housekeeping:" {found=1}' "$config_path")
  hk_dry_run=$(awk 'found && $1=="dry_run:" {print $2; exit} $1=="housekeeping:" {found=1}' "$config_path")
  hk_rule_count=$(awk '
    $1=="housekeeping:" {in_hk=1}
    in_hk && $1=="rules:" {in_rules=1; next}
    in_hk && in_rules && $1=="name:" {count+=1}
    in_hk && in_rules && $1!~/^name:/ && $1!~/:/ && NF==0 {in_rules=0}
    END {print count+0}
  ' "$config_path")

  printf "%-24s %s\n" "Enabled" "${hk_enabled:-unknown}"
  printf "%-24s %s\n" "Interval seconds" "${hk_interval:-unknown}"
  printf "%-24s %s\n" "Min file age seconds" "${hk_min_age:-unknown}"
  printf "%-24s %s\n" "Dry run" "${hk_dry_run:-unknown}"
  printf "%-24s %s\n" "Rule count" "${hk_rule_count:-0}"
  printf "%-24s %s\n" "Config source" "$config_path"
else
  echo "Config missing: $config_path"
fi
