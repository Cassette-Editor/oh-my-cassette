---
description: Show or change the Cassette model and thinking level for the current media session
---

Open the Oh My Cassette model picker for the active Cassette media session.

Use the `cassette` MCP server's `cassette_config` tool. Reuse the active `session_id`; never invent
one. If there is no active media session, tell the user to add media first. Do not start an edit.

If `$ARGUMENTS` names a valid model or thinking level, save it directly and confirm that it applies
from the next edit turn. Otherwise read the current configuration, show compact numbered model and
thinking lists, wait for the user's choice, and save it. The default is GPT-5.6 Luna with `xhigh`
thinking. Never open this picker automatically as part of an ordinary edit.
