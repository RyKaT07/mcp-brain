# Write policy (example)

mcp-brain can inject a write-discipline policy into every MCP client that
connects to it. At startup the server reads `knowledge/_meta/write-policy.md`
and puts its contents into the `InitializeResult.instructions` field of the
MCP protocol. Conforming clients (like Claude Code) prepend these
instructions to the LLM's system prompt for the session, so every agent
using your brain starts with the same rules — no per-device config drift.

This file is an **example template**. To use it:

```bash
# on the server where mcp-brain runs
mkdir -p /opt/mcp-brain/data/knowledge/_meta
cp /path/to/mcp-brain/docs/write-policy.example.md \
   /opt/mcp-brain/data/knowledge/_meta/write-policy.md
# edit to taste, then restart the container
docker compose restart
```

Your actual `knowledge/_meta/write-policy.md` lives inside your knowledge
store, which has its own git history. It is never committed back to this
repo — it is personal policy, not framework code.

---

## Mechanism

- The server reads this file **once at startup**. To reload after edits,
  restart the container.
- If the file is missing, mcp-brain starts with no custom instructions —
  the MVP works fine without one.
- The file should be plain markdown. Anything you write is visible to
  every connected agent, so don't put secrets in it.

---

## Example — opt-in writes with an undo safety net

Paste something like the following into your copy. Every section is a
suggestion — customize scope names, language, and tone to match your own
setup.

### 1. Propose, don't write

When something worth remembering comes up in conversation — a preference,
a decision, a project fact, a config detail — do **not** call
`knowledge_update` unprompted. Propose the save in chat first, and show
the exact content that would land:

> Save to `<scope>/<project>.md § <section>`?
>
> ```
> - exact content that will be written
> - one line per bullet, so the user can eyeball it
> ```

Only write after the user replies affirmatively. Silence or an off-topic
reply means "skip".

### 2. Confirm after writing

After any `knowledge_update`, end the reply with a compact footer so the
user can verify what landed:

> 📝 Saved to brain:
> - `work/project.md § Architecture` — decision about caching layer
>
> Reply "undo" to revert.

One bullet per `knowledge_update` call, in `scope/project § section`
format followed by a half-line description. If you also wrote to inbox,
list those separately with an `(inbox)` tag. If you wrote nothing, skip
the footer entirely — silence is the signal for "nothing was saved".

### 3. Respond to undo

If the user's next reply (or one shortly after) says "undo", "revert",
"delete that" or any clear equivalent, immediately call
`knowledge_undo(steps=N)` where N is the number of saves in the most
recent footer (default 1). Report what was reverted and never re-save the
same content in the same session — assume it was a deliberate rejection.

### 4. Sensitive scopes are extra careful

For scopes that store infrastructure, credentials locations, or
health / finance data:

- **Always show the full proposed content** in the proposal, not just a
  summary. Let the user eyeball exact strings before accepting.
- **Prefer `inbox_add`** for anything the agent is less than fully sure
  about. Inbox review is a second chance to catch mistakes before they
  become authoritative.
- **Never infer sensitive facts from context** — e.g. don't propose a
  medical note based on an offhand comment; wait for an explicit "save
  this to health".

### 5. Inbox stays manual

`inbox_add` is fine to call without asking — it's a staging area, not
authoritative. But never call `inbox_accept` on behalf of the user
unless they explicitly request merging a specific item. Inbox review is
the whole point of inbox existing.

---

## Customizing

- **Language.** If you chat in a language other than English, write the
  announcement footer and undo response text in your language (rules 2
  and 3). The agent sees those strings verbatim and copies them back.
- **Scope list.** Match your `meta.yaml`. The agent will notice if you
  refer to a scope that doesn't exist in meta and can ask before creating
  it.
- **Opt-in vs opt-out.** This example is opt-in (agent proposes every
  save). If you prefer opt-out (agent writes freely, you undo mistakes),
  invert rule 1 and lean more on rule 3 — `knowledge_undo` works the
  same way in both models.
- **Per-scope overrides.** Add rules like "for `work/`, always include
  the repo name in the section title" or "for `people/`, one file per
  person, name it `firstname-lastname.md`".

---

## Tool policy

The content of **this** section gets injected directly into the MCP
`description` field of the `knowledge_update` tool at server startup
(see `mcp_brain/server.py::_load_tool_policy` + `_extract_h2_section`).

**Why a second channel.** The `InitializeResult.instructions` mechanism
described at the top of this file is the canonical place for server-wide
rules, and Claude Code CLI honors it faithfully. But not every client
does — claude.ai web was observed to receive the instructions, call them
"server system prompt", and still write to the knowledge base without
following them. Tool descriptions are part of the schema every client
must pass through to the model verbatim, so rules embedded here cannot
be silently ignored by an intermediate layer.

Everything that follows is markdown pasted into the tool description
verbatim — keep it short (a few hundred lines max) and phrased as
rules the LLM is about to follow, not background explanation.

### Before calling `knowledge_update`

1. **Never call this tool unprompted.** Propose the save in chat first,
   showing the exact section body that would land. Only call
   `knowledge_update` after the user replies affirmatively ("yes",
   "save", "ok", "go ahead", or an equivalent in their language). A
   tangential reply, a deflection, or silence means skip — do not call.

2. **Exception for direct save requests.** If the user explicitly asks
   you to save ("save this to brain", "add that to notes", "put it in
   work/foo"), call directly without a separate proposal step, but
   still announce what you wrote afterwards.

3. **Sensitive scopes require extra care.** For anything under
   `health/`, `finance/`, or `homelab/`, always show the full proposed
   content (not a summary) before writing. Never infer facts in these
   scopes from conversational context — wait for an explicit user
   request.

### After calling `knowledge_update`

End your reply with a compact footer so the user can verify and undo:

> 📝 Saved to brain:
> - `scope/project § section` — half-line description of what landed
>
> Reply "undo" to revert.

One bullet per `knowledge_update` call in this turn. If the user's next
reply says "undo", "revert", "delete that", or any clear equivalent,
immediately call `knowledge_undo(steps=N)` where N is the number of
`knowledge_update` calls in your most recent footer (default 1). Never
re-save content the user just rejected in the same session — assume it
was a deliberate rejection.
