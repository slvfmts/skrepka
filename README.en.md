# skrepka 📎

An AI co-author in Google Docs. The agent works from your terminal inside the document itself: it reads the comments, replies to them, and edits the text in place.

*In Russian: [README.md](README.md).*

## Why

Working on text with an AI usually means two windows: the document and a chat with the agent. Copy a paragraph into the chat, paste the answer back, fix the formatting by hand. While the text travels back and forth, edits and comments get lost.

skrepka removes the copying. You leave ordinary comments in the text about structure, logic, and wording. Then you point the agent at the document; it reads the threads, replies, and edits the text in place. Your client shows up, comments in the same document, and the work continues the same way. The comments survive: even when a commented paragraph is rewritten from scratch, the thread stays and moves onto the new text. Where that cannot be done safely, skrepka refuses that one operation, names the reason, and edits the rest of the document — see [what it does not do](https://github.com/slvfmts/skrepka/blob/main/docs/LIMITATIONS.md). Only a human closes a thread.

## Who it is for

Editors, copywriters, and content managers who run and sign off documents in Google Docs and bring an AI agent into the work: Claude Code, Codex, or another one. You do not need to be a developer; the `skrepka init` wizard sets up Google access for you.

## What it does

The main scenario is working through comments. You tell the agent to handle the comments; it reads the threads, replies to the point, and edits the text.

skrepka also exports a document to markdown and pushes edits back, creates documents from markdown, and reviews suggested changes. Full list of scenarios in [docs/PLUGIN.md](docs/PLUGIN.md).

## Getting started

1. Install skrepka: `pipx install skrepka`.
2. Set up Google access: `skrepka init`. First-time setup takes 15 to 30 minutes; the walkthrough with screenshots is in [docs/QUICKSTART.md](docs/QUICKSTART.md).
3. Connect the skills to your agent: [docs/PLUGIN.md](docs/PLUGIN.md).

Then ask the agent to work through the comments in a test document.

You create your own Google Cloud project and act under your own account. skrepka has no server and no telemetry: its author never sees your documents or your tokens. Details in [PRIVACY.md](PRIVACY.md).

## Docs

| File | What's inside |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | Step-by-step Google authorization |
| [docs/PLUGIN.md](docs/PLUGIN.md) | Connecting the skills to your agent |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What skrepka deliberately does not do |
| [PRIVACY.md](PRIVACY.md) | What data goes where |
| [SECURITY.md](SECURITY.md) | Threat model and how to report a vulnerability |

## License

MIT
