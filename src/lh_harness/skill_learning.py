"""Prompt-driven, category-level Skill learning for the role-managed harness.

The bank is opt-in. Candidate validation requires a caller supplied replay that
restores the original task environment; this module never guesses that a live
workspace can safely be replayed in place.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapters.base import AgentAdapter
from .environment.base import Environment
from .types import EpisodeBudget, EpisodeResult
from .agent_logs import visible_output
from .auditor_agent import parse_audit_report
from .supervisor.control_bus import _atomic_bytes_write, _ensure_dir_nofollow
from .skill_prompt_texts import (
    ROUTER_PROMPT_EN,
    SUCCESS_SUMMARIZER_PROMPT_EN,
    FAILURE_DIAGNOSIS_PROMPT_EN,
    ROUTER_REPAIR_PROMPT_EN,
    SKILL_BUILDER_PROMPT_EN,
)

ROLES = {"manager", "gui_executor", "cli_executor", "gui_auditor", "cli_auditor"}
VIEWS = {"manager": "manager", "gui_executor": "executor", "cli_executor": "executor", "gui_auditor": "auditor", "cli_auditor": "auditor"}
FIELDS = ("input_use", "output_spec", "guidance", "avoid")
BENCHMARK_SCOPES = {"shared", "generic", "weavebench", "osworld_v2", "terminal_bench_2_1"}
INTERACTION_MODES = {"gui", "cli", "hybrid"}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
NON_ENGLISH_SCRIPT_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
logger = logging.getLogger(__name__)

ROUTER_PROMPTS = {"en": ROUTER_PROMPT_EN}
SUMMARY_PROMPTS = {"en": SUCCESS_SUMMARIZER_PROMPT_EN}
DIAGNOSIS_PROMPTS = {"en": FAILURE_DIAGNOSIS_PROMPT_EN}
BUILDER_PROMPTS = {"en": SKILL_BUILDER_PROMPT_EN}
REPAIR_PROMPTS = {"en": ROUTER_REPAIR_PROMPT_EN}


def parse_json_result(result: EpisodeResult) -> dict[str, Any]:
    if result.status != "done":
        raise ValueError(f"model episode status: {result.status}")
    raw = str(result.metadata.get("assistant_visible_output") or visible_output(result.actions_log)).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("model output must be a JSON object")
    return value


async def model_json(agent: AgentAdapter, env: Environment, budget: EpisodeBudget, system: str, payload: dict[str, Any]) -> tuple[dict[str, Any], EpisodeResult]:
    prompt = system + "\n\nINPUT JSON:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    result = await agent.run_episode(prompt, env, budget)
    return parse_json_result(result), result


def validate_skill(skill: dict[str, Any], *, language: str, candidate: bool = False) -> None:
    if language != "en":
        raise ValueError("Skill learning supports English only")
    if not isinstance(skill, dict) or not ID_RE.fullmatch(str(skill.get("skill_id", ""))):
        raise ValueError("invalid skill_id")
    if not ID_RE.fullmatch(str(skill.get("category_id", ""))):
        raise ValueError("invalid category_id")
    if type(skill.get("version")) is not int or skill["version"] < 1:
        raise ValueError("invalid Skill version")
    if skill.get("content_language") != "en":
        raise ValueError("Skill language mismatch")
    if skill.get("benchmark_scope") not in BENCHMARK_SCOPES:
        raise ValueError("invalid Skill benchmark scope")
    if (not isinstance(skill.get("interaction_modes"), list)
            or not skill["interaction_modes"]
            or any(mode not in INTERACTION_MODES for mode in skill["interaction_modes"])):
        raise ValueError("invalid Skill interaction modes")
    if skill.get("status") not in ({"candidate"} if candidate else {"validated", "verified"}):
        raise ValueError("invalid Skill status")
    for name in ("problem_mechanism",):
        if not isinstance(skill.get(name), str) or not skill[name].strip():
            raise ValueError(f"missing {name}")
        if NON_ENGLISH_SCRIPT_RE.search(skill[name]):
            raise ValueError(f"{name} must be in English")
    for name in ("subcases", "triggers", "exclusions", "compatible_with", "conflicts_with"):
        if not isinstance(skill.get(name), list) or any(not isinstance(v, str) for v in skill[name]):
            raise ValueError(f"invalid {name}")
        if any(NON_ENGLISH_SCRIPT_RE.search(v) for v in skill[name]):
            raise ValueError(f"{name} must be in English")
    roles = skill.get("roles")
    if not isinstance(roles, dict) or set(roles) != {"manager", "executor", "auditor"}:
        raise ValueError("Skill needs all three role views")
    for view in roles.values():
        if not isinstance(view, dict) or any(not isinstance(view.get(f), str) or not view[f].strip() for f in FIELDS):
            raise ValueError("incomplete role view")
        if any(NON_ENGLISH_SCRIPT_RE.search(view[field_name]) for field_name in FIELDS):
            raise ValueError("Skill role guidance must be in English")
    if not isinstance(skill.get("provenance"), dict) or not isinstance(skill.get("validation"), dict):
        raise ValueError("Skill needs provenance and validation records")
    if candidate and not _blank_validation(skill["validation"]):
        raise ValueError("candidate cannot self-validate")


def _blank_validation(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_blank_validation(item) for item in value.values())
    if isinstance(value, list):
        return not value
    return value is None


def candidate_from_success_spec(
    spec: dict[str, Any], *, benchmark_id: str, interaction_mode: str,
    task_id: str, run_id: str, evidence_refs: list[str],
) -> dict[str, Any]:
    """Wrap the summarizer's complete role guidance without another model call."""
    category = spec.get("category_id")
    mechanism = spec.get("problem_mechanism")
    if not isinstance(category, str) or not ID_RE.fullmatch(category):
        raise ValueError("invalid success Skill category")
    if not isinstance(mechanism, str) or not mechanism.strip():
        raise ValueError("missing success Skill mechanism")
    digest = hashlib.sha256((category + "\n" + mechanism).encode("utf-8")).hexdigest()[:8]
    candidate = {
        "skill_id": f"{category[:70]}_{digest}", "category_id": category,
        "version": 1, "version_parent": None, "status": "candidate",
        "content_language": "en", "benchmark_scope": benchmark_id,
        "interaction_modes": [interaction_mode],
        "problem_mechanism": mechanism,
        "subcases": spec.get("subcases", []),
        "triggers": spec.get("triggers", []),
        "exclusions": spec.get("exclusions", []),
        "roles": spec.get("roles"),
        "compatible_with": [], "conflicts_with": [],
        "provenance": {"task_id": task_id, "run_id": run_id, "evidence_refs": evidence_refs},
        "validation": {"baseline_success": None, "candidate_success": None, "reward_delta": None, "hard_constraint_regression": None},
    }
    validate_skill(candidate, language="en", candidate=True)
    return candidate


def compact_skill_cards(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("skill_id", "category_id", "version", "status", "benchmark_scope", "interaction_modes", "problem_mechanism", "triggers", "exclusions")
    return [{key: skill[key] for key in fields if key in skill} for skill in skills]


def compact_trajectory(report: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    rounds = [
        {
            "round_id": f"round_{item.get('round_index', 0):03d}",
            "route": item.get("next_step"),
            "manager_plan": str(item.get("plan_text") or "")[:1600],
            "executor_output": str(item.get("executor_output") or "")[:2400],
            "auditor_report": str(item.get("auditor_report") or "")[:2400],
            "harness_feedback": str(item.get("harness_feedback") or "")[:1200],
        }
        for item in report.get("rounds", [])
        if isinstance(item, dict)
    ]
    routes = [
        {key: record.get(key) for key in ("round", "role", "router_input", "router_output", "selected_skills", "injected_context")}
        for record in records
    ]
    final = {key: value for key, value in report.items() if key not in ("rounds", "final_response")}
    return {"final_report": final, "rounds": rounds, "router_inputs_and_outputs": routes}


@dataclass
class SkillBank:
    path: Path
    skills: list[dict[str, Any]] = field(default_factory=list)
    router_prompts: dict[str, str] = field(default_factory=lambda: dict(ROUTER_PROMPTS))
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "SkillBank":
        path = Path(path)
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
            raise ValueError("invalid SkillBank")
        stored_prompts = data.get("router_prompts", {})
        if not isinstance(stored_prompts, dict) or set(stored_prompts) - {"en"}:
            raise ValueError("SkillBank Router Prompt must be English only")
        bank = cls(path, data["skills"], {**ROUTER_PROMPTS, **stored_prompts}, data.get("history", []))
        if not isinstance(bank.history, list):
            raise ValueError("invalid SkillBank history")
        for entry in bank.history:
            if not isinstance(entry, dict):
                raise ValueError("invalid SkillBank history entry")
            if entry.get("kind") == "router":
                if entry.get("language") != "en" or not isinstance(entry.get("prompt"), str) or NON_ENGLISH_SCRIPT_RE.search(entry["prompt"]):
                    raise ValueError("SkillBank history must be English only")
            elif entry.get("kind") == "skill":
                validate_skill(entry.get("skill"), language="en")
            else:
                raise ValueError("invalid SkillBank history entry")
        if any(not isinstance(value, str) or not value.strip() for value in bank.router_prompts.values()):
            raise ValueError("invalid Router Prompt")
        if NON_ENGLISH_SCRIPT_RE.search(bank.router_prompts["en"]):
            raise ValueError("Router Prompt must be in English")
        ids: set[str] = set()
        for skill in bank.skills:
            validate_skill(skill, language=skill.get("content_language", ""))
            if skill["skill_id"] in ids:
                raise ValueError("duplicate Skill ID")
            ids.add(skill["skill_id"])
        return bank

    def save(self) -> None:
        if set(self.router_prompts) != {"en"} or NON_ENGLISH_SCRIPT_RE.search(self.router_prompts["en"]):
            raise ValueError("SkillBank Router Prompt must be English only")
        for skill in self.skills:
            validate_skill(skill, language="en")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "skills": self.skills, "router_prompts": self.router_prompts, "history": self.history}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def eligible(
        self, role: str, language: str, candidate: dict[str, Any] | None = None,
        *, benchmark_id: str = "generic", interaction_mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        if language != "en":
            raise ValueError("Skill routing supports English only")
        if role not in ROLES:
            raise ValueError("unknown role")
        result = []
        role_mode = "gui" if role.startswith("gui_") else "cli" if role.startswith("cli_") else interaction_mode
        active = [s for s in self.skills if candidate is None or s["skill_id"] != candidate["skill_id"]]
        for skill in [*active, *([candidate] if candidate else [])]:
            if skill["content_language"] != language or skill["status"] not in ({"validated", "verified", "candidate"} if candidate else {"validated", "verified"}):
                continue
            if skill["benchmark_scope"] not in ("shared", benchmark_id):
                continue
            if not (
                role == "manager" and interaction_mode == "hybrid"
                or role_mode in skill["interaction_modes"]
                or interaction_mode in skill["interaction_modes"]
            ):
                continue
            # Router sees an index card. The full role view is loaded only
            # after it selects this exact ID and version.
            result.append({key: skill[key] for key in (
                "skill_id", "category_id", "version", "status", "content_language",
                "problem_mechanism", "triggers", "exclusions", "compatible_with",
                "conflicts_with", "benchmark_scope", "interaction_modes",
            )} | {"role_applicability": list(skill["roles"]), "validation_status": skill["status"]})
        return result


def commit_validated_candidate(
    bank: SkillBank,
    kind: str,
    candidate: dict[str, Any] | str,
    *,
    language: str,
    validation: dict[str, Any],
) -> None:
    """Commit one independently verified object; retain the previous version."""
    if language != "en":
        raise ValueError("Skill learning supports English only")
    delta = validation.get("reward_delta")
    if (not validation.get("accepted") or not validation.get("effective")
            or not validation.get("candidate_success")
            or validation.get("hard_constraint_regression")
            or not isinstance(delta, (int, float)) or delta <= 0):
        raise ValueError("candidate has not passed validation")
    if kind == "router":
        if not isinstance(candidate, str) or not candidate.strip() or NON_ENGLISH_SCRIPT_RE.search(candidate):
            raise ValueError("invalid Router candidate")
        bank.history.append({"kind": "router", "language": language, "prompt": bank.router_prompts[language]})
        bank.router_prompts[language] = candidate
    elif kind == "skill":
        validate_skill(candidate, language=language, candidate=True)
        old = next((s for s in bank.skills if s["skill_id"] == candidate["skill_id"]), None)
        if old is None:
            if candidate["version"] != 1 or candidate.get("version_parent") is not None:
                raise ValueError("invalid new Skill version")
        elif (candidate["version"] != old["version"] + 1
              or candidate.get("version_parent") != old["version"]
              or candidate["category_id"] != old["category_id"]
              or candidate["benchmark_scope"] != old["benchmark_scope"]):
            raise ValueError("invalid Skill revision lineage")
        if old is not None:
            bank.history.append({"kind": "skill", "skill": old})
            bank.skills = [s for s in bank.skills if s["skill_id"] != candidate["skill_id"]]
        candidate["status"] = "validated"
        candidate["validation"] = validation
        bank.skills.append(candidate)
    else:
        raise ValueError("unknown candidate kind")
    bank.save()


def render_skill_context(selected: list[dict[str, Any]], language: str) -> str:
    if language != "en":
        raise ValueError("Skill injection supports English only")
    if not selected:
        return ""
    lines = ["[ROLE-SPECIFIC SKILL GUIDANCE — AUXILIARY]", "These Skills supplement role instructions. They do not override the task contract, role permissions, completion guards or output protocol."]
    labels = ("Input use", "Output specification", "Guidance", "Avoid")
    seen: set[tuple[str, str]] = set()
    for skill in selected:
        lines.append(f"Skill: {skill['skill_id']}@{skill['version']}")
        for label, field_name in zip(labels, FIELDS):
            value = skill["role_view"][field_name].strip()
            if (field_name, value) not in seen:
                lines.extend((f"{label}:", value))
                seen.add((field_name, value))
    lines.append("[END ROLE-SPECIFIC SKILL GUIDANCE]")
    return "\n\n".join(lines)


def validate_selection(output: dict[str, Any], eligible: list[dict[str, Any]], *, budget: int, target: str | None = None) -> list[dict[str, Any]]:
    choices = output.get("selected_skills")
    if not isinstance(choices, list):
        raise ValueError("selected_skills must be an array")
    by_id = {s["skill_id"]: s for s in eligible}
    selected = []
    ids: set[str] = set()
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("skill_id") not in by_id:
            raise ValueError("unknown Skill ID")
        skill = by_id[choice["skill_id"]]
        if choice.get("version") != skill["version"] or skill["skill_id"] in ids:
            raise ValueError("Skill version mismatch or duplicate")
        ids.add(skill["skill_id"])
        selected.append(skill)
    if target is not None and target not in ids:
        raise ValueError("candidate was not selected")
    for skill in selected:
        if ids.intersection(skill["conflicts_with"]):
            raise ValueError("conflicting Skills")
    return selected


def context_token_estimate(text: str) -> int:
    """Conservative admission estimate, never used for task reward."""
    return max((len(text) + 3) // 4, (len(text.encode("utf-8")) + 2) // 3)


def token_usage(result: EpisodeResult) -> int | None:
    """Only provider reported usage counts; no character-based substitute."""
    sources = [result.metadata, result.metadata.get("usage")]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("total_tokens", "totalTokens"):
            value = source.get(key)
            if type(value) is int and value >= 0:
                return value
        inp = source.get("input_tokens", source.get("inputTokens"))
        out = source.get("output_tokens", source.get("outputTokens"))
        if type(inp) is int and type(out) is int and inp >= 0 and out >= 0:
            return inp + out
        # Harbor's Claude Code bridge already includes cached input in
        # n_input_tokens, so do not add n_cache_tokens again.
        inp, out = source.get("n_input_tokens"), source.get("n_output_tokens")
        if type(inp) is int and type(out) is int and inp >= 0 and out >= 0:
            return inp + out
    # CLI adapters preserve provider JSONL in actions_log. Parse only known
    # provider usage envelopes; never infer tokens from text length.
    codex_turns: list[int] = []
    opencode_steps: list[int] = []
    opencode_ids: set[str] = set()
    for line in result.actions_log.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "turn.completed":
            usage = record.get("usage", {})
            if isinstance(usage, dict) and type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
                codex_turns.append(usage["input_tokens"] + usage["output_tokens"])
        if record.get("type") == "result" and isinstance(record.get("usage"), dict):
            usage = record["usage"]
            if type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
                return usage["input_tokens"] + usage["output_tokens"]
        if record.get("type") == "step_finish" and isinstance(record.get("part"), dict):
            part = record["part"]
            step_id = part.get("id")
            tokens = part.get("tokens")
            if isinstance(tokens, dict) and isinstance(step_id, str) and step_id not in opencode_ids:
                total = tokens.get("total")
                inp, out = tokens.get("input"), tokens.get("output")
                if type(total) is int and total >= 0:
                    opencode_steps.append(total)
                    opencode_ids.add(step_id)
                elif type(inp) is int and type(out) is int and inp >= 0 and out >= 0:
                    opencode_steps.append(inp + out)
                    opencode_ids.add(step_id)
    if codex_turns:
        return sum(codex_turns)
    if opencode_steps:
        return sum(opencode_steps)
    return None


def task_success(report: dict[str, Any], language: str = "en") -> bool:
    if not (report.get("completion_satisfied") and report.get("status") == "complete"):
        return False
    text = report.get("latest_auditor_report")
    if not isinstance(text, str) or not text.strip():
        return False
    audit = parse_audit_report(text, int(report.get("rounds_run") or 0), language=language)
    return audit.status == "complete" and audit.integrity_status == "clean" and audit.contract_audit_status == "aligned"


def task_reward(report: dict[str, Any], tokens: int | None, *, reference_tokens: int, beta: float = 0.1, language: str = "en") -> float | None:
    if type(tokens) is not int or tokens < 0 or reference_tokens <= 0:
        return None
    success = task_success(report, language)
    return float(success) - beta * min(1.0, tokens / reference_tokens)


def accept_candidate(baseline: dict[str, Any], candidate: dict[str, Any], *, baseline_tokens: int | None, candidate_tokens: int | None, reference_tokens: int, effective: bool, hard_constraint_regression: bool, language: str = "en") -> tuple[bool, float | None]:
    base = task_reward(baseline, baseline_tokens, reference_tokens=reference_tokens, language=language)
    cand = task_reward(candidate, candidate_tokens, reference_tokens=reference_tokens, language=language)
    delta = cand - base if base is not None and cand is not None else None
    accepted = bool(effective and task_success(candidate, language) and not hard_constraint_regression and delta is not None and delta > 0)
    return accepted, delta

Replay = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class SkillSession:
    bank: SkillBank
    language: str
    agent: AgentAdapter
    env: Environment
    budget: EpisodeBudget
    log_dir: Path
    token_budget: int = 2000
    candidate: dict[str, Any] | None = None
    router_prompt_override: str | None = None
    benchmark_id: str = "generic"
    interaction_mode: str = "hybrid"
    dataset_id: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)
    token_counts: list[int | None] = field(default_factory=list)
    bank_snapshot: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.language != "en":
            raise ValueError("Skill sessions support English prompts only")
        if self.benchmark_id not in BENCHMARK_SCOPES - {"shared"} or self.interaction_mode not in INTERACTION_MODES:
            raise ValueError("invalid Skill routing context")
        self.bank_snapshot = copy.deepcopy(self.bank.skills)

    async def route(self, role: str, base_prompt: str, round_index: int, routing_brief: dict[str, Any] | None = None) -> str:
        brief = routing_brief or {"task_goal": base_prompt[:2000], "current_state": "", "recent_actions": [], "evidence": [], "task_status": "running"}
        eligible = self.bank.eligible(
            role, self.language, self.candidate,
            benchmark_id=self.benchmark_id, interaction_mode=self.interaction_mode,
        )
        if not eligible:
            self.records.append({"round": round_index, "role": role, "role_base_prompt": base_prompt, "router_input": {"benchmark_id": self.benchmark_id, "dataset_id": self.dataset_id, "interaction_mode": self.interaction_mode, "routing_brief": brief, "eligible_skill_cards": []}, "router_skipped": "no_eligible_skill_cards", "selected_skills": [], "injected_context": ""})
            self._save_records()
            return ""
        target = self.candidate["skill_id"] if self.candidate else None
        payload = {
            "mode": "candidate_validation" if target else "production",
            "prompt_language": self.language,
            "role": role,
            "benchmark_id": self.benchmark_id,
            "dataset_id": self.dataset_id,
            "interaction_mode": self.interaction_mode,
            "routing_brief": brief,
            "eligible_skill_cards": eligible,
            "validation_target_skill_id": target,
            "token_budget": self.token_budget,
            "validation_history": [
                {"skill_id": s["skill_id"], "status": s["status"]} for s in eligible
            ],
        }
        record: dict[str, Any] = {"round": round_index, "role": role, "role_base_prompt": base_prompt, "router_input": payload, "selected_skills": [], "injected_context": ""}
        try:
            router_prompt = (self.router_prompt_override or self.bank.router_prompts[self.language]) + "\n\nINPUT JSON:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
            result = await self.agent.run_episode(router_prompt, self.env, self.budget)
            self.token_counts.append(token_usage(result))
            record["router_raw_output"] = str(result.metadata.get("assistant_visible_output") or visible_output(result.actions_log))
            output = parse_json_result(result)
            record["router_output"] = output
            selected_cards = validate_selection(output, eligible, budget=self.token_budget, target=target)
            active = {skill["skill_id"]: skill for skill in [*self.bank.skills, *([self.candidate] if self.candidate else [])]}
            selected = [
                {"skill_id": card["skill_id"], "version": card["version"], "role_view": active[card["skill_id"]]["roles"][VIEWS[role]]}
                for card in selected_cards
            ]
            context = render_skill_context(selected, self.language)
            if context_token_estimate(context) > self.token_budget:
                raise ValueError("Skill token budget exceeded")
            record["selected_skills"] = [{"skill_id": s["skill_id"], "version": s["version"]} for s in selected]
            record["injected_context"] = context
            record["injected_context_token_estimate"] = context_token_estimate(context)
            return context
        except Exception as exc:
            record["router_error"] = str(exc)
            # During validation a failed injection makes the replay ineffective.
            return ""
        finally:
            self.records.append(record)
            self._save_records()

    def record_episode(self, result: EpisodeResult) -> None:
        self.token_counts.append(token_usage(result))

    def record_injected_prompt(self, role: str, round_index: int, prompt: str) -> None:
        for record in reversed(self.records):
            if record["role"] == role and record["round"] == round_index:
                record["actual_role_prompt"] = prompt
                self._save_records()
                return

    def total_tokens(self) -> int | None:
        return sum(self.token_counts) if all(v is not None for v in self.token_counts) else None

    def _save_records(self) -> None:
        try:
            _ensure_dir_nofollow(self.log_dir)
            _atomic_bytes_write(self.log_dir / "skill_routes.json", (json.dumps(self.records, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except OSError:
            logger.warning("Could not save Skill routing records", exc_info=True)

    def _save_candidate(self, kind: str, candidate: Any, assessment: dict[str, Any], validation: Any) -> None:
        _ensure_dir_nofollow(self.log_dir)
        payload = {"kind": kind, "candidate": candidate, "assessment": assessment, "validation": validation}
        _atomic_bytes_write(self.log_dir / "skill_candidate.json", (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    async def finish(self, report: dict[str, Any], *, replay: Replay | None = None, reference_tokens: int = 100_000) -> dict[str, Any]:
        """Learn one object; commit only after a caller-controlled full-task replay.

        replay receives {kind, candidate, mode}. It must restore the original
        workspace/environment and return the complete run report, token count,
        effective flag and hard_constraint_regression flag.
        """
        self._save_records()
        status = {"task_tokens": self.total_tokens(), "token_usage_missing": self.total_tokens() is None, "action": "NO_CHANGE", "validation": "not_requested"}
        # A cancelled, blocked, timed-out or runtime-failed run is not a
        # diagnosable Skill failure.
        success = task_success(report, self.language)
        diagnosable = report.get("status") == "incomplete" and report.get("abort_reason") in ("", "max_rounds_exhausted")
        if not success and not diagnosable:
            return status
        task_id = hashlib.sha256((self.dataset_id + "\n" + str(report.get("task", ""))).encode("utf-8")).hexdigest()
        if success:
            injected = {item["skill_id"] for record in self.records if record.get("injected_context") for item in record["selected_skills"]}
            changed = False
            for skill in self.bank.skills:
                if skill["skill_id"] not in injected:
                    continue
                if skill.get("provenance", {}).get("task_id") == task_id:
                    continue
                # These are observational task outcomes. Without a comparable
                # cached baseline they do not establish causal benefit.
                evidence = skill.setdefault("validation", {}).setdefault("observational_task_ids", [])
                if task_id not in evidence:
                    evidence.append(task_id)
                    changed = True
            if changed:
                self.bank.save()
        trajectory = compact_trajectory(report, self.records)
        relevant_skills = [
            skill for skill in self.bank_snapshot
            if skill["benchmark_scope"] in ("shared", self.benchmark_id)
            and (self.interaction_mode == "hybrid" or self.interaction_mode in skill["interaction_modes"])
        ]
        skill_index = compact_skill_cards(relevant_skills)
        assessment_input = (
            {
                "benchmark_id_and_interaction_mode": {"benchmark_id": self.benchmark_id, "interaction_mode": self.interaction_mode},
                "trajectory_batch": [{
                    "task_id": task_id, "task": report.get("task"),
                    "final_report": trajectory["final_report"],
                    "compact_trajectory_summary": trajectory["rounds"],
                    "key_evidence_excerpts": trajectory["rounds"],
                    "selected_skill_ids_versions_and_injections": trajectory["router_inputs_and_outputs"],
                }],
                "relevant_skill_cards": skill_index,
                "language": "en",
            }
            if success else {
                "original_task": report.get("task"),
                "final_report": trajectory["final_report"],
                "relevant_trajectory_evidence": trajectory["rounds"],
                "router_inputs_and_outputs": trajectory["router_inputs_and_outputs"],
                "actual_skill_injections": [
                    {key: record.get(key) for key in ("round", "role", "selected_skills", "injected_context")}
                    for record in self.records if record.get("injected_context")
                ],
                "skillbank_index_at_run_start": skill_index,
                "runtime_and_environment_errors": report.get("failure_reason") or "",
            }
        )
        prompt = SUMMARY_PROMPTS[self.language] if success else DIAGNOSIS_PROMPTS[self.language]
        try:
            assessment, _ = await model_json(self.agent, self.env, self.budget, prompt, assessment_input)
        except Exception as exc:
            status["learning_error"] = str(exc)
            return status
        status["assessment"] = assessment
        action = assessment.get("action")
        allowed = {"NO_NEW_SKILL", "CREATE_NEW_SKILL"} if success else {"UPDATE_ROUTER_PROMPT", "CREATE_NEW_SKILL", "REVISE_SKILL", "NO_CHANGE", "INSUFFICIENT_EVIDENCE"}
        if action not in allowed:
            status["learning_error"] = "invalid assessment action"
            return status
        status["action"] = action
        if action not in {"UPDATE_ROUTER_PROMPT", "CREATE_NEW_SKILL", "REVISE_SKILL"}:
            return status
        if not isinstance(assessment.get("evidence_refs"), list) or not assessment["evidence_refs"]:
            status["learning_error"] = "update needs concrete evidence references"
            return status
        if success and action == "CREATE_NEW_SKILL" and not isinstance(assessment.get("candidate_spec"), dict):
            status["learning_error"] = "successful-task candidate needs a specification"
            return status
        target_old = next((s for s in self.bank.skills if s["skill_id"] == assessment.get("target_skill_id")), None)
        actually_injected = any(
            record.get("injected_context") and any(item["skill_id"] == assessment.get("target_skill_id") for item in record["selected_skills"])
            for record in self.records
        )
        if action == "REVISE_SKILL" and (target_old is None or not actually_injected):
            status["learning_error"] = "revision target was not injected"
            return status
        if action == "UPDATE_ROUTER_PROMPT":
            if not any(record.get("router_output") for record in self.records):
                status["learning_error"] = "no Router selection to repair"
                return status
            if assessment.get("router_error_type") == "candidate_filtering":
                status["learning_error"] = "candidate filtering is not a Router selection error"
                return status
            system = REPAIR_PROMPTS[self.language]
            payload = {
                "old_router_prompt": self.bank.router_prompts[self.language],
                "diagnosis": assessment,
                "router_input_and_output": trajectory["router_inputs_and_outputs"],
                "eligible_skill_cards": [record.get("router_input", {}).get("eligible_skill_cards", []) for record in self.records],
                "skillbank_index_at_run_start": skill_index,
                "language": "en",
            }
        else:
            system = BUILDER_PROMPTS[self.language]
            payload = {
                "update_action": action, "diagnosis_or_success_summary": assessment,
                "trajectory_evidence": trajectory, "existing_target_skill": target_old,
                "benchmark_id_and_interaction_mode": {"benchmark_id": self.benchmark_id, "interaction_mode": self.interaction_mode},
                "skillbank_snapshot": skill_index, "content_language": "en",
                "benchmark_scope": self.benchmark_id,
                "interaction_modes": [self.interaction_mode],
            }
        try:
            if success:
                candidate = candidate_from_success_spec(
                    assessment["candidate_spec"], benchmark_id=self.benchmark_id,
                    interaction_mode=self.interaction_mode,
                    task_id=task_id, run_id=str(self.log_dir),
                    evidence_refs=assessment["evidence_refs"],
                )
                proposal = {"action": action, "skill": candidate}
            else:
                proposal, _ = await model_json(self.agent, self.env, self.budget, system, payload)
            if proposal.get("action") != action:
                raise ValueError("proposal action mismatch")
            if action == "UPDATE_ROUTER_PROMPT":
                candidate = proposal.get("candidate_router_prompt")
                if (not isinstance(candidate, str) or not candidate.strip()
                        or NON_ENGLISH_SCRIPT_RE.search(candidate)
                        or candidate == self.bank.router_prompts[self.language]):
                    raise ValueError("invalid Router revision")
                kind = "router"
            else:
                candidate = proposal.get("skill")
                validate_skill(candidate, language=self.language, candidate=True)
                if action == "CREATE_NEW_SKILL" and candidate["benchmark_scope"] != self.benchmark_id:
                    raise ValueError("new Skill must keep its source benchmark scope")
                if action == "CREATE_NEW_SKILL" and any(s["skill_id"] == candidate["skill_id"] for s in self.bank.skills):
                    raise ValueError("new Skill ID already exists")
                if action == "CREATE_NEW_SKILL" and (candidate["version"] != 1 or candidate.get("version_parent") is not None):
                    raise ValueError("new Skill must start at version 1")
                if action == "REVISE_SKILL" and (candidate["skill_id"] != target_old["skill_id"] or candidate["category_id"] != target_old["category_id"] or candidate["version"] != target_old["version"] + 1 or candidate.get("version_parent") != target_old["version"]):
                    raise ValueError("invalid Skill version lineage")
                if action == "REVISE_SKILL" and candidate["benchmark_scope"] != target_old["benchmark_scope"]:
                    raise ValueError("Skill revision cannot change benchmark scope")
                kind = "skill"
        except Exception as exc:
            status["learning_error"] = str(exc)
            return status
        status["candidate"] = candidate
        status["validation"] = "pending_replay"
        self._save_candidate(kind, candidate, assessment, status["validation"])
        if replay is None:
            return status
        try:
            replay_result = await replay({"kind": kind, "candidate": candidate, "mode": "candidate_validation" if kind == "skill" else "production"})
            if not isinstance(replay_result, dict) or type(replay_result.get("effective")) is not bool or type(replay_result.get("hard_constraint_regression")) is not bool:
                raise ValueError("replay must report effective and hard_constraint_regression as booleans")
            candidate_report = replay_result["report"]
            if not isinstance(candidate_report, dict):
                raise ValueError("replay must return a complete report")
            effective = bool(replay_result.get("effective"))
            # Skill replay must prove actual target injection, not just Router selection.
            if kind == "skill":
                effective = effective and any(candidate["skill_id"] == s.get("skill_id") for rec in replay_result.get("skill_routes", []) for s in rec.get("selected_skills", []) if rec.get("injected_context"))
            accepted, delta = accept_candidate(report, candidate_report, baseline_tokens=self.total_tokens(), candidate_tokens=replay_result.get("task_tokens"), reference_tokens=reference_tokens, effective=effective, hard_constraint_regression=bool(replay_result.get("hard_constraint_regression")), language=self.language)
            status["validation"] = {
                "accepted": accepted, "reward_delta": delta, "effective": effective,
                "baseline_success": task_success(report, self.language),
                "candidate_success": task_success(candidate_report, self.language),
                "baseline_tokens": self.total_tokens(),
                "candidate_tokens": replay_result.get("task_tokens"),
                "hard_constraint_regression": replay_result["hard_constraint_regression"],
            }
            if accepted:
                commit_validated_candidate(self.bank, kind, candidate, language=self.language, validation=status["validation"])
            elif kind == "skill":
                candidate["status"] = "rejected"
            self._save_candidate(kind, candidate, assessment, status["validation"])
            return status
        except Exception as exc:
            status["validation"] = {"accepted": False, "error": str(exc)}
            if kind == "skill":
                candidate["status"] = "rejected"
            self._save_candidate(kind, candidate, assessment, status["validation"])
            return status
