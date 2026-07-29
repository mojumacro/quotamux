# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-07-30

### Documentation
- Added a **Using it with Claude Code** section: the one-line `eval "$(quotamux --export)"`
  recipe, why the whole model-alias family is switched at once, and a FAQ answering
  *"can a subscription produce an `ANTHROPIC_AUTH_TOKEN`?"* — yes for third-party
  Anthropic-compatible subscriptions, **no** for an Anthropic first-party subscription
  (OAuth; injecting it downgrades the session to Bearer and breaks the subscription channel,
  which is why such pools are `native: true` and export nothing).

## [0.2.0] — 2026-07-30

### Added
- `pick_spread()` / `quotamux --spread N` — heterogeneous pool selection: up to N
  pools from N **distinct vendors** (best pool within each vendor, vendors ranked
  by remaining quota). For multi-model review panels and adversarial cross-checks,
  where greedy `--pick` collapses every seat onto the single emptiest pool
  (diverse prompts, same model). Never pads with a second pool from the same
  vendor; honest shortfall + stderr note when fewer vendors qualify.

## [0.1.1] — 2026-07-29

### Fixed
- Project URLs now point at the current GitHub owner (`yandie-AI`). The 0.1.0 metadata
  carries the previous owner name; PyPI metadata is immutable once published, so this
  release supersedes it. Old links still redirect, but a redirect breaks if someone
  claims the vacated username — hence the correction.

## [0.1.0] — 2026-07-29

First public release.

### Added
- Real quota readings from provider usage endpoints — no estimation from local logs.
  Built-in support for **Kimi Code**, **MiniMax**, **Anthropic** (Claude Max/Pro) and
  **DeepSeek** (pay-as-you-go, no pool). **OpenAI** ships as configuration only and is
  explicitly marked untested.
- Selection picks the pool with the **most quota left**, subject to rate-window headroom
  (`--min-window`, default 15 %).
- Three levels of "which pool is eligible": any model, one named model, or a candidate
  set (`--model k3,m3` — "either vendor is fine"). Candidate order expresses preference.
- Per-model quota where the vendor reports it, so a subscription that is 90 % free
  overall cannot mask a model that is down to 5 %.
- `--export` emits environment variables, so **any** coder works — Claude Code, aider,
  your own agent.
- Declarative provider registry: adding a vendor is configuration, not code. Three parse
  modes (`ratio`, `percent`, `percent_used`) cover every provider met so far.
- User config at `~/.quotamux/config.yaml` (or `QUOTAMUX_CONFIG`) overrides and extends
  the shipped registry; it carries environment-variable **names**, never secrets.
- Concurrent queries with a 60 s cache (`--fresh` to bypass).
- Secrets are never printed except by `--export`, which exists for that purpose.
