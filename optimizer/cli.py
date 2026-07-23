"""Command-line interface for the selective Maven build optimizer."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config_loader import (
    ConfigurationError,
    apply_cli_overrides,
    load_config,
    resolve_config_path,
)
from .core import (
    ImpactAnalysisError,
    build_dependency_graph,
    determine_impact,
    discover_modules,
    module_selectors,
)
from .git_utils import GitError, get_changed_files
from .maven_utils import MavenAction, build_maven_actions, run_checked


EXIT_SUCCESS = 0
EXIT_NO_CHANGES = 10
EXIT_DOCS_ONLY = 20
EXIT_NO_AFFECTED_MODULES = 30
EXIT_ERROR = 1


def main(argv: list[str] | None = None) -> int:
    """Run the optimizer CLI."""

    args = build_parser().parse_args(argv)
    started_at = time.perf_counter()
    project_root = Path(args.project_root).expanduser().resolve()

    payload: dict[str, Any] = {
        "status": "error",
        "exit_code": EXIT_ERROR,
        "changed_files": [],
        "directly_affected_modules": [],
        "affected_modules": [],
        "actions": [],
    }

    try:
        import os
        
        def resolve_base(cli_base: str | None) -> str | None:
            if cli_base:
                return cli_base
            prev_succ = os.environ.get("GIT_PREVIOUS_SUCCESSFUL_COMMIT")
            if prev_succ and prev_succ != "null":
                return prev_succ
            prev = os.environ.get("GIT_PREVIOUS_COMMIT")
            if prev and prev != "null":
                return prev
            return None
            
        def resolve_head(cli_head: str | None) -> str | None:
            if cli_head:
                return cli_head
            git_commit = os.environ.get("GIT_COMMIT")
            if git_commit and git_commit != "null":
                return git_commit
            return None
            
        config_path = resolve_config_path(args.config, project_root)
        config = load_config(config_path, project_root)
        config = apply_cli_overrides(
            config,
            base_ref=resolve_base(args.base),
            head_ref=resolve_head(args.head),
            dry_run=args.dry_run,
            output_format=args.output_format,
        )

        changed_files = get_changed_files(project_root, config.base_ref, config.head_ref)
        modules = discover_modules(project_root, config.modules)
        reverse_deps = build_dependency_graph(modules, config.maven.group_id)
        impact = determine_impact(changed_files, modules, reverse_deps, config)
        selectors = module_selectors(modules)
        actions = build_maven_actions(impact.affected_modules, selectors, config.maven)

        print_summary(
            base_ref=config.base_ref,
            head_ref=config.head_ref,
            config_path=config_path,
            changed_files=impact.changed_files,
            directly_affected=impact.directly_affected_modules,
            affected_modules=impact.affected_modules,
            actions=actions,
            dry_run=config.dry_run,
        )

        payload.update(
            {
                "base_ref": config.base_ref,
                "head_ref": config.head_ref,
                "config_path": str(config_path) if config_path else None,
                "changed_files": impact.changed_files,
                "directly_affected_modules": sorted(impact.directly_affected_modules),
                "affected_modules": sorted(impact.affected_modules),
                "actions": [
                    {"name": action.name, "command": action.command, "dry_run": config.dry_run}
                    for action in actions
                ],
            }
        )

        if impact.no_changes:
            return finish(
                payload,
                status="no_changes",
                exit_code=EXIT_NO_CHANGES,
                output_format=config.output.format,
                started_at=started_at,
            )

        if impact.docs_only:
            return finish(
                payload,
                status="documentation_only",
                exit_code=EXIT_DOCS_ONLY,
                output_format=config.output.format,
                started_at=started_at,
            )

        if not impact.affected_modules:
            return finish(
                payload,
                status="no_affected_modules",
                exit_code=EXIT_NO_AFFECTED_MODULES,
                output_format=config.output.format,
                started_at=started_at,
            )

        for action in actions:
            if not run_checked(action, cwd=project_root, dry_run=config.dry_run):
                return finish(
                    payload,
                    status="maven_failed",
                    exit_code=EXIT_ERROR,
                    output_format=config.output.format,
                    started_at=started_at,
                )

        if args.carbon_aware:
            from .scheduler import run_scheduler

            carbon_aware_cfg = {
                "provider": config.carbon_aware.provider,
                "electricity_maps_api_key": config.carbon_aware.electricity_maps_api_key,
                "electricity_maps_zone": config.carbon_aware.electricity_maps_zone,
                "model_path": config.carbon_aware.model_path,
                "history_store_path": config.carbon_aware.history_store_path,
                "min_history_hours": config.carbon_aware.min_history_hours,
                "backfill_on_empty": config.carbon_aware.backfill_on_empty,
            }
            payload["scheduling"] = run_scheduler(payload, carbon_aware_config=carbon_aware_cfg)

        return finish(
            payload,
            status="success",
            exit_code=EXIT_SUCCESS,
            output_format=config.output.format,
            started_at=started_at,
        )

    except (ConfigurationError, GitError, ImpactAnalysisError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        payload["error"] = str(exc)
        output_format = args.output_format or "json"
        return finish(
            payload,
            status="error",
            exit_code=EXIT_ERROR,
            output_format=output_format,
            started_at=started_at,
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    parser = argparse.ArgumentParser(
        description="Detect changed files and run selective Maven builds/tests."
    )
    parser.add_argument("--base", help="Git base ref to compare from. Overrides config.")
    parser.add_argument("--head", help="Git head ref to compare to. Overrides config.")
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=None,
        type=parse_bool,
        help="Print Maven actions without executing them. Accepts true/false.",
    )
    parser.add_argument("--config", help="Path to YAML or JSON optimizer config.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Repository root to analyze. Defaults to the current directory.",
    )
    parser.add_argument(
        "--output-format",
        choices=("json", "key-value", "none"),
        help="Structured output format. Overrides config.",
    )
    parser.add_argument(
        "--carbon-aware",
        action="store_true",
        default=False,
        help="Run carbon-aware scheduling after the build and include a recommendation in the output.",
    )
    return parser


def parse_bool(value: str | bool) -> bool:
    """Parse a CLI boolean value."""

    if isinstance(value, bool):
        return value

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise argparse.ArgumentTypeError("expected true or false")


def print_summary(
    *,
    base_ref: str,
    head_ref: str,
    config_path: Path | None,
    changed_files: list[str],
    directly_affected: set[str],
    affected_modules: set[str],
    actions: list[MavenAction],
    dry_run: bool,
) -> None:
    """Print a human-readable summary for pipeline logs."""

    print("Optimizer configuration:")
    print(f"  - git diff: {base_ref}..{head_ref}")
    print(f"  - config: {config_path if config_path else 'built-in defaults'}")

    print("Changed files:")
    if changed_files:
        for file_name in changed_files:
            print(f"  - {file_name}")
    else:
        print("  - No changed files detected")

    print("Directly affected modules:")
    if directly_affected:
        for module in sorted(directly_affected):
            print(f"  - {module}")
    else:
        print("  - None")

    print("Affected modules:")
    if affected_modules:
        for module in sorted(affected_modules):
            print(f"  - {module}")
    else:
        print("  - None")

    print("Actions taken:")
    if actions:
        for action in actions:
            mode = "dry-run" if dry_run else "run"
            print(f"  - {action.name}: {mode} ({' '.join(action.command)})")
    else:
        print("  - skip")


def finish(
    payload: dict[str, Any],
    *,
    status: str,
    exit_code: int,
    output_format: str,
    started_at: float,
) -> int:
    """Emit structured output and return the process exit code."""

    payload["status"] = status
    payload["exit_code"] = exit_code
    payload["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
    emit_structured_output(payload, output_format)
    return exit_code


def emit_structured_output(payload: dict[str, Any], output_format: str) -> None:
    """Emit machine-readable output for CI/CD systems."""

    if output_format == "none":
        return

    print("Structured output:")
    if output_format == "json":
        print(json.dumps(payload, sort_keys=True))
        return

    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, list):
            rendered = ",".join(_render_key_value_item(item) for item in value)
        elif isinstance(value, dict):
            rendered = json.dumps(value, sort_keys=True)
        else:
            rendered = "" if value is None else str(value)
        print(f"optimizer_{key}={rendered}")


def _render_key_value_item(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)
