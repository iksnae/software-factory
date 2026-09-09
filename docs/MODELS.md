# Models: how a lane gets a brain

Two independent questions, and conflating them is the usual source of confusion:

| question | answered by | values today |
| --- | --- | --- |
| **Which harness runs the agent?** | `coding_agent:` | `pi` (default), `codex` |
| **Which model does it call?** | `model:` | any id the harness can resolve |

Under both is the same primitive: an agent is a model, a harness, tools, and a
prompt. A roster holds three of those constant and changes the model.

---

## pi is the default harness, and pi owns the model catalog

`model:` is written `<pi-provider>/<model-id>`. Only the FIRST segment is the
provider; everything after it is the id, which often contains slashes of its own:

```yaml
model: openrouter/deepseek/deepseek-v4-flash-0731
#      ^provider  ^--------- model id ----------^

model: openai-codex/gpt-6-astra
#      ^provider    ^model id^
```

**`pi --list-models` is the authority.** The factory validates every roster model
against it before a run starts (`agent_pi.resolve_model`), so an unresolvable id
is a `sf check` failure, not a surprise three phases in.

That catalog is a MERGE of two sources, and knowing which is which saves an hour:

1. **`~/.pi/agent/models-store.json`** — pi's own catalog, fetched live from each
   provider. It already carries 366 OpenRouter models and every Codex
   subscription model. You do not maintain this.
2. **`~/.pi/agent/models.json`** — your additions and overrides only.

The trap: `models.json` listing one model does **not** mean pi knows only one
model. It is an overlay, not the catalog. Editing it to "add" a model that the
store already has just duplicates the entry with hand-copied prices that drift
from the provider's real rates — and those prices are what the trace bills with.
Check before you edit:

```bash
pi --list-models | awk '$1=="openrouter"{print $2}' | grep kimi
pi --list-models | awk '$1=="openai-codex"{print $2}'
```

---

## Providers on this host

`~/.pi/agent/auth.json` holds which providers are authenticated. Two matter here:

### `openai-codex` — the ChatGPT subscription

Already OAuth-authenticated for pi, so no key goes in `.env`. Serves the closed
frontier models:

```
gpt-6-astra    gpt-5.6-sol    gpt-5.6-terra    gpt-5.6-luna
gpt-5.5        gpt-5.4        gpt-5.4-mini     gpt-5.3-codex-spark
```

Subscription lanes still report a dollar figure through pi, and it is NOTIONAL: pi
prices every turn from its catalog's list rates, so an `openai-codex/…` lane shows
what those tokens WOULD have cost on the metered API, not what your subscription
was charged. A measured planning phase reported $5.72 for 3.7M tokens (3.58M of
them cache reads) against a flat-rate subscription. Read those numbers as usage
weight, not as an invoice. Only the `coding_agent: codex` CLI path reports
$0.0000, because Codex's own `usage` object carries no cost field at all.

### `openrouter` — open-weight models only

This setup uses OpenRouter **exclusively** for open weights. Needs
`OPENROUTER_API_KEY` in `.env` (the factory reads the factory checkout's `.env`,
then the target repo's, repo winning).

Prefer a DIRECT provider where pi has one, because it skips OpenRouter's margin on
the same weights. On this host `deepseek` and `moonshotai` are authenticated
directly, so `deepseek/deepseek-v4-pro` and `moonshotai/kimi-k3` beat routing the
same models through OpenRouter. `z-ai` has no direct provider, so GLM must go
through it.

The tradeoff: direct providers expose UNDATED ids only (`deepseek-v4-pro`, never
`-0813`). Those are rolling aliases, so two best-of-N runs months apart are not
comparing the same weights. When reproducibility matters more than margin, pin
OpenRouter's dated id instead.

OpenRouter also *lists* closed models — `openrouter/openai/gpt-6-astra` resolves
fine. Routing those through it would pay per token for a model the subscription
already covers, so don't.

---

## Choosing models, in increasing order of effort

**Swap the roster per run.** Nothing else changes:

```bash
sf run sdlc "<work>" --repo . --roster codex-open
just config rosters          # what ships, and what each one staffs
just config models           # every model any roster names
```

**Override one lane** in your repo's `sssf.config.yaml`. Agents merge by name, so
this touches the builder and nothing else:

```yaml
agents:
  - name: builder
    model: openai-codex/gpt-5.6-terra
    thinking: high            # off | minimal | low | medium | high | xhigh | max
```

**Write a roster.** Copy `config/rosters/codex-open.yaml`, change the five
`model:` lines, save as `config/rosters/<name>.yaml`, then `--roster <name>`.
Keep the `# roster:` line — `just config rosters` reads it.

**Add a provider pi does not have.** Only then touch `~/.pi/agent/models.json`:

```json
{ "providers": { "myprovider": {
    "baseUrl": "https://api.example.com/v1",
    "api": "openai-completions",
    "apiKey": "env:MY_API_KEY",
    "models": [ { "id": "some-model", "contextWindow": 200000,
                  "maxTokens": 32000, "reasoning": true, "input": ["text"],
                  "cost": { "input": 1.0, "output": 3.0,
                            "cacheRead": 0.1, "cacheWrite": 0.0 } } ] } } }
```

All four `cost` fields are required. A partial cost block makes pi drop the whole
provider, and every run then reports `$0.0000` while genuinely spending.

Local models work the same way — `lmstudio/…`, `ollama/…` — if they are
registered and running. Whether a local model can hold a plan→build→review loop
together is an open question, but nothing in the factory stops you testing it.

---

## The `codex` harness

`coding_agent: codex` drives the Codex CLI itself (`codex exec`) instead of pi.
It is a different thing from `openai-codex/…`, which is pi calling the
subscription's API.

```yaml
agents:
  - name: reviewer
    coding_agent: codex
    model: codex/gpt-6-astra     # or a bare id: gpt-6-astra
```

What it buys: `--sandbox read-only` is an OS boundary, so `writes: []` becomes
something the kernel refuses rather than something the engine detects afterwards.
Permission checking still runs — a sandbox can express "nothing at all", not
"only `specs/`".

**Status: UNVERIFIED.** The event parser and usage mapping were written against
two live probe runs of `codex exec --json`, but no factory workflow has run
through this adapter. `coding_agent: pi` with `openai-codex/…` models reaches the
same models on the verified path, so prefer it unless you specifically want the
sandbox.

Three things it does deliberately, each measured on those probes:

- **`--ignore-user-config`.** Your `~/.codex/config.toml` carries MCP servers
  (which emitted OAuth transport errors), notify hooks, and a default model. A
  phase must depend on none of them. Auth still resolves — it lives in
  `CODEX_HOME`, not in `config.toml`.
- **It skips the `codex` on PATH when that is a wrapper.** On this host it is a
  cmux shim that injects `--dangerously-bypass-hook-trust`, which is where the
  first probe's stray `error` items came from. Override with `CODEX_PATH`.
- **`item.type == "error"` is informational.** The first probe returned four on a
  turn that succeeded. Treating them as fatal would fail every run.

Model ids are checked against an allowlist rather than a catalog, because Codex
exposes no `--list-models`. Extend it without editing code:

```bash
CODEX_EXTRA_MODELS=gpt-7-whatever sf check --repo .
```

`coding_agent: claude_code` remains a stub that raises. It is listed in the
schema so a config naming it fails with one clear sentence.
