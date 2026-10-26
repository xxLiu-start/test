from __future__ import annotations

import json
import importlib.util
from io import BytesIO
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lh_harness.harbor_url import claude_code_upstream_base_url
from lh_harness.skill_learning import SkillBank
from lh_harness.training_provenance import ProvenanceWriter
from lh_harness.train_terminal_bench_pro import _require_valid_trial_report, _run_trial, _write_trial_config, discover_tasks, main, train_one


def _candidate() -> dict:
    view = {"input_use": "Read audited evidence", "output_spec": "Report proof", "guidance": "Check state", "avoid": "Do not guess"}
    return {
        "skill_id": "evidence_handoff", "category_id": "evidence_handoff", "version": 1,
        "version_parent": None, "status": "candidate", "content_language": "en",
        "benchmark_scope": "terminal_bench_2_1", "interaction_modes": ["cli"],
        "problem_mechanism": "Missing handoff evidence", "subcases": [],
        "triggers": [], "exclusions": [], "roles": {role: view for role in ("manager", "executor", "auditor")},
        "compatible_with": [], "conflicts_with": [], "provenance": {"evidence_refs": ["round_001"]},
        "validation": {"baseline_success": None, "candidate_success": None},
    }


def test_training_config_uses_pro_and_test_config_keeps_original_dataset() -> None:
    root = Path(__file__).resolve().parents[1]
    train = yaml.safe_load((root / "training/terminal_bench_pro_skill_learning.yaml").read_text())
    test = yaml.safe_load((root / "training/terminal_bench21_skill_test.yaml").read_text())
    assert train["datasets"][0]["path"] == "train_dataset/terminal-bench-pro"
    assert train["jobs_dir"] == "output/terminal-bench-pro/jobs"
    assert train["agents"][0]["kwargs"]["prompt_language"] == "en"
    assert train["agents"][0]["env"]["LH_HARNESS_MODEL_IO_TRACE"] == "1"
    for config in (train, test):
        agent = config["agents"][0]
        assert agent["model_name"] == "deepseek-v4.1-flash"
        assert agent["kwargs"]["harness_model"] == "deepseek-v4.1-flash"
        assert agent["kwargs"]["verifier_model"] == "deepseek-v4.1-flash"
        assert agent["env"]["LH_HARNESS_PROXY_BEARER_AUTH"] == "1"
        assert agent["env"]["LH_HARNESS_PROXY_OPENAI_BRIDGE"] == "1"
    assert train["n_concurrent_trials"] == 1
    assert test["datasets"][0]["path"] == "eval/TB-harness/datasets/terminal-bench-2-1/tasks"
    assert test["jobs_dir"] == "output/terminal-bench-2-1/jobs"
    assert test["agents"][0]["kwargs"]["prompt_language"] == "en"
    assert test["agents"][0]["kwargs"]["learning_enabled"] is False
    assert test["n_attempts"] == 3


def test_claude_code_upstream_does_not_duplicate_v1() -> None:
    assert claude_code_upstream_base_url("https://api.example.test/v1") == "https://api.example.test"
    assert claude_code_upstream_base_url("https://api.example.test/v1/v1") == "https://api.example.test"
    assert claude_code_upstream_base_url("https://api.example.test/anthropic/v1/") == "https://api.example.test/anthropic"
    assert claude_code_upstream_base_url("https://api.example.test/anthropic") == "https://api.example.test/anthropic"


def _proxy_module():
    root = Path(__file__).resolve().parents[1]
    proxy_path = root / "eval/TB-harness/Harness/src/assets/claudecode_request_proxy.py"
    spec = importlib.util.spec_from_file_location("claudecode_request_proxy", proxy_path)
    assert spec is not None and spec.loader is not None
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    return proxy


def test_gateway_proxy_sends_bearer_auth_without_x_api_key(monkeypatch) -> None:
    proxy = _proxy_module()
    handler = object.__new__(proxy.RequestProxyHandler)
    headers = Message()
    headers["x-api-key"] = "test-secret"
    headers["anthropic-version"] = "2023-06-01"
    handler.headers = headers
    handler.config = proxy.ProxyConfig(
        upstream="https://example.test", overrides={}, dashscope_wait_timeout=None,
        log_file=None, requests_log_file=None,
    )

    monkeypatch.setenv("LH_HARNESS_PROXY_BEARER_AUTH", "1")
    forwarded = handler._forward_headers(b"{}")
    assert forwarded["Authorization"] == "Bearer test-secret"
    assert all(key.lower() != "x-api-key" for key in forwarded)
    assert forwarded["anthropic-version"] == "2023-06-01"

    monkeypatch.delenv("LH_HARNESS_PROXY_BEARER_AUTH")
    forwarded = handler._forward_headers(b"{}")
    assert forwarded["x-api-key"] == "test-secret"
    assert all(key.lower() != "authorization" for key in forwarded)


def test_proxy_records_exact_client_model_input_separately(monkeypatch, tmp_path: Path) -> None:
    proxy = _proxy_module()
    handler = object.__new__(proxy.RequestProxyHandler)
    trace_file = tmp_path / "model_io.jsonl"
    handler.config = proxy.ProxyConfig(
        upstream="https://example.test", overrides={}, dashscope_wait_timeout=None,
        log_file=tmp_path / "proxy.log", requests_log_file=None, model_io_file=trace_file,
    )
    handler.command = "POST"
    handler.path = "/v1/messages?beta=true"
    handler.headers = Message()
    handler.headers["x-api-key"] = "test-secret"
    payload = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "read /app"}]}
    handler._read_body = lambda: json.dumps(payload).encode()
    bridged = []
    handler._forward_openai_bridge = lambda body, headers, *, trace_id: bridged.append((body, headers, trace_id))
    monkeypatch.setenv("LH_HARNESS_PROXY_BEARER_AUTH", "1")
    monkeypatch.setenv("LH_HARNESS_PROXY_OPENAI_BRIDGE", "1")
    handler._forward()

    records = [json.loads(line) for line in trace_file.read_text().splitlines()]
    assert records[0]["event"] == "client_request"
    assert records[0]["payload"]["value"] == payload
    assert bridged[0][1]["Authorization"] == "Bearer test-secret"
    assert bridged[0][2] == records[0]["trace_id"]
    assert "test-secret" not in trace_file.read_text()


def test_openai_bridge_preserves_tool_call_loop() -> None:
    proxy = _proxy_module()
    request = {
        "model": "deepseek-v4.1-flash", "max_tokens": 128, "stream": True,
        "system": [{"type": "text", "text": "Use tools.", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "system", "content": {"type": "text", "text": "Only use /app."}},
            {"role": "user", "content": "Read a file"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_previous", "name": "Bash", "input": {"command": "pwd"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_previous", "content": "/app"}]},
        ],
        "tools": [{"name": "Bash", "description": "Run shell", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
    }
    translated = proxy.anthropic_to_openai(request)
    assert translated["stream"] is False
    assert translated["messages"][0] == {"role": "system", "content": "Use tools."}
    assert translated["messages"][1] == {"role": "system", "content": "Only use /app."}
    assert translated["messages"][3]["tool_calls"][0]["id"] == "toolu_previous"
    assert translated["messages"][4] == {"role": "tool", "tool_call_id": "toolu_previous", "content": "/app"}
    assert translated["tools"][0]["function"]["name"] == "Bash"
    tool_history = dict(request)
    tool_history["messages"] = [{"role": "tool", "tool_call_id": "toolu_previous", "content": "ok"}]
    assert proxy.anthropic_to_openai(tool_history)["messages"][-1] == {
        "role": "tool", "tool_call_id": "toolu_previous", "content": "ok",
    }

    completion = {
        "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "reasoning_content": "checked the next step", "tool_calls": [
            {"type": "function", "id": "call_1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}
        ]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 12},
    }
    reasoning = {}
    message = proxy.openai_to_anthropic(completion, request["model"], reasoning_by_tool_id=reasoning)
    assert message["stop_reason"] == "tool_use"
    assert message["content"][0]["input"] == {"command": "ls"}
    assert message["content"][0]["id"].startswith("toolu_")
    assert reasoning[message["content"][0]["id"]] == "checked the next step"
    next_request = dict(request)
    next_request["messages"] = [
        {"role": "user", "content": "Read a file"},
        {"role": "assistant", "content": message["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": message["content"][0]["id"], "content": "ok"}]},
    ]
    continued = proxy.anthropic_to_openai(next_request, reasoning_by_tool_id=reasoning)
    assert continued["messages"][2]["reasoning_content"] == "checked the next step"
    events = [json.loads(line[6:]) for line in proxy.anthropic_sse(message).decode().splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "message_start"
    assert any(event.get("content_block", {}).get("type") == "tool_use" for event in events)
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    assert events[-1]["type"] == "message_stop"


def test_openai_bridge_forwards_to_chat_endpoint_and_returns_tool_sse(monkeypatch, tmp_path: Path) -> None:
    proxy = _proxy_module()
    handler = object.__new__(proxy.RequestProxyHandler)
    handler.config = proxy.ProxyConfig(
        upstream="https://example.test", overrides={}, dashscope_wait_timeout=None,
        log_file=None, requests_log_file=None, model_io_file=tmp_path / "model_io.jsonl",
    )
    handler.path = "/v1/messages?beta=true"
    handler.wfile = BytesIO()
    response_status = []
    response_headers = {}
    handler.send_response = lambda status: response_status.append(status)
    handler.send_header = lambda name, value: response_headers.__setitem__(name.lower(), value)
    handler.end_headers = lambda: None
    upstream_calls = []

    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({
                "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "reasoning_content": "check files first", "tool_calls": [
                    {"type": "function", "id": "call_1", "function": {"name": "Bash", "arguments": '{"command":"pwd"}'}}
                ]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }).encode()

    class FakeConnection:
        def request(self, method, target, body, headers):
            upstream_calls.append((method, target, json.loads(body), headers))

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(proxy, "make_connection", lambda parsed: FakeConnection())
    body = json.dumps({
        "model": "deepseek-v4.1-flash", "stream": True, "max_tokens": 128,
        "messages": [
            {"role": "system", "content": "Use tools."},
            {"role": "user", "content": "Run pwd"},
        ],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }).encode()
    handler._forward_openai_bridge(body, {"Authorization": "Bearer test-secret"})

    assert response_status == [200]
    assert response_headers["content-type"] == "text/event-stream"
    assert upstream_calls[0][0:2] == ("POST", "/v1/chat/completions")
    assert upstream_calls[0][2]["stream"] is False
    assert upstream_calls[0][2]["messages"][0] == {"role": "system", "content": "Use tools."}
    assert upstream_calls[0][3]["Authorization"] == "Bearer test-secret"
    assert b'"type":"tool_use"' in handler.wfile.getvalue()
    assert list(handler.config.reasoning_by_tool_id.values()) == ["check files first"]
    model_events = [json.loads(line) for line in (tmp_path / "model_io.jsonl").read_text().splitlines()]
    assert [item["event"] for item in model_events] == ["model_input", "model_output", "client_response"]
    assert model_events[0]["payload"]["messages"][0]["role"] == "system"
    assert model_events[1]["payload"]["format"] == "json"
    assert model_events[2]["payload"]["format"] == "sse"
    assert "test-secret" not in (tmp_path / "model_io.jsonl").read_text()


def test_dry_run_uses_dataset_name_for_output_root(tmp_path: Path, capsys) -> None:
    dataset = tmp_path / "custom-tasks"
    task_dir = dataset / "sample-task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('version = "1.0"')
    (task_dir / "instruction.md").write_text("Do the task")
    assert main(["--dataset", str(dataset), "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["output_root"] == str(Path.cwd() / "output" / "custom-tasks")


def test_start_from_selects_that_task_and_later_tasks(tmp_path: Path, capsys) -> None:
    dataset = tmp_path / "custom-tasks"
    for name in ("alpha", "beta", "gamma"):
        task_dir = dataset / name
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text('version = "1.0"')
        (task_dir / "instruction.md").write_text("Do the task")

    assert main(["--dataset", str(dataset), "--start-from", "beta", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == ["beta", "gamma"]
    assert main(["--dataset", str(dataset), "--start-from", "beta", "--limit", "1", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == ["beta"]
    with pytest.raises(SystemExit, match="2"):
        main(["--dataset", str(dataset), "--start-from", "missing", "--dry-run"])


def test_repeated_harbor_trials_receive_distinct_job_names(tmp_path: Path) -> None:
    template = {"agents": [{"kwargs": {}}]}
    configs = [tmp_path / f"trial-{index}.yaml" for index in range(2)]
    for path in configs:
        _write_trial_config(
            template, task="sample-task", phase="baseline", dataset=tmp_path / "dataset",
            bank=tmp_path / "bank.json", jobs_dir=tmp_path / "jobs", config_path=path,
        )
    names = [yaml.safe_load(path.read_text())["job_name"] for path in configs]
    assert names[0] != names[1]
    assert all(name.startswith("skill-sample-task-baseline-") for name in names)
    assert all(yaml.safe_load(path.read_text())["agents"][0]["env"]["LH_HARNESS_MODEL_IO_TRACE"] == "1" for path in configs)


def test_training_driver_replays_same_task_and_commits_only_after_success(tmp_path: Path, monkeypatch) -> None:
    dataset = tmp_path / "terminal-bench-pro"
    task_dir = dataset / "sample-task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('version = "1.0"')
    (task_dir / "instruction.md").write_text("Do the task")
    assert discover_tasks(dataset) == ["sample-task"]
    template = {"agents": [{"kwargs": {"skill_reward_reference_tokens": 1000}}]}
    bank_path = tmp_path / "bank.json"
    calls = []

    def fake_run_trial(config_path, jobs_dir):
        config = yaml.safe_load(config_path.read_text())
        calls.append(config)
        output = jobs_dir / "trial" / "skill_harness"
        routes_dir = output / "lh_harness" / "role_orchestration"
        routes_dir.mkdir(parents=True)
        report = {"task": "Do the task", "rounds_run": 1, "rounds": [], "abort_reason": "max_rounds_exhausted", "skill_learning": {"task_tokens": 100}}
        if len(calls) == 1:
            report.update({"status": "incomplete", "completion_satisfied": False, "latest_auditor_report": ""})
            (routes_dir / "skill_candidate.json").write_text(json.dumps({"kind": "skill", "candidate": _candidate()}))
            (routes_dir / "skill_routes.json").write_text("[]")
        else:
            report.update({"status": "complete", "completion_satisfied": True, "abort_reason": "", "latest_auditor_report": "Status: complete\nIntegrity: clean\nContract audit: aligned", "skill_learning": {"task_tokens": 150}})
            (routes_dir / "skill_routes.json").write_text(json.dumps([{"selected_skills": [{"skill_id": "evidence_handoff", "version": 1}], "injected_context": "Skill: evidence_handoff@1"}]))
        return report, output

    monkeypatch.setattr("lh_harness.train_terminal_bench_pro._run_trial", fake_run_trial)
    result = train_one(
        "sample-task", template=template, dataset=dataset, bank_path=bank_path,
        generated_dir=tmp_path / "generated", jobs_root=tmp_path / "jobs",
        decisions_dir=tmp_path / "decisions", reference_tokens=1000,
    )
    assert result["accepted"]
    assert [call["datasets"][0]["task_names"] for call in calls] == [["sample-task"], ["sample-task"]]
    assert calls[0]["agents"][0]["kwargs"]["skill_bank_path"] == calls[1]["agents"][0]["kwargs"]["skill_bank_path"]
    assert all(call["agents"][0]["kwargs"]["prompt_language"] == "en" for call in calls)
    assert calls[1]["agents"][0]["kwargs"]["candidate_path"].endswith("skill_candidate.json")
    assert SkillBank.load(bank_path).skills[0]["status"] == "validated"
    provenance_files = list((tmp_path / "jobs" / "provenance" / "sample-task").glob("*.jsonl"))
    assert len(provenance_files) == 1
    provenance_events = [json.loads(line)["event"] for line in provenance_files[0].read_text().splitlines()]
    assert "candidate_version" in provenance_events
    assert "decision" in provenance_events
    assert provenance_events.count("bank_snapshot") == 4
    assert provenance_events.count("candidate_version") == 2


def test_harbor_exception_surfaces_even_with_zero_exit_and_stale_report(tmp_path: Path, monkeypatch) -> None:
    jobs_dir = tmp_path / "jobs"
    old_trial = jobs_dir / "old"
    old_trial.mkdir(parents=True)
    (old_trial / "training_report.json").write_text(json.dumps({"report": {"status": "complete"}}))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.test/v1/v1")

    def fake_run(*args, **kwargs):
        assert kwargs["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.test"
        new_trial = jobs_dir / "new"
        new_trial.mkdir()
        (new_trial / "exception.txt").write_text("Docker build failed: proxyconnect 127.0.0.1:17897")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("lh_harness.train_terminal_bench_pro.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="proxyconnect 127.0.0.1:17897"):
        _run_trial(tmp_path / "trial.yaml", jobs_dir)


def test_provider_failure_is_retried_and_not_recorded_as_no_candidate(tmp_path: Path, monkeypatch) -> None:
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    decision_path = decisions / "sample-task.json"
    decision_path.write_text(json.dumps({"baseline_status": "failed", "reason": "no candidate proposed"}))
    calls = []

    def fake_run(config_path, jobs_dir):
        calls.append(config_path)
        return {
            "status": "failed", "abort_reason": "provider_provider_error",
            "failure_reason": "selected model is unavailable",
        }, jobs_dir / "trial" / "skill_harness"

    monkeypatch.setattr("lh_harness.train_terminal_bench_pro._run_trial", fake_run)
    with pytest.raises(RuntimeError, match="selected model is unavailable"):
        train_one(
            "sample-task", template={"agents": [{"kwargs": {}}]},
            dataset=tmp_path / "dataset", bank_path=tmp_path / "bank.json",
            generated_dir=tmp_path / "generated", jobs_root=tmp_path / "jobs",
            decisions_dir=decisions, reference_tokens=1000,
        )
    assert len(calls) == 1
    assert json.loads(decision_path.read_text())["reason"] == "no candidate proposed"
    traces = list((tmp_path / "jobs" / "provenance" / "sample-task").glob("*.jsonl"))
    assert len(traces) == 1
    assert any(
        json.loads(line).get("event") == "trial_failure"
        for line in traces[0].read_text().splitlines()
    )


def test_provider_failure_includes_proxy_response(tmp_path: Path) -> None:
    output_dir = tmp_path / "skill_harness"
    proxy_log = output_dir / "episodes" / "orchestrator" / "ep001" / "request_proxy_requests.jsonl"
    proxy_log.parent.mkdir(parents=True)
    proxy_log.write_text(json.dumps({
        "event": "upstream_http_error", "status": 404,
        "body_head": "Invalid URL (POST /v1/v1/messages)",
    }) + "\n")
    with pytest.raises(RuntimeError, match=r"Provider HTTP 404: Invalid URL \(POST /v1/v1/messages\)"):
        _require_valid_trial_report({"status": "failed", "failure_reason": "model error"}, output_dir, "baseline")


def test_plain_text_tool_calls_are_not_recorded_as_no_candidate(tmp_path: Path) -> None:
    output_dir = tmp_path / "skill_harness"
    stream = output_dir / "episodes" / "cli_task" / "ep001" / "claude_stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(json.dumps({
        "type": "assistant", "message": {"content": [{"type": "text", "text": "<｜｜DSML｜｜ calls>"}]},
    }) + "\n")
    report = {
        "status": "incomplete",
        "rounds": [{"executor_output": '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="bash">'}],
    }
    with pytest.raises(RuntimeError, match="plain text rather than Anthropic tool_use"):
        _require_valid_trial_report(report, output_dir, "baseline")


def test_view_only_provenance_keeps_skill_prompt_history_and_model_io(tmp_path: Path) -> None:
    path = tmp_path / "jobs" / "provenance" / "sample-task" / "run-1.jsonl"
    writer = ProvenanceWriter(path, task="sample-task", run_id="run-1")
    previous = _candidate()
    current = {**_candidate(), "version": 2, "version_parent": 1, "status": "validated"}
    bank = SkillBank(
        tmp_path / "state" / "skillbank.json",
        skills=[current], router_prompts={"en": "current router prompt"},
        history=[{"kind": "skill", "skill": previous}, {"kind": "router", "language": "en", "prompt": "previous router prompt"}],
    )
    writer.static_prompts()
    writer.bank_snapshot("before_baseline", bank)
    proposal_path = tmp_path / "proposal.json"
    writer.candidate("proposed", {"kind": "skill", "candidate": _candidate(), "assessment": {"action": "REVISE_SKILL"}}, source_path=proposal_path)
    writer.candidate(
        "proposed", {"kind": "router", "candidate": "new router prompt"},
        source_path=proposal_path, previous_router_prompt="current router prompt",
    )
    config_path = tmp_path / "trial.yaml"
    config_path.write_text("job_name: job-1\n")
    model_io_path = tmp_path / "jobs" / "job-1" / "trial" / "model_io.jsonl"
    model_io_path.parent.mkdir(parents=True)
    model_io_path.write_text(json.dumps({"event": "model_input", "payload": {"model": "deepseek-v4.1-flash"}}) + "\n")
    writer.ingest_model_io(config_path, tmp_path / "jobs", phase="baseline")

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(item["event"] == "skill_version" and item["source"] == "bank_history" and item["version"] == 1 for item in records)
    assert any(item["event"] == "skill_version" and item["source"] == "active_bank" and item["version"] == 2 for item in records)
    assert any(item["event"] == "prompt_version" and item["source"] == "bank_history" and item["content"] == "previous router prompt" for item in records)
    assert any(item["event"] == "prompt_version" and item["source"] == "code_template" for item in records)
    assert any(item["event"] == "candidate_version" and item["kind"] == "skill" for item in records)
    assert any(item["event"] == "prompt_version" and item["source"] == "router_candidate" and item["previous_version_id"] for item in records)
    assert any(item["event"] == "model_io" and item["record"]["payload"]["model"] == "deepseek-v4.1-flash" for item in records)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not bank.path.exists()


def test_view_only_trace_write_failure_does_not_stop_training(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not_a_directory"
    blocked_parent.write_text("keep")
    writer = ProvenanceWriter(blocked_parent / "trace.jsonl", task="task", run_id="run")
    writer.append("run_start")
    assert blocked_parent.read_text() == "keep"

    proxy = _proxy_module()
    config = proxy.ProxyConfig(
        upstream="https://example.test", overrides={}, dashscope_wait_timeout=None,
        log_file=None, requests_log_file=None, model_io_file=blocked_parent / "model_io.jsonl",
    )
    config.record_model_io({"event": "model_input", "payload": {"model": "deepseek-v4.1-flash"}})
    assert blocked_parent.read_text() == "keep"
