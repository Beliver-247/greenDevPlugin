"""Git helpers for detecting changed files."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git command fails."""


def get_changed_files(project_root: Path, base_ref: str, head_ref: str) -> list[str]:
    """Return files changed between two git references."""

    command = ["git", "diff", "--name-only", base_ref, head_ref]
    try:
        completed = subprocess.run(
            command,
            cwd=str(project_root),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"Failed to run git diff: {exc}") from exc

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown git diff error"
        raise GitError(f"git diff failed for {base_ref}..{head_ref}: {message}")

    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
