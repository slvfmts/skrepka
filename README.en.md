# skrepka 📎

Your AI co-author in Google Docs.

An AI agent in your terminal reads your client's comments, replies to them, and edits the text. The comments and formatting stay in place.

*In Russian: [README.md](README.md).*

## Why

When software edits a Google Doc, comments are easy to lose. You change the text and your client's comments vanish from the document. skrepka does not do that. It only takes safe paths, and it refuses any operation that would destroy someone's work, telling you why.

## Who it is for

For editors, copywriters, and content managers. You run documents with clients in Google Docs and work through an AI agent such as Claude Code or Codex. You do not need to be a developer; the `skrepka init` wizard sets up access for you.

## What it does

Most of the time you reach for skrepka to work through a client's comments and fix the text in place. You tell the agent to handle the comments, it reads the threads, replies on point, and makes the edits. You close the threads yourself; the agent never resolves them. skrepka also exports a document to markdown and pushes edits back, creates documents from markdown, and reviews suggested changes.

## Getting started

1. `pipx install skrepka`
2. `skrepka init` sets up Google access; the wizard takes 15 to 30 minutes and is walked through with screenshots in [docs/QUICKSTART.md](docs/QUICKSTART.md)
3. Connect the skills to your agent, see [docs/PLUGIN.md](docs/PLUGIN.md)

You use your own Google project and act under your own account. skrepka has no server; it does not store or see your data ([PRIVACY.md](PRIVACY.md)).

## Docs

| File | What's inside |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | Step-by-step Google authorization |
| [docs/PLUGIN.md](docs/PLUGIN.md) | Connecting the skills to your agent |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What 0.9 deliberately does not do |
| [PRIVACY.md](PRIVACY.md) | What data goes where |
| [SECURITY.md](SECURITY.md) | Threat model and how to report a vulnerability |

## License

MIT
