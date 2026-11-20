# Henge test builds: app source (unofficial)

> **Henge is not my project.** [Henge](https://github.com/Leviidev/Henge) is made by **Leviidev** and its
> contributors, and all credit for it goes to them. These are unofficial test builds from a fork
> ([Tomba247/Henge](https://github.com/Tomba247/Henge)) that adds experimental features on top, such as a Linux
> machine (Ubuntu and Kali desktops on Henge's engine). They aren't affiliated with or endorsed by the Henge
> project. For the official app, go to [Leviidev/Henge](https://github.com/Leviidev/Henge). Report problems with
> these builds to Tomba247, not to Henge.

Add one of these URLs as a source (repo) in FlareStore, SideStore, AltStore, Feather or any app that reads
AltStore-format sources:

```
https://raw.githubusercontent.com/Tomba247/Henge/source/henge.json
https://raw.githubusercontent.com/Tomba247/Henge/source/henge-experimental.json
```

`henge.json` lists the working builds (branch `league`); `henge-experimental.json` lists the experimental ones
(branch `league-el0`), which may crash. `source.json` lists both and is what the two are made from.

Every build the fork's GitHub Actions publish is added to `source.json` by `update_source.py`, newest
first. Each build's notes say what changed, its branch and commit, and that it is an unofficial build. Same bundle ID (`com.henge.app`) throughout, so
each install is an update and keeps your Macs, as long as it's signed with the same certificate.

Henge needs JIT, so sign it with a **development** certificate (one with `get-task-allow`). A
distribution certificate installs the app, but JIT won't turn on.
