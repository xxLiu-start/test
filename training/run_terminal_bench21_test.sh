#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
conda_env="${CONDA_ENV:-terminal-bench-2-1}"
config_path="${TEST_CONFIG_PATH:-${repo_dir}/training/terminal_bench21_skill_test.yaml}"

export PYTHONPATH="${repo_dir}/src:${repo_dir}/eval/TB-harness/Harness/src${PYTHONPATH:+:${PYTHONPATH}}"
export LH_SKILL_BANK_PATH="${LH_SKILL_BANK_PATH:-${repo_dir}/output/terminal-bench-pro/state/skillbank.json}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
cd "$repo_dir"

if [[ ! -f "$LH_SKILL_BANK_PATH" ]]; then
  echo "SkillBank not found: $LH_SKILL_BANK_PATH" >&2
  exit 2
fi
if [[ ! -d "$repo_dir/eval/TB-harness/datasets/terminal-bench-2-1/tasks" ]]; then
  echo "Original Terminal-Bench 2.1 test tasks are missing; see eval/TB-harness/README.zh-CN.md" >&2
  exit 2
fi
if [[ -z "${ANTHROPIC_API_KEY:-}" || -z "${ANTHROPIC_BASE_URL:-}" ]]; then
  echo "ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL are required" >&2
  exit 2
fi

# Harbor's scoped AgentConfig.env overrides the role adapter's per-exec env.
# Claude Code sends /v1/messages, so pass the upstream origin to Harbor.
ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/}"
while [[ "$ANTHROPIC_BASE_URL" == */v1 ]]; do
  ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/v1}"
done
export ANTHROPIC_BASE_URL

mkdir -p output/terminal-bench-2-1/jobs output/terminal-bench-2-1/generated_configs output/terminal-bench-2-1/decisions output/terminal-bench-2-1/state
conda run --no-capture-output -n "$conda_env" harbor run -c "$config_path" "$@"
