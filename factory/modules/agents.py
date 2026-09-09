"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml

from . import agent_cc, agent_codex, agent_pi, permissions, prompts
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, PiRequest, PiResult,
                         SSSFConfig, UsageBreakdown)
from .utils import new_id

# Fallback only. The live value is cfg.limits.json_fix_attempts, read per run —
# this constant was the reason tuning it meant editing the engine.
JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON

# Keys an agent inherits from `defaults` unless it states its own.
INHERITED = ("coding_agent", "model", "thinking", "color", "tools", "writes")

# How deep an `extends:` chain may go before we call it a cycle. A roster
# extending a roster extending a base is already more indirection than a config
# should need; anything past this is a loop or a mistake, and both deserve the
# same clear error rather than a RecursionError.
MAX_EXTENDS_DEPTH = 5


# Which module drives which harness. Every entry must expose the same four
# names — resolve_model, assistant_message_records, ToolCallTracker, run — which
# is what lets `execute` below stay harness-agnostic instead of branching.
#
# `claude_code` is still the stub it always was: listed so a config naming it
# fails with one clear sentence rather than a KeyError.
HARNESSES = {
    "pi": agent_pi,
    "codex": agent_codex,
    "claude_code": agent_cc,
}

IMPLEMENTED = ("pi", "codex")


def harness(agent: AgentConfig):
    """The module that runs this agent. Raises SystemExit on an unusable choice."""
    module = HARNESSES.get(agent.coding_agent)
    if module is None:
        raise SystemExit(f"agent {agent.name!r}: unknown coding_agent "
                         f"{agent.coding_agent!r} — available: {sorted(HARNESSES)}")
    return module


class GateFailure(RuntimeError):
    pass


class ConfigError(SystemExit):
    """A config the operator can fix, reported as a message rather than a trace."""


# ── config ───────────────────────────────────────────────────────────────────

def _merge(base: dict, override: dict) -> dict:
    """Deep-merge `override` onto `base`. Agents merge BY NAME, not by position.

    Lists replace wholesale — a repo narrowing `tools` means exactly that list —
    with `agents` as the one exception, because merging by index would silently
    re-model the roster if the base ever reordered. An override naming `builder`
    edits the builder and leaves the other four alone.
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if key == "agents" and isinstance(current, list) and isinstance(value, list):
            by_name = {a.get("name"): dict(a) for a in current if isinstance(a, dict)}
            for entry in value:
                if not isinstance(entry, dict) or "name" not in entry:
                    raise ConfigError("config: every entry under `agents:` needs a `name`")
                name = entry["name"]
                by_name[name] = _merge(by_name.get(name, {}), entry)
            merged[key] = list(by_name.values())
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _read_raw(path: Path, paths, seen: list[str], depth: int = 0) -> dict:
    """Load one config file, resolving `extends:` beneath it first.

    `extends` is what keeps a target repo's config small. The rosters and the
    stock workflows ship with the factory; a repo says which roster it wants and
    then states only what is genuinely its own — its quality commands, its
    paths, its overrides. Without it every repo would carry a verbatim copy of
    the five-agent roster, which is the duplication this migration exists to end.
    """
    if depth > MAX_EXTENDS_DEPTH:
        raise ConfigError(f"config: `extends` nested more than {MAX_EXTENDS_DEPTH} deep "
                          f"(cycle?) — chain: {' -> '.join(seen)}")
    resolved = str(path)
    if resolved in seen:
        raise ConfigError(f"config: `extends` cycle — {' -> '.join([*seen, resolved])}")
    seen.append(resolved)

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as error:
        raise ConfigError(f"config not found: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"config {path} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path}: expected a mapping at the top level")

    parents = raw.pop("extends", []) or []
    if isinstance(parents, str):
        parents = [parents]
    merged: dict = {}
    for parent in parents:
        merged = _merge(merged, _read_raw(paths.resolve_asset(parent), paths,
                                         seen, depth + 1))
    return _merge(merged, raw)


def load_config(path: str, paths) -> SSSFConfig:
    """Read a target repo's config: resolve `extends`, then resolve every asset.

    Asset refs become ABSOLUTE here, once, because the CLI chdir's into the
    target repo afterwards. Downstream code keeps reading `agent.prompt_engineering
    .system` as a path and never learns that the file might have come from the
    factory rather than the repo.
    """
    raw = _read_raw(Path(path).expanduser().resolve(), paths, seen=[])
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in INHERITED:
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))

    try:
        cfg = SSSFConfig(**raw)
    except Exception as error:                 # pydantic ValidationError et al
        raise ConfigError(f"config {path} is invalid:\n{error}") from error

    # `writes:` has to follow `paths:` or the two disagree: a planner allowed only
    # `specs/` cannot write the plan into a repo that keeps specs in docs/rfcs/,
    # and the phase dies on a permission breach the operator never configured.
    # Same two placeholders the prompts use, resolved in the allowlist.
    path_vars = {"{{specs_dir}}": cfg.paths.specs.rstrip("/"),
                 "{{docs_dir}}": cfg.paths.docs.rstrip("/")}
    for agent in cfg.agents:
        if agent.writes:
            resolved_writes = []
            for pattern in agent.writes:
                for token, value in path_vars.items():
                    pattern = pattern.replace(token, value)
                resolved_writes.append(pattern)
            agent.writes = resolved_writes

    for agent in cfg.agents:
        for label in ("system", "user"):
            ref = getattr(agent.prompt_engineering, label)
            try:
                setattr(agent.prompt_engineering, label, str(paths.resolve_asset(ref)))
            except FileNotFoundError as error:
                raise ConfigError(f"agent {agent.name!r}: {label} prompt — {error}") from error
        resolved_extensions = []
        for ref in agent.harness_engineering:
            try:
                resolved_extensions.append(str(paths.resolve_asset(ref)))
            except FileNotFoundError as error:
                raise ConfigError(f"agent {agent.name!r}: harness extension — {error}") from error
        agent.harness_engineering = resolved_extensions
    return cfg


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent."""
    problems = []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if agent.coding_agent not in IMPLEMENTED:
            problems.append(f"agent {name!r}: coding_agent {agent.coding_agent!r} "
                            f"is not implemented — available: {list(IMPLEMENTED)}")
            continue                 # its model cannot be resolved either
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        try:
            # Each harness validates model ids its own way: pi against the merged
            # `pi --list-models` catalog, codex against its allowlist. Asking the
            # wrong one is how a valid subscription model gets reported missing.
            harness(agent).resolve_model(agent.model)
        except ValueError as e:
            problems.append(f"agent {name!r}: {e}")
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> pi run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    module = harness(agent)                  # pi or codex; same four names either way
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Trailing slashes are stripped so a prompt writes `{{specs_dir}}/<name>.md`
    # and reads naturally whether the config said "specs" or "specs/".
    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
        # Without these two, `paths:` in config would be a setting nothing reads:
        # the stock prompts named `specs/` and `app_docs/` literally, so a repo
        # that keeps plans elsewhere was overridden by its own agent's prompt.
        "specs_dir": run.cfg.paths.specs.rstrip("/"),
        "docs_dir": run.cfg.paths.docs.rstrip("/"),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    session_id = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME pi session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: PiResult | None = None
    spent = UsageBreakdown()

    def send(prompt_text: str) -> PiResult:
        nonlocal latest
        request = PiRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session_id,
            # absolute: these are read by the pi subprocess, which runs in repo_root
            # Per harness, because the two keep different things here: pi its own
            # session files, codex the thread id that a resume needs.
            session_dir=str((agent_dir / f"{agent.coding_agent}_sessions").resolve()),
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            tools=agent.tools,
            extensions=agent.harness_engineering,
            cwd=str(run.repo_root),
            sandbox=_sandbox_for(agent),
        )
        result = module.run(
            request,
            on_event=_event_forwarder(run, phase, agent.name, module),
            on_spawn=lambda pid: run.tracer.process_start(
                run.adw_id, "agent", agent.name, pid,
                f"{agent.coding_agent} {agent.name} {agent.model}"),
            on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        latest = result
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline. The bytes go with it, so an
    # out-of-scope change to work that was already uncommitted can be put back
    # instead of only reported — the difference between a run that continues
    # and a run that dies on someone else's scratch file.
    tree_before = permissions.snapshot(run)
    preserved_before = permissions.preserve(run, tree_before)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            violations.extend(found)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before,
                                      preserved_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files}))
        raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                    "coding_agent": agent.coding_agent})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    if entry and entry.get("model") == agent.model:
        return entry["session_id"]           # rejoin the existing context window
    return f"sssf-{run.adw_id}-{agent.name}-{new_id(4)}"


def _sandbox_for(agent: AgentConfig) -> str:
    """An agent that may change nothing in the repo gets a read-only sandbox.

    `writes: []` was previously only detectable after the fact. Where a harness
    can enforce it (codex --sandbox), it now also cannot happen. Anything more
    specific than "nothing at all" — `writes: ["specs/"]` — is beyond what a
    sandbox can express, so those still rely on permissions.py.
    """
    return agent_codex.SANDBOX_READ_ONLY if agent.writes == [] \
        else agent_codex.SANDBOX_WRITE


def _event_forwarder(run, phase: Phase, agent_name: str, module):
    """One tool_call event per real tool call, with its exact args and result —
    plus one thinking / one agent_message event per COMPLETE assistant message.
    Complete messages only, never message_update deltas: the extraction reads
    message_end alone (see agent_pi.assistant_message_records)."""
    tracker = module.ToolCallTracker()

    def forward(event: dict) -> None:
        for message in module.assistant_message_records(event):
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type=message.pop("kind"),
                                         name=message.pop("label"),
                                         payload={**message, "agent": agent_name}))
        record = tracker.observe(event)
        if record is None:
            return
        # The call's span rides the columns; duration_ms stays in the payload as
        # pi's own authoritative number.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=record.pop("label"),
                                     started_at=record.pop("started_at", None),
                                     ended_at=record.pop("ended_at", None),
                                     payload={**record, "agent": agent_name}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    budget = getattr(run.cfg.limits, "json_fix_attempts", JSON_FIX_ATTEMPTS)
    for attempt in range(1, budget + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > budget:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, budget,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2))
