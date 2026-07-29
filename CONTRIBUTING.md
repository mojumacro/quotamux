# Contributing

## Adding a provider

Most providers need **no code** — add a block to `quotamux/providers.yaml` (or your own
`~/.quotamux/config.yaml`) using one of the three parse modes documented in the README.

Finding the usage endpoint is usually the hard part. What works, in order:

1. **The vendor's own CLI source.** Kimi's endpoint came from
   `kimi_cli/ui/shell/usage.py` in three minutes.
2. **`strings` on a shipped binary.** Anthropic's subscription-usage endpoint is in the
   Claude Code binary and in no public doc.
3. **The dashboard's network tab.** If a console renders the number, an endpoint serves it.

Guessing endpoint names is the slowest path — we tried eight and got eight 404s, then
found the real one (`/usages`, plural) by reading source.

## Pull requests

- Add a test alongside any behaviour change. `pytest tests/ -q`.
- Never commit key material. Config carries environment-variable **names** only.
- Keep provider support declarative; if a vendor forces provider-specific code, say so
  explicitly in the PR rather than hiding a branch in the core.
