# skrepka 📎

**Careful collaborative editing for Google Docs.** Reply to your client's comments and
edit the text — without turning their comments into ghosts and without flattening styles.

*The primary documentation is in Russian: [README.md](README.md). This is a short summary.*

> **Status: 0.9 pre-release.** The engine has passed multi-round cross-model code review
> and live characterization against the real Google Docs API. Packaging, the onboarding
> wizard, and docs are in progress. Not yet published to PyPI.

## Why

Editing a Google Doc programmatically is easy. Editing it **without destroying the
conversation** is not:

- a full re-upload turns every comment into an invisible ghost (alive in the API, gone
  from the UI);
- deleting a text range that overlaps a comment anchor corrupts the text;
- a replacement that fully covers an anchor silently kills the comment thread.

skrepka encodes the empirically verified safe paths (see [docs/FINDINGS.md](docs/FINDINGS.md))
behind fail-closed guards: an operation that would lose someone's work is **refused with
an explanation**, not forced.

## What it does

The core 0.9 workflow is **"work through your client's comments and fix the text in place"**:

- `skrepka comments / reply` — read and answer comments from the CLI (the same CLI the
  AI agent drives). Only a human resolves threads, never the agent.
- `skrepka patch` — surgical text edits that keep live comment anchors alive
  (full-coverage replacements are refused, with a hint on how to rewrite).
- `skrepka download` — export the doc as markdown for local reading or editing.
- `skrepka upload / update` — create docs from markdown; destructive full updates are
  blocked when the doc has comments unless explicitly acknowledged (with an automatic
  backup).

> The local markdown → doc round-trip (`sync`) ships as **experimental** in 0.9 and is
> not part of the supported workflow yet.

## Install (pre-release)

```bash
pipx install skrepka   # after the first PyPI release
skrepka init           # guided Google authorization (15–30 min the first time)
skrepka doctor         # diagnose credentials, token, scopes, API access
```

You bring your **own** Google OAuth client (your own Cloud project) — skrepka ships no
shared verified app. Why, and what it means for your data, is spelled out honestly in
[PRIVACY.md](PRIVACY.md).

## Docs

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — step-by-step Google authorization.
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md) — what 0.9 deliberately does not do.
- [PRIVACY.md](PRIVACY.md) — what data goes where.
- [SECURITY.md](SECURITY.md) — threat model, protections, how to report a vulnerability.
- [docs/FINDINGS.md](docs/FINDINGS.md) — how the Docs API actually behaves with comments.

## License

MIT
