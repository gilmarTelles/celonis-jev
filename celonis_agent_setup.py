"""Wire celonis-jev into the agent hosts on this machine.

Idempotent, reversible, and it tells you what it touched.

  python3 celonis_agent_setup.py            install / refresh
  python3 celonis_agent_setup.py --check    report only, change nothing
  python3 celonis_agent_setup.py --uninstall

What it does:

  1. adds the MCP server to ~/.omp/agent/mcp.json           (any session, any repo)
  2. copies the same entry into ~/.claude/mcp.json if that host is present
  3. runs `celonis_cli.py doctor` and prints what still needs a human

MCP is the one agent interface. Install and `--uninstall` both remove the omp
extension symlink older versions installed, when it is dangling or points into
this repo; `--check` reports it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent
OLD_OMP_EXT = Path.home() / ".omp/agent/extensions/celonis.ts"
PROJECT_MCP = HOME / ".omp/mcp.json"
OMP_MCP = Path.home() / ".omp/agent/mcp.json"
CLAUDE_MCP = Path.home() / ".claude/mcp.json"
SERVER = {"type": "stdio", "command": "python3", "args": [str(HOME / "celonis_mcp.py")]}


def stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def patch_mcp(path: Path, check: bool, uninstall: bool) -> str:
    """Add or remove the celonis server, keeping every other entry."""
    if not path.parent.exists():
        return f"skip   {path} (host not installed)"
    doc: dict = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError:
            return f"FAIL   {path} is not valid JSON; left alone"
    servers = doc.setdefault("mcpServers", {})
    present = "celonis" in servers
    action = "keep" if (present and not uninstall) else ("add" if not uninstall else "remove")
    if check:
        return f"{'have' if present else 'miss'}   {path} (would {action})"
    if uninstall:
        if present:
            path.with_suffix(path.suffix + f".bak-{stamp()}").write_text(json.dumps(doc, indent=2) + "\n")
            servers.pop("celonis")
            path.write_text(json.dumps(doc, indent=2) + "\n")
            return f"remove {path} (backup kept)"
        return f"skip   {path} (nothing to remove)"
    servers["celonis"] = SERVER
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return f"{'keep' if present else 'add'}   {path}"


def stale_extension(link: Path = OLD_OMP_EXT) -> bool:
    """Is `link` the extension an older install made: a symlink whose target is
    gone or lies in this repo? Anything else there is someone else's file."""
    if not link.is_symlink():
        return False
    return not link.exists() or link.resolve().is_relative_to(HOME)


def doctor() -> int:
    print("\ncelonis_cli.py doctor")
    proc = subprocess.run([sys.executable, str(HOME / "celonis_cli.py"), "doctor"], text=True)
    return proc.returncode


def main() -> int:
    check = "--check" in sys.argv
    uninstall = "--uninstall" in sys.argv
    print(f"celonis-jev at {HOME}\n")
    if stale_extension():
        if check:
            print(f"  stale  {OLD_OMP_EXT} (extension from an older install; would remove)")
        else:
            OLD_OMP_EXT.unlink()
            print(f"  remove {OLD_OMP_EXT} (extension from an older install)")
    for line in (patch_mcp(OMP_MCP, check, uninstall),
                 patch_mcp(PROJECT_MCP, check, uninstall),
                 patch_mcp(CLAUDE_MCP, check, uninstall)):
        print(" ", line)
    if check:
        return 0
    if not uninstall:
        code = doctor()
        print("\nIn omp:  /mcp reload   then  /mcp list  (should show celonis) and /mcp test celonis")
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
