---
name: doc-organization
description: "Write, move or tidy a project's documentation so people and the code knowledge base can use it: one docs/ folder by domain, a README index, a head line per doc, what works now apart from plans, contract docs between services, links and exact names in backticks. Use when you document a feature or a fix, add or rename a doc, start docs/ in a repo, split a long doc, write down a connection to another service, or when asked to organise, clean up or check the docs."
metadata:
  author: project
---

# Doc organization

The full guide with reasons and examples is `docs/documentation-guide.md` in the knowledge-index repo
(github.com/xamresor/knowledge-index). This skill is the checklist. Follow the repo's own docs rules
first when it has them (its CLAUDE.md, AGENTS.md or `docs/README.md`); use this where they are silent.

## Before you write

1. Read `docs/README.md` (the index) and find the domain the change belongs to. Search the docs first:
   the `kb` tool `search` with `domain: docs`, or grep. Extend the existing doc; do not start a second
   one on the same subject.
2. No domain fits? Create `docs/<domain>/README.md` and add one row to the index table
   `Domain | Location | What it covers`, with the words people will search for.

## While you write

- **Head:** `# Title`, then `> **Domain:** \`<domain>\` · **Component:** \`<path>\` · **Ticket:** <id>`.
- **Order:** what it does in plain words, the flow, the rules, the endpoints or commands, then
  **Entry points** with the real paths and class names.
- **Names exactly, in backticks:** `` `RefundService` ``, `` `.approve()` ``, `` `POST v1/orders/{order}/refunds` ``.
  The KB links a doc to the code only through exact names.
- **Links:** relative Markdown links to other docs (`[refunds](../billing/refunds.md)`), or `[[wikilinks]]`
  in a wiki. Each link becomes an edge the agent can walk.
- **Now vs future:** `docs/<domain>/` describes the code as it is, checked against it. Designs not
  built yet, open questions and known bugs go to `docs/plans/`. Never mix them in one page.
- **A connection to another service** (an HTTP client, a queue, a webhook): one file
  `docs/contracts/<from>-<to>.md` with front matter `from`, `to`, `protocol`, `summary`.
- One subject per file; absolute dates (`YYYY-MM-DD`); short sentences.
- Never: secrets, tokens, passwords, customer personal data, pasted code (point to the path instead).

## After you write

- The doc changes in the same commit as the code it describes.
- The index row in `docs/README.md` is still true (and new docs have one).
- With the `kb` tools, after the next build: `search` with `domain: docs` finds the doc for a question
  it answers; `graph_explain` on a class it describes lists it under `documents`; `problems` shows no
  missing contract doc for the connection you described.

## Moving or splitting docs

- Move by domain, never by ticket or date. Keep the old path working where people link to it: update
  every relative link that pointed to it (grep for the file name).
- Split a doc when one domain page covers several features people look up separately; the domain
  `README.md` keeps the overview and links to each part.
- Report what moved where in the commit message.
