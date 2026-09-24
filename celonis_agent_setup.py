"""Wire celonis-jev into the agent hosts on this machine.

Idempotent, reversible, and it tells you what it touched.

  python3 celonis_agent_setup.py            install / refresh
  python3 celonis_agent_setup.py --check    report only, change nothing
  python3 celonis_agent_setup.py --uninstall

What it does:

  1. symlinks omp/celonis.ts into ~/.omp/agent/extensions/  (omp extension:
     celonis_resolve / celonis_open / celonis_read / celonis_doctor + /celonis)
  2. adds the MCP server to ~/.omp/agent/mcp.json           (any session, any repo)
  3. copies the same entry into ~/.claude/mcp.json if that host is present
  4. runs `celonis_cli.py doctor` and prints what still needs a human
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent
OMP_EXT = Path.home() / ".omp/agent/extensions/celonis.ts"
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


def link_extension(check: bool, uninstall: bool) -> str:
    source = HOME / "omp/celonis.ts"
    if not OMP_EXT.parent.exists():
        return "skip   ~/.omp/agent/extensions (omp not installed)"
    if uninstall:
        if OMP_EXT.is_symlink() or OMP_EXT.exists():
            OMP_EXT.unlink()
            return "remove ~/.omp/agent/extensions/celonis.ts"
        return "skip   no extension to remove"
    if check:
        return f"{'have' if OMP_EXT.exists() else 'miss'}   {OMP_EXT} (would link -> {source})"
    if OMP_EXT.is_symlink() and OMP_EXT.resolve() == source:
        return f"keep   {OMP_EXT} -> {source}"
    if OMP_EXT.exists():
        OMP_EXT.with_suffix(f".ts.bak-{stamp()}").write_text(OMP_EXT.read_text())
        OMP_EXT.unlink()
    OMP_EXT.symlink_to(source)
    return f"link   {OMP_EXT} -> {source}"


def doctor() -> int:
    print("\ncelonis_cli.py doctor")
    proc = subprocess.run([sys.executable, str(HOME / "celonis_cli.py"), "doctor"], text=True)
    return proc.returncode


def main() -> int:
    check = "--check" in sys.argv
    uninstall = "--uninstall" in sys.argv
    print(f"celonis-jev at {HOME}\n")
    for line in (link_extension(check, uninstall),
                 patch_mcp(OMP_MCP, check, uninstall),
                 patch_mcp(PROJECT_MCP, check, uninstall),
                 patch_mcp(CLAUDE_MCP, check, uninstall)):
        print(" ", line)
    if check:
        return 0
    if not uninstall:
        code = doctor()
        print("\nIn omp:  /mcp reload   then  /mcp list  (should show celonis) and /mcp test celonis")
        print("Extension tools load at startup - start a new omp process for those.")
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
