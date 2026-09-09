# just/config.just — inspect and validate configuration. Nothing here runs work.
#
# A module inherits nothing from the root justfile — not variables, not settings —
# so both are re-declared here. Two measured surprises are why the paths look the
# way they do: inside a module `justfile_directory()` is still the ROOT justfile's
# directory (NOT this file's — use `source_file()` for that), and recipes run from
# there rather than from where you typed the command. So factory-owned paths hang
# off `justfile_directory()` with no `..`, and a repo argument defaults to
# `invocation_directory()` rather than to ".".
set working-directory := '.'
set positional-arguments
set dotenv-load

SF := justfile_directory() / "scripts/sf.py"

# list the config commands
default:
    @just --list config

# what this repo's config resolves to: agents, checks, workflows
list *ARGS:
    uv run {{SF}} list "$@"

# render one workflow's phase chain, bounds, and acceptance rule
explain WORKFLOW *ARGS:
    uv run {{SF}} explain {{WORKFLOW}} "${@:2}"

# validate config + every workflow. No agent spawns, no spend.
check *ARGS:
    uv run {{SF}} check "$@"

# host prerequisites for THIS machine: git, uv, pi, key, models, check binaries
doctor *ARGS:
    uv run {{SF}} doctor "$@"

# write a starter sssf.config.yaml into a repo
init *ARGS:
    uv run {{SF}} init "$@"

# the model rosters that ship with this factory
rosters:
    #!/usr/bin/env bash
    set -euo pipefail
    for f in {{justfile_directory()}}/config/rosters/*.yaml; do
        name=$(basename "$f" .yaml)
        # An explicit `# roster:` field, not "the first comment": the shared
        # `extends` header sits above it and was being reported instead.
        note=$(grep -m1 '^# roster: ' "$f" | sed 's/^# roster: //')
        printf '  %-14s %s\n' "$name" "$note"
    done

# every model a roster names, and which agents use it
models:
    #!/usr/bin/env bash
    set -euo pipefail
    grep -h 'model:' {{justfile_directory()}}/config/rosters/*.yaml \
        | sed 's/.*model: *//; s/ *#.*//' | sort | uniq -c | sort -rn

# show the three config layers for a repo, in resolution order
layers REPO=invocation_directory():
    #!/usr/bin/env bash
    set -euo pipefail
    echo "1. factory defaults   {{justfile_directory()}}/config/base.yaml"
    echo "2. roster (staffing)  whichever config/rosters/*.yaml your config extends"
    repo=$(cd "{{REPO}}" && pwd)
    for candidate in sssf.config.yaml .sssf/config.yaml .sssf/sssf.config.yaml; do
        if [ -f "$repo/$candidate" ]; then
            echo "3. this repo          $repo/$candidate"
            exit 0
        fi
    done
    echo "3. this repo          (none — run: just config init --repo $repo)"
