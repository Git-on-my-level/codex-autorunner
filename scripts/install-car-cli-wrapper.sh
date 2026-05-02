#!/usr/bin/env bash
set -euo pipefail

# Install the user-facing `car` command as a dispatcher to the active CAR hub venv.
#
# Overrides:
#   CURRENT_VENV_LINK  Symlink to the active hub venv
#                      (default: ~/.local/pipx/venvs/codex-autorunner.current)
#   LOCAL_BIN          Directory that should contain the `car` wrapper
#                      (default: ~/.local/bin)

CURRENT_VENV_LINK="${CURRENT_VENV_LINK:-$HOME/.local/pipx/venvs/codex-autorunner.current}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
CAR_WRAPPER_PATH="${CAR_WRAPPER_PATH:-$LOCAL_BIN/car}"

fail() {
  echo "$1" >&2
  exit 1
}

mkdir -p "$(dirname "${CAR_WRAPPER_PATH}")"

cat > "${CAR_WRAPPER_PATH}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

# Installed hub venv python: ${CURRENT_VENV_LINK}/bin/python
current_venv_link="\${CAR_CURRENT_VENV_LINK:-${CURRENT_VENV_LINK}}"
python_bin="\${current_venv_link}/bin/python"

if [[ ! -L "\${current_venv_link}" ]]; then
  echo "car: active codex-autorunner venv symlink is missing: \${current_venv_link}" >&2
  echo "car: run the hub refresh script to recreate it, or set CAR_CURRENT_VENV_LINK." >&2
  exit 127
fi

if [[ ! -x "\${python_bin}" ]]; then
  echo "car: active codex-autorunner Python is missing or not executable: \${python_bin}" >&2
  echo "car: current symlink target: \$(readlink "\${current_venv_link}" 2>/dev/null || echo unknown)" >&2
  exit 127
fi

exec "\${python_bin}" -m codex_autorunner.cli "\$@"
EOF

chmod 0755 "${CAR_WRAPPER_PATH}"

if ! grep -Fq "${CURRENT_VENV_LINK}/bin/python" "${CAR_WRAPPER_PATH}"; then
  fail "car wrapper verification failed: ${CAR_WRAPPER_PATH} does not dispatch through ${CURRENT_VENV_LINK}/bin/python"
fi

echo "Installed car wrapper at ${CAR_WRAPPER_PATH} -> ${CURRENT_VENV_LINK}/bin/python"
