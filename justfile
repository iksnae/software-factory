# software-factory — one factory, many repos.
#
# Every recipe takes the target repo as its first argument, defaulting to the cwd.
# That default is what makes the shared checkout comfortable: `cd` into a project,
# run `just -f ~/Developer/software-factory/justfile sdlc "..."`, or add the shell
# alias from `just install-alias` and just type `sf`.
#
# THREE NAMESPACES, and the namespace answers WHAT you are doing:
#   (root)   run work:        sdlc / simple-sdlc / scout / plan / build / quality / ...
#   config   inspect config:  list / explain / check / doctor / init / rosters
#   obs      read the trace:  sessions / phases / events / tail / gates / costs

set positional-arguments
set dotenv-load

# A module inherits NOTHING — not variables, not settings, not the working
# directory — so each one re-declares what it needs. Every missing line fails in
# a different silent way, which is why they are repeated rather than centralised.
#
# The comment directly above a `mod` becomes its help text, so each gets its own.

# inspect and validate configuration
mod config 'just/config.just'

# read the trace of past and running work
mod obs 'just/obs.just'

SF := justfile_directory() / "scripts/sf.py"

# just runs recipes with cwd set to the JUSTFILE directory, not the one you
# typed in. Left alone, `--repo .` would mean the factory itself. Every run
# recipe therefore passes the invocation directory explicitly, BEFORE "$@",
# so an explicit --repo later on the line still wins.
HERE := invocation_directory()

# show what this factory can do
default:
    @just --list --unsorted
    @echo ""
    @echo "config:  just config    (list / explain / check / doctor / init / layers)"
    @echo "trace:   just obs       (sessions / phases / tail / gates / costs)"

# ─── run work ────────────────────────────────────────────────────────────────
# Each recipe is one `sf run <workflow>`. The extra ARGS pass straight through,
# so --repo, --roster, --adw-id, and --owner all work on every one of them.

# read-only recon: where does X live? (no writes, no commits)
scout *ARGS:
    uv run {{SF}} run scout --repo {{HERE}} "$@"

# ask one agent one question: just ask planner "..." — any roster agent
ask AGENT *ARGS:
    uv run {{SF}} run ask --owner {{AGENT}} --repo {{HERE}} "${@:2}"

# turn a request into a spec, no code
plan *ARGS:
    uv run {{SF}} run plan --repo {{HERE}} "$@"

# one-shot implementation, no plan and no verification
build *ARGS:
    uv run {{SF}} run build --repo {{HERE}} "$@"

# planner then builder
plan-build *ARGS:
    uv run {{SF}} run plan_build --repo {{HERE}} "$@"

# builder then the suite, with a bounded repair loop
build-test *ARGS:
    uv run {{SF}} run build_test --repo {{HERE}} "$@"

# builder then a reviewer, with a bounded revision loop
build-review *ARGS:
    uv run {{SF}} run build_review --repo {{HERE}} "$@"

# plan → build → test/fix — the everyday chain
sdlc *ARGS:
    uv run {{SF}} run sdlc --repo {{HERE}} "$@"

# sdlc plus every deterministic check at the end
sdlc-quality *ARGS:
    uv run {{SF}} run sdlc_quality --repo {{HERE}} "$@"

# the full chain: plan → build → test → review → document, three commits
simple-sdlc *ARGS:
    uv run {{SF}} run simple_sdlc --repo {{HERE}} "$@"

# deterministic checks only — no agents, no spend
quality *ARGS:
    uv run {{SF}} run quality --repo {{HERE}} "$@"

# write up work already done, from the diff against main
document *ARGS:
    uv run {{SF}} run document --repo {{HERE}} "$@"

# any workflow by name, including one your repo defined itself
run WORKFLOW *ARGS:
    uv run {{SF}} run {{WORKFLOW}} --repo {{HERE}} "${@:2}"

# ─── fan out ─────────────────────────────────────────────────────────────────

# same prompt, every roster, sequentially — the local best-of-N
best-of-n PROMPT *ARGS:
    #!/usr/bin/env bash
    set -uo pipefail
    # Sequential on purpose. Parallel runs against ONE working tree would fight
    # over the same files; isolation is a git worktree away (see `worktree`), and
    # that is the operator's call, not this recipe's.
    for roster in $(ls {{justfile_directory()}}/config/rosters/*.yaml | xargs -n1 basename | sed 's/\.yaml$//'); do
        echo ""
        echo "─── roster: $roster ──────────────────────────────────────────"
        uv run {{SF}} run sdlc "{{PROMPT}}" --repo {{HERE}} --roster "$roster" "${@:2}" || echo "  roster $roster: run failed"
    done

# make an isolated worktree so a run cannot touch your working tree
worktree BRANCH REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    repo=$(cd "{{REPO}}" && git rev-parse --show-toplevel)
    dest="${repo}-{{BRANCH}}"
    [ -e "$dest" ] && { echo "worktree: $dest already exists" >&2; exit 1; }
    git -C "$repo" worktree add -b "{{BRANCH}}" "$dest"
    echo ""
    echo "isolated tree: $dest"
    echo "run against it:  just sdlc \"<work>\" --repo $dest"
    echo "remove it later: git -C $repo worktree remove $dest"

# ─── setup ───────────────────────────────────────────────────────────────────

# print a shell alias so `sf` works from anywhere
install-alias:
    @echo "# add to ~/.zshrc:"
    @echo "alias sf='uv run {{SF}}'"
