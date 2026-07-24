# skrepka 📎

**Careful collaborative editing for Google Docs.** Reply to your client's comments and edit the text — without turning their comments into ghosts and without flattening styles.

> Status: 0.9 pre-release. The engine has passed multi-round cross-model code review and live characterization testing against the real Google Docs API; packaging, onboarding wizard, and docs are in progress. Not yet published to PyPI.

## Why

Editing a Google Doc programmatically is easy. Editing it **without destroying the conversation** is not:

- a full re-upload turns every comment into an invisible ghost (alive in the API, gone from the UI);
- deleting a text range that overlaps a comment anchor corrupts the text;
- a replacement that fully covers an anchor silently kills the comment thread.

skrepka encodes the empirically verified safe paths (see `docs/FINDINGS.md`) behind fail-closed guards: operations that would lose someone's work are refused with an explanation, not «forced».

## What it does

- `skrepka comments / reply` — read and answer comments from the CLI (or via the Claude Code plugin).
- `skrepka patch` — surgical text edits that keep live comment anchors alive (docx-export anchor mapping; full-coverage replacements are refused).
- `skrepka download` — export the doc as markdown for local reading or editing.
- `skrepka upload / update` — create docs from markdown; destructive full updates are blocked when the doc has comments, unless explicitly acknowledged (with automatic backup).

> Local-markdown → doc round-trip (`sync`) ships as **experimental** in 0.9 and is not part of the supported workflow yet.

## Install (pre-release)

```bash
pipx install skrepka   # after first PyPI release
skrepka init           # guided Google authorization (15–30 min first time)
```

Docs: `docs/QUICKSTART.md` (авторизация по шагам), `docs/LIMITATIONS.md`, `PRIVACY.md`, `SECURITY.md` — in progress.

## License

MIT
