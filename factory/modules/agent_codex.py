"""Codex CLI as a coding agent — the slot `agent_cc.py` stubbed and never filled.

Same module interface as `agent_pi`, so `agents.execute` dispatches on
`coding_agent` and nothing else in the engine changes: `resolve_model`,
`assistant_message_records`, `ToolCallTracker`, `run`.

## Why this exists beside pi rather than replacing it

Two billing models, and the split is the point. Codex authenticates against a
ChatGPT subscription and serves closed models (gpt-6-astra, gpt-5.6-sol/terra/
luna/pro); OpenRouter is used exclusively for open-weight models through pi. A
roster can therefore put judgement on a subscription model and code generation on
an open-weight one, and pay for each the way it is actually sold.

## What Codex gives that pi does not

  --sandbox        a real enforcement boundary (read-only / workspace-write),
                   so `writes: []` becomes something the OS refuses rather than
                   something we detect afterwards. Permission checking still runs
                   — belt and braces — but the belt is now real.
  exec resume      resumes a thread by id, which is exactly what a gate
                   correction needs: same context, bounded retries.

## Four things measured on live runs, each of which would otherwise bite

1. `item.type == "error"` is INFORMATIONAL. A first probe returned four of them
   — hook-trust notices, a skill-budget warning — on a turn that succeeded and
   returned the right answer. Treating them as failures would fail every run.

2. `--ignore-user-config` removes all of that noise, and is correct for a
   factory regardless: the operator's `config.toml` carries MCP servers (which
   emitted OAuth transport errors on the probe), notify hooks, and a default
   model. A phase must depend on none of them. Auth still resolves, because auth
   lives in CODEX_HOME rather than in config.toml.

3. The `codex` on PATH may not be Codex. On this machine it is a cmux shim that
   injects `--dangerously-bypass-hook-trust`; the probe's stray error items came
   from exactly that. `_binary()` therefore skips shim directories.

4. `usage` carries NO cost. Subscription billing is not metered per token, so
   cost is reported as 0.0 and the token counts are real. A codex lane showing
   $0.0000 beside real tokens in the trace is correct, not a bug — and it is why
   `just obs costs` cannot be read as total spend for a mixed roster.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult
from .utils import now_iso, operator_env

# Confirmed present in codex-cli 0.153.4. Kept as an allowlist so a typo in a
# roster is a config error at `sf check` rather than a failure three phases into
# a run — the same guarantee `resolve_model` gives the pi side. Extend without a
# code change via CODEX_EXTRA_MODELS (comma-separated).
KNOWN_MODELS = {
    "gpt-6-astra",
    "gpt-5.6-pro", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
    "gpt-5.3-codex", "gpt-5.2-codex", "gpt-5.1-codex",
}

PROVIDER = "codex"

# pi's thinking vocabulary mapped onto Codex's reasoning effort. Codex has no
# "off": the floor is minimal, and saying so here beats silently sending a value
# it will reject.
REASONING = {
    "off": "minimal", "minimal": "minimal", "low": "low",
    "medium": "medium", "high": "high", "xhigh": "high", "max": "high",
}

# An agent that may not modify the repo gets a sandbox that cannot, rather than a
# promise that it will not.
SANDBOX_READ_ONLY = "read-only"
SANDBOX_WRITE = "workspace-write"

RESULT_SNIPPET_CHARS = 20_000
LABEL_CHARS = 80


def _binary() -> str:
    """The real Codex binary, skipping wrapper shims that inject flags."""
    override = os.environ.get("CODEX_PATH", "").strip()
    if override:
        return override
    for candidate in _which_all("codex"):
        if "cli-shims" not in candidate and "cmux" not in candidate:
            return candidate
    return shutil.which("codex") or "codex"


def _which_all(name: str) -> list[str]:
    found = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(str(candidate))
    return found


def _known_models() -> set[str]:
    extra = os.environ.get("CODEX_EXTRA_MODELS", "")
    return KNOWN_MODELS | {m.strip() for m in extra.split(",") if m.strip()}


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve `codex/<model>` (or a bare model id) to ``(provider, model_id)``.

    Deliberately NOT a catalog lookup the way the pi side does it: Codex exposes
    no `--list-models`, so the honest options were an allowlist or no validation
    at all. An allowlist that can be extended by env keeps the typo caught.
    """
    model_id = pattern.split("/", 1)[1] if pattern.startswith(f"{PROVIDER}/") else pattern
    if "/" in model_id:
        raise ValueError(
            f"model {pattern!r} looks like a provider-routed id — codex serves its own "
            f"subscription models, so write `codex/gpt-6-astra` or `gpt-6-astra`. "
            f"Route open-weight models through pi instead.")
    known = _known_models()
    if model_id not in known:
        raise ValueError(
            f"model {model_id!r} is not a known codex model — known: "
            f"{sorted(known)}. Add it with CODEX_EXTRA_MODELS if this version serves it.")
    return PROVIDER, model_id


def context_window(provider: str, model_id: str) -> int:
    """0 — Codex declares no window through its CLI.

    The field's documented meaning is already "no ceiling declared", so a guess
    here would put an invented denominator under the visualizer's context bar.
    """
    return 0


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [{len(text)} chars]"


def assistant_message_records(event: dict) -> list[dict]:
    """Complete assistant messages and reasoning, as trace records.

    Codex emits whole items, never keystroke deltas, so unlike the pi side there
    is nothing to filter out — `item.completed` IS the complete-thought unit.
    """
    if event.get("type") != "item.completed":
        return []
    item = event.get("item") or {}
    kinds = {"agent_message": "agent_message", "reasoning": "thinking"}
    kind = kinds.get(item.get("type", ""))
    if not kind:
        return []
    body = item.get("text") or item.get("summary") or ""
    if not body.strip():
        return []
    return [{
        "kind": kind,
        "label": _clip(" ".join(body.split()), LABEL_CHARS),
        "text": _clip(body, RESULT_SNIPPET_CHARS),
        "stop_reason": "",           # Codex reports no per-message stop reason
    }]


class ToolCallTracker:
    """One normalized record per completed Codex item that did something.

    Codex brackets work with `item.started` / `item.completed` carrying the same
    item id, so the start is where the clock begins and the completion is where
    the record is emitted — one trace event per real call, matching the pi side's
    contract exactly so the tracer and UI need no special case.
    """

    # Item types that represent work rather than narration.
    WORK = {"command_execution", "file_change", "mcp_tool_call", "web_search",
            "patch_apply", "todo_list"}

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        etype = event.get("type", "")
        item = event.get("item") or {}
        item_type = item.get("type", "")
        if item_type not in self.WORK:
            return None
        item_id = str(item.get("id") or "")

        if etype == "item.started":
            if item_id:
                self._open[item_id] = {"started_at": now_iso(), "clock": time.monotonic()}
            return None
        if etype != "item.completed":
            return None

        opened = self._open.pop(item_id, {})
        args = self._args(item)
        record = {
            "tool": item_type,
            "tool_call_id": item_id,
            "args": args,
            # exit_code is absent on non-command items; absence is not failure.
            "ok": item.get("exit_code") in (0, None) and item.get("status") != "failed",
            "label": self._label(item_type, args),
            "ended_at": now_iso(),
        }
        output = item.get("aggregated_output") or item.get("output") or ""
        if output:
            record["result_snippet"] = _clip(str(output), RESULT_SNIPPET_CHARS)
        if opened.get("started_at"):
            record["started_at"] = opened["started_at"]
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        return record

    @staticmethod
    def _args(item: dict) -> dict:
        """The item's inputs, without its (possibly huge) output."""
        drop = {"aggregated_output", "output", "id", "type", "status"}
        return {k: (_clip(v, RESULT_SNIPPET_CHARS) if isinstance(v, str) else v)
                for k, v in item.items() if k not in drop}

    @staticmethod
    def _label(item_type: str, args: dict) -> str:
        value = next((args[k] for k in ("command", "path", "query", "url", "changes")
                      if isinstance(args.get(k), str) and args[k].strip()), "")
        value = " ".join(str(value).split())
        return f"{item_type}: {_clip(value, LABEL_CHARS)}" if value else item_type


def _thread_file(request: PiRequest) -> Path:
    """Where this logical session's Codex thread id is remembered.

    The engine's session id is its own invention; Codex mints a thread id on the
    first turn. A gate correction must land in the SAME thread, and it arrives as
    a separate `run()` call in a separate process, so the mapping goes on disk
    rather than in memory.
    """
    return Path(request.session_dir) / f"{request.session_id}.thread"


def _usage_into(result: PiResult, usage: dict) -> int:
    """Fold one `turn.completed` usage object in. Returns the turn's tokens.

    Codex reports `input_tokens` INCLUSIVE of cached input; pi's UsageBreakdown
    defines `input` as excluding cache reads. The subtraction is what makes a
    mixed-roster trace add up instead of double-counting cached prompt.
    """
    cached = usage.get("cached_input_tokens") or 0
    fresh_input = max((usage.get("input_tokens") or 0) - cached, 0)
    output = usage.get("output_tokens") or 0
    turn = fresh_input + cached + output
    result.usage.add_turn({
        "input": fresh_input,
        "output": output,
        "cacheRead": cached,
        "cacheWrite": usage.get("cache_write_input_tokens") or 0,
        "reasoning": usage.get("reasoning_output_tokens") or 0,
        "cost": {},              # subscription billing: no per-token cost exists
    }, turn)
    result.tokens += turn
    return turn


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive Codex turn, resuming the thread if one exists."""
    _, model_id = resolve_model(request.model)
    thread_file = _thread_file(request)
    thread_file.parent.mkdir(parents=True, exist_ok=True)
    thread_id = thread_file.read_text().strip() if thread_file.is_file() else ""

    last_message = Path(request.session_dir) / f"{request.session_id}.last.txt"
    sandbox = request.sandbox or SANDBOX_WRITE

    cmd = [_binary(), "exec"]
    if thread_id:
        # Same thread, so a gate correction reads its own previous attempt.
        cmd += ["resume", thread_id]
    cmd += [
        "--json",
        "--ignore-user-config",      # see module docstring, note 2
        "--skip-git-repo-check",
        "--color", "never",
        "-m", model_id,
        "-c", f"model_reasoning_effort={REASONING.get(request.thinking, 'medium')}",
        "-s", sandbox,
        "-C", request.cwd,
        "-o", str(last_message),
    ]
    # Codex has no --system-prompt. The system text is prepended to the first
    # turn's prompt under a heading instead; on a resume it is already in the
    # thread's context and must not be repeated.
    prompt = request.prompt if thread_id else (
        f"# Operating instructions\n\n{request.system_prompt}\n\n"
        f"# Task\n\n{request.prompt}")
    cmd.append(prompt)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=thread_id or request.session_id,
                      context_window=context_window(PROVIDER, model_id))
    # stdin DEVNULL for the same reason as the pi side: the prompt rides in argv,
    # and an inherited stdin makes the child wait on input that never comes. The
    # probe printed "Reading additional input from stdin..." when it was left
    # open, which is precisely that wait beginning.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)

    informational: list[str] = []
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in process.stdout:
            raw.write(line)
            raw.flush()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            if etype == "thread.started" and event.get("thread_id"):
                result.session_id = event["thread_id"]
                thread_file.write_text(result.session_id)
            elif etype == "turn.completed":
                turn = _usage_into(result, event.get("usage") or {})
                if turn:
                    result.context_tokens = turn
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and (item.get("text") or "").strip():
                    result.text = item["text"]          # last message wins
                elif item.get("type") == "error":
                    # Informational, not fatal — note 1. Kept only to explain a
                    # genuinely empty result at the end.
                    informational.append(str(item.get("message", ""))[:200])
            if on_event:
                on_event(event)

    stderr = process.stderr.read() if process.stderr else ""
    result.returncode = process.wait()
    if on_exit:
        on_exit(process.pid)

    # `-o` is the authoritative final message; the event stream is the narration.
    if not result.text and last_message.is_file():
        result.text = last_message.read_text().strip()

    if result.returncode != 0 and not result.text:
        detail = stderr.strip()[-800:] or "; ".join(informational[-3:])
        raise RuntimeError(f"codex exited {result.returncode}: {detail}")
    return result
