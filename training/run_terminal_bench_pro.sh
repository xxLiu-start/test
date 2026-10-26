#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
conda_env="${CONDA_ENV:-terminal-bench-2-1}"

export PYTHONPATH="${repo_dir}/src:${repo_dir}/eval/TB-harness/Harness/src${PYTHONPATH:+:${PYTHONPATH}}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
cd "$repo_dir"

dataset_path="train_dataset/terminal-bench-pro"
args=("$@")
for ((index=0; index<${#args[@]}; index++)); do
  case "${args[index]}" in
    --dataset)
      if ((index + 1 < ${#args[@]})); then
        dataset_path="${args[index + 1]}"
      fi
      ;;
    --dataset=*) dataset_path="${args[index]#--dataset=}" ;;
  esac
done
dataset_name="$(basename "${dataset_path%/}")"
if [[ -z "$dataset_name" || "$dataset_name" == "." ]]; then
  echo "Invalid training dataset path: $dataset_path" >&2
  exit 2
fi
output_root="output/$dataset_name"
mkdir -p "$output_root/jobs/console_logs" "$output_root/generated_configs" "$output_root/decisions" "$output_root/state"
log_file="$(mktemp "$output_root/jobs/console_logs/train-$(date +%Y%m%d-%H%M%S)-XXXXXX.log")"
printf 'Terminal output log: %s\n' "$repo_dir/$log_file" | tee "$log_file"
conda run --no-capture-output -n "$conda_env" python -m lh_harness.train_terminal_bench_pro "$@" 2>&1 | tee -a "$log_file"
