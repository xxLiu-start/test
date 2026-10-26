# SkillBank train/test split

Training uses `train_dataset/terminal-bench-pro`. Testing uses the original Terminal-Bench 2.1 task directory, `eval/TB-harness/datasets/terminal-bench-2-1/tasks`. The new test config loads the learned bank with updates disabled.

Both workflows use English role prompts, learning prompts, and Skill content (`prompt_language: en`). Task instructions stay in their source language.
Both configurations use `deepseek-v4.1-flash`. Claude Code sends Anthropic Messages to a local request proxy; for these workflows, the proxy translates text and tool messages to OpenAI Chat Completions at `/v1/chat/completions` and converts structured function calls back into Anthropic `tool_use` blocks. This uses the gateway protocol that returned structured tool calls in the endpoint check. The driver and test script normalize trailing `/v1` before Harbor applies its scoped environment, and the proxy forwards the configured key as `Authorization: Bearer`.

Use the existing Terminal-Bench 2.1 Conda environment, Docker, and an Anthropic-compatible model endpoint:

```bash
export ANTHROPIC_API_KEY=...
export ANTHROPIC_BASE_URL=...
python training/check_model_endpoint.py
./training/run_terminal_bench_pro.sh --dry-run
./training/run_terminal_bench_pro.sh --task maze-shortest-path-solver
./training/run_terminal_bench_pro.sh --start-from analyze-arm-shellcode-network-connections
./training/run_terminal_bench_pro.sh
./training/run_terminal_bench21_test.sh
```

The trainer runs one task at a time. A proposed candidate is replayed in a fresh Harbor task container, then committed only if it takes effect, satisfies the original completion guard and hard constraints, and improves the complete-task reward using provider-reported token usage. Outputs are grouped by training dataset name under `output/<dataset-name>/{jobs,generated_configs,decisions,state}/`; the default bank is `output/terminal-bench-pro/state/skillbank.json`. Use `--start-from TASK` to include that task and all later tasks in sorted order, `--limit N` to bound the remaining tasks, and `--force` to rerun decided tasks. An interrupted task restarts in a fresh environment. Each replay gets a unique Harbor job name, preserving earlier logs.

Every training-script invocation also streams stdout and stderr to a unique `output/<dataset-name>/jobs/console_logs/train-*.log` file. The script prints the exact path and preserves the training command's exit status. Terminal-Bench 2.1 test jobs go to `output/terminal-bench-2-1/jobs/` and load the training bank read-only.

## View-only provenance

Each task attempt creates an additional append-only JSONL file at `output/<dataset-name>/jobs/provenance/<task>/<run-id>.jsonl`; the exact path is printed when the task starts. This file is never read by the trainer or used for Skill selection or acceptance. Existing SkillBank, decision, Harbor job, and console log files keep their current paths and formats.

Records include the complete model requests and responses for every role and Skill-learning call, with one `trace_id` tying client and upstream sides of a call together. Bridge runs save the incoming Anthropic request, translated OpenAI request, OpenAI response, and translated Anthropic response. JSON and SSE bodies are saved as structured data alongside their original text; authentication headers are excluded. Individual call records also remain in each Harbor episode's `model_io.jsonl`.

The same task-run file includes active and historical Skill versions, current and historical Router Prompt versions, the generated and assessed candidate, static English prompt templates with SHA-256 version IDs, baseline/candidate reports, trial failures, and the final decision. SkillBank snapshots before training, after each trial, and after commit make changes traceable across runs. The files are created with owner-only permissions because full prompts and responses may contain task data. Runs completed before this feature cannot be retroactively given exact model request/response bodies.

The Terminal-Bench 2.1 test tasks are not present in this checkout. Prepare them as described in `eval/TB-harness/README.md` before testing. Model runs have not been launched here.

The endpoint check makes one small request to each of `/v1/chat/completions` and `/v1/messages` using the same Bearer key, and prints only HTTP status codes. If the proxy still fails, its `request_proxy_requests.jsonl` records a `forwarded_auth` event with boolean `bearer` and `x_api_key` fields, without recording the key.
If both checks fail with `ConnectionRefusedError`, run `python training/check_model_endpoint.py --direct` to bypass a configured HTTP(S) proxy and distinguish a broken local proxy from an API response.
Run `python training/check_model_endpoint.py --direct --tools` to check whether each protocol honors a forced structured tool call. A plain-text imitation of a tool call cannot be executed by Claude Code and is treated as a provider protocol failure rather than a completed training decision. The bridge currently supports text messages and client tool calls; unsupported content blocks fail explicitly.
For DeepSeek thinking-mode tool continuations, the bridge keeps `reasoning_content` in memory for the life of each role episode and restores it to later OpenAI requests. It does not write that content to the proxy audit log.
