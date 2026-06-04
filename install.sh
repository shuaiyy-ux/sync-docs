#!/usr/bin/env bash
# sync-docs installer — wires this repo into Codex as the sync-docs,
# kb-integrate, and kb-search skills, substituting absolute paths.
#
# Re-run after `git pull` to refresh installed skill files.

set -euo pipefail

SKILL_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_SKILLS="${HOME}/.codex/skills"

# --- KB_HOME selection -------------------------------------------------------
# Default: ~/.claude-knowledge (hidden, conventional)
# Override: export KB_HOME=/some/path before running
# Honor existing KB at ~/Downloads/claude-knowledge (legacy location) so
# upgrading users don't lose data.
if [ -n "${KB_HOME:-}" ]; then
    : # honor user-set env var
elif [ -d "${HOME}/Downloads/claude-knowledge" ]; then
    KB_HOME="${HOME}/Downloads/claude-knowledge"
    echo "[install] Detected existing KB at ${KB_HOME} — keeping it."
else
    KB_HOME="${HOME}/.claude-knowledge"
fi

# --- Dependency checks -------------------------------------------------------
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")

if [ ${#missing[@]} -ne 0 ]; then
    echo "[install] Missing dependencies:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 1
fi

mkdir -p "${CODEX_SKILLS}" "${KB_HOME}"

# --- Bootstrap embedding venv (so first /sync-docs run doesn't fail) ----------
VENV="${KB_HOME}/.venv"
if [ ! -x "${VENV}/bin/python" ]; then
    echo "[install] Creating embedding venv at ${VENV} ..."
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet sentence-transformers numpy
    echo "[install] venv ready"
fi

# --- Substitute placeholders in skill markdown -------------------------------
install_skill() {
    local src="$1"
    local name="$2"
    local dest_dir="${CODEX_SKILLS}/${name}"
    local dest="${dest_dir}/SKILL.md"
    if [ ! -f "${src}" ]; then
        echo "[install] Source missing: ${src}" >&2
        exit 1
    fi
    mkdir -p "${dest_dir}"
    # Use a sentinel-safe sed (handles slashes in paths via | delimiter)
    sed -e "s|@SKILL_HOME@|${SKILL_HOME}|g" \
        -e "s|@KB_HOME@|${KB_HOME}|g" \
        "${src}" > "${dest}.tmp"
    mv "${dest}.tmp" "${dest}"
    echo "[install] Wrote ${dest}"
}

# Remove pre-existing symlinks/files at the target so users coming from the
# symlink era end up with the substituted version, not a dangling link.
for skill in sync-docs kb-integrate kb-search; do
    target="${CODEX_SKILLS}/${skill}/SKILL.md"
    [ -e "${target}" ] || [ -L "${target}" ] && rm -f "${target}"
    install_skill "${SKILL_HOME}/${skill}.md" "${skill}"
done

chmod +x "${SKILL_HOME}/scripts/"*.py 2>/dev/null || true

cat <<EOF

[install] Done.
  SKILL_HOME = ${SKILL_HOME}
  KB_HOME    = ${KB_HOME}
  Skills installed at ${CODEX_SKILLS}

Try the sync-docs skill in Codex. To change KB_HOME later:
  KB_HOME=/new/path ./install.sh
EOF
