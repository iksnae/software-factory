"""The workflow interpreter: a declared phase graph, executed.

Each stock workflow used to be one Python file whose docstring WAS its chain —
the single best teaching artifact in the original repo. Moving the graph into
config buys reuse (a repo picks and tunes a workflow instead of forking a
script) and costs that legibility, so it is paid back deliberately:
`describe_chain` renders the resolved graph from config, and the run prints it
and writes it into the trace before the first phase opens. The chain is still
readable; it is derived now instead of hand-written, and therefore cannot drift
from what actually runs.

Eight step kinds cover every stock workflow:

    request       record the incoming ask (the engineer's own phase)
    agent         one agent call: typed envelope out, gates verified
    quality       run named deterministic checks
    verify_loop   quality, then an agent repair, bounded — "does it run"
    review_loop   an agent verdict, then an agent revision, bounded — "is it what was asked"
    changes       diff the run against its pinned baseline, for a documenter
    commit        commit the tree in one agent's own words
    group         a nested list of steps, usually behind a `when:` condition

Ahead of all of them, when the repo's config declares `prepare:`, comes one phase
that no workflow asks for: the commands that make the tree runnable at all. It is
not a step kind because it is not a choice — a tree with no dependencies installed
cannot be planned against, built in, or tested, whichever workflow you picked.

Nothing here decides anything a phase could decide for itself. The engine's only
judgements are the ones the ADW scripts made in Python: what counts as verified,
when a stale green result must be re-earned, and what stops the chain.
"""

from __future__ import annotations

from typing import Optional

from .modules import agents, changes, gates, git_helper, quality
from .modules.data_types import (AgentCall, ChangeCapture, EnvelopeBase, EventRecord,
                                 PhaseParams, QualityResult, ReviewOutput, StepConfig,
                                 WorkflowConfig)
from .modules.data_types import (BuildOutput, DocumentOutput, GenericOutput, PlanOutput,
                                 ScoutOutput)

# Envelope types a workflow may name in `output:`. An unknown name is a config
# error at validation, not an AttributeError at phase time.
ENVELOPES: dict[str, type[EnvelopeBase]] = {
    "GenericOutput": GenericOutput,
    "PlanOutput": PlanOutput,
    "BuildOutput": BuildOutput,
    "ScoutOutput": ScoutOutput,
    "ReviewOutput": ReviewOutput,
    "DocumentOutput": DocumentOutput,
}

# Gates a workflow may name. Same rule: resolved and checked before anything runs.
GATES = {
    "artifacts_exist": gates.artifacts_exist,
    "files_non_empty": gates.files_non_empty,
    "json_parses": gates.json_parses,
    "diff_matches_claims": gates.diff_matches_claims,
    "verdict_consistent": gates.verdict_consistent,
}

CONDITIONS = ("", "verified", "revised_and_approved", "not_verified")


class WorkflowError(SystemExit):
    """A workflow the operator can fix, reported as a message rather than a trace."""


# ── validation (everything checkable, before the first agent spawns) ─────────

def _require(step: StepConfig, field: str, where: str) -> None:
    if not getattr(step, field, None):
        raise WorkflowError(f"{where}: step kind {step.step!r} requires `{field}:`")


def _require_agent_repair(step: StepConfig, where: str) -> None:
    """A `repair` must be an agent step, because that is all execution can run.

    `_verify_loop` and `_review_loop` pass `step.repair` straight to
    `_agent_phase` without inspecting its kind, so a repair declared as any other
    kind dies on its missing `output` after the loop has already paid for the
    phase that preceded it. Validation used to judge a repair BY its kind and
    accept `changes`, `commit`, or `quality` there — an asymmetry between what
    the validator allowed and what the engine could execute.

    Refusing it here keeps the two honest. Widening this is the correct place to
    start if a non-agent repair ever becomes something the loops dispatch.
    """
    if step.step != "agent":
        raise WorkflowError(
            f"{where}: a repair must be `step: agent` — the loops hand it to the "
            f"agent phase without checking its kind, so {step.step!r} cannot run there")


def _validate_step(step: StepConfig, cfg, where: str, seen: set[str]) -> None:
    if step.name:
        if step.name in seen:
            raise WorkflowError(f"{where}: duplicate step name {step.name!r} — "
                                f"names address earlier results and must be unique")
        seen.add(step.name)
    if step.when not in CONDITIONS:
        raise WorkflowError(f"{where}: unknown `when:` {step.when!r} — "
                            f"expected one of {[c for c in CONDITIONS if c]}")

    if step.step == "agent":
        for field in ("name", "owner", "output"):
            _require(step, field, where)
        if step.output not in ENVELOPES:
            raise WorkflowError(f"{where}: unknown output type {step.output!r} — "
                                f"available: {sorted(ENVELOPES)}")
        unknown = [g for g in step.gates if g not in GATES]
        if unknown:
            raise WorkflowError(f"{where}: unknown gate(s) {unknown} — "
                                f"available: {sorted(GATES)}")
        if step.previous and step.previous not in seen:
            raise WorkflowError(f"{where}: `previous: {step.previous}` names no earlier "
                                f"step — known so far: {sorted(seen)}")
    elif step.step == "commit":
        _require(step, "name", where)
        _require(step, "source", where)
        if step.source not in seen:
            raise WorkflowError(f"{where}: `source: {step.source}` names no earlier step")
    elif step.step in ("quality", "verify_loop"):
        _require(step, "name", where)
        if not (step.checks or step.group):
            raise WorkflowError(f"{where}: step kind {step.step!r} requires "
                                f"`checks:` or `group:`")
        try:
            cfg.quality.resolve(step.checks, step.group)      # names must exist NOW
        except ValueError as error:
            raise WorkflowError(f"{where}: {error}") from error
        if step.step == "verify_loop":
            _require(step, "repair", where)
            _require_agent_repair(step.repair, f"{where} -> repair")
            _validate_step(step.repair, cfg, f"{where} -> repair", seen)
    elif step.step == "review_loop":
        for field in ("name", "owner", "output"):
            _require(step, field, where)
        if step.output not in ENVELOPES:
            raise WorkflowError(f"{where}: unknown output type {step.output!r}")
        unknown = [g for g in step.gates if g not in GATES]
        if unknown:
            raise WorkflowError(f"{where}: unknown gate(s) {unknown}")
        if step.repair is not None:
            _require_agent_repair(step.repair, f"{where} -> revise")
            _validate_step(step.repair, cfg, f"{where} -> revise", seen)
    elif step.step == "changes":
        _require(step, "name", where)
    elif step.step == "group":
        if not step.steps:
            raise WorkflowError(f"{where}: `group` with no `steps:` does nothing")
        for i, child in enumerate(step.steps):
            _validate_step(child, cfg, f"{where} -> steps[{i}]", seen)
    elif step.step != "request":
        raise WorkflowError(f"{where}: unknown step kind {step.step!r}")


def required_agents(workflow: WorkflowConfig) -> list[str]:
    """Every agent name the graph will ask for, declared or inferred.

    Inferred rather than only declared, because `requires:` in config is a claim
    like any other and a workflow that names an owner it forgot to require would
    otherwise fail mid-run instead of at startup.
    """
    names = list(workflow.requires)

    def walk(steps: list[StepConfig]) -> None:
        for step in steps:
            if step.owner:
                names.append(step.owner)
            if step.repair is not None:
                walk([step.repair])
            if step.steps:
                walk(step.steps)

    walk(workflow.steps)
    return sorted({n for n in names if n})


def validate(workflow: WorkflowConfig, cfg, name: str) -> None:
    """Check the whole graph against the config before anything spawns."""
    if not workflow.steps:
        raise WorkflowError(f"workflow {name!r} declares no steps")
    if workflow.accept not in ("", "verified"):
        raise WorkflowError(f"workflow {name!r}: unknown `accept: {workflow.accept!r}` — "
                            f"expected '' or 'verified'")
    seen: set[str] = set()
    for i, step in enumerate(workflow.steps):
        _validate_step(step, cfg, f"workflow {name!r} steps[{i}]", seen)

    # A `changes` step measuring against "baseline" needs a baseline to exist.
    # Caught here rather than at phase time, where it would surface as an empty
    # ref deep inside git plumbing after the run had already spent on agents.
    #
    # Walks `repair` as well as `steps`, matching `required_agents` below. An
    # earlier version walked only `steps`, so a `changes` step declared as a
    # loop's `repair` passed validation — probed: with `pin_baseline: false` a
    # top-level `changes` was refused while the same step nested in a `repair`
    # validated clean.
    #
    # That gap could not actually reach a baseline comparison, and an earlier
    # version of this comment wrongly claimed it did. `_verify_loop` and
    # `_review_loop` hand a repair to `_agent_phase` WITHOUT looking at its kind
    # (see their calls below), so a non-agent repair dies on its missing
    # `output` long before any git work. The reachable defect was the asymmetry
    # itself — validation judged a repair by its kind while execution ignored
    # it — which `_require_agent_repair` now closes. This walk stays because it
    # is the correct shape for a recursive check and because it must not be the
    # thing standing between a future repair kind and this guard.
    def needs_baseline(steps: list[StepConfig]) -> bool:
        for step in steps:
            if step.step == "changes" and step.base == "baseline":
                return True
            if step.repair is not None and needs_baseline([step.repair]):
                return True
            if step.steps and needs_baseline(step.steps):
                return True
        return False

    if needs_baseline(workflow.steps) and not workflow.pin_baseline:
        raise WorkflowError(
            f"workflow {name!r}: a `changes` step measures against `base: baseline`, "
            f"so the workflow must set `pin_baseline: true` (or name a git ref instead)")


# ── rendering (the chain the docstrings used to carry) ───────────────────────

def describe_chain(workflow: WorkflowConfig, cfg) -> str:
    """One line naming every phase the graph will open, in order."""

    def label(step: StepConfig, text: str) -> str:
        """Mark a conditional step, so a reader never mistakes it for unconditional."""
        return f"{text} if {step.when}" if step.when else text

    def render(steps: list[StepConfig]) -> list[str]:
        out = []
        for step in steps:
            if step.step == "request":
                out.append("engineer(request)")
            elif step.step == "agent":
                out.append(label(step, f"{step.owner}({step.name})"))
            elif step.step == "commit":
                out.append(label(step, f"git({step.name})"))
            elif step.step == "changes":
                out.append(label(step, f"code({step.name})"))
            elif step.step == "quality":
                out.append(label(step, f"code({step.name})"))
            elif step.step == "verify_loop":
                limit = step.max_attempts or cfg.limits.max_fix_loops
                out.append(f"code({step.name}) [-> {step.repair.owner}"
                           f"({step.repair.name}) -> code({step.name}) ... x{limit}]")
            elif step.step == "review_loop":
                limit = step.max_attempts or cfg.limits.max_revision_loops
                revise = (f" [-> {step.repair.owner}({step.repair.name}) -> "
                          f"{step.owner}({step.name}) ... x{limit}]" if step.repair else "")
                out.append(f"{step.owner}({step.name}){revise}")
            elif step.step == "group":
                inner = " -> ".join(render(step.steps))
                out.append(f"if {step.when or 'always'}: ({inner})" if step.when else inner)
        return out

    rendered = render(workflow.steps)
    if cfg.prepare:
        # Prepended, not part of the graph: it runs for every workflow rather
        # than being declared by any one of them.
        rendered.insert(0, f"code(prepare)[{', '.join(cfg.prepare)}]")
    return " -> ".join(rendered)


# ── execution ───────────────────────────────────────────────────────────────

class State:
    """What later steps are allowed to know about earlier ones."""

    def __init__(self, baseline: str):
        self.baseline = baseline
        self.envelopes: dict[str, EnvelopeBase] = {}
        self.quality: Optional[QualityResult] = None
        self.review: Optional[ReviewOutput] = None
        self.revised = False

    @property
    def verified(self) -> bool:
        """Both questions answered clean, where both were asked.

        The suite asks "does it run"; a reviewer asks "is this what was asked
        for". Neither can answer the other's, so a workflow that runs only one
        of them is verified on that one alone.
        """
        checks_ok = self.quality is None or self.quality.passed
        review_ok = self.review is None or self.review.approved
        return checks_ok and review_ok

    def holds(self, condition: str) -> bool:
        if not condition:
            return True
        if condition == "verified":
            return self.verified
        if condition == "not_verified":
            return not self.verified
        if condition == "revised_and_approved":
            # A revision edited code after the suite last ran, so the green light
            # is stale and has to be re-earned before anything is committed.
            return self.revised and self.review is not None and self.review.approved
        raise WorkflowError(f"unknown condition {condition!r}")


def _agent_phase(run, state: State, step: StepConfig, prompt: str,
                 previous: Optional[EnvelopeBase] = None) -> EnvelopeBase:
    """Open an agent phase and record its envelope under the step's name."""
    handoff = previous
    if handoff is None and step.previous:
        handoff = state.envelopes.get(step.previous)
    with run.phase(PhaseParams(name=step.name, kind="agent", owner=step.owner,
                               description=step.description,
                               retries=step.retries)) as ph:
        envelope = ph.call(AgentCall(output_type=ENVELOPES[step.output],
                                     prompt=prompt, previous=handoff,
                                     gates=[GATES[g] for g in step.gates]))
    state.envelopes[step.name] = envelope
    return envelope


def _record(ph, result: QualityResult) -> None:
    """Log a deterministic block's verdict — the same shape every workflow uses."""
    passed = sum(1 for check in result.checks if check.passed)
    ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
           artifacts=", ".join(result.artifacts))


def _quality_phase(run, state: State, step: StepConfig, name: str) -> QualityResult:
    with run.phase(PhaseParams(name=name, kind="code", owner="quality",
                               description=step.description)) as ph:
        result = quality.run_checks(run, quality_names(run, step))
        _record(ph, result)
    state.quality = result
    return result


def quality_names(run, step: StepConfig) -> list[str]:
    return run.cfg.quality.resolve(step.checks, step.group)


def _commit_phase(run, state: State, step: StepConfig) -> None:
    """Commit in the words of the agent whose work product this is.

    Each agent's `commit_message` covers its own diff, so a chain that commits
    per step never reuses one agent's sentence for another agent's changes.
    """
    envelope = state.envelopes[step.source]
    message = (getattr(envelope, "commit_message", "")
               or f"sssf({run.adw_id}): {envelope.summary}")
    with run.phase(PhaseParams(name=step.name, kind="code", owner="git",
                               description=step.description)) as ph:
        ph.log(sha=git_helper.commit_all(message), message=message)


def _changes_phase(run, state: State, step: StepConfig) -> None:
    base = state.baseline if step.base == "baseline" else step.base
    with run.phase(PhaseParams(name=step.name, kind="code", owner="git",
                               description=step.description)) as ph:
        changeset = changes.capture(run, ChangeCapture(
            base=base, max_diff_lines=run.cfg.limits.max_diff_lines))
        ph.log(base=f"{changeset.base.label} @ {changeset.base.commit[:7]}",
               reason=changeset.base.reason,
               files=len(changeset.files) + len(changeset.untracked),
               lines=f"+{changeset.insertions} -{changeset.deletions}",
               diff=changeset.diff_path)
        if changeset.empty:
            raise RuntimeError(
                f"nothing changed since {changeset.base.label} "
                f"({changeset.base.reason}) — there is nothing to document.")
    state.envelopes[step.name] = changes.as_envelope(changeset, step.description)


def _verify_loop(run, state: State, step: StepConfig, prompt: str) -> None:
    """Checks, then a bounded repair from their verbatim output."""
    limit = step.max_attempts or run.cfg.limits.max_fix_loops
    for attempt in range(1, limit + 1):
        result = _quality_phase(run, state, step, f"{step.name}_{attempt}")
        if result.passed:
            return
        repair = step.repair.model_copy(update={
            "name": f"{step.repair.name or 'fix'}_{attempt}"})
        _agent_phase(run, state, repair, prompt,
                     previous=quality.as_envelope(result, step.name))


def _review_loop(run, state: State, step: StepConfig, prompt: str) -> None:
    """A verdict, then a bounded revision that must close its blocking findings."""
    limit = step.max_attempts or run.cfg.limits.max_revision_loops
    for attempt in range(1, limit + 1):
        verdict = _agent_phase(
            run, state,
            step.model_copy(update={"name": f"{step.name}_{attempt}"}),
            prompt)
        state.review = verdict if isinstance(verdict, ReviewOutput) else None
        # The name without its attempt suffix stays addressable, so a later
        # `previous:` or `source:` does not have to know how many rounds ran.
        state.envelopes[step.name] = verdict
        if getattr(verdict, "approved", False) or attempt == limit or step.repair is None:
            return
        revise = step.repair.model_copy(update={
            "name": f"{step.repair.name or 'revise'}_{attempt}"})
        _agent_phase(run, state, revise, prompt, previous=verdict)
        state.revised = True


def _run_steps(run, state: State, steps: list[StepConfig], prompt: str) -> None:
    for step in steps:
        if not state.holds(step.when):
            continue
        if step.step == "request":
            with run.phase(PhaseParams(name=step.name or "request", kind="engineer",
                                       owner=run.engineer,
                                       description=step.description)) as ph:
                ph.log(input=prompt, baseline=git_helper.short_sha(state.baseline)
                       if state.baseline else "(unpinned)")
        elif step.step == "agent":
            _agent_phase(run, state, step, prompt)
        elif step.step == "quality":
            _quality_phase(run, state, step, step.name)
        elif step.step == "verify_loop":
            _verify_loop(run, state, step, prompt)
        elif step.step == "review_loop":
            _review_loop(run, state, step, prompt)
        elif step.step == "changes":
            _changes_phase(run, state, step)
        elif step.step == "commit":
            _commit_phase(run, state, step)
        elif step.step == "group":
            _run_steps(run, state, step.steps, prompt)


def _prepare(run) -> None:
    """Make the repo runnable before anything judges it.

    Runs as the first phase of EVERY workflow, ahead of the request, because a
    tree whose dependencies are missing cannot be planned against, built in, or
    tested — and the factory's own `just worktree` hands over exactly such a
    tree. Six checks were measured red in a fresh worktree and all six green
    after one `npm install`; without this the builder would have spent its whole
    repair budget on a missing dependency it did not cause.

    A failure here ABORTS. That is the difference from a quality phase: a red
    check is a finding to repair, an unprepared tree was never fit to judge, so
    no agent is spawned and no money is spent.
    """
    if not run.cfg.prepare:
        return
    with run.phase(PhaseParams(
            name="prepare", kind="code", owner="quality",
            description="Make the repo runnable — install what the checks and the "
                        "builder both need before either is asked anything")) as ph:
        result = quality.run_prepare(run)
        _record(ph, result)
        if not result.passed:
            raise RuntimeError(
                "prepare failed, so nothing was planned, built or judged:\n"
                + "\n".join(result.failures))


def execute(run, workflow: WorkflowConfig, name: str, prompt: str) -> int:
    """Run a declared workflow end to end and return its exit code."""
    # Pinned BEFORE the first commit phase, because by then the run has moved
    # the branch itself and "what this run changed" would measure against its
    # own output.
    baseline = git_helper.rev("HEAD") if (workflow.pin_baseline
                                          and git_helper.is_repo()) else ""
    state = State(baseline)

    chain = describe_chain(workflow, run.cfg)
    run.console.note(f"workflow {name}: {chain}")
    run.tracer.event(EventRecord(adw_id=run.adw_id, type="log", name="workflow",
                                 payload={"workflow": name, "chain": chain,
                                          "description": workflow.description,
                                          **run.paths.describe()}))

    _prepare(run)
    _run_steps(run, state, workflow.steps, prompt)

    accepted = state.verified if workflow.accept == "verified" else True
    return run.finish(accepted=accepted,
                      reason=workflow.accept_reason
                      or "the checks or the review never came back clean")
