# software-factory

> One factory checkout. Many repos. No VM, no account, no provisioning key.

Deterministic Python owns the phase graph; coding agents are bounded nodes inside
it. **Agent proposes, code disposes.** Extracted from
[disler/super-simple-software-factory](https://github.com/disler/super-simple-software-factory)
and restructured around three changes:

| Was | Is |
| --- | --- |
| Stamped into each repo as a copy | One shared checkout, target passed with `--repo` |
| Quality commands hardcoded in Python | Named entries in the target repo's config |
| One Python file per workflow | Declared phase graphs, rendered back as a chain |

The sandbox tier is **not** part of this. The original ran agents inside a
disposable exe.dev VM; that tier needed a paid account and a provisioning key, and
none of it is here. Isolation is a `git worktree` (`just worktree`) — your call,
not a dependency.

---

## Install

```bash
brew install just uv          # or your package manager of choice
curl -fsSL https://raw.githubusercontent.com/badlogic/pi-mono/main/install.sh | sh   # pi
echo "OPENROUTER_API_KEY=sk-or-..." > .env

just config doctor            # git, uv, pi, key, model resolution, check binaries
```

`pi` is the harness and it owns the model catalog: it ships a live-fetched store
of hundreds of models, so if `pi --list-models` shows what your roster names, you
are done — **no `models.json` editing required**, and editing it to "add" a model
the store already has just duplicates it with prices that drift.

Auth is per provider. `OPENROUTER_API_KEY` covers `openrouter/…`; the
`openai-codex/…` models come from a ChatGPT subscription that pi authenticates
over OAuth, with no key in `.env`. Check what you have:

```bash
pi --list-models | awk '$1=="openai-codex"{print $2}'   # subscription models
```

`codex` (the CLI) is optional — only `coding_agent: codex` needs it. See
[`docs/MODELS.md`](docs/MODELS.md).

## Use it on a repo

```bash
cd ~/Projects/my-app
FACTORY=~/Developer/software-factory

just -f $FACTORY/justfile config init      # detects your stack, writes sssf.config.yaml
$EDITOR sssf.config.yaml                   # read what it inferred; fix or delete
just -f $FACTORY/justfile config check     # validates everything, spends nothing
just -f $FACTORY/justfile quality "smoke"  # deterministic only — still no spend
just -f $FACTORY/justfile sdlc "add a word-count badge to the editor footer"
```

`just install-alias` prints a shell alias so `sf` works from anywhere without the
`-f` dance.

Your repo holds exactly two things: `sssf.config.yaml`, and a gitignored
`.sssf/data/` for run artifacts and the trace db.

---

## The three config layers

Each answers one question, and only the third is yours to write.

```
config/base.yaml        WHAT runs      stock workflows, loop bounds, paths
config/rosters/*.yaml   WHO runs it    model staffing per agent
<your repo>/sssf.config.yaml   AGAINST WHAT   your commands, your overrides
```

`extends:` chains them, and merges are by name — an override naming `builder`
edits the builder and leaves the other four alone. `just config layers` prints the
resolution order for any repo.

A minimal repo config is short on purpose, and `sf init` drafts it by reading your
stack — npm/bun/pnpm/yarn (the lockfile picks the frozen-install form), uv, cargo,
SwiftPM, go, plus `package.json` scripts and `justfile` recipes:

```yaml
extends: config/rosters/default.yaml

prepare:                      # first phase of every workflow; failure aborts the run
  install:
    argv: [npm, ci]

quality:
  checks:
    test:
      argv: [bun, test]
      timeout_seconds: 600
  groups:
    test: [test]
    full: [test]
```

`prepare:` is what makes an isolated worktree usable: a fresh one has no
`node_modules`, so without it every check fails for reasons unrelated to the code
and a builder burns its repair budget on a missing dependency.

Full reference: [`docs/CONFIG.md`](docs/CONFIG.md). Models, providers, and
harnesses: [`docs/MODELS.md`](docs/MODELS.md). Multi-submodule workspaces —
**design input, nothing implemented** — [`docs/MULTI-PROJECT.md`](docs/MULTI-PROJECT.md). Worked examples:
[`examples/inkwell.sssf.yaml`](examples/inkwell.sssf.yaml) (Bun app) and
[`examples/khaos-publisher.sssf.yaml`](examples/khaos-publisher.sssf.yaml) (npm
workspace monorepo).

## Models

`model:` is `<pi-provider>/<model-id>`, validated against `pi --list-models`
before a run starts. Six rosters ship; `just config rosters` lists them.

```bash
sf run sdlc "<work>" --repo . --roster codex-open
```

`codex-open` staffs judgement from a ChatGPT subscription and code from open
weights, with no Anthropic models:

| lane | model | billing |
| --- | --- | --- |
| planner | `openai-codex/gpt-6-astra` | subscription |
| reviewer | `openai-codex/gpt-5.6-sol` | subscription |
| builder | `openrouter/moonshotai/kimi-k3` | metered |
| scout, documenter | `openrouter/deepseek/deepseek-v4-flash-0731` | metered |

Costs in the trace are computed from catalog list rates, so a subscription lane's
dollar figure is notional — what those tokens would have cost metered, not what you
were billed. Details, plus how to add providers or local models, in
[`docs/MODELS.md`](docs/MODELS.md).

## Workflows

Twelve ship. `just config list` shows them for your repo, `just config explain
<name>` renders one:

```
$ just config explain simple_sdlc
chain:  engineer(request) -> planner(plan) -> git(commit_plan) -> builder(build)
        -> code(test) [-> builder(fix) -> code(test) ... x3]
        -> reviewer(review) [-> builder(revise) -> reviewer(review) ... x2]
        -> code(retest) if revised_and_approved
        -> if verified: (git(commit_build) -> code(changes)
                         -> documenter(document) -> git(commit_docs))
```

That chain is **rendered from the config**, not written by hand, so it cannot
drift from what runs. Declaring your own is a `workflows:` block in your repo's
config; it merges with the stock set rather than replacing it.

Eight step kinds: `request`, `agent`, `quality`, `verify_loop`, `review_loop`,
`changes`, `commit`, `group`.

## Watching a run

```bash
just obs sessions            # every run in this repo: workflow, status, spend
just obs phases   <adw_id>   # each phase, owner, status, duration
just obs gates    <adw_id>   # what every gate checked and found
just obs envelopes <adw_id>  # the typed output each agent produced
just obs tail     <adw_id>   # follow a live run
just obs costs               # spend per agent and per model
just obs artifacts <adw_id>  # prompts, diffs, and check logs on disk
```

Everything streams into `.sssf/data/sssf.db` as it happens (WAL, so reads never
block the writer). The visualizer UI from the original repo was **not** migrated;
these queries are the read surface for now.

---

## What is verified, and what is not

The original repo's main failure was documentation that outran the code, so:

**Verified on this machine.** The 12 workflows load, validate, and render. Path
resolution across two roots works. The `quality` workflow runs end to end against
a scratch repo — phases open and close, checks execute, artifacts land, the trace
records, and a red check fails the *run* (exit 1) while the phase that ran it
still succeeds. All six rosters validate and every model they name resolves
through `pi`, including the subscription models on `codex-open`.

**The agent path is verified too, once.** A `plan` workflow ran end to end against
a real npm monorepo: 16m45s, one agent phase, 3.7M tokens, and a 51KB spec written
to the configured `specs:` directory — with the planner's `writes` allowlist
holding, so nothing else in that repo was touched. Gates passed, the trace recorded
every tool call, and the session closed clean.

**Still unexercised: the loops.** No run has yet driven `verify_loop` (a failing
check going back to the builder), `review_loop`, or any commit phase. Those are the
original's code with the config changes described here. Run the first one on a
`git worktree` (`just worktree`), not on a branch you care about.

**`coding_agent: codex` is present but UNVERIFIED.** The Codex CLI adapter
(`factory/modules/agent_codex.py`) was written against two live probe runs of
`codex exec --json` — event shapes, usage mapping, and thread resume are real, but
no workflow has run through it. Its draw is that `--sandbox read-only` makes
`writes: []` an OS boundary instead of an after-the-fact check. The same models
are reachable on the verified path with `coding_agent: pi` and an
`openai-codex/…` model, so prefer that unless you want the sandbox.

**Known limits, carried over.** `coding_agent: claude_code` is still a stub that
raises. `--owner` applies only to workflows with exactly one agent step.
`best-of-n` is sequential, because parallel runs against one working tree would
fight over the same files.

**One inconsistency inherited from upstream, left alone deliberately.** Four of the
five rosters restrict the planner and documenter with `writes:`; `frontier` declares
neither, so under that roster both agents are unrestricted (except
`protected_files`). Tightening it would change a roster's safety semantics, which is
your call, not a migration's — but know it before you run `--roster frontier`.

## Layout

```
factory/          the engine
  cli.py          entrypoint: run / list / explain / check / doctor / init
  engine.py       the workflow interpreter and chain renderer
  paths.py        the two roots — factory_home vs repo_root
  modules/        envelopes, gates, permissions, tracer, pi interface
config/
  base.yaml       stock workflows, limits, paths
  rosters/        five model rosters
  prompts/        default agent prompts (a repo can override any of them)
  harness/        pi extensions
just/             config and obs recipe modules
scripts/sf.py     the `sf` entrypoint
examples/         a worked repo config
```

## Credit

A derivative work. The engine — typed envelopes, gates, after-the-fact permission
enforcement, the tracer, the session model, the pi interface — plus the agent
prompts and the model rosters are **IndyDevDan's**, from
[disler/super-simple-software-factory](https://github.com/disler/super-simple-software-factory),
and the design ideas are his: deterministic code owning the graph, agents as
bounded phases, gates verifying claims, a known command being code rather than a
judgement call. Read his repo and
[watch the breakdown](https://youtu.be/SEI_qIW4o2c) — this is a restructuring of
his work, not a replacement for it.

What is new here is packaging: one checkout serving many repos, the phase graph
declared rather than coded, and the quality layer moved from Python into config.

MIT, as the upstream is. Both copyright notices are in [`LICENSE`](LICENSE).
