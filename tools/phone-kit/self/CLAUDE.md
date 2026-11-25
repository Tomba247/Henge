# Henge, built on the phone

`build` with this folder makes Henge.ipa: the C parts by ninja (kit/selfbuild.py reads the Makefile's lists), then ios/Henge's Swift and C.
The user signs it and installs it over Henge. Projects and Macs stay.

- Keep a copy of the IPA that works (Files > Henge > Shared) before each new one. A build that does not start is replaced by it.
- Raise CFBundleVersion in henge-project.json ("info") for each IPA, as 101.138 to 101.139. Never change bundle_id or CFBundleIdentifier.
- Not built here, taken from the IPA this folder was made from (kit/app): the icons, StikJIT, the JIT helper, the GPU frameworks, henge-agent.gz,
  henge-tools.z (the terminal's own sh and devtools.py) and tl-samples.dmg. kit/lib holds ready libraries (installer, networking, pairing).
  Changing tools/phone-kit/devtools.py or sh.c here has no effect on the IPA: use Projects/Toolkit/bin for tools.
- The IPA carries no entitlements of its own: the signing app's profile gives them.
- Tests in tests/ are for a Mac. What can be checked here: that it builds, and small "tool" projects around the C code.
- core/ runs the macOS machine: a change there says what it does to macOS, is behind an HV_* knob (core/machine.c) and off by default.
- Never hide a failure: say what was built, and that it was not run until the user has.
- Leviidev made Henge's engine; keep the credits. kit/ is for this phone only: never github_save it. No personal details, no model names in commits.
- To save source changes: github_save of the changed files to the branch the user names, not main.
