"""Git interop. Soft-imports gitpython; degrades to None when no repo."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitInfo:
    sha: str | None
    dirty: bool | None
    repo_path: str | None


def collect(start: str | os.PathLike | None = None) -> GitInfo:
    try:
        import git  # type: ignore
    except Exception:
        return GitInfo(None, None, None)
    try:
        search = Path(start or os.getcwd()).resolve()
        repo = git.Repo(search, search_parent_directories=True)
        sha = repo.head.commit.hexsha if not repo.head.is_detached or repo.head.commit else None
        try:
            sha = repo.head.commit.hexsha
        except Exception:
            sha = None
        dirty = bool(repo.is_dirty(untracked_files=False))
        return GitInfo(sha=sha, dirty=dirty, repo_path=str(repo.working_dir))
    except Exception:
        return GitInfo(None, None, None)
