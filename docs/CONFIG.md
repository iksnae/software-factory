# Configuration reference

One file in your repo drives everything. This is what it may contain.

`just config check --repo <path>` validates all of it — every workflow, every
agent, every model, every check name — and spawns nothing. Run it after any edit.

---

## Resolution

```
config/base.yaml               ← workflows, limits, paths
  ↑ extends
config/rosters/<name>.yaml     ← agents (model staffing)
  ↑ extends
<your repo>/sssf.config.yaml   ← quality, overrides
```

`extends:` takes a path or a list. Files are searched in this order, which is also
the override mechanism for prompts and harness extensions:

1. `<repo>/.sssf/<ref>`
2. `<repo>/<ref>`
3. `<factory>/<ref>`

Drop a file at the same relative path inside your repo and it wins, with no config
edit. Merges are deep for mappings, wholesale for lists, **by name** for `agents:`
and `workflows:`.

Config discovery, when `--config` is not given: `sssf.config.yaml`, then
`.sssf/config.yaml`, then `.sssf/sssf.config.yaml`.

---

## `quality:` — required

The one block nobody can write for you. A factory that assumed `npm test` would be
confidently wrong in most repos.

```yaml
quality:
  checks:
    test:
      argv: [bun, test, apps/inkwell/server.test.ts]   # no shell: no word-splitting, no globbing
      area: backend            # free-form label, for grouping and display
      operation: test          # free-form label
      timeout_seconds: 600     # default 120; a timeout is exit 124
      description: The suite the verify loop runs after every build
    backend_typecheck:
      argv: [bun, build, --target=bun, server.ts, --outdir, "{outdir}"]

  groups:
    test: [test]                              # what verify_loop re-runs after a repair
    full: [test, backend_typecheck]           # what the `quality` workflow runs
```

- `{outdir}` becomes a per-check directory inside the session — for commands that
  must write a bundle somewhere.
- Commands run with `cwd` = your repo root and your own environment, minus the
  `uv` venv (so `python3` in a check is the same `python3` your shell gives you).
- A missing binary is exit 127 with the real OS error, not a crash.
- The stock workflows reference the groups `test` and `full`. Keep both defined
  even when they overlap.
- Every check name is resolved when the config loads. A typo is a startup error
  naming the unknown check — this is the class of bug that let the original ship
  two workflows calling a function that had been renamed.

## `agents:` — override without restating

```yaml
agents:
  - name: builder
    model: openrouter/anthropic/claude-sonnet-5
```

Per agent: `model`, `thinking` (off|minimal|low|medium|high|xhigh|max), `color`,
`purpose`, `tools`, `writes`, `harness_engineering`, `prompt_engineering.{system,user}`.

`writes` is enforced in code after every call, by comparing working-tree
change-sets — not by trusting `tools`. `bash` runs `git checkout` and `write`
reaches any path, so a capability list is a claim nothing verifies.

- `None` (key absent) — unrestricted, except `protected_files`
- `[]` — may modify nothing in the repo (still writes its own report)
- `["specs/"]` — only these; trailing `/` is a directory prefix, `*` stops at `/`

Naming a protected path in `writes` is what unlocks it.

## `paths:`, `limits:`, `defaults:`

Two placeholders carry `paths:` into the places that need it, so setting it once is
enough: `{{specs_dir}}` and `{{docs_dir}}` are substituted in **agent prompts** and
in **`writes:` allowlists** (quote them in YAML — bare `{{...}}` parses as a flow
mapping). Both are needed: a planner told to write `docs/rfcs/` but permitted only
`specs/` dies on a permission breach nobody configured.

```yaml
paths:
  specs: specs/          # where plans land
  docs: docs/            # where write-ups land

limits:
  max_fix_loops: 3       # verify_loop attempts
  max_revision_loops: 2  # review_loop attempts
  json_fix_attempts: 2   # malformed-JSON corrections, same agent session
  max_diff_lines: 2000   # diff artifact truncation

defaults:
  protected_files: [sssf.config.yaml, .sssf/]
  data_dir: .sssf/data
```

Gitignore `.sssf/data/`. The engine lives outside your repo, so no pattern is
needed to protect it — an agent cannot reach it at all.

## `workflows:` — the phase graph

Merges by name, so this adds to the stock twelve rather than replacing them.

```yaml
workflows:
  smoke:
    description: Build, then only the fast checks.
    accept: verified          # "" = every phase passing; "verified" = also the verdict
    pin_baseline: false       # capture HEAD before the first commit
    steps:
      - step: request
        description: Capture the incoming ask
      - step: agent
        name: build
        owner: builder
        output: BuildOutput
        gates: [diff_matches_claims]
        description: Implement the request directly
      - step: quality
        name: smoke
        checks: [test]
        description: Run only the suite
```

Every step needs a `description` that does not merely restate its `name` — it is
the only sentence the trace shows about intent, and an echo is rejected at
construction.

### Step kinds

| kind | required | does |
| --- | --- | --- |
| `request` | — | records the incoming ask |
| `agent` | `name`, `owner`, `output` | one agent call: typed envelope out, gates verified |
| `quality` | `name`, `checks`/`group` | runs deterministic checks once |
| `verify_loop` | `name`, `checks`/`group`, `repair` | checks, then a bounded agent repair |
| `review_loop` | `name`, `owner`, `output` | a verdict, then a bounded agent revision (`repair`) |
| `changes` | `name` | diffs the run for a documenter; `base:` is a ref or `baseline` |
| `commit` | `name`, `source` | commits in the words of `source`'s envelope |
| `group` | `steps` | nested steps, usually behind `when:` |

Shared fields: `previous:` hands an earlier step's envelope to an agent,
`retries:` sets gate-correction attempts, `when:` gates a step on
`verified` / `not_verified` / `revised_and_approved`, `max_attempts:` overrides
the matching entry in `limits`.

### Output types

`GenericOutput`, `PlanOutput`, `BuildOutput`, `ScoutOutput`, `ReviewOutput`,
`DocumentOutput`. Each agent's final JSON is parsed against the declared type; a
parse failure re-prompts the same session with a correction, bounded.

### Gates

| gate | verifies |
| --- | --- |
| `artifacts_exist` | every declared artifact is on disk |
| `files_non_empty` | none of them is zero bytes |
| `json_parses` | declared `.json` artifacts parse |
| `diff_matches_claims` | every file claimed changed exists |
| `verdict_consistent` | a review's verdict agrees with its own findings |

Gates check what is mechanically checkable. Violations go back to the **same**
agent session as a correction, with context intact. They do not judge quality —
that is a reviewer's job.

---

## Two things worth knowing

**Acceptance is two questions, not one.** A test phase that ran the suite did its
job even when the suite came back red: the *phase* succeeds, the *run* does not.
That is why `just obs sessions` can show `phases 2/2` beside `status fail`, and why
the exit code is 1.

**A revision invalidates a green result.** If a review revision edits code after
the checks last ran, the stock `simple_sdlc` re-runs them (`when:
revised_and_approved`) before committing. Any workflow that revises after
verifying should do the same, or it commits on evidence that predates the change.
