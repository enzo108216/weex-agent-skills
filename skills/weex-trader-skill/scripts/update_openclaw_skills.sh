#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_REPO_URL="https://github.com/weex-labs/weex-agent-skills.git"
readonly DEFAULT_BRANCH="main"
readonly OPENCLAW_ROOT="${OPENCLAW_HOME:-${HOME}/.openclaw}"
readonly REPO_URL="${WEEX_OPENCLAW_REPO_URL:-${DEFAULT_REPO_URL}}"
readonly REPO_DIR="${WEEX_OPENCLAW_REPO_DIR:-${OPENCLAW_ROOT}/skill-repos/weex-agent-skills}"
readonly SKILLS_DIR="${WEEX_OPENCLAW_SKILLS_DIR:-${OPENCLAW_ROOT}/skills}"
readonly BRANCH="${WEEX_OPENCLAW_BRANCH:-${DEFAULT_BRANCH}}"
readonly BIN_LINK="${WEEX_OPENCLAW_BIN_LINK:-${HOME}/bin/update-weex-openclaw-skills.sh}"
readonly SCRIPT_RELATIVE_PATH="skills/weex-trader-skill/scripts/update_openclaw_skills.sh"

readonly -a WEEX_SKILLS=(
  "weex-trader-skill"
  "weex-analysis-skill"
  "weex-monitor-skill"
  "weex-partner-skill"
)

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

ensure_symlink() {
  local source_path="$1"
  local destination_path="$2"

  [[ -e "${source_path}" ]] || fail "symlink source does not exist: ${source_path}"
  mkdir -p "$(dirname "${destination_path}")"

  if [[ -L "${destination_path}" ]]; then
    ln -sfn "${source_path}" "${destination_path}"
    return
  fi
  if [[ -e "${destination_path}" ]]; then
    fail "destination exists and is not a symbolic link: ${destination_path}"
  fi
  ln -s "${source_path}" "${destination_path}"
}

update_repository() {
  if [[ -e "${REPO_DIR}" && ! -d "${REPO_DIR}/.git" ]]; then
    fail "repository path exists but is not a Git checkout: ${REPO_DIR}"
  fi

  if [[ ! -e "${REPO_DIR}" ]]; then
    mkdir -p "$(dirname "${REPO_DIR}")"
    git clone --branch "${BRANCH}" --single-branch "${REPO_URL}" "${REPO_DIR}"
    return
  fi

  git -C "${REPO_DIR}" fetch origin "${BRANCH}"
  if git -C "${REPO_DIR}" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
    git -C "${REPO_DIR}" checkout "${BRANCH}"
  else
    git -C "${REPO_DIR}" checkout -b "${BRANCH}" FETCH_HEAD
  fi
  git -C "${REPO_DIR}" pull --ff-only origin "${BRANCH}"
}

link_skills() {
  local skill_name

  mkdir -p "${SKILLS_DIR}"
  for skill_name in "${WEEX_SKILLS[@]}"; do
    [[ -f "${REPO_DIR}/skills/${skill_name}/SKILL.md" ]] \
      || fail "invalid skill checkout; missing skills/${skill_name}/SKILL.md"
    ensure_symlink "${REPO_DIR}/skills/${skill_name}" "${SKILLS_DIR}/${skill_name}"
  done
  ensure_symlink "${REPO_DIR}/${SCRIPT_RELATIVE_PATH}" "${BIN_LINK}"
}

validate_openclaw() {
  require_command openclaw
  openclaw skills list --eligible
  openclaw skills info weex-trader-skill
  openclaw skills check
}

print_version() {
  local version
  local latest_commit

  version="$(git -C "${REPO_DIR}" describe --tags --always --dirty)"
  latest_commit="$(git -C "${REPO_DIR}" log -1 --format='%h %s (%cI)')"
  printf 'OpenClaw WEEX skills are ready.\n'
  printf 'Repository: %s\n' "${REPO_DIR}"
  printf 'Branch: %s\n' "${BRANCH}"
  printf 'Version: %s\n' "${version}"
  printf 'Latest commit: %s\n' "${latest_commit}"
  printf 'Update command: %s\n' "${BIN_LINK}"
}

main() {
  require_command git
  update_repository
  link_skills
  validate_openclaw
  print_version
}

main "$@"
