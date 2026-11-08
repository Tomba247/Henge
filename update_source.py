#!/usr/bin/env python3
"""Adds one Henge build to source.json (an AltStore-format source, which FlareStore, SideStore,
Feather and others read). The build workflow runs it after each release; it can also be run by hand:

  python3 update_source.py --version 0.1.6 --build 6 --branch macos27-xchain \
      --url https://github.com/Tomba247/Henge/releases/download/<tag>/Henge.ipa --size 6619088 \
      --date 2026-11-08T04:30:18Z --notes "What changed"

Newest first; a build already listed (same download URL) is replaced; at most 40 are kept.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "source.json")

p = argparse.ArgumentParser()
p.add_argument("--version", required=True)
p.add_argument("--build", required=True)
p.add_argument("--branch", required=True)
p.add_argument("--url", required=True)
p.add_argument("--size", type=int, required=True)
p.add_argument("--date", required=True)
p.add_argument("--notes", default="")
a = p.parse_args()

with open(SOURCE) as f:
    source = json.load(f)
app = source["apps"][0]

entry = {
    "version": a.version,
    "buildVersion": a.build,
    "date": a.date,
    "localizedDescription": f"[{a.branch}] {a.notes}".strip(),
    "downloadURL": a.url,
    "size": a.size,
    "minOSVersion": "17.0",
}
versions = [v for v in app.get("versions", []) if v.get("downloadURL") != a.url]
versions.append(entry)
versions.sort(key=lambda v: v["date"], reverse=True)
app["versions"] = versions[:40]

with open(SOURCE, "w") as f:
    json.dump(source, f, indent=2)
    f.write("\n")
print(f"source.json: {a.version} ({a.build}) from {a.branch}, {len(app['versions'])} builds listed")
