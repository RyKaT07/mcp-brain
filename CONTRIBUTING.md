# Contributing

mcp-brain is a personal project published under the [PolyForm Noncommercial
1.0.0](LICENSE) license. It is not designed to accept external contributions
at scale, but small fixes and improvements are welcome.

## Before opening a PR

By submitting a pull request you agree that:

1. You have the right to contribute the code (you wrote it, or you have
   permission from the author).
2. Your contribution is licensed under the same PolyForm Noncommercial
   1.0.0 terms as the rest of the project.
3. **You grant the maintainer permission to relicense
   your contribution under any other license in the future**, including
   commercial licenses. This lets the project change direction (e.g. to
   Apache-2.0 or a dual-licensing arrangement) without having to track
   down every past contributor.

This is a lightweight CLA — no forms, no signatures. Opening the PR is
the agreement. If you do not agree, please do not submit.

## What kinds of changes are welcome

- Bug fixes
- Small quality-of-life improvements to existing tools
- Documentation corrections
- Additional scope grammar edge cases, new wildcard behaviors
- Dockerfile / installer / CI workflow improvements

## What is out of scope

- Large refactors that change the auth model or scope grammar (open an
  issue first, I'll tell you whether it fits)
- New integrations (Todoist, Calendar, etc.) — those have a planned order
  in `CLAUDE.md`, opening an unsolicited PR just duplicates effort
- AI logic inside the MCP server — mcp-brain stays dumb and stable; AI
  behavior belongs in the client or a separate agent runner

## Commercial use

PolyForm Noncommercial forbids production use by for-profit entities. If
you or your company want to use mcp-brain commercially, open an issue or
email me — I am happy to negotiate a commercial license on a case-by-case
basis.
