See AGENTS.md — same rules, same commands.

Quickest path in a session: `python3 celonis_cli.py doctor`, then
`python3 celonis_cli.py ask "<what you want in Celonis>"`.
Agents that speak MCP get five tools from `.mcp.json`: celonis_search, then
celonis_open or celonis_read with a candidate's id (no Jev key needed), plus
celonis_resolve and celonis_doctor. The project check is `/verify-celonis`.
