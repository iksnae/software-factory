"""Where things live when the factory is NOT inside the repo it works on.

The original design had one answer for every path: the process cwd. The factory
was stamped into the repo, so `adws/adw_data/...`, `specs/`, and every prompt ref
all resolved against the same root and nothing had to say which root it meant.

A shared checkout has two roots, and conflating them is the whole bug surface:

    factory_home   this checkout — engine code, default prompts, rosters,
                   workflow definitions. Read-only during a run. SHARED.
    repo_root      the target codebase — where agents work, what git commits,
                   where specs and docs land, where the run data goes. PER RUN.

`Paths` is the only thing that knows both. Two rules keep it honest:

  * Every factory-owned path is resolved to an ABSOLUTE path before the run
    starts, because `cli` then chdir's into `repo_root`. After that, a bare
    relative path means "in the target repo" — which is exactly what the ported
    modules already assumed, so they needed no rewiring.

  * Assets are looked up repo-first, factory-second (`resolve_asset`). That is
    the override mechanism: a repo that wants its own planner prompt drops it at
    the same relative path and gets it, with no config edit and no fork.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The factory checkout root: this file is <home>/factory/paths.py.
FACTORY_HOME = Path(__file__).resolve().parent.parent

# Where a target repo may place overrides, relative to its own root. Kept as one
# directory so a repo's factory surface is a single reviewable thing.
REPO_OVERRIDE_DIR = ".sssf"


class AssetNotFound(FileNotFoundError):
    """A config referenced a prompt/harness/workflow file that exists nowhere."""


@dataclass(frozen=True)
class Paths:
    """Resolved roots for one run. Built once by the CLI, carried on the Run."""

    factory_home: Path
    repo_root: Path
    data_dir: Path              # absolute; run artifacts (sessions, trace db)

    @classmethod
    def build(cls, repo_root: str | Path, data_dir: str) -> "Paths":
        repo = Path(repo_root).expanduser().resolve()
        if not repo.is_dir():
            raise NotADirectoryError(f"--repo is not a directory: {repo}")
        data = Path(data_dir)
        return cls(factory_home=FACTORY_HOME, repo_root=repo,
                   data_dir=data if data.is_absolute() else repo / data)

    # ── asset lookup (repo overrides factory defaults) ──────────────────────
    def resolve_asset(self, ref: str) -> Path:
        """Find a prompt / harness / workflow file. Repo first, then factory.

        Absolute refs are taken as given — an escape hatch for a prompt kept
        outside both trees. Everything else is searched in override order, so
        `config/prompts/planner/system.md` in a repo's `.sssf/` wins over the
        factory's copy of the same relative path.
        """
        candidate = Path(ref).expanduser()
        if candidate.is_absolute():
            if candidate.is_file():
                return candidate
            raise AssetNotFound(f"asset not found: {candidate}")

        for root in self.asset_roots:
            found = root / ref
            if found.is_file():
                return found.resolve()
        searched = "\n  ".join(str(root / ref) for root in self.asset_roots)
        raise AssetNotFound(f"asset {ref!r} not found. Looked in:\n  {searched}")

    @property
    def asset_roots(self) -> tuple[Path, ...]:
        """Search order for assets: repo override dir, repo root, factory home."""
        return (self.repo_root / REPO_OVERRIDE_DIR, self.repo_root, self.factory_home)

    def describe(self) -> dict[str, str]:
        """What the run banner and the trace record about where things came from."""
        return {"factory_home": str(self.factory_home),
                "repo_root": str(self.repo_root),
                "data_dir": str(self.data_dir)}
