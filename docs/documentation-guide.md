# How to write and keep project docs so the KB can use them

The KB reads every `docs/` folder listed in `kb.projects.toml` (`docs = "…"`). Each Markdown file
becomes a node of the code graph, next to the classes and files. Three kinds of edges come out of a doc:

- **`links_to`**: a relative Markdown link (`[app switcher](app-switcher.md)`) or a `[[wikilink]]` to
  another doc. Exact, taken from the text.
- **`documents`**: a class, method or file named in backticks (`` `AppSwitcherController` ``) that exists
  in the code. Matched by name, so it is marked INFERRED.
- **contracts**: a file in `docs/contracts/` with `from`/`to` front matter becomes one line of the
  service map (`connections`) and is checked by `problems`.

So a doc written with these habits is found by `search`, shows up in `graph_explain` of the classes it
names, appears in `graph_affected` ("change this class, re-read this doc") and in `graph_routes`.

The rules below come from the projects this KB was built on: a Laravel API with two Vue frontends and a
support system (about 380 docs), a Unity game (`docs/domains/`, `docs/plans/`) and a personal wiki of
several hundred pages. They are habits that paid off there, not a standard.

## 1. One `docs/` folder per repository, organised by domain

A domain is a business or functional area: `auth/`, `billing/`, `notifications/`, `customers/`. Not a
ticket, not a sprint, not a person. A domain gets a folder with a `README.md` overview and more files
when it needs them:

```
docs/
  README.md                 index: one table row per domain
  auth/README.md            sign-in, second factor, tokens
  auth/app-switcher.md      one feature of the domain, when the overview gets long
  billing/README.md
  contracts/                one file per connection to another service (section 4)
  plans/                    the future only (section 3)
```

`docs/README.md` is a router, not a summary: a table `Domain | Location | What it covers`, one line per
domain, with the words people will search for. Detail lives in the domain's own files.

## 2. The head of each doc says what it is about

```markdown
# App switcher and handoff login

> **Domain:** `auth` · **Component:** `app/Components/AppSwitcher` · **Ticket:** SM-3046
```

Then: what it does (plain words first), the flow, the rules, the endpoints, and at the end an
**Entry points** section with the real paths and class names in backticks. That last section is what
links the doc to the code in the graph.

## 3. Keep "what works now" apart from "what we plan"

- `docs/<domain>/` describes the code as it is today. Numbers and rules are checked against the code.
  When the code changes, the doc changes in the same pull request.
- `docs/plans/` holds the future: designs not built yet, open questions, known bugs. A plan that is
  built moves into its domain (and the plan is deleted or marked done).

A reader, or an agent, must never have to guess whether a sentence describes the code or a wish. In
the game repo, before the split, pages described features as working that were only planned, and the
agent built on them.

## 4. Connections between services: `docs/contracts/<from>-<to>.md`

One file per connection, named after its two ends, with this front matter:

```markdown
---
from: api
to: sw
protocol: https rest (bearer token)
summary: The API forwards the support requests of the vehicle tablets to Support & Workshop.
---
# api -> sw

What is sent, which route, which auth, what comes back, who owns which side.
```

`connections` lists them as the service map; `problems` reports a connection the code shows but no
contract doc describes (for example `shop-web -> billing-api`), and a contract doc with missing fields. Write
one when you add an outgoing call to another service.

## 5. Link and name things so the graph can follow

- Link related docs with relative links: `[the handoff login](../auth/app-switcher.md)`. A link the
  reader can click is also an edge the agent can walk.
- Name classes, methods and files exactly, in backticks: `` `HandoffCodeStore` ``, `` `.consume()` ``,
  `` `app/Components/AppSwitcher/` ``. A paraphrase ("the code store") links nothing.
- Name routes as the API declares them: `POST v1/login/handoff`. `contracts_search` and the doc
  meet on that string.

## 6. Small habits

- One subject per file. Several small facts of one subject stay in its file.
- Dates are absolute (`2026-11-19`), never "last week".
- Front matter for anything you want to filter: status, owner, dates. The personal wiki uses
  `title`, `kind`, `status`, `updated` on every page and a `_index.md` router in every folder.
- Short sentences and plain words. A doc an agent quotes is also a doc a new colleague reads.
- Never put secrets, tokens, passwords or customer personal data in a doc. The KB serves docs to
  everyone with access to the project, and contract docs to everyone.
- Do not paste code into docs: point to it (Entry points). Pasted code goes stale; a path does not.

## 7. Check it with the KB

After `make update`:

- `search` with `domain: docs` for a question the doc should answer: is it in the top three?
- `graph_explain` on a class the doc describes: is the doc among its `documents` connections?
- `problems` for the project: missing contract docs, unknown routes.
- `connections` with `service: <name>`: does the service map match what you know?

If none of these finds the doc, add the missing names, links or words to it and run `make update` again.
