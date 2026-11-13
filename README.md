# Henge test builds: app source

Add one of these URLs as a source (repo) in FlareStore, SideStore, AltStore, Feather or any app that reads
AltStore-format sources:

```
https://raw.githubusercontent.com/Tomba247/Henge/source/henge.json
https://raw.githubusercontent.com/Tomba247/Henge/source/henge-experimental.json
```

`henge.json` lists the working builds (branch `league`); `henge-experimental.json` lists the experimental ones
(branch `league-el0`), which may crash. `source.json` lists both and is what the two are made from.

Every build the fork's GitHub Actions publish is added to `source.json` by `update_source.py`, newest
first, with its branch name at the start of its notes. Same bundle ID (`com.henge.app`) throughout, so
each install is an update and keeps your Macs, as long as it's signed with the same certificate.

Henge needs JIT, so sign it with a **development** certificate (one with `get-task-allow`). A
distribution certificate installs the app, but JIT won't turn on.
