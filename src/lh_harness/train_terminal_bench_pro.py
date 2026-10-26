"""Sequential SkillBank training with paired fresh Harbor trials.

Each baseline/candidate pair uses the same Terminal-Bench Pro task. Harbor
creates a fresh Docker environment for each trial; the driver updates only the
SkillBank on the host after comparing complete task reports.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .auditor_agent import parse_audit_report
from .harbor_url import claude_code_upstream_base_url
from .skill_learning import SkillBank, accept_candidate, commit_validated_candidate, task_success
from .training_provenance import ProvenanceWriter


def discover_tasks(root: Path) -> list[str]:
    if not root.is_dir():
        raise ValueError(f"training dataset does not exist: {root}")
    return sorted(
        path.name for path in root.iterdir()
        if path.is_dir() and (path / "task.toml").is_file() and (path / "instruction.md").is_file()
    )


def _write_trial_config(
    template: dict[str, Any], *, task: str, phase: str,
    dataset: Path, bank: Path, jobs_dir: Path, config_path: Path,
    candidate_path: Path | None = None,
) -> None:
    import yaml

    config = copy.deepcopy(template)
    # Harbor reuses an existing job directory when job_name is repeated.
    # Give every baseline/candidate replay a fresh job so --force really runs
    # a new trial while keeping prior logs intact.
    config["job_name"] = f"skill-{task}-{phase}-{uuid.uuid4().hex[:8]}"
    config["jobs_dir"] = str(jobs_dir)
    config["n_attempts"] = 1
    config["n_concurrent_trials"] = 1
    config["datasets"] = [{"path": str(dataset), "task_names": [task]}]
    config["agents"][0].setdefault("env", {})["LH_HARNESS_MODEL_IO_TRACE"] = "1"
    kwargs = config["agents"][0]["kwargs"]
    if kwargs.get("prompt_language", "en") != "en":
        raise ValueError("Skill training requires English prompts")
    kwargs["prompt_language"] = "en"
    kwargs["skill_bank_path"] = str(bank)
    kwargs["learning_enabled"] = True
    if candidate_path is not None:
        kwargs["candidate_path"] = str(candidate_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _run_trial(config_path: Path, jobs_dir: Path) -> tuple[dict[str, Any], Path]:
    # Harbor may return zero even when the only trial raised an exception.
    # A forced rerun can also leave older reports under the same jobs_dir.
    previous_reports = {
        path: path.stat().st_mtime_ns for path in jobs_dir.rglob("training_report.json")
    }
    previous_exceptions = {
        path: path.stat().st_mtime_ns for path in jobs_dir.rglob("exception.txt")
    }
    # Harbor applies AgentConfig.env as a scoped environment that overrides
    # the role adapter's per-exec ANTHROPIC_BASE_URL. Normalize the value
    # before Harbor expands ${ANTHROPIC_BASE_URL} into that scoped environment.
    harbor_env = os.environ.copy()
    if harbor_env.get("ANTHROPIC_BASE_URL"):
        harbor_env["ANTHROPIC_BASE_URL"] = claude_code_upstream_base_url(harbor_env["ANTHROPIC_BASE_URL"])
    completed = subprocess.run(["harbor", "run", "-c", str(config_path)], check=False, env=harbor_env)
    reports = sorted(
        (
            path for path in jobs_dir.rglob("training_report.json")
            if path.stat().st_mtime_ns != previous_reports.get(path)
        ),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if completed.returncode != 0 or not reports:
        exceptions = sorted(
            (
                path for path in jobs_dir.rglob("exception.txt")
                if path.stat().st_mtime_ns != previous_exceptions.get(path)
            ),
            key=lambda path: path.stat().st_mtime_ns,
        )
        detail = ""
        if exceptions:
            exception_path = exceptions[-1]
            detail = f"\nHarbor exception: {exception_path}\n{exception_path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
        raise RuntimeError(
            f"Harbor trial failed (exit {completed.returncode}; fresh report: {bool(reports)}): {config_path}{detail}"
        )
    path = reports[-1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("report"), dict):
        raise ValueError(f"invalid training report: {path}")
    return payload["report"], path.parent


def _require_valid_trial_report(report: dict[str, Any], output_dir: Path, phase: str) -> None:
    # A gateway may return a successful HTTP response while serializing tool
    # calls into plain text. Claude Code cannot execute those calls, so this is
    # a provider/tool-protocol failure rather than a valid no-candidate trial.
    pseudo_tool_calls = any(
        isinstance(round_.get("executor_output"), str)
        and "<｜｜DSML｜｜ calls>" in round_["executor_output"]
        and "invoke name=" in round_["executor_output"]
        for round_ in report.get("rounds", []) if isinstance(round_, dict)
    )
    real_tool_calls = False
    if pseudo_tool_calls:
        for stream_path in (output_dir / "episodes" / "cli_task").glob("ep*/claude_stream.jsonl"):
            for line in stream_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message") if isinstance(record, dict) else None
                if isinstance(message, dict) and any(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in message.get("content", [])
                ):
                    real_tool_calls = True
                    break
            if real_tool_calls:
                break
    if pseudo_tool_calls and not real_tool_calls:
        raise RuntimeError(
            f"{phase} trial could not execute tools: the model returned DSML tool-call markup "
            f"as plain text rather than Anthropic tool_use blocks. "
            f"Check structured tool calling with `python training/check_model_endpoint.py --direct --tools`. "
            f"Report: {output_dir / 'lh_harness' / 'report.json'}"
        )
    if report.get("status") == "failed":
        reason = report.get("failure_reason") or report.get("abort_reason") or "unknown provider/runtime error"
        proxy_detail = ""
        proxy_logs = sorted(
            output_dir.rglob("request_proxy_requests.jsonl"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for proxy_log in proxy_logs:
            for line in reversed(proxy_log.read_text(encoding="utf-8", errors="replace").splitlines()):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event") in {"upstream_http_error", "upstream_error"}:
                    detail = str(event.get("body_head") or event.get("error") or "").replace("\n", " ")[:500]
                    proxy_detail = (
                        f" Provider HTTP {event.get('status', 'error')}: {detail}."
                        f" Proxy log: {proxy_log}"
                    )
                    break
            if proxy_detail:
                break
        raise RuntimeError(
            f"{phase} trial failed before Skill learning: {reason}.{proxy_detail} "
            f"Report: {output_dir / 'lh_harness' / 'report.json'}"
        )


def _routes(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "lh_harness" / "role_orchestration" / "skill_routes.json"
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def _effective(kind: str, candidate: Any, baseline_dir: Path, candidate_dir: Path) -> bool:
    candidate_routes = _routes(candidate_dir)
    if kind == "skill":
        return any(
            route.get("injected_context") and any(
                item.get("skill_id") == candidate["skill_id"] and item.get("version") == candidate["version"]
                for item in route.get("selected_skills", [])
            ) for route in candidate_routes
        )
    def signature(routes: list[dict[str, Any]]) -> list[Any]:
        return [(r.get("round"), r.get("role"), r.get("selected_skills")) for r in routes]
    return bool(candidate_routes and signature(candidate_routes) != signature(_routes(baseline_dir)))


def _hard_regression(report: dict[str, Any]) -> bool:
    for item in report.get("rounds", []):
        audit = item.get("auditor_status", {})
        if isinstance(audit, dict) and any(audit.get(key) for key in (
            "verifier_workspace_mutation_detected", "auditor_workspace_mutation_detected", "contract_violation",
        )):
            return True
        text = item.get("auditor_report")
        if isinstance(text, str) and text.strip():
            parsed = parse_audit_report(text, int(item.get("round_index") or 0))
            if parsed.integrity_status == "violation" or parsed.contract_audit_status == "invalid":
                return True
    return False


def train_one(
    task: str, *, template: dict[str, Any], dataset: Path, bank_path: Path,
    generated_dir: Path, jobs_root: Path, decisions_dir: Path, reference_tokens: int,
    force: bool = False,
) -> dict[str, Any]:
    decision_path = decisions_dir / f"{task}.json"
    if decision_path.exists() and not force:
        previous = json.loads(decision_path.read_text(encoding="utf-8"))
        # Older runs recorded provider failures as ordinary no-candidate
        # decisions. Retry them once the endpoint has been fixed.
        if previous.get("baseline_status") != "failed":
            return previous
    # Both complete runs must see the same persistent bank. The baseline may
    # accumulate positive evidence, so keep it on a disposable snapshot.
    frozen_bank = SkillBank.load(bank_path)
    run_id = uuid.uuid4().hex
    provenance = ProvenanceWriter(
        jobs_root / "provenance" / task / f"{run_id}.jsonl", task=task, run_id=run_id,
    )
    print(f"View-only provenance log: {provenance.path}", file=sys.stderr, flush=True)
    provenance.append("run_start", dataset=str(dataset), bank_path=str(bank_path))
    provenance.static_prompts()
    provenance.bank_snapshot("before_baseline", frozen_bank)
    trial_bank_path = jobs_root / task / "bank_snapshot.json"
    frozen_bank.path = trial_bank_path
    frozen_bank.save()
    base_jobs = jobs_root / task / "baseline"
    base_config = generated_dir / f"{task}-baseline.yaml"
    _write_trial_config(template, task=task, phase="baseline", dataset=dataset, bank=trial_bank_path, jobs_dir=base_jobs, config_path=base_config)
    try:
        baseline, base_output = _run_trial(base_config, base_jobs)
    except Exception as exc:
        provenance.append("trial_failure", phase="baseline", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        provenance.ingest_model_io(base_config, base_jobs, phase="baseline")
        provenance.bank_snapshot_file("after_baseline_trial", trial_bank_path)
    provenance.append("trial_report", phase="baseline", report_path=str(base_output / "lh_harness" / "report.json"), report=baseline)
    try:
        _require_valid_trial_report(baseline, base_output, "baseline")
    except Exception as exc:
        provenance.append("trial_failure", phase="baseline", error_type=type(exc).__name__, error=str(exc))
        raise
    proposal_path = base_output / "lh_harness" / "role_orchestration" / "skill_candidate.json"
    result: dict[str, Any] = {"task": task, "training_dataset": str(dataset), "baseline_status": baseline.get("status"), "accepted": False}
    if not proposal_path.is_file():
        result["reason"] = "no candidate proposed"
    else:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        provenance.candidate(
            "proposed", proposal, source_path=proposal_path,
            previous_router_prompt=frozen_bank.router_prompts.get("en"),
        )
        kind, candidate = proposal["kind"], proposal["candidate"]
        result["candidate_kind"] = kind
        result["candidate_id"] = candidate.get("skill_id") if kind == "skill" else "router_prompt"
        candidate_jobs = jobs_root / task / "candidate"
        candidate_config = generated_dir / f"{task}-candidate.yaml"
        frozen_bank.save()
        _write_trial_config(template, task=task, phase="candidate", dataset=dataset, bank=trial_bank_path, jobs_dir=candidate_jobs, config_path=candidate_config, candidate_path=proposal_path)
        try:
            candidate_report, candidate_output = _run_trial(candidate_config, candidate_jobs)
        except Exception as exc:
            provenance.append("trial_failure", phase="candidate", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            provenance.ingest_model_io(candidate_config, candidate_jobs, phase="candidate")
            provenance.bank_snapshot_file("after_candidate_trial", trial_bank_path)
        provenance.append("trial_report", phase="candidate", report_path=str(candidate_output / "lh_harness" / "report.json"), report=candidate_report)
        try:
            _require_valid_trial_report(candidate_report, candidate_output, "candidate")
        except Exception as exc:
            provenance.append("trial_failure", phase="candidate", error_type=type(exc).__name__, error=str(exc))
            raise
        baseline_tokens = baseline.get("skill_learning", {}).get("task_tokens")
        candidate_tokens = candidate_report.get("skill_learning", {}).get("task_tokens")
        effective = _effective(kind, candidate, base_output, candidate_output)
        hard_regression = _hard_regression(candidate_report)
        accepted, delta = accept_candidate(
            baseline, candidate_report,
            baseline_tokens=baseline_tokens, candidate_tokens=candidate_tokens,
            reference_tokens=reference_tokens, effective=effective,
            hard_constraint_regression=hard_regression,
            language="en",
        )
        validation = {
            "accepted": accepted, "reward_delta": delta, "effective": effective,
            "baseline_success": task_success(baseline),
            "candidate_success": task_success(candidate_report),
            "baseline_tokens": baseline_tokens, "candidate_tokens": candidate_tokens,
            "hard_constraint_regression": hard_regression,
        }
        result["validation"] = validation
        result["candidate_status"] = candidate_report.get("status")
        provenance.candidate(
            "assessed", {**proposal, "validation": validation}, source_path=proposal_path,
            previous_router_prompt=frozen_bank.router_prompts.get("en"),
        )
        if accepted:
            bank = SkillBank.load(bank_path)
            commit_validated_candidate(bank, kind, candidate, language="en", validation=validation)
            provenance.bank_snapshot("after_commit", bank)
            result["accepted"] = True
        else:
            result["reason"] = "candidate failed full-task acceptance checks"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    provenance.append("decision", decision_path=str(decision_path), result=result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train a SkillBank on local Terminal-Bench Pro tasks using fresh Harbor trials")
    parser.add_argument("--dataset", type=Path, default=Path("train_dataset/terminal-bench-pro"))
    parser.add_argument("--template", type=Path, default=Path("training/terminal_bench_pro_skill_learning.yaml"))
    parser.add_argument("--bank", type=Path, help="SkillBank path (default: output/<dataset-name>/state/skillbank.json)")
    parser.add_argument("--task", action="append", help="Train only this task name; repeatable")
    parser.add_argument("--start-from", metavar="TASK", help="Start at this task name and continue through later tasks in sorted order")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-run tasks that already have decisions")
    args = parser.parse_args(argv)
    root = Path.cwd()
    dataset = args.dataset.resolve()
    output_root = root / "output" / dataset.name
    names = discover_tasks(dataset)
    all_names = set(names)
    if args.start_from:
        if args.start_from not in names:
            parser.error(f"unknown --start-from task: {args.start_from}")
        names = names[names.index(args.start_from):]
    if args.task:
        unknown = set(args.task) - all_names
        if unknown:
            parser.error(f"unknown training tasks: {', '.join(sorted(unknown))}")
        excluded = set(args.task) - set(names)
        if excluded:
            parser.error(f"--task names are not at or after --start-from: {', '.join(sorted(excluded))}")
        names = [name for name in names if name in set(args.task)]
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        names = names[:args.limit]
    if args.dry_run:
        print(json.dumps({"dataset": str(dataset), "output_root": str(output_root), "start_from": args.start_from, "tasks": names, "test_dataset": "eval/TB-harness/datasets/terminal-bench-2-1/tasks"}, ensure_ascii=False, indent=2))
        return 0
    if not os.getenv("ANTHROPIC_API_KEY") or not os.getenv("ANTHROPIC_BASE_URL"):
        parser.error("ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL are required")
    import yaml
    template = yaml.safe_load(args.template.read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        parser.error("invalid training YAML")
    bank_path = args.bank.resolve() if args.bank else output_root / "state" / "skillbank.json"
    for name in names:
        result = train_one(
            name, template=template, dataset=dataset, bank_path=bank_path,
            generated_dir=output_root / "generated_configs",
            jobs_root=output_root / "jobs", decisions_dir=output_root / "decisions",
            reference_tokens=int(template["agents"][0]["kwargs"].get("skill_reward_reference_tokens", 100_000)),
            force=args.force,
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
