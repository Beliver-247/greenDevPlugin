"""Maven command construction and execution helpers."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .config_loader import MavenConfig


@dataclass(frozen=True)
class CommandResult:
    """Captured result from a command execution."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class MavenAction:
    """A Maven action to execute."""

    name: str
    command: list[str]


def format_module_list(modules: Iterable[str], module_selectors: Mapping[str, str] | None = None) -> str:
    """Format module names for Maven's ``-pl`` argument."""

    selectors = module_selectors or {}
    return ",".join(sorted(selectors.get(module, module) for module in set(modules)))


def build_maven_actions(
    affected_modules: Iterable[str],
    module_selectors: Mapping[str, str],
    config: MavenConfig,
) -> list[MavenAction]:
    """Build Maven command lines for the affected modules."""

    module_arg = format_module_list(affected_modules, module_selectors)
    if not module_arg:
        return []

    base_args = [config.executable, *config.extra_args, "-pl", module_arg]

    actions: list[MavenAction] = []
    if config.run_build and config.build_goals:
        build_args = [*base_args]
        if config.also_make:
            build_args.append("-am")
        if config.run_tests:
            build_args.append("-DskipTests")
        actions.append(MavenAction(name="build", command=[*build_args, *config.build_goals]))
    if config.run_tests and config.test_goals:
        test_args = [*base_args]
        if config.also_make_tests:
            test_args.append("-am")
        actions.append(MavenAction(name="test", command=[*test_args, *config.test_goals]))
    if config.run_integration_tests and config.integration_test_goals:
        it_args = [*base_args]
        if config.also_make_integration_tests:
            it_args.append("-am")
        actions.append(MavenAction(name="integration-test", command=[*it_args, *config.integration_test_goals]))

    return actions


def run_command(command: list[str], *, cwd: Path, dry_run: bool) -> CommandResult:
    """Run a command and return captured output."""

    if dry_run:
        return CommandResult(returncode=0, stdout="", stderr="")

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return CommandResult(returncode=1, stdout="", stderr=str(exc))

    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_checked(action: MavenAction, *, cwd: Path, dry_run: bool) -> bool:
    """Run a Maven action and stream readable logs. Returns True on success."""

    prefix = "DRY RUN" if dry_run else "Executing"
    print(f"{prefix} [{action.name}]: {' '.join(action.command)}")
    result = run_command(action.command, cwd=cwd, dry_run=dry_run)

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)

    if result.returncode != 0:
        print(
            f"Maven action '{action.name}' failed with exit code {result.returncode}",
            file=sys.stderr,
        )
        return False

    return True
