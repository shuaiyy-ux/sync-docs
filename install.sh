#!/usr/bin/env bash
# sync-docs installer — wires this repo into Claude Code as the sync-docs,
# kb-integrate, and kb-search skills, substituting absolute paths.
#
# Re-run after `git pull` to refresh installed skill files.

set -euo pipefail

SKILL_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Install target: claude (default, Claude-native) or codex (legacy).
# Override: SKILL_TARGET=codex ./install.sh
SKILL_TARGET="${SKILL_TARGET:-claude}"
SKILLS_DIR="${HOME}/.${SKILL_TARGET}/skills"

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

mkdir -p "${SKILLS_DIR}" "${KB_HOME}"

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
    local dest_dir="${SKILLS_DIR}/${name}"
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

# 2026-09-09: 三个 skill(sync-docs / kb-search / kb-integrate)合并成一个 `kb`。
# 一个子系统开三扇门,每扇都得贴「此门非彼门」,模型会选错。现在只有一扇门,
# 模式在读完 SKILL.md 之后选。三份正文原样保留,装进 kb/references/。
LEGACY_SKILLS="sync-docs kb-integrate kb-search"
for skill in ${LEGACY_SKILLS}; do
    if [ -d "${SKILLS_DIR}/${skill}" ]; then
        rm -rf "${SKILLS_DIR}/${skill}"
        echo "[install] Removed legacy skill ${skill} (merged into kb)"
    fi
done

install_skill "${SKILL_HOME}/kb.md" "kb"

# 三份正文原样装进 references/,剥掉各自的 frontmatter
mkdir -p "${SKILLS_DIR}/kb/references"
install_reference() {
    local src="$1" dst="$2"
    [ -f "${src}" ] || { echo "[install] Source missing: ${src}" >&2; exit 1; }
    sed -e "s|@SKILL_HOME@|${SKILL_HOME}|g" -e "s|@KB_HOME@|${KB_HOME}|g" "${src}" \
      | awk 'fm<2 && /^---$/{fm++; next} fm>=2{print}' \
      > "${SKILLS_DIR}/kb/references/${dst}.md"
    echo "[install] Wrote ${SKILLS_DIR}/kb/references/${dst}.md"
}
install_reference "${SKILL_HOME}/kb-search.md"    "search"
install_reference "${SKILL_HOME}/sync-docs.md"    "rebuild"
install_reference "${SKILL_HOME}/kb-integrate.md" "wire-project"

chmod +x "${SKILL_HOME}/scripts/"*.py 2>/dev/null || true

cat <<EOF

[install] Done.
  SKILL_HOME = ${SKILL_HOME}
  KB_HOME    = ${KB_HOME}
  Skills installed at ${SKILLS_DIR}

Try the kb skill in Claude Code (/kb). If ~/.claude/skills/ was just created,
restart Claude Code once so the new skills directory is watched.
To change KB_HOME later:  KB_HOME=/new/path ./install.sh
To install to Codex instead:  SKILL_TARGET=codex ./install.sh
EOF
