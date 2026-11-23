---
layout: home
hero:
  name: Knowledge Index
  text: GraphRAG for code
  tagline: One search over several related code projects and their docs, for AI agents. A code graph across the repositories, every frontend API call linked to the controller that serves it, and on-device search over the docs. Runs on your machine, no API keys.
  actions:
    - theme: brand
      text: Getting started
      link: /getting-started
    - theme: alt
      text: Configuration
      link: /configuration
    - theme: alt
      text: GitHub
      link: https://github.com/xamresor/knowledge-index
features:
  - title: One graph across repositories
    details: A Laravel API and its Vue or React frontends in one graph. Which screen calls which route, what a class depends on, what breaks if it changes.
  - title: Ways from A to B
    details: Several paths between two places in the code, cheapest first, with the HTTP routes each one crosses.
  - title: Review context for a diff
    details: Changed symbols, what depends on them in every project, the routes and screens they reach, the docs that name them.
  - title: Search by meaning, on device
    details: Docs and code by keywords and by meaning. Embeddings run locally; nothing leaves the machine.
  - title: A team server
    details: One index for a team, access per project from your own system through two webhooks, browser sign-in, admin pages, audit and Prometheus metrics.
  - title: Add-ons and plugins
    details: Batch calls, code mode in a sandbox, fast routes, metrics, audit and questions. Plugins can live outside the core under their own licence.
---
