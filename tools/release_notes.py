#!/usr/bin/env python3
"""A release's title and notes in this fork's style (tools/release-notes.sh).

    release_notes.py RELEASE.json TAG REPO [CHANGE]

RELEASE.json is `gh release view --json name,body,assets`. Prints the title on the
first line and the notes after it. CHANGE is the build's change (default: the one
already in the notes, or their first line).
"""
import json
import re
import sys

UPSTREAM = "https://github.com/Leviidev/Henge"

FILES = {
    "Henge.ipa": ("The app", "Install it with SideStore, AltStore, Feather or another sideloader."),
    "Henge.tipa": ("The app, for TrollStore", "The same app, packaged for TrollStore."),
    "Henge-Ubuntu-GNOME.zip": ("Ubuntu 24.04 · GNOME", "Ubuntu's own desktop and dock, Firefox, Google Chrome and a YouTube app."),
    "Henge-Ubuntu-Xfce.zip": ("Ubuntu 24.04 · Xfce", "A lighter desktop with Firefox and a YouTube app."),
    "Henge-Kali.zip": ("Kali Linux · Xfce", "Kali rolling with Kali's own Xfce desktop, Firefox ESR, Chromium and Kali's top 10 tools."),
    "Henge-Kali-GNOME.zip": ("Kali Linux · GNOME", "Kali rolling with GNOME and a dock, Firefox ESR, Chromium and Kali's top 10 tools."),
    "Henge-Ubuntu-GNOME-Vulkan-test.zip": ("Ubuntu 24.04 · GNOME, Vulkan test", "For trying Vulkan, not for everyday use: the same desktop with Vulkan's drivers and a **Vulkan test** button in the dock. Vulkan is off unless the line `HV_LINUX_VULKAN=1` is in Files › On My iPhone › Henge › henge-env.txt."),
    "Henge-Kali-GNOME-Vulkan-test.zip": ("Kali Linux · GNOME, Vulkan test", "For trying Vulkan, not for everyday use: the same desktop with Vulkan's drivers and a **Vulkan test** button in the dock. Vulkan is off unless the line `HV_LINUX_VULKAN=1` is in Files › On My iPhone › Henge › henge-env.txt."),
}


def size(n):
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def main():
    rel = json.load(open(sys.argv[1]))
    tag, repo = sys.argv[2], sys.argv[3]
    change = sys.argv[4].strip() if len(sys.argv) > 4 else ""
    name, body = rel.get("name") or "", rel.get("body") or ""
    # The version: from notes already written here, else from the title a build first gave ("Henge 0.3.0 (101.84) ...").
    kept = re.search(r"^\*\*Version:\*\* ([\d.]+ \(build [\d.]+\))", body, re.M)
    m = re.search(r"Henge ([\d.]+) \(([\d.]+)\)", name)
    version = kept.group(1) if kept else f"{m.group(1)} (build {m.group(2)})" if m else tag
    if not change:
        old = re.search(r"^\*\*What changed:\*\* (.*)$", body, re.M)
        change = old.group(1).strip() if old else (body.strip().splitlines() or [""])[0]
    sha = re.search(r"built from ([0-9a-f]{40})", body)
    assets = {a["name"]: a["size"] for a in rel.get("assets", [])}
    machines = [f for f in FILES if f.endswith(".zip") and f in assets]

    out = [f"Unofficial fork of Henge (by Leviidev) · Linux on iPhone test build {tag.rsplit('-', 1)[-1]}"]
    out += [
        "> [!IMPORTANT]",
        f"> **Henge is not my project.** [Henge]({UPSTREAM}) was created by **Leviidev**, with its contributors. All credit for Henge, its engine, and running macOS on iPhone goes to them.",
        "> This is an **unofficial, experimental fork build**, not a Henge release. I'm not affiliated with the Henge project and I'm not claiming any of its work as mine.",
        "",
        "### What this fork adds",
        f"On top of Henge's engine, this fork adds a **Linux machine**: real Ubuntu and Kali desktops running on the iPhone, with the phone's GPU for 3D. That, with some input and sound changes, is what's new here. Everything else is Henge. It was written with [Claude Code](https://claude.com/claude-code) (an AI coding assistant) in [{repo}](https://github.com/{repo}). It's offered back to the Henge developer, to use or ignore as they see fit.",
        "",
        "### Please",
        f"- **Want Henge itself?** Get it from [the official project]({UPSTREAM}), and support Leviidev there.",
        f"- **Found a bug in these builds?** Tell [{repo.split('/')[0]}](https://github.com/{repo}), **not** the Henge project. These builds may break things the official app doesn't.",
        "- Henge's license and copyright stay as they are. This fork keeps them unchanged.",
        "",
        f"**Version:** {version}  ",
        f"**What changed:** {change}" + (f"  \n**Source:** [{sha.group(1)[:7]}](https://github.com/{repo}/commit/{sha.group(1)})" if sha else ""),
        "",
        "## Downloads",
        "",
        "| File | Size | What it is |",
        "|---|---|---|",
    ]
    for f in [f for f in FILES if f in assets] + sorted(f for f in assets if f not in FILES):
        title, what = FILES.get(f, (f, ""))
        out.append(f"| [`{f}`](https://github.com/{repo}/releases/download/{tag}/{f}) | {size(assets[f])} | **{title}.** {what} |")
    out += [
        "",
        "## Running a Linux machine",
        "",
        "1. Install **Henge.ipa** (or **Henge.tipa** with TrollStore). If you already have it, installing over it keeps your machines.",
    ]
    if machines:
        out += [
            "2. Download a Linux machine above and tap it in the **Files** app to unzip it. It needs about three times the zip's size free while unzipping.",
            "3. In Henge, bring in the unzipped folder the same way you bring in a Mac.",
            "4. It signs in as **henge** with no password, and `sudo` works.",
        ]
    else:
        out += ["2. Linux machines from an earlier release of this fork work with this app."]
    out += [
        "",
        "Tips:",
        "- 3D runs on the phone's GPU (virglrenderer on ANGLE, drawn with Metal). If a machine misbehaves, add the line `HV_LINUX_NO_3D=1` to **Files › On My iPhone › Henge › henge-env.txt** to turn 3D off.",
        "- The phone slows itself down when it gets hot. A cooler, no case, and not charging while you use it all help.",
    ]
    out += ["", "---", f"<sub>Henge © Leviidev and contributors, {UPSTREAM}. This is an unofficial fork build and isn't affiliated with or endorsed by the Henge project.</sub>"]
    if sha:
        out += ["", f"<sub>built from {sha.group(1)}</sub>"]
    print("\n".join(out))


main()
