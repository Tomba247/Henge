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


def write(path, name, identifier, keep):
    """One source file listing the builds `keep` accepts, newest first. The newest is also written in the
    older single-version fields (version, versionDate, downloadURL, size), which some stores read instead."""
    out = json.loads(json.dumps(source))
    out["name"], out["identifier"] = name, identifier
    out_app = out["apps"][0]
    out_app["versions"] = [v for v in app["versions"] if keep(v)]
    for k in ("version", "buildVersion", "versionDate", "versionDescription", "downloadURL", "size", "minOSVersion"):
        out_app.pop(k, None)
    if out_app["versions"]:
        top = out_app["versions"][0]
        out_app.update(version=top["version"], buildVersion=top["buildVersion"], versionDate=top["date"],
                       versionDescription=top["localizedDescription"], downloadURL=top["downloadURL"], size=top["size"],
                       minOSVersion=top["minOSVersion"])
    with open(os.path.join(HERE, path), "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


def experimental(v):
    return v["localizedDescription"].startswith("[league-el0]")


with open(SOURCE, "w") as f:  # every build, the record the two below are cut from
    json.dump(source, f, indent=2)
    f.write("\n")
write("henge.json", "Henge (Tomba247 builds)", "com.tomba247.henge.builds", lambda v: not experimental(v))
write("henge-experimental.json", "Henge experimental (Tomba247)", "com.tomba247.henge.experimental", experimental)
print(f"source.json: {a.version} ({a.build}) from {a.branch}, {len(app['versions'])} builds listed")
