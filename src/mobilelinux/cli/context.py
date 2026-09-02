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
    execute: bool = False
    allow_dangerous: bool = False

    @classmethod
    def create(cls, repo_path: str | None, dry_run: bool, verbose: bool,
               assume_yes: bool, execute: bool = False,
               allow_dangerous: bool = False) -> "Context":
        repo = find_repo(repo_path)
        return cls(
            repo=repo,
            registry=Registry(repo),
            dry_run=dry_run,
            verbose=verbose,
            assume_yes=assume_yes,
            execute=execute,
            allow_dangerous=allow_dangerous,
        )

    def runner(self):
        from ..core.tools import Runner
        return Runner(dry_run=self.dry_run, verbose=self.verbose,
                      execute=self.execute, allow_dangerous=self.allow_dangerous)
