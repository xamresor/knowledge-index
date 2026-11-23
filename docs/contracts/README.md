# Contract docs

One file per connection of this KB service to another service: `<from>-<to>.md`. The folder is empty by
default: a server adds the files of its own connections here (the API that signs people in, the
checkouts it indexes, the agents that call it). This README is no contract: `bin/contracts.py` skips it.

A checkout of the KB indexes this folder as project `kb` (`KB_SELF_DOCS_NAME`), and `bin/kb` adds each
file to the service map (`connections`). The format is the same as in the indexed projects
([documentation guide](../documentation-guide.md#4-connections-between-services-docscontractsfrom-tomd)):

```markdown
---
from: kb
to: my-api
protocol: HTTP webhook
summary: the KB asks the API who a token belongs to
---

# kb → my-api

What crosses the connection, who starts it, and what breaks when it changes.
```
