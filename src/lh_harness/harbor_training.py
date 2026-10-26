"""Harbor adapter for SkillBank training on Terminal-Bench Pro.

The Terminal-Bench 2.1 evaluation adapter supplies Harbor's container and
Claude Code bridges. This adapter runs the current ``lh_harness.manager``
implementation; the training and test configs select the OpenAI tool bridge.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harbor.models.agent.context import AgentContext
from harbor.environments.base import BaseEnvironment
from harbor_agent import CuaHarnessClaudeCodeAgent
from harbor_backends import HarborEnvironmentAdapter

from .harbor_url import claude_code_upstream_base_url
from .manager import run
from .types import EpisodeBudget, HarnessConfig


class _RoleBridge:
    """Supply the older Harbor episode adapter's turn budget and signature."""

    def __init__(self, adapter: Any, max_turns: int) -> None:
        self.adapter = adapter
        self.max_turns = max_turns

    async def run_episode(
        self,
        prompt: str,
        env: HarborEnvironmentAdapter,
        budget: EpisodeBudget,
        live_trajectory_path: str | None = None,
    ) -> Any:
        del live_trajectory_path  # The Harbor adapter saves its own episode log.
        harbor_budget = SimpleNamespace(
            max_turns=self.max_turns,
            max_duration_seconds=budget.max_duration_seconds,
        )
        return await self.adapter.run_episode(prompt, env, harbor_budget)


class SkillLearningHarborAgent(CuaHarnessClaudeCodeAgent):
    """Train the main Skill-enabled MEA loop inside a Harbor task container."""

    def __init__(
        self,
        *args: Any,
        skill_bank_path: str,
        skill_token_budget: int = 2000,
        skill_reward_reference_tokens: int = 100_000,
        prompt_language: str = "en",
        learning_enabled: bool = True,
        candidate_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.skill_bank_path = str(Path(skill_bank_path).expanduser().resolve())
        self.skill_token_budget = int(skill_token_budget)
        self.skill_reward_reference_tokens = int(skill_reward_reference_tokens)
        self.learning_enabled = bool(learning_enabled)
        self.candidate_path = candidate_path
        if prompt_language != "en":
            raise ValueError("Skill training and evaluation require English prompts")
        self.prompt_language = prompt_language

    @staticmethod
    def name() -> str:
        return "lh-harness-skill-learning"

    def _resolve_base_url(self) -> str | None:
        return claude_code_upstream_base_url(super()._resolve_base_url())

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        output_dir = self.logs_dir / "skill_harness"
        output_dir.mkdir(parents=True, exist_ok=True)
        workspace_path, _ = await self._resolve_workspace_path(environment)
        env_adapter = HarborEnvironmentAdapter(environment, workspace_path=workspace_path)
        model = self._resolve_model()
        verifier_model = self._resolve_verifier_model(model)
        role_agents = self._make_role_agents(
            environment=environment,
            env_adapter=env_adapter,
            output_dir=output_dir,
            model=model,
            verifier_model=verifier_model,
            api_key=self._resolve_api_key(),
            base_url=self._resolve_base_url(),
            workspace_path=workspace_path,
            event_logger=None,
        )
        manager_agent = _RoleBridge(role_agents["orchestrator"], self._orchestrator_max_turns)
        executor_agent = _RoleBridge(role_agents["cli_task"], self._max_turns)
        auditor_agent = _RoleBridge(role_agents["cli_verifier"], self._verifier_max_turns)
        config = HarnessConfig(
            max_total_episodes=self._max_rounds,
            manager_budget=EpisodeBudget(max_duration_seconds=self._orchestrator_timeout_sec),
            cli_executor_budget=EpisodeBudget(max_duration_seconds=self._cli_task_timeout_sec),
            gui_executor_budget=EpisodeBudget(max_duration_seconds=self._cli_task_timeout_sec),
            auditor_budget=EpisodeBudget(max_duration_seconds=self._verifier_timeout_sec),
            workspace_path=workspace_path,
            harness_dir=f"{workspace_path.rstrip('/')}/.harness_skill",
            log_dir=str(output_dir / "lh_harness"),
            prompt_language=self.prompt_language,
            skill_bank_path=self.skill_bank_path,
            skill_token_budget=self.skill_token_budget,
            skill_reward_reference_tokens=self.skill_reward_reference_tokens,
            skill_update_enabled=self.learning_enabled,
            skill_benchmark_id="terminal_bench_2_1",
            skill_interaction_mode="cli",
            skill_dataset_id="terminal_bench_pro" if self.learning_enabled else "terminal_bench_2_1",
        )
        started = time.monotonic()
        candidate_skill = None
        candidate_router_prompt = None
        if self.candidate_path:
            proposal = json.loads(Path(self.candidate_path).read_text(encoding="utf-8"))
            if proposal.get("kind") == "skill":
                candidate_skill = proposal["candidate"]
            elif proposal.get("kind") == "router":
                candidate_router_prompt = proposal["candidate"]
            else:
                raise ValueError("candidate_path must contain a Skill or Router proposal")
        report = await run(
            task=instruction,
            env=env_adapter,
            config=config,
            manager_agent=manager_agent,
            cli_executor_agent=executor_agent,
            gui_executor_agent=executor_agent,
            cli_auditor_agent=auditor_agent,
            gui_auditor_agent=auditor_agent,
            auditor_format_repair_agent=auditor_agent,
            final_response_agent=manager_agent,
            skill_candidate=candidate_skill,
            skill_router_prompt_override=candidate_router_prompt,
        )
        usage = {
            key: sum(int(adapter.usage_totals.get(key) or 0) for adapter in role_agents.values())
            for key in ("n_input_tokens", "n_output_tokens", "n_cache_tokens")
        }
        context.n_input_tokens = usage["n_input_tokens"]
        context.n_output_tokens = usage["n_output_tokens"]
        context.n_cache_tokens = usage["n_cache_tokens"]
        context.metadata = {
            "variant": "lh_harness_skill_learning",
            "dataset_split": "train" if self.learning_enabled else "test",
            "skill_bank_path": self.skill_bank_path,
            "model": model,
            "verifier_model": verifier_model,
            "workspace_path": workspace_path,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "completion_satisfied": bool(report.get("completion_satisfied")),
            "status": report.get("status"),
            "skill_learning": report.get("skill_learning"),
            "usage_totals": usage,
            "report_path": str(output_dir / "lh_harness" / "report.json"),
        }
        (output_dir / "training_report.json").write_text(
            json.dumps({"report": report, "usage_totals": usage}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
