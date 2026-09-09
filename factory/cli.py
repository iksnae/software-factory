"""`sf` — the one entrypoint. Picks a target repo, a config, and a workflow.

Ordering here is the whole trick of a shared factory:

  1. Read the config from wherever the operator pointed, resolving `extends`
     and every asset ref to an ABSOLUTE path.
  2. Build `Paths` from the config's own `data_dir`.
  3. Validate: agents resolve, models resolve, quality checks exist, the
     workflow graph is well-formed. Nothing has spawned or spent yet.
  4. `chdir` into the target repo.
  5. Run.

Step 4 is why the ported modules needed almost no rewiring: after it, a bare
relative path means "in the target repo", which is what they already assumed
back when the factory was stamped inside one. Step 1 has to precede it so that
factory-owned files are still findable afterwards.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import engine
from .modules import agents, agent_pi, session, utils
from .modules.data_types import SSSFConfig
from .paths import FACTORY_HOME, Paths

# Where a target repo's config is looked for when `--config` is not given, in
# order. A repo that wants the factory adds one file and nothing else.
CONFIG_CANDIDATES = ("sssf.config.yaml", ".sssf/config.yaml", ".sssf/sssf.config.yaml")

ROSTER_DIR = "config/rosters"


def _find_config(repo: Path, given: str = "") -> Path:
    if given:
        path = Path(given).expanduser()
        candidate = path if path.is_absolute() else repo / path
        if not candidate.is_file():
            # Also allow --config to name a factory-shipped file, so a repo with
            # no config of its own can still be driven from the examples.
            shipped = FACTORY_HOME / path
            if shipped.is_file():
                return shipped.resolve()
            raise SystemExit(f"config not found: {candidate}")
        return candidate.resolve()
    for name in CONFIG_CANDIDATES:
        candidate = repo / name
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit(
        f"no factory config in {repo}. Looked for: {', '.join(CONFIG_CANDIDATES)}.\n"
        f"Create one with:  sf init --repo {repo}")


def _roster_path(name: str) -> str:
    """Accept a bare roster name, a filename, or a path."""
    if "/" in name or name.endswith((".yaml", ".yml")):
        return name
    return f"{ROSTER_DIR}/{name}.yaml"


def available_rosters() -> list[str]:
    return sorted(p.stem for p in (FACTORY_HOME / ROSTER_DIR).glob("*.yaml"))


def load(repo: str, config: str = "", roster: str = "") -> tuple[SSSFConfig, Paths, Path]:
    """Resolve paths + config together. Returns (cfg, paths, config_path)."""
    repo_root = Path(repo).expanduser().resolve()
    if not repo_root.is_dir():
        raise SystemExit(f"--repo is not a directory: {repo_root}")
    config_path = _find_config(repo_root, config)

    # Provisional paths: asset resolution needs the roots, and `data_dir` comes
    # out of the config that resolution is about to read. The default is only
    # ever used for that one read, then replaced with what the config says.
    provisional = Paths.build(repo_root, SSSFConfig().defaults.data_dir)
    cfg = agents.load_config(str(config_path), provisional)
    paths = Paths.build(repo_root, cfg.defaults.data_dir)

    if roster:
        swapped = agents.load_config(str(paths.resolve_asset(_roster_path(roster))), paths)
        if not swapped.agents:
            raise SystemExit(f"roster {roster!r} defines no agents")
        cfg.agents = swapped.agents          # staffing swap; everything else stands
    return cfg, paths, config_path


def _workflow(cfg: SSSFConfig, name: str):
    if name not in cfg.workflows:
        raise SystemExit(f"unknown workflow {name!r} — available: "
                         f"{sorted(cfg.workflows) or '(none)'}")
    return cfg.workflows[name]


# ── commands ─────────────────────────────────────────────────────────────────

def _override_owner(workflow, owner: str, name: str):
    """Retarget a single-agent workflow at a different agent.

    This is the one thing `adw_prompt.py --agent NAME` did that a fixed graph
    cannot: ask any one agent a question. Restricted to workflows with exactly
    one agent step, because "which agent" is otherwise ambiguous and silently
    picking the first would be a guess.
    """
    agent_steps = [s for s in workflow.steps if s.step == "agent"]
    if len(agent_steps) != 1:
        raise SystemExit(
            f"--owner only applies to a workflow with exactly one agent step; "
            f"{name!r} has {len(agent_steps)}")
    return workflow.model_copy(update={
        "steps": [s.model_copy(update={"owner": owner}) if s.step == "agent" else s
                  for s in workflow.steps]})


def cmd_run(args) -> int:
    cfg, paths, config_path = load(args.repo, args.config, args.roster)
    workflow = _workflow(cfg, args.workflow)
    if args.owner:
        workflow = _override_owner(workflow, args.owner, args.workflow)

    engine.validate(workflow, cfg, args.workflow)
    agents.validate(cfg, engine.required_agents(workflow))

    prompt = utils.resolve_prompt(args.prompt)
    os.chdir(paths.repo_root)                # after every factory path is absolute
    env_files = utils.load_env(paths)        # factory .env, then the repo's own
    run = session.ensure(cfg, paths, args.adw_id, workflow=args.workflow)
    run.console.note(f"repo: {paths.repo_root}")
    run.console.note(f"config: {config_path}")
    if env_files:
        run.console.note(f"env: {', '.join(env_files)}")
    return engine.execute(run, workflow, args.workflow, prompt)


def cmd_list(args) -> int:
    cfg, _, config_path = load(args.repo, args.config, args.roster)
    print(f"config:  {config_path}")
    print(f"agents:  {', '.join(a.name for a in cfg.agents) or '(none)'}")
    checks = ", ".join(sorted(cfg.quality.checks)) or "(none)"
    print(f"checks:  {checks}")
    groups = ", ".join(f"{k}[{len(v)}]" for k, v in sorted(cfg.quality.groups.items()))
    print(f"groups:  {groups or '(none)'}")
    print("\nworkflows:")
    width = max((len(n) for n in cfg.workflows), default=0)
    for name, workflow in sorted(cfg.workflows.items()):
        print(f"  {name:<{width}}  {workflow.description}")
    return 0


def cmd_explain(args) -> int:
    cfg, _, _ = load(args.repo, args.config, args.roster)
    workflow = _workflow(cfg, args.workflow)
    engine.validate(workflow, cfg, args.workflow)
    print(f"{args.workflow}: {workflow.description}\n")
    print(f"chain:  {engine.describe_chain(workflow, cfg)}\n")
    print(f"agents: {', '.join(engine.required_agents(workflow)) or '(none)'}")
    print(f"accept: {workflow.accept or 'every phase passing'}")
    print(f"limits: fix x{cfg.limits.max_fix_loops}, "
          f"revise x{cfg.limits.max_revision_loops}, "
          f"json-retry x{cfg.limits.json_fix_attempts}")
    return 0


def cmd_check(args) -> int:
    """Validate everything checkable without spending a token."""
    cfg, paths, config_path = load(args.repo, args.config, args.roster)
    problems: list[str] = []

    for name, workflow in sorted(cfg.workflows.items()):
        try:
            engine.validate(workflow, cfg, name)
            agents.validate(cfg, engine.required_agents(workflow))
        except SystemExit as error:
            problems.append(f"{name}: {error}")

    print(f"config:  {config_path}")
    for key, value in paths.describe().items():
        print(f"{key + ':':<14}{value}")
    print(f"\nworkflows: {len(cfg.workflows)}   agents: {len(cfg.agents)}   "
          f"checks: {len(cfg.quality.checks)}")
    if problems:
        print("\nFAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK — every workflow validates against this config")
    return 0


def cmd_doctor(args) -> int:
    """Host prerequisites. No account, no key mint, no VM — just this machine."""
    import shutil
    import subprocess

    ok = True

    def check(label: str, passed: bool, note: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  {'ok  ' if passed else 'FAIL'}  {label}{f'  ({note})' if note else ''}")

    for binary in ("git", "uv", "pi"):
        found = shutil.which(binary)
        check(f"{binary} on PATH", bool(found), found or "not found")

    try:
        _, doctor_paths, _ = load(args.repo, args.config, args.roster)
        utils.load_env(doctor_paths)
    except SystemExit:
        pass                     # config problems are reported on their own line below
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    check("OPENROUTER_API_KEY set", bool(key), "" if key else "add it to .env")

    # Assert on OUTPUT, never on the exit code: `pi --list-models` exits 0 while
    # printing "No models available", so a returncode check passes a broken host.
    models = ""
    try:
        result = subprocess.run(["pi", "--list-models"], capture_output=True,
                                text=True, timeout=60)
        models = result.stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        models = ""
        check("pi --list-models runs", False, str(error))
    else:
        check("pi --list-models returns a catalog",
              bool(models.strip()) and "no models available" not in models.casefold(),
              f"{len(models.splitlines())} rows")

    try:
        cfg, _, config_path = load(args.repo, args.config, args.roster)
    except SystemExit as error:
        check("config loads", False, str(error))
        print("\n  factory doctor: FAILED")
        return 1
    check("config loads", True, str(config_path))

    unresolved = []
    for agent in cfg.agents:
        try:
            agent_pi.resolve_model(agent.model)
        except ValueError:
            unresolved.append(f"{agent.name}:{agent.model}")
    check("every roster model resolves", not unresolved,
          ", ".join(unresolved) if unresolved else f"{len(cfg.agents)} agents")

    missing = [name for name, check_cfg in cfg.quality.checks.items()
               if not shutil.which(check_cfg.argv[0])]
    check("every quality command's binary exists", not missing,
          ", ".join(missing) if missing else f"{len(cfg.quality.checks)} checks")

    print(f"\n  factory doctor: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def cmd_init(args) -> int:
    """Write a starter config into a target repo. Never overwrites."""
    repo = Path(args.repo).expanduser().resolve()
    target = repo / "sssf.config.yaml"
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists — pass --force to overwrite")
    template = FACTORY_HOME / "config" / "starter.config.yaml"
    body = template.read_text().replace("{{roster}}", _roster_path(args.roster or "default"))
    target.write_text(body)
    print(f"wrote {target}")
    print(f"next: edit its `quality:` block, then `sf check --repo {repo}`")
    return 0


# ── argv ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sf", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--repo", default=".", help="target repo (default: cwd)")
        p.add_argument("--config", default="", help="config file (default: discovered)")
        p.add_argument("--roster", default="", help="swap model staffing, e.g. frontier")
        return p

    run = common(sub.add_parser("run", help="run a workflow against a repo"))
    run.add_argument("workflow")
    run.add_argument("prompt", help="inline text or a path to a prompt file")
    run.add_argument("--adw-id", default=None, help="join or pin an existing session")
    run.add_argument("--owner", default="",
                     help="retarget a single-agent workflow, e.g. --owner scout")
    run.set_defaults(func=cmd_run)

    common(sub.add_parser("list", help="workflows, agents, and checks in this config")
           ).set_defaults(func=cmd_list)

    explain = common(sub.add_parser("explain", help="render one workflow's chain"))
    explain.add_argument("workflow")
    explain.set_defaults(func=cmd_explain)

    common(sub.add_parser("check", help="validate config + every workflow, no spend")
           ).set_defaults(func=cmd_check)
    common(sub.add_parser("doctor", help="host prerequisites for this machine")
           ).set_defaults(func=cmd_doctor)

    init = common(sub.add_parser("init", help="write a starter config into a repo"))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
