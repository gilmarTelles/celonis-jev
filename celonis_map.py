"""Map the Celonis sandbox: what is public, what is gated, and what the tenant holds.

Read-only. Uses the public app shell for the UI inventory and the team API key in
`~/.celonis/environments.json` for the tenant content.

  python3 celonis_map.py            # print the map
  python3 celonis_map.py --json     # also write celonis-map.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import requests

import celonis_api as api
import cliargs

OUT = Path(__file__).with_name("celonis-map.json")
SHELL_RE = re.compile(r"vmfs/([A-Za-z0-9_\-]+)/versions/([^/]+)/")


def shell() -> dict:
    """The /ui/ shell is public; it carries the micro-frontend inventory."""
    r = requests.get(api.base() + "/ui/", timeout=30)
    html = r.text
    apps = {}
    for name, version in SHELL_RE.findall(html):
        apps.setdefault(name, version)
    data = re.search(r'<script type="application/json" id="initial-data"[^>]*>(.*?)</script>',
                     html, re.S)
    manifest = json.loads(data.group(1)) if data else {}
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    return {"status": r.status_code, "shell_bytes": len(html),
            "shell_version": next(iter(apps.values()), None),
            "remote": scripts[0] if scripts else None,
            "anonymous_shell": manifest.get("isAnonymous"),
            "feature_flags": len(manifest.get("frontendFeatures") or []),
            "apps": dict(sorted(apps.items()))}


def boundary() -> dict:
    """No session vs team key, on the same host."""
    paths = ["/ui/", "/ui/studio", "/ui/process-explorer", "/integration/api/pools",
             "/package-manager/api/packages", "/semantic-layer/api/knowledge-model"]
    out = {}
    for p in paths:
        row = {}
        for label, hdr in (("anonymous", {}), ("api_key", api.headers())):
            try:
                r = requests.get(api.base() + p, headers=hdr, timeout=25, allow_redirects=False)
                row[label] = {"status": r.status_code,
                              "location": r.headers.get("location", "")[:70]}
            except Exception as e:
                row[label] = {"error": repr(e)[:60]}
        out[p] = row
    return out


def tenant() -> dict:
    h = api.headers()
    spaces = requests.get(api.base() + "/package-manager/api/spaces", headers=h, timeout=30).json()
    packages = requests.get(api.base() + "/package-manager/api/packages", headers=h, timeout=30).json()
    nodes = requests.get(api.base() + "/package-manager/api/nodes", headers=h, timeout=60).json()
    by_pkg: dict[str, Counter] = {}
    for n in nodes:
        by_pkg.setdefault(n.get("rootNodeKey", "?"), Counter())[n.get("assetType") or n.get("nodeType")] += 1

    packages_out = []
    for p in packages:
        counts = by_pkg.get(p["key"], Counter())
        packages_out.append({
            "key": p["key"], "name": p["name"], "space": p.get("spaceId"),
            "version": p.get("version"), "draft": bool(p.get("draftId")),
            "nodes": sum(counts.values()),
            "assets": dict(counts.most_common()),
        })
    packages_out.sort(key=lambda p: -p["nodes"])

    pools = requests.get(api.base() + "/integration/api/pools", headers=h, timeout=30).json()
    pools_out = []
    for pool in pools:
        jobs = requests.get(f"{api.base()}/integration/api/pools/{pool['id']}/jobs",
                            headers=h, timeout=30).json()
        pools_out.append({
            "id": pool["id"], "name": pool["name"], "type": pool.get("dataPoolType"),
            "configured": pool.get("configurationStatus"),
            "jobs": [{"id": j["id"], "name": j["name"], "type": j.get("type"),
                      "status": j.get("status")} for j in jobs],
        })
    return {
        "spaces": [{"id": s["id"], "name": s["name"]} for s in spaces],
        "packages": packages_out,
        "pools": pools_out,
        "totals": {"spaces": len(spaces), "packages": len(packages), "nodes": len(nodes),
                   "pools": len(pools),
                   "jobs": sum(len(p["jobs"]) for p in pools_out)},
    }


def main() -> None:
    _, opts = cliargs.parse_argv(sys.argv[1:])
    sh = shell()
    print(f"origin {api.base()}")
    print(f"  shell: {sh['status']} {sh['shell_bytes']:,} bytes, version {sh['shell_version']}, "
          f"anonymous={sh['anonymous_shell']}, {len(sh['apps'])} micro-frontends, "
          f"{sh['feature_flags']} feature flags")
    print("  apps: " + ", ".join(sorted(sh["apps"])))

    print("\nauth boundary (status / redirect):")
    bd = boundary()
    for path, row in bd.items():
        print(f"  {path:<42} anon={row['anonymous']['status']}"
              f"{' -> ' + row['anonymous']['location'] if row['anonymous'].get('location') else ''}"
              f"   key={row['api_key']['status']}")

    t = tenant()
    print(f"\ntenant: {t['totals']}")
    print("  spaces:")
    for s in t["spaces"]:
        print(f"    {s['name']}")
    print("  packages (nodes):")
    for p in t["packages"]:
        top = ", ".join(f"{k}={v}" for k, v in list(p["assets"].items())[:4])
        print(f"    {p['name']:<38} nodes={p['nodes']:<4} v{p['version']} {top[:70]}")
    print("  pools:")
    for p in t["pools"]:
        print(f"    {p['name']:<44} jobs={len(p['jobs'])} {p['type']}")
        for j in p["jobs"][:3]:
            print(f"        {j['name']} ({j['type']}) {j['status']}")

    if "--json" in opts:
        doc = {"origin": api.base(), "shell": sh, "boundary": bd, "tenant": t}
        OUT.write_text(json.dumps(doc, indent=1))
        print(f"\nwrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
