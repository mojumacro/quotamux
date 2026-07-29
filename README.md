# Quotamux

**Route your coder to the subscription with the most quota left.**

多模型多订阅额度调度器 —— 让任意 coder 自动落到**余量最多**的那个订阅上。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The problem

You pay for several AI coding subscriptions. Then this happens:

```
kimi-A     weekly  7% left   ← burned out on Tuesday
kimi-B     weekly 46% left
minimax-1  weekly 73% left
minimax-2  weekly 99% left   ← never touched
```

One subscription is exhausted while another sits idle, because nothing tells your
tooling which pool still has room. You find out when a job dies with a 429.

Worse, the usual reflex is to *estimate* usage from your own logs. Local logs are a
**proxy**: they miss calls that bypass them, their accounting differs from the
provider's, and your own grouping code can be wrong. Every conclusion built on a proxy
inherits its bias. (Ask us how we know — a full day of modelling, all of it wrong.)

## What it does

Asks each provider **how much is actually left**, then hands your coder the environment
for the pool with the **most** quota remaining — not merely one that is non-empty.
"Non-empty is good enough" is exactly how one subscription burns out while another idles.

```bash
$ quotamux
池                       周剩      窗剩  重置
🔴kimi-A                  7%     99%  2026-08-04 00:39
🟢kimi-B                 46%    100%  2026-08-04 05:25
🟢minimax-1              73%     98%  2026-08-03 01:00
🟢minimax-2              99%    100%  2026-08-03 01:00 ←选它
🟡claude-max             22%     86%  2026-07-31 23:59
```

```bash
# Works with ANY coder — quotamux only emits environment variables
eval "$(quotamux --export)" && claude -p "fix the failing test"
eval "$(quotamux --export)" && aider --message "..."
eval "$(quotamux --export)" && your-own-agent
```

## Install

```bash
pip install quotamux        # or: pipx install quotamux
```

## Configure

Providers ship built in. Declare **your** subscriptions in `~/.quotamux/config.yaml`
(YAML or JSON). You only ever write **environment-variable names** — never secrets:

```yaml
providers:
  kimi:
    subscriptions:
      # Multiple keys under one subscription share ONE quota pool.
      # (Verified: same-account keys return byte-identical usage.)
      # So rotating to another key of the same account when throttled is a no-op.
      - name: "kimi-team"
        keys: [KIMI_CODE_API_KEY, KIMI_CODE_API_KEY_1, KIMI_CODE_API_KEY_2]
      - name: "kimi-personal"
        keys: [KIMI_CODE_API_KEY_5]
```

Point elsewhere with `QUOTAMUX_CONFIG=/path/to/config.yaml`.

## Using it with Claude Code

One line — `--export` emits shell-ready `export` statements, so `eval` them and start Claude Code:

```bash
eval "$(quotamux --export --model k3,m3)" && claude
```

What lands in the environment (real output, key redacted):

```
export ANTHROPIC_BASE_URL='https://api.kimi.com/coding'
export ANTHROPIC_MODEL='kimi-k3'
export ANTHROPIC_AUTH_TOKEN='sk-…'
export ANTHROPIC_DEFAULT_OPUS_MODEL='kimi-k3'
export ANTHROPIC_DEFAULT_SONNET_MODEL='kimi-k3'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='kimi-k3'
export CLAUDE_CODE_SUBAGENT_MODEL='kimi-k3'
```

The whole **model-alias family** is switched together, not just `ANTHROPIC_MODEL`. That is
deliberate: miss one and a subagent will send a native Anthropic model name to the
third-party endpoint and get a 401 that is annoying to trace back.

### Can a subscription produce an `ANTHROPIC_AUTH_TOKEN`?

Depends which kind of subscription — and the difference matters:

**Third-party subscriptions with an Anthropic-compatible endpoint** (Kimi, MiniMax, …): **yes**,
and that is exactly the intended path. The subscription hands you an API key; the endpoint
accepts it as `ANTHROPIC_AUTH_TOKEN` alongside `ANTHROPIC_BASE_URL`. `--export` does this for you.

**An Anthropic first-party subscription** (Claude Max / Pro): **no — and you should not try.**
Its credential is an OAuth access token (in `~/.claude/.credentials.json`), and Claude Code
already uses it natively. Injecting it as `ANTHROPIC_AUTH_TOKEN` (with a `BASE_URL`) makes
Claude Code treat the session as a third-party Bearer call, and the subscription channel breaks —
we shipped that bug before adding the guard for it. So such pools are declared `native: true`,
and `--export` deliberately emits **nothing** for them, just a note on stderr:

```
$ quotamux --export --provider anthropic
# anthropic:claude-max is a native subscription — run your coder with no env override
```

quotamux still reads that OAuth token, but only to call the usage endpoint so your Max quota
shows up in the same table as everything else. It is never emitted as an environment variable.

Rule of thumb: **third-party subscription → env injection; first-party subscription → touch nothing.**

## Supported providers

| Provider | Weekly | Rate window | Usage endpoint |
|---|---|---|---|
| **Kimi Code** | ✅ | ✅ | `{base}/usages` |
| **MiniMax** | ✅ | ✅ | `/v1/token_plan/remains` |
| **Anthropic** (Claude Max/Pro) | ✅ | ✅ | `/api/oauth/usage` |
| **OpenAI** | ⚠️ config only, untested | — | `/v1/organization/costs` |
| **DeepSeek** | pay-as-you-go, no pool | — | — |

> Two of these endpoints are **not in any public documentation**. We found them by
> reading the vendors' own clients — Kimi's from its CLI source, Anthropic's from
> `strings` on the Claude Code binary. If a dashboard can show a number, something
> serves that number; follow the signpost back to the machine.

## Add a provider — config only, no code

Adding a vendor means adding a block to your config. Two parse modes cover every
provider we have met:

```yaml
providers:
  acme:
    display: ACME AI
    usage:
      url: "https://api.acme.example/v1/quota"
      auth: bearer                    # or: x-api-key
      headers: {api-version: "2026-01-01"}
      parse:
        mode: ratio                   # vendor returns absolute remaining/limit
        week:   {remaining: "quota.remaining", limit: "quota.limit", reset: "quota.reset_at"}
        window: {list: "windows", remaining: "detail.remaining", limit: "detail.limit"}
    lane:
      base_url: "https://api.acme.example/anthropic"
      model: "acme-1"
      auth_env: ANTHROPIC_AUTH_TOKEN
    subscriptions:
      - name: "acme-seat-1"
        keys: [ACME_KEY_1]
```

| mode | when the vendor returns | fields |
|---|---|---|
| `ratio` | absolute `remaining` / `limit` | `week.{remaining,limit,reset}`, `window.{list,remaining,limit}` |
| `percent` | remaining **percent** | `select.{path,where}`, `week.{pct,reset_ms}`, `window.{pct}` |
| `percent_used` | **used** percent (utilization) | `week.{used_pct,reset}`, `window.{used_pct}` |

Declare the models a provider serves so `--model` can find it:

```yaml
    models:
      - {id: "acme-1", aliases: [acme, a1]}
      # usage_row: only when the vendor reports quota per model — names the row to read
      - {id: "acme-turbo", aliases: [turbo], usage_row: turbo}
```

A test in this repo proves the claim: a fully fictional provider that appears
**nowhere in the source** runs end to end from config alone.

## "Most quota left" — three levels

All three levels pick the **emptiest-loaded** pool, never just "one that still has
something". What changes between levels is *which pools are eligible*, and that depends
on **what the task needs**:

```bash
quotamux --pick                 # any model will do → most weekly quota left, full stop
quotamux --model opus --pick    # needs this model  → only pools that serve it
quotamux --model k3,m3 --pick   # either is fine    → pools serving k3 OR m3,
                                #                      then the emptiest across vendors
```

The third form is the one you want most days: *"this job runs fine on Kimi or MiniMax"*.
It keeps the job runnable (right model) **and** stops one subscription burning out while
another idles (most quota left). Order expresses preference — with `--model k3,m3`, a
pool serving both gets `k3`.

Where a vendor reports quota **per model** (MiniMax returns one row per model), the
per-model figure wins. A subscription can look 90 % free overall while the model you
need is down to 5 %.

## Spread: one seat per vendor (`--spread N`)

Greedy `--pick` has a blind spot: dispatch three review agents in a row and **all three
land on the same emptiest pool** — diverse prompts, same model. For cross-checking work
(adversarial review, multi-model panels, N-version generation) model diversity *is* the
point, and quota greed quietly destroys it.

```bash
quotamux --spread 3                 # 3 pools, all different vendors, emptiest first
quotamux --spread 2 --model k3,m3   # per line: provider:subscription + model id
```

Within each vendor the best pool still wins (window-eligible, most weekly left); vendors
are then ranked by quota and the top N returned — one line each, so a dispatcher can
`readarray` and pin one seat per line. If fewer than N distinct vendors qualify you get
fewer lines (and a note on stderr) — it never pads with a second pool from the same
vendor, because that would *look* heterogeneous while reintroducing the very bias you
asked it to remove.

## Selection rule

1. If `--model` is given, drop pools that do not serve it. Weekly quota is worthless on
   a pool that will not run your model.
2. A pool is eligible when its **tightest rate window** has ≥ `--min-window` percent
   left (default 15). Weekly quota is useless if you are about to get throttled.
3. Among eligible pools, pick the one with the **most quota remaining** — per-model if
   the vendor reports it, otherwise subscription-level.
   This is the rule that stops one subscription burning out while another idles.
4. Pay-as-you-go pools are excluded unless you pass `--allow-metered` — spending
   money should be an explicit decision.
5. Nothing eligible → exit code 1. Queue and wait for the window. Buying another
   subscription rarely fixes what is a scheduling problem.

## Safety

- **Secrets are never printed** by `quotamux`, `--pick` or `--json`. Only `--export`
  emits keys — that is its entire job — and it writes them to stdout so you can
  `eval` them. Do not pipe `--export` into logs or CI output.
- Config holds **environment-variable names**, never key material.
- Native subscriptions (Claude Max OAuth) emit **no** environment at all. Injecting a
  base URL or bearer token there downgrades OAuth and kills the subscription channel —
  a mistake we made in production so you do not have to.

## Notes

- Queries run **concurrently** and are cached for 60 s (`--fresh` to bypass), so
  launching a batch of workers does not hammer the usage endpoints.
- `quotamux --json` is stable machine output; build your own policy on top of it.

## License

MIT — see [LICENSE](LICENSE).
