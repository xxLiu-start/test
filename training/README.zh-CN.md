# SkillBank 训练与测试

## 数据划分

| 用途 | 数据集 | 配置 |
|---|---|---|
| 训练与候选重跑 | `train_dataset/terminal-bench-pro` | `terminal_bench_pro_skill_learning.yaml` |
| 测试 | 原有 Terminal-Bench 2.1：`eval/TB-harness/datasets/terminal-bench-2-1/tasks` | `terminal_bench21_skill_test.yaml` |

测试任务仍使用原评测目录。新的测试配置用当前 `src/lh_harness/` 主实现加载训练后 SkillBank，并设置 `learning_enabled: false`，因此测试任务不会更新 SkillBank。

训练和测试均固定使用英文角色 Prompt、英文学习 Prompt 和英文 Skill（`prompt_language: en`）；数据集中的任务原文保持原样。
本方法的训练与测试配置均使用 `deepseek-v4.1-flash`。Claude Code 仍向本地代理发送 Anthropic Messages；代理把文本与工具消息转换为 `/v1/chat/completions` 请求，再把 OpenAI 的结构化函数调用转回 Anthropic `tool_use`。这使用了接口检查中能够返回结构化工具调用的协议。训练驱动与测试脚本会在启动 Harbor 前规范化 Base URL 的结尾 `/v1`，代理把密钥作为 `Authorization: Bearer` 转发。

## 运行

使用原 Terminal-Bench 2.1 环境（`eval/TB-harness/environment-tbench21.yml`），准备 Docker、Harbor 和模型 API 环境变量：

```bash
export ANTHROPIC_API_KEY=...
export ANTHROPIC_BASE_URL=...
python training/check_model_endpoint.py
./training/run_terminal_bench_pro.sh --dry-run
./training/run_terminal_bench_pro.sh --task maze-shortest-path-solver
./training/run_terminal_bench_pro.sh
```

从指定题目继续时，先用 `--dry-run` 核对范围，再运行：

```bash
./training/run_terminal_bench_pro.sh --start-from analyze-arm-shellcode-network-connections --dry-run
./training/run_terminal_bench_pro.sh --start-from analyze-arm-shellcode-network-connections
```

任务按目录名字典序处理；`--start-from` 包含指定题目，跳过其前面的题目，`--limit N` 对剩余题目生效。训练驱动按任务逐个运行。每个任务先运行基线；若提出候选，再让 Harbor 在**新的任务容器**里重跑同一任务。只有候选实际生效、原始完成守卫通过、无硬约束回归、且按真实 Token 计算的完整任务奖励提高时，才写入 `output/terminal-bench-pro/state/skillbank.json`。训练输出按数据集目录归档：`output/terminal-bench-pro/{jobs,generated_configs,decisions,state}/`；使用 `--dataset` 指定其他训练集时，目录名随数据集目录名变化。`--task` 可重复指定，`--force` 可重新运行已有决定的题。每次重跑使用新的 Harbor 作业名，旧日志保留。已完成题目通过决定文件跳过；中断在题目内部时，该题从初始环境重新运行。

每次调用训练脚本都会将终端标准输出和错误输出同步保存到 `output/<数据集名>/jobs/console_logs/train-*.log`，同时在终端打印具体日志路径。脚本保留原训练命令的退出状态，方便定位失败。

## 只读溯源记录

每次实际训练任务都会另建一个追加式 JSONL 文件：`output/<数据集名>/jobs/provenance/<题目>/<运行编号>.jsonl`；任务开始时会打印具体路径。训练程序不会读取它，也不会用它做 Skill 选择或候选验收。原有 SkillBank、决策、Harbor 作业和终端日志的路径及格式保持不变。

记录覆盖每个角色及 Skill 学习调用的完整模型输入和输出，用同一 `trace_id` 关联客户端与上游模型调用。桥接模式分别保存收到的 Anthropic 请求、转成的 OpenAI 请求、OpenAI 响应和转回的 Anthropic 响应。JSON 与 SSE 保存结构化内容和原始文本；不记录认证请求头。每个 Harbor 回合目录也单独保留 `model_io.jsonl`。

同一文件还记录当前与历史 Skill 版本、当前与历史 Router Prompt 版本、生成及验收后的候选、带 SHA-256 版本号的英文静态 Prompt、基线与候选报告、试跑失败原因，以及最终决策。训练前、每次试跑后及提交后的 SkillBank 快照可用于跨运行比较。文件采用仅所有者可读写的权限，因为完整 Prompt 和响应可能包含任务数据。功能上线前已经结束的运行无法补录精确的模型请求和响应。

训练完成并确认 SkillBank 已生成后，使用原测试任务目录运行：

```bash
./training/run_terminal_bench21_test.sh
```

测试输出放在 `output/terminal-bench-2-1/`，其中 `jobs/` 保存 Harbor 日志；测试从 `output/terminal-bench-pro/state/skillbank.json` 加载已训练的 SkillBank，不更新它。测试配置保持原实验的 `n_attempts: 3` 和 `n_concurrent_trials: 16`。当前工作区未放入原 Terminal-Bench 2.1 测试 tasks，运行测试前仍需按 `eval/TB-harness/README.zh-CN.md` 准备到上述路径。这里没有自动执行模型训练或测试任务；它们需要 Docker、Harbor、模型凭据及对应的运行时间。

`python training/check_model_endpoint.py` 会用同一 Bearer 密钥分别请求 `/v1/chat/completions` 和 `/v1/messages`，只打印 HTTP 状态码。请求代理的 `request_proxy_requests.jsonl` 还会记录 `forwarded_auth` 事件，只有 `bearer`、`x_api_key` 布尔值，不记录密钥。
如果两个接口均报 `ConnectionRefusedError`，运行 `python training/check_model_endpoint.py --direct` 绕过已配置的 HTTP(S) 代理，区分本机代理故障与 API 返回的错误。
运行 `python training/check_model_endpoint.py --direct --tools` 可检查两种协议是否遵循强制结构化工具调用。Claude Code 无法执行纯文本模拟的工具调用；训练驱动会将这种情况报为接口协议错误，不再记录为普通的“没有候选”。桥接器目前支持文本消息和客户端工具调用；不支持的内容块会明确报错。
DeepSeek 思考模式下，桥接器会在单个角色回合的代理进程内暂存 `reasoning_content`，并在后续工具请求中回传；代理审计日志不写入这段内容。
