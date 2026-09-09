# Multi-project workspaces — design input, not shipped

**Status: NOTHING HERE IS IMPLEMENTED.** The factory today is rooted at exactly one
git repository: `--repo` sets a single `repo_root`, `cli.py` chdir's into it, and
every relative path afterwards means "inside that repo". This document records what
a real multi-submodule workspace demanded when it was reviewed read-only, so the
feature can be built against evidence instead of imagination.

It is filed as design input because the alternative — writing the feature list into
the README and building it later — is the exact failure this factory was extracted
to fix. See the "what is verified" section of the README for the standing rule.

The workspace reviewed is private, so it is described by shape rather than by name.
The patterns are what generalise; the repo names would not.

---

## The shape that was reviewed

A parent repository with **ten submodules**, each its own git repo on its own
remote, plus several non-submodule directories at the root.

What made it a useful specimen:

- **Every gitlink was ahead of the parent's index.** All ten submodule HEADs had
  moved past the pointer the parent recorded. That is the normal state of an active
  workspace, not a defect, and any commit design has to assume it.
- **Members sat on different branches** — some on the default branch, others on
  feature and docs branches. A run touching two members spans two branch contexts.
- **Three members had dirty working trees** at review time.
- **Stacks were mixed**: Go at the root of one member and in a subdirectory of
  another, Node in four, Node+Tauri in one, and three with no manifest at all.
- **Nine of ten exposed `just build` and `just test`; one exposed neither.**

## The parent already solved fan-out, and solved it the same way

The workspace's own root `justfile` dispatches to each member's own recipes, and
states the principle in a comment:

> The workspace does not know how any repo builds or tests itself — that belongs to
> the repo.

That is this factory's `quality:` block in one sentence. Its member list comes from
`.gitmodules` rather than a literal list, with the reason recorded: a copied list
had drifted and claimed ten members when there were nine.

**Design consequence.** The factory should not build its own fan-out. A workspace
that has one already is better served by `prepare`/`quality` commands that call it
(`argv: [just, test]` at the parent root), and a workspace that does not can be
given one. Reimplementing dispatch inside the engine would duplicate the thing the
target repo is entitled to own.

---

## Use cases, ordered by evidence

### U1 — one member, in place · works today, zero code

```bash
sf init --repo ~/workspace/member-repo
```

A submodule is an ordinary git repository. Detection reads its manifest, `prepare`
runs its install, commits land where the code lives. Nothing about the parent is
involved. **This is the recommended starting point for any workspace.**

### U2 — read-wide, write-narrow

An agent reads the whole workspace to reason about a change, while writes and
commits stay inside one member. The specimen showed why: two members carried
feature branches whose names described the same logical fix, so understanding
either required reading both.

Mechanically small: `PiRequest.cwd` becomes the workspace root while `repo_root`
stays the member. Every downstream assumption — git, permissions, checks — keeps
its single-repo shape. `writes` and `protected_files` stay member-relative, which
is what makes the narrow half enforceable.

### U3 — fan-out verification

`prepare` and the `quality` checks run the parent's existing dispatch. Needs only
an optional per-check `cwd:`, or nothing at all if the factory is rooted at the
parent for a verification-only workflow.

### U4 — coordinated cross-repo change with RELEASE coupling

The hard case, and harder than a monorepo. In the specimen, one member consumed
another **as a git-tag dependency** (`git+ssh://…#<tag>`) while the local
source of that dependency was already a version ahead.

A builder therefore *cannot* edit both members and test the result: the consumer's
install fetches the published tag from the remote, not the sibling working tree.
Verifying such a change requires either tagging the producer first, or injecting a
temporary override (`file:`/`link:`/resolutions) that must then be removed before
anything is committed.

This is a release pipeline, not a build step. Defer it.

### U5 — pointer hygiene

After a member commit, the parent's gitlink must be staged and committed or the
work is invisible at workspace level. Ordering matters and the failure is
asymmetric: commit the member, fail to commit the parent, and you have left a
member commit that nothing references.

### U6 — occupancy-aware dispatch

See the next section. It is the reason the sequencing below starts where it does.

---

## Concurrency is the real hazard, and the factory is implicated

The reviewed workspace documents, in its own agent instructions, a near-miss in
which one actor branched and popped a stash on top of another agent's in-flight
staged deletions. Nineteen deletions appeared that nobody had made. It was backed
out "by luck, not by process".

Its remedy is a process-list check for peer agent sessions, and its instructions
name the gap in that instrument precisely:

> A subagent you dispatched yourself occupies a tree exactly as a peer does and
> never appears in this list at all … the one occupant you are most likely to have
> created is the one occupant the instrument cannot observe, and its silence reads
> as *safe to branch here*.

**The factory is that dispatcher.** It spawns its coding agent as a child process,
so it is invisible to any peer-session check by construction. A factory that
gained multi-member commits without addressing this would be a machine for
reproducing that incident at speed.

**The instrument already exists inside the factory.** The trace's `processes` table
records every agent pid against its `adw_id`, with `ended_at IS NULL` for live
ones — that is what `just obs procs` reads, pid-reuse guard included. Publishing
that as an occupancy signal, and refusing to open a run against a member another
run holds, converts the unobservable occupant into the observable one.

That is the smallest change here and the only one that prevents destroying work.
It goes first.

---

## Support needs

| | need | size | note |
| --- | --- | --- | --- |
| S1 | discover members from `.gitmodules`, never a literal list | small | mirror the parent's own rule |
| S2 | `git_helper` needs a `cwd` parameter | medium | it has none today; ~15 functions, six composed by `changes.py` |
| S3 | optional per-check `cwd:` | small | or delegate to the parent's dispatch |
| S4 | commit ordering: member → stage gitlink → parent, with partial-failure semantics | medium | `commit_all` commits one repo and trusts it |
| S5 | per-member permission snapshots; member-scoped `writes` | medium | `snapshot()` fingerprints one tree |
| S6 | **occupancy guard, refusing at prepare time** | small | highest value per line |
| S7 | check classification, so an expected-red gate cannot reach a `verify_loop` | small | see below |
| S8 | one baseline per member for the documenter | small | `pin_baseline` records one HEAD |
| S9 | tag/release as a phase, for U4 | large | defer |

### S7 deserves its own note

The specimen carried a cross-repo gate that builds every member twice — once
against the version it pins, once against the latest — to detect semantic drift in
a shared protocol. Its header states that **red is the correct answer today** and
that it is deliberately excluded from the aggregate check, because the migrations
it reports are outstanding work and a green there would be the lie.

A `verify_loop` handed that check would spend its entire repair budget asking a
builder to fix a known, tracked, multi-repo migration.

The factory can already express this by simply not listing such a check in the
`test` group. What it cannot express is *why*, so nothing stops a later edit from
wiring it in. A check needs a way to say "informational: never a repair signal".

---

## What breaks today, specifically

| | |
| --- | --- |
| R1 | `cli.py`'s single `os.chdir(paths.repo_root)` is load-bearing — it is why the ported modules needed no rewiring. Multi-root makes every bare relative path ambiguous, and the failures are silent: a file written into the wrong member, not an exception. |
| R2 | `git_helper._git()` runs git against the process cwd with no `cwd` argument at all. `permissions.py` already threads cwd properly; `git_helper` does not. |
| R3 | `commit_all()` at a superproject root produces one of **two** wrong outcomes, neither of them the gitlink commit an earlier revision of this document claimed. Probed on a throwaway parent+submodule fixture — see the correction note below. **(a)** With only submodule content dirty: `git add -A` stages nothing, but `status --porcelain` still reports ` M sub` so the guard at `git_helper.py:50-51` passes, and `git commit` exits 1 → a loud `RuntimeError` carrying a misleading message. **(b)** With a parent-owned file *also* dirty: the commit **succeeds**, returns a sha, and omits every submodule-resident change. (b) is the dangerous one and it is the common one. |
| R4 | `.sssf/data` derives from `repo_root`. Per-member traces and one workspace-level trace are both defensible; nothing currently decides. An absolute `data_dir` (honoured verbatim by `paths.py` and `session.py`) collapses per-member isolation into one shared db, so per-member traces are a convention rather than an invariant. |
| R5 | The factory's own dispatch creates occupancy that no external instrument can see (see above). |
| R6 | A `changes` step at a superproject root has R3's defect and no guard catches it. `git diff --name-only HEAD` returns `sub`, so `ChangeSet.empty` is false and the empty-diff guard in `engine.py` does not fire. The run then writes a diff artifact whose entire content is a `-Subproject commit …-dirty` marker and hands it to a documenter or reviewer as the run's work product: `+0 -0` where real work exists. Refusing `commit` while admitting `changes` stops the loud half and keeps the silent half. |
| R7 | Permission enforcement is **coarse at a superproject root, and can be blind.** `git diff HEAD --numstat` yields `0 0 sub` for arbitrary work inside a submodule, and `ls-files --others` does not see untracked files created inside one. So if a submodule was already dirty when a phase opened, `changed_paths()` reports nothing and a `writes: []` agent can rewrite or `git checkout` anything inside it undetected — the exact incident class `permissions.py` exists to catch. If it was clean, the path is detected but unrecoverable, because `preserve()` skips it (`target.is_file()` is false for a directory) and the phase aborts on something nothing can undo. |
| R8 | The codex harness's OS sandbox root **is** the agent's cwd: `agent_codex.py` passes `-C request.cwd` alongside `-s sandbox`, and `_sandbox_for()` grants `workspace-write` to every agent that is not `writes: []`. Widening the agent's cwd to a workspace therefore widens a real OS write boundary from one member to the whole workspace. Any read-wide design must decouple the sandbox root from the agent cwd. |

### Correction note

R3 previously read: *"stages gitlinks, never the contents of a submodule … silently commits
pointers to work that was never committed."* That was **wrong**, and it was wrong in the way
this document warns about two sections down: written from reasoning, never probed. It was
inherited verbatim into a downstream architecture frame before a reviewer built a ten-minute
git fixture and disproved it.

The refusal it argues for survives — the probed behaviour is worse, not better, because
outcome (b) is silent and succeeds. But the failure mode a verification asserts has to be the
one that exists, and R6, R7 and R8 were found by the same fixture that corrected R3. **A
superproject-root fixture belongs in the first slice of this work, before any design rests on
what git does at that root.**

---

## Sequencing

1. **U1** — point the factory at one member. Works now.
2. **S6** — occupancy guard. Prevents destroying work; smallest change here.
3. **U2** — read-wide, write-narrow.
4. **U3** — fan-out verification through the parent's own dispatch.
5. **S2 + S4 + U5** — per-repo git contexts and gitlink commits, developed against
   **two throwaway repos in a submodule relationship**, exercising commit-then-fail
   before touching a real workspace.
6. **U4** — release-coupled cross-repo change. Only after all of the above.

## Calibration

Rough confidence that each lands correctly on a first serious attempt: U1 is done;
S6 and U2/U3 are comfortable; gitlink commits are roughly even odds and are where I
would expect to be wrong first; U4 is low and should not be attempted early.

The reason these are estimates rather than claims: every error found while building
this factory — a cost figure that was notional rather than zero, a config block that
was read by nothing, a regex that hung on a real 900-line file — was caught by
running it, never by reasoning about it. Multi-project support cannot be verified by
reasoning either. Treat the numbers as a sequencing aid, not as evidence.
