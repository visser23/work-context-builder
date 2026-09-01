# Vision: Work Context Mirror

## North Star

Configure a project once, schedule a sync every morning, and thereafter have an
automatically refreshed body of project knowledge that an LLM can interrogate as
if it were an expert in that project.

## What It Is

A lightweight macOS application that creates and continuously maintains a local,
LLM-friendly mirror of selected work knowledge held across Confluence, Jira, and
SharePoint/OneDrive.

## What It Is Not

- Not a hosted knowledge platform
- Not a chatbot or web UI
- Not an enterprise search application
- Not an API server or MCP server
- Not a RAG service or vector database
- Not infrastructure requiring maintenance

## Non-Negotiables

1. **Zero admin** — must work using credentials already available to the end user;
   no IT administrator involvement for normal setup.
2. **Filesystem first** — canonical output is clean Markdown files on disk; no
   running service required for LLMs to consume the corpus.
3. **Read only** — never modifies source systems.
4. **Incremental by default** — cost proportional to change, not corpus size.
5. **Local processing** — document conversion happens on the Mac; no content sent
   to external services.
6. **Boring technology** — SQLite, files, Python, official APIs.

## Success Criteria

A technically capable user can install the tool, configure it in minutes, run an
initial sync, install a morning schedule, and then forget the application exists
while ChatGPT Work, Codex, or Claude Code can interrogate current project context
directly from the resulting filesystem.
