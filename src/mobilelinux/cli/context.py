"""Shared CLI context: repo + registry, constructed once per invocation."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.registry import Registry
from ..core.repo import Repo, find_repo


@dataclass
class Context:
    repo: Repo
    registry: Registry
    dry_run: bool = False
    verbose: bool = True
    assume_yes: bool = False

    @classmethod
    def create(cls, repo_path: str | None, dry_run: bool, verbose: bool, assume_yes: bool) -> "Context":
        repo = find_repo(repo_path)
        return cls(
            repo=repo,
            registry=Registry(repo),
            dry_run=dry_run,
            verbose=verbose,
            assume_yes=assume_yes,
        )
