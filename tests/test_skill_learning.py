from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lh_harness.skill_learning import (
    SkillBank, SkillSession, accept_candidate, render_skill_context,
    task_success, token_usage, validate_selection, validate_skill,
)
from lh_harness.types import EpisodeBudget, EpisodeResult


def skill(skill_id: str, *, conflict: list[str] | None = None) -> dict:
    view = {"input_use": "Check upstream evidence", "output_spec": "Report status and proof", "guidance": "Verify one step", "avoid": "Do not guess"}
    return {
        "skill_id": skill_id, "category_id": "evidence_handoff", "version": 1,
        "version_parent": None, "status": "validated", "content_language": "en",
        "benchmark_scope": "shared", "interaction_modes": ["gui", "cli", "hybrid"],
        "problem_mechanism": "Missing evidence at handoff", "subcases": [],
        "triggers": [], "exclusions": [], "roles": {r: view for r in ("manager", "executor", "auditor")},
        "compatible_with": [], "conflicts_with": conflict or [], "provenance": {}, "validation": {},
    }


class FakeAgent:
    def __init__(self, replies: list[dict]):
        self.replies = replies
        self.prompts: list[str] = []

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
        self.prompts.append(prompt)
        return EpisodeResult(status="done", metadata={"assistant_visible_output": json.dumps(self.replies.pop(0)), "usage": {"input_tokens": 20, "output_tokens": 10}})


def test_router_selects_multiple_and_logs_exact_inputs(tmp_path: Path) -> None:
    bank = SkillBank(tmp_path / "bank.json", skills=[skill("first"), skill("second")])
    bank.save()
    agent = FakeAgent([{"active_issue_atoms": [], "selected_skills": [{"skill_id": "first", "version": 1}, {"skill_id": "second", "version": 1}], "rejected_candidates": [], "uncovered_issue_ids": [], "selection_rationale": "complementary"}])
    session = SkillSession(bank, "en", agent, None, EpisodeBudget(), tmp_path / "run")
    context = asyncio.run(session.route("manager", "Original manager prompt", 1))
    assert "Skill: first@1" in context and "Skill: second@1" in context
    assert "Output specification" in context
    assert session.records[0]["role_base_prompt"] == "Original manager prompt"
    assert "role_base_prompt" not in session.records[0]["router_input"]
    assert session.records[0]["injected_context"] == context
    assert "role_view" not in session.records[0]["router_input"]["eligible_skill_cards"][0]
    assert (tmp_path / "run" / "skill_routes.json").exists()


def test_invalid_router_output_falls_back_without_injection(tmp_path: Path) -> None:
    bank = SkillBank(tmp_path / "bank.json", skills=[skill("first")])
    agent = FakeAgent([{"selected_skills": [{"skill_id": "unknown", "version": 1}]}])
    session = SkillSession(bank, "en", agent, None, EpisodeBudget(), tmp_path / "run")
    assert asyncio.run(session.route("manager", "base", 1)) == ""
    assert "unknown Skill ID" in session.records[0]["router_error"]


def test_conflicts_and_candidate_injection_are_hard_checked() -> None:
    eligible = [skill("first", conflict=["second"]), skill("second")]
    eligible = [{**s, "role_view": s["roles"]["manager"]} for s in eligible]
    with pytest.raises(ValueError, match="conflicting"):
        validate_selection({"selected_skills": [{"skill_id": "first", "version": 1}, {"skill_id": "second", "version": 1}]}, eligible, budget=2000)
    with pytest.raises(ValueError, match="candidate was not selected"):
        validate_selection({"selected_skills": []}, eligible, budget=2000, target="first")
    assert not render_skill_context([], "en")


def test_candidate_revision_replaces_old_version_in_router_view(tmp_path: Path) -> None:
    old = skill("first")
    candidate = {**skill("first"), "version": 2, "version_parent": 1, "status": "candidate"}
    views = SkillBank(tmp_path / "bank.json", skills=[old]).eligible("manager", "en", candidate)
    assert [(view["skill_id"], view["version"]) for view in views] == [("first", 2)]


def test_scope_and_modality_filter_cards_before_routing(tmp_path: Path) -> None:
    terminal_cli = {**skill("terminal_cli"), "benchmark_scope": "terminal_bench_2_1", "interaction_modes": ["cli"]}
    osworld_gui = {**skill("osworld_gui"), "benchmark_scope": "osworld_v2", "interaction_modes": ["gui"]}
    shared_gui = {**skill("shared_gui"), "interaction_modes": ["gui"]}
    bank = SkillBank(tmp_path / "bank.json", skills=[terminal_cli, osworld_gui, shared_gui])
    cli_cards = bank.eligible("cli_executor", "en", benchmark_id="terminal_bench_2_1", interaction_mode="cli")
    assert [card["skill_id"] for card in cli_cards] == ["terminal_cli"]
    assert all("role_view" not in card for card in cli_cards)
    manager_cards = bank.eligible("manager", "en", benchmark_id="terminal_bench_2_1", interaction_mode="hybrid")
    assert [card["skill_id"] for card in manager_cards] == ["terminal_cli", "shared_gui"]


def test_success_observation_does_not_verify_skill(tmp_path: Path) -> None:
    existing = skill("first")
    bank = SkillBank(tmp_path / "bank.json", skills=[existing])
    agent = FakeAgent([{"action": "NO_NEW_SKILL", "candidate_spec": None}])
    session = SkillSession(bank, "en", agent, None, EpisodeBudget(), tmp_path / "run", dataset_id="training")
    session.records.append({"round": 1, "role": "manager", "selected_skills": [{"skill_id": "first", "version": 1}], "injected_context": "Skill: first@1"})
    report = {"task": "independent task", "status": "complete", "completion_satisfied": True, "latest_auditor_report": "Status: complete\nIntegrity: clean\nContract audit: aligned", "rounds": []}
    asyncio.run(session.finish(report))
    assert bank.skills[0]["status"] == "validated"
    assert len(bank.skills[0]["validation"]["observational_task_ids"]) == 1


def test_skill_workflow_rejects_non_english_content_and_prompt_language(tmp_path: Path) -> None:
    from lh_harness.role_prompts import (
        build_role_auditor_prompt, build_role_executor_prompt, build_role_manager_prompt,
    )
    from lh_harness.types import HarnessConfig

    validate_skill(skill("first"), language="en")
    with pytest.raises(ValueError, match="English"):
        validate_skill({**skill("first"), "content_language": "zh"}, language="zh")
    translated = skill("first")
    translated["roles"]["manager"] = {**translated["roles"]["manager"], "guidance": "核验状态"}
    with pytest.raises(ValueError, match="English"):
        validate_skill(translated, language="en")
    with pytest.raises(ValueError, match="English"):
        HarnessConfig(skill_bank_path=str(tmp_path / "bank.json"), prompt_language="zh")
    assert HarnessConfig(prompt_language="zh").prompt_language == "zh"
    with pytest.raises(ValueError, match="English"):
        SkillSession(SkillBank(tmp_path / "bank.json"), "zh", FakeAgent([]), None, EpisodeBudget(), tmp_path / "run")
    old_bank = tmp_path / "legacy_bank.json"
    old_bank.write_text(json.dumps({"skills": [], "router_prompts": {"zh": "中文路由"}}))
    with pytest.raises(ValueError, match="English"):
        SkillBank.load(old_bank)
    for builder, kwargs in (
        (build_role_manager_prompt, {"task": "task", "rounds": [], "round_index": 1}),
        (build_role_executor_prompt, {"task": "task", "plan_text": "plan", "next_step": "cli"}),
        (build_role_auditor_prompt, {"task": "task", "plan_text": "plan", "executor_output": "done", "next_step": "cli"}),
    ):
        with pytest.raises(ValueError, match="English"):
            builder(**kwargs, language="zh", skill_context="Skill: first@1")


def test_reward_requires_auditor_guard_and_reported_tokens() -> None:
    base = {"status": "incomplete", "completion_satisfied": False, "latest_auditor_report": ""}
    good = {"status": "complete", "completion_satisfied": True, "latest_auditor_report": "Status: complete\nIntegrity: clean\nContract audit: aligned\nAudit facts: verified"}
    bad = {**good, "latest_auditor_report": "Status: incomplete\nIntegrity: clean\nContract audit: aligned"}
    assert task_success(good)
    assert not task_success(bad)
    assert accept_candidate(base, good, baseline_tokens=100, candidate_tokens=200, reference_tokens=1000, effective=True, hard_constraint_regression=False)[0]
    assert not accept_candidate(base, bad, baseline_tokens=100, candidate_tokens=200, reference_tokens=1000, effective=True, hard_constraint_regression=False)[0]
    assert not accept_candidate(base, good, baseline_tokens=None, candidate_tokens=200, reference_tokens=1000, effective=True, hard_constraint_regression=False)[0]
    assert token_usage(EpisodeResult(status="done", actions_log='{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":4}}')) == 7
    assert token_usage(EpisodeResult(status="done", actions_log='{"type":"step_finish","part":{"id":"one","tokens":{"total":9,"input":7,"output":2}}}')) == 9
    assert token_usage(EpisodeResult(status="done", metadata={"n_input_tokens": 12, "n_output_tokens": 3, "n_cache_tokens": 4})) == 15
    assert token_usage(EpisodeResult(status="done", actions_log="plain text")) is None


def test_learning_candidate_stays_out_of_bank_without_replay(tmp_path: Path) -> None:
    source = skill("new_mechanism")
    spec = {"category_id": "evidence_handoff", "problem_mechanism": source["problem_mechanism"], "subcases": [], "triggers": [], "exclusions": [], "roles": source["roles"]}
    bank = SkillBank(tmp_path / "bank.json")
    agent = FakeAgent([
        {"action": "CREATE_NEW_SKILL", "candidate_spec": spec, "evidence_refs": ["round_001"]},
    ])
    session = SkillSession(bank, "en", agent, None, EpisodeBudget(), tmp_path / "run")
    session.token_counts.append(100)
    report = {"task": "first task", "status": "complete", "completion_satisfied": True, "latest_auditor_report": "Status: complete\nIntegrity: clean\nContract audit: aligned", "rounds": [], "rounds_run": 1}
    result = asyncio.run(session.finish(report))
    assert result["validation"] == "pending_replay"
    assert not bank.skills
    assert (tmp_path / "run" / "skill_candidate.json").exists()
    assert len(agent.prompts) == 1


def test_full_replay_accepts_only_effective_reward_gain(tmp_path: Path) -> None:
    source = skill("new_mechanism")
    spec = {"category_id": "evidence_handoff", "problem_mechanism": source["problem_mechanism"], "subcases": [], "triggers": [], "exclusions": [], "roles": source["roles"]}
    bank = SkillBank(tmp_path / "bank.json")
    agent = FakeAgent([
        {"action": "CREATE_NEW_SKILL", "candidate_spec": spec, "evidence_refs": ["round_001"]},
    ])
    session = SkillSession(bank, "en", agent, None, EpisodeBudget(), tmp_path / "run")
    session.token_counts.append(200)
    report = {"task": "first task", "status": "complete", "completion_satisfied": True, "latest_auditor_report": "Status: complete\nIntegrity: clean\nContract audit: aligned", "rounds": [], "rounds_run": 1}

    async def replay(request):
        assert request["kind"] == "skill" and request["mode"] == "candidate_validation"
        candidate_id = request["candidate"]["skill_id"]
        return {"report": report, "task_tokens": 100, "effective": True, "hard_constraint_regression": False,
                "skill_routes": [{"selected_skills": [{"skill_id": candidate_id, "version": 1}], "injected_context": f"Skill: {candidate_id}@1"}]}

    result = asyncio.run(session.finish(report, replay=replay, reference_tokens=1000))
    assert result["validation"]["accepted"]
    assert SkillBank.load(bank.path).skills[0]["status"] == "validated"


def test_manager_run_routes_and_injects_skill(tmp_path: Path) -> None:
    from lh_harness.manager import run
    from lh_harness.types import ExecResult, HarnessConfig

    class Env:
        staging_dir = str(tmp_path / "staging")

        async def exec(self, command, timeout=30, tee_path=None):
            return ExecResult("", "", 0, 0)

        async def upload(self, local_path, remote_path):
            return None

        async def download(self, remote_path, local_path):
            return None

        async def screenshot(self):
            return b""

    class Agent:
        def __init__(self):
            self.prompts = []

        async def run_episode(self, prompt, env, budget, live_trajectory_path=None):
            self.prompts.append(prompt)
            if "Skill Router for LongHorizon-Harness" in prompt:
                output = json.dumps({"selected_skills": [{"skill_id": "first", "version": 1}]})
            else:
                output = "Next: blocked\n\nReason: unavailable"
            return EpisodeResult(status="done", actions_log=output, metadata={"assistant_visible_output": output, "usage": {"input_tokens": 5, "output_tokens": 5}})

    bank = SkillBank(tmp_path / "bank.json", skills=[skill("first")])
    bank.save()
    agent = Agent()
    report = asyncio.run(run(
        task="test task", env=Env(), agent=agent,
        config=HarnessConfig(max_total_episodes=1, workspace_path=str(tmp_path), harness_dir=str(tmp_path / "harness"), log_dir=str(tmp_path / "logs"), skill_bank_path=str(bank.path)),
    ))
    assert report["status"] == "blocked"
    assert any("Skill: first@1" in prompt for prompt in agent.prompts)
    assert report["skill_learning"]["task_tokens"] == 20
    routes = json.loads((tmp_path / "logs" / "role_orchestration" / "skill_routes.json").read_text())
    assert routes[0]["selected_skills"] == [{"skill_id": "first", "version": 1}]
