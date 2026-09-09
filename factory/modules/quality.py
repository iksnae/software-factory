"""Deterministic checks: a known command is not a judgement call.

Anything whose invocation you can write down belongs here as code — it runs in
milliseconds, costs nothing, and returns the same answer every time. Agents are
for the parts that need reading and deciding. An agent rediscovering `bun test`
on every run cost ~1M tokens and 85s; a subprocess costs neither.

What changed in the shared factory: the commands are no longer written down HERE.
They are named entries under `quality:` in the target repo's config, resolved and
validated when the config loads. This module now knows how to RUN a check, record
it, and hand a failure back to an agent — and nothing about what any repo's
toolchain is called.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityConfig,
                         QualityResult, VerifyOutput)
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000

# Substituted in any argv element with this check's own output directory inside
# the session. Bundle and typecheck blocks need somewhere to write; without this
# a target repo would have to invent a path and remember to gitignore it.
OUTDIR_TOKEN = "{outdir}"


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def specs_for(run, names: list[str]) -> list[QualityCheckSpec]:
    """Turn configured check names into runnable specs, resolving {outdir}.

    Argv is taken verbatim apart from that one token. No shell, so nothing in a
    repo's config is word-split or glob-expanded behind its back — a path with a
    space is just a path with a space.
    """
    quality: QualityConfig = run.cfg.quality
    specs = []
    for name in quality.resolve(names):
        configured = quality.checks[name]
        outdir = _check_dir(run, name) / "out"
        argv = [part.replace(OUTDIR_TOKEN, str(outdir)) for part in configured.argv]
        specs.append(QualityCheckSpec(
            name=name, area=configured.area, operation=configured.operation,
            argv=argv, timeout_seconds=configured.timeout_seconds))
    return specs


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = operator_env()             # the engineer's own shell environment

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            spec.argv,
            cwd=run.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        # A genuinely missing binary lands here as exit 127 with the real error
        # text, which is more useful than pre-flighting every argv[0].
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


def run_checks(run, names: list[str]) -> QualityResult:
    """Run the named checks and collect EVERY failure, not just the first.

    A builder repairing one failure at a time pays for a whole phase per fix, so
    all checks run even after one goes red.
    """
    checks = [_run(spec, run) for spec in specs_for(run, names)]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing check flows back into the builder through
    exactly the same door another agent's report would have used.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )
