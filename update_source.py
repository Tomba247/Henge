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

CREDIT = ("Unofficial fork build. Henge is made by Leviidev (github.com/Leviidev/Henge) and its contributors, "
          "and all credit for Henge goes to them. This fork (github.com/Tomba247/Henge) only adds experimental features "
          "on top, such as the Linux machine (Ubuntu and Kali desktops). It isn't affiliated with or endorsed by the Henge "
          "project: report problems with these builds to Tomba247, not to Henge.")


def describe(branch, notes):
    """A version's notes: what changed, from which branch and commit, then the credit."""
    lines = [l for l in notes.strip().splitlines() if l.strip()]
    sha = next((l.split("built from", 1)[1].strip(" )") for l in lines if "built from" in l), "")
    change = next((l for l in lines if "built from" not in l), "")
    head = f"What changed: {change}" if change else "What changed: see the release"
    where = f"Branch: {branch}" + (f" · commit {sha[:7]}" if sha else "")
    return f"{head}\n{where}\n\n{CREDIT}"


def reformat(v):
    """An entry written before describe(): "[branch] notes", rewritten (idempotent)."""
    d = v.get("localizedDescription", "")
    if d.startswith("What changed:"):
        return
    branch, notes = "", d
    if d.startswith("[") and "]" in d:
        branch, notes = d[1:d.index("]")], d[d.index("]") + 1:]
    v["branch"] = v.get("branch") or branch
    v["localizedDescription"] = describe(v["branch"], notes)


entry = {
    "version": a.version,
    "buildVersion": a.build,
    "date": a.date,
    "branch": a.branch,
    "localizedDescription": describe(a.branch, a.notes),
    "downloadURL": a.url,
    "size": a.size,
    "minOSVersion": "17.0",
}
versions = [v for v in app.get("versions", []) if v.get("downloadURL") != a.url]
for v in versions:
    reformat(v)
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
    return v.get("branch") == "league-el0"


source.update(
    name="Henge (Tomba247 test builds, unofficial)",
    subtitle="Unofficial test builds. Henge is by Leviidev.",
    description="Unofficial test builds from github.com/Tomba247/Henge, a fork of Henge. Henge (github.com/Leviidev/Henge, "
                "GPL-2.0-or-later) is made by Leviidev and its contributors: all credit for Henge goes to them, and the "
                "official app is theirs. This fork only adds experimental features on top, such as the Linux machine "
                "(Ubuntu and Kali desktops on Henge's engine), and isn't affiliated with or endorsed by the Henge project. "
                "Henge needs JIT (StikJIT or StikDebug) and the increased-memory and extended-addressing entitlements, "
                "so sign it with a development certificate.",
)
app.update(
    developerName="Leviidev (unofficial fork build by Tomba247)",
    localizedDescription="Henge runs macOS on iPhone and iPad: a virtual machine with its own arm64 translator, emulated "
                         "devices and Metal graphics. Henge is made by Leviidev and its contributors "
                         "(github.com/Leviidev/Henge); all credit for it goes to them.\n\nThese are unofficial test builds "
                         "from a fork (github.com/Tomba247/Henge) that adds a Linux machine: Ubuntu and Kali desktops on "
                         "Henge's engine, with the phone's GPU for 3D. They're not official Henge releases and aren't "
                         "affiliated with or endorsed by the Henge project. Get the official app from Leviidev, and report "
                         "problems with these builds to Tomba247, not to Henge.",
)
with open(SOURCE, "w") as f:  # every build, the record the two below are cut from
    json.dump(source, f, indent=2)
    f.write("\n")
write("henge.json", "Henge (Tomba247 builds, unofficial)", "com.tomba247.henge.builds", lambda v: not experimental(v))
write("henge-experimental.json", "Henge experimental (Tomba247, unofficial)", "com.tomba247.henge.experimental", experimental)
print(f"source.json: {a.version} ({a.build}) from {a.branch}, {len(app['versions'])} builds listed")
