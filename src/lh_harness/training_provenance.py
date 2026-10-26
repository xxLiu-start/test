"""Append-only, view-only provenance for Terminal-Bench Skill training.

Nothing in the training or evaluation decision path reads these records.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .role_prompts import (
    LH_HARNESS_CLI_AUDITOR_INSTRUCTIONS,
    LH_HARNESS_CLI_EXECUTOR_INSTRUCTIONS,
    LH_HARNESS_GUI_AUDITOR_INSTRUCTIONS,
    LH_HARNESS_GUI_EXECUTOR_INSTRUCTIONS,
    LH_HARNESS_MANAGER_INSTRUCTIONS,
)
from .skill_learning import SkillBank
from .skill_prompt_texts import (
    FAILURE_DIAGNOSIS_PROMPT_EN,
    ROUTER_PROMPT_EN,
    ROUTER_REPAIR_PROMPT_EN,
    SKILL_BUILDER_PROMPT_EN,
    SUCCESS_SUMMARIZER_PROMPT_EN,
)

logger = logging.getLogger(__name__)

STATIC_PROMPTS = {
    "role_manager": LH_HARNESS_MANAGER_INSTRUCTIONS,
    "role_cli_executor": LH_HARNESS_CLI_EXECUTOR_INSTRUCTIONS,
    "role_gui_executor": LH_HARNESS_GUI_EXECUTOR_INSTRUCTIONS,
    "role_cli_auditor": LH_HARNESS_CLI_AUDITOR_INSTRUCTIONS,
    "role_gui_auditor": LH_HARNESS_GUI_AUDITOR_INSTRUCTIONS,
    "skill_router_default": ROUTER_PROMPT_EN,
    "skill_success_summarizer": SUCCESS_SUMMARIZER_PROMPT_EN,
    "skill_failure_diagnosis": FAILURE_DIAGNOSIS_PROMPT_EN,
    "skill_router_repair": ROUTER_REPAIR_PROMPT_EN,
    "skill_builder": SKILL_BUILDER_PROMPT_EN,
}


def _version(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProvenanceWriter:
    def __init__(self, path: Path, *, task: str, run_id: str) -> None:
        self.path = path
        self.task = task
        self.run_id = run_id

    def append(self, event: str, **fields: Any) -> None:
        record = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task": self.task,
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("Could not write view-only provenance %s: %s", self.path, exc)

    def static_prompts(self) -> None:
        for name, content in STATIC_PROMPTS.items():
            self.append(
                "prompt_version", stage="run_start", source="code_template",
                prompt_name=name, language="en", version_id=_version(content), content=content,
            )

    def bank_snapshot(self, stage: str, bank: SkillBank) -> None:
        self.append("bank_snapshot", stage=stage, bank_path=str(bank.path),
                    active_skill_count=len(bank.skills), history_count=len(bank.history))
        for skill in bank.skills:
            self.append(
                "skill_version", stage=stage, source="active_bank",
                skill_id=skill.get("skill_id"), version=skill.get("version"),
                content=skill,
            )
        for index, item in enumerate(bank.history):
            if item.get("kind") == "skill" and isinstance(item.get("skill"), dict):
                skill = item["skill"]
                self.append(
                    "skill_version", stage=stage, source="bank_history", history_index=index,
                    skill_id=skill.get("skill_id"), version=skill.get("version"), content=skill,
                )
        for language, content in bank.router_prompts.items():
            self.append(
                "prompt_version", stage=stage, source="active_router_prompt",
                prompt_name="skill_router", language=language,
                version_id=_version(content), content=content,
            )
        for index, item in enumerate(bank.history):
            if item.get("kind") == "router" and isinstance(item.get("prompt"), str):
                content = item["prompt"]
                self.append(
                    "prompt_version", stage=stage, source="bank_history", history_index=index,
                    prompt_name="skill_router", language=item.get("language"),
                    version_id=_version(content), content=content,
                )

    def bank_snapshot_file(self, stage: str, path: Path) -> None:
        """Archive a trial's disposable bank without affecting training decisions."""
        try:
            if path.is_file():
                self.bank_snapshot(stage, SkillBank.load(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not archive view-only SkillBank snapshot %s: %s", path, exc)

    def candidate(
        self, stage: str, proposal: dict[str, Any], *, source_path: Path,
        previous_router_prompt: str | None = None,
    ) -> None:
        kind = proposal.get("kind")
        content = proposal.get("candidate")
        self.append(
            "candidate_version", stage=stage, kind=kind,
            source_path=str(source_path),
            skill_id=content.get("skill_id") if isinstance(content, dict) else None,
            version=content.get("version") if isinstance(content, dict) else None,
            version_parent=content.get("version_parent") if isinstance(content, dict) else None,
            version_id=_version(content) if isinstance(content, str) else None,
            content=content, assessment=proposal.get("assessment"),
            validation=proposal.get("validation"),
        )
        if kind == "router" and isinstance(content, str):
            self.append(
                "prompt_version", stage=stage, source="router_candidate",
                prompt_name="skill_router", language="en", version_id=_version(content),
                previous_version_id=_version(previous_router_prompt) if previous_router_prompt else None,
                content=content,
            )

    def ingest_model_io(self, config_path: Path, jobs_dir: Path, *, phase: str) -> None:
        """Copy this trial's per-episode model records into the task run file."""
        if not config_path.is_file():
            return
        try:
            import yaml
            job_name = yaml.safe_load(config_path.read_text(encoding="utf-8"))["job_name"]
            job_dir = jobs_dir / job_name
            for model_io_path in sorted(job_dir.rglob("model_io.jsonl")):
                with model_io_path.open(encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            self.append("model_io_parse_error", phase=phase, source_path=str(model_io_path), line_number=line_number)
                            continue
                        self.append(
                            "model_io", phase=phase, source_path=str(model_io_path),
                            line_number=line_number, record=record,
                        )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Could not collect view-only model I/O for %s: %s", self.task, exc)
