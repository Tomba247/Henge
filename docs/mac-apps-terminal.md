# A coding terminal on the iPhone, without macOS

Henge's **Mac Apps** runs Mac programs through its own loader, with no virtual Mac started. This adds a **terminal** to
it, and a **toolchain that runs in the same process**: a command-line coding program (Claude Code is the one tried) can
edit a project, compile it, link it into an iPhone app, run it without a window and read what it did, all on the phone.

- **No macOS, no virtual machine, no remote computer** for the building. (Claude Code itself still talks to its service.)
- **Off by default**: Henge › Settings › **Mac apps without macOS**.
- **Nothing in `core/`**: the engine, the virtual Mac and Linux machines run as before.

**Credits:** Henge, its engine and the Mac Apps loader this builds on (`tl/`) are Leviidev's and Henge's contributors'. The
terminal and toolchain were added by Tomba247.

## What it is for

The usual way to make an iPhone app with Claude and no Mac is Claude with **GitHub Actions**: each try is a commit, a
push, a wait for a cloud Mac, then reading its log or downloading an IPA to sign and install. A missing semicolon costs
a whole round.

Here the compiler is **on the phone, beside Claude**. It compiles, reads the errors, fixes them and runs code without a
window before anything is pushed or installed. So it takes **fewer test builds than going through Claude and GitHub
Actions**:

| | Claude + GitHub Actions | This terminal |
|---|---|---|
| A compile error | Found after a push and a cloud build | Found on the phone, in the same conversation |
| Build after a small change | A new cloud machine, often from nothing | Only the changed files |
| Testing logic | A workflow run, or an installed IPA | Run on the phone's own processor, no install |
| Actions minutes, commits just to test | Every try | None |
| IPAs to sign and install | Often one for each try | When there is something worth looking at |

Tests need **no simulator**: `"kind": "tool"` builds C, C++ or Swift into a command-line program that runs at once, and
Python is there as well. Screens and touch still need the installed app.

### What a strong phone is good for here

| Tried on a Mac, in the same loader | Result |
|---|---|
| Metal app with a shader file (compiled on the device by the app), SwiftUI, 120 Hz view | IPA built |
| Emulator-style C++20 core on four threads | Built and run here |
| AI-style kernels: Accelerate and NEON, 700 MB of data | Built and run here |
| Randomizer for a made-up ROM: shuffled table, fixed checksum, IPS patch, and its app | Run here; IPA built |
| A 40-file C project with generated data, by `make` and by CMake and Ninja | Built and run here |
| Swift command-line tests: `print`, `await` in main, actors, task groups, main actor | Built and run here |
| Code for another processor (`--target=armv4t-none-eabi`) | Compiles; there is no linker for it |
| Mac source to iPhone: shared SwiftUI, SwiftData, Charts and Objective-C++ | IPA built |

Most of the made apps were not installed or tested on an iPhone. For ROM tools and ports, use your own copy of a game.

You still install an IPA to check what it looks like and how it feels, and Actions is still right for releases, Xcode
projects and anything this toolchain lacks (below). How many tests it saves depends on the project; it was not measured.

## How to use it

**You do not install macOS for this**: no virtual Mac, no 43 GB. You need an iPhone or iPad that Henge runs on with JIT,
your own copy of the program you want in the terminal, and, **once, to make the toolchain, any Mac with Xcode** (your own,
a friend's or a rented one). After that no Mac is needed. Building needs the toolchain; without it the terminal has little more
than the program you bring.

Henge makes its folders when it opens: **Projects** (with Toolkit in it), **Mac Apps** and **Shared**, all in Files › On My
iPhone › Henge. You make none of them.

1. **Make the toolchain**, on a Mac with Xcode and Homebrew's `python`, `ninja` and `cmake`:
   `zsh tools/phone-kit/make_toolchain.sh ~/Toolchain` (set `BREW` to Homebrew's folder, such as `/opt/homebrew`).
   It copies Apple's compiler and iPhone SDK from your Xcode. They are Apple's: for your own use, **not to be shared**.
   No release of this fork contains them.
2. **Put it on the phone**: the folder called `Toolchain` that you just made goes inside Files › On My iPhone › Henge ›
   **Projects** (Projects is already there).
3. **Turn on** Settings › **Mac apps without macOS**.
4. **Bring in the program** (Mac Apps): a program, an `.app`, or a `.dmg`, also one left in Files › Henge › Mac Apps. For Claude
   Code, its macOS arm64 program file by itself (the `claude` in Anthropic's macOS download). It is not included: it is
   Anthropic's, under its own licence.
5. **Open in a Terminal.** Sign in the way the program asks; that is between you and it.
6. Ask for something: "make an app that…", "build this and fix the errors". The result is an **unsigned `.ipa`** in the
   project's folder, to sign with whatever you sign Henge with.

One program runs per launch. Quit Henge to run another, or to start a Mac.

## How it works

```
 iPhone app process (the only one: iOS allows no fork and no exec)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ MacTerminal + TerminalScreen   the screen, keys, paste and selection     │
 │        │ keystrokes / output over a socketpair                           │
 │ henge-tl-term.c                a pretend tty, spawn, wait and keychain   │
 │        │                                                                 │
 │ Claude Code (Mach-O, arm64)    loaded by tl/, its JavaScript interpreted │
 │        │ MCP over HTTP, 127.0.0.1 only, secret made at each launch       │
 │ ToolServer                     build · run_command · search · …          │
 │        │                                                                 │
 │ ProjectMaker / ProjectBuilder  henge-project.json → units → link → IPA   │
 │        │ henge_tl_tool_run                                               │
 │ tl_tool.h                      clang · ld · swift-frontend · cmake ·     │
 │                                ninja · python3 · sh · what you built     │
 └──────────────────────────────────────────────────────────────────────────┘
```

### The loader (`tl/loader`)

`tl` maps a Mac program's Mach-O images into Henge's own process and binds them to iOS's frameworks and to its stand-ins.
The code is already arm64 and runs as it is; it is neither translated nor emulated. What this work adds to it:

- Libraries are found the way dyld does it: a bare `@loader_path` or `@executable_path`, the search paths of the images
  that load a library, and a library already loaded under the same install name. Xcode's Swift compiler needs all three.
- `first_lookup`: a host's own answer for a symbol can come before the loader's general one.

### A pretend terminal (`henge-tl-term.c`, `MacTerminal.swift`, `TerminalScreen.swift`)

iOS gives apps no pseudo-terminal. The program's input and output are one end of a socket; `isatty`, `tcgetattr`,
`ioctl(TIOCGWINSZ)` and the rest answer for it. `TerminalScreen` is a small terminal emulator written for this: colours,
the alternate screen, scrolling regions, wide characters, links, and OSC 52 for the clipboard. It keeps track of wrapped
rows, so a link across several rows opens and copies whole.

- **Paste** uses the system's paste control, as iOS refuses an app that reads another app's text by itself.
- **Keys** a phone keyboard lacks (Esc, Tab, Control, arrows) are in a row that scrolls, with Hide always in reach.
- **Web pages** a program wants opened are asked about first.

Claude Code is a Bun program. Bun's JavaScript JIT is turned off and the interpreter runs.

### Saving a sign-in

On a Mac, Claude Code keeps its sign-in by running `/usr/bin/security`. iOS has no such program, and an app cannot start
one. Henge's answer is a `security` that holds no items and refuses to add any, with the real tool's exit codes. Claude
Code then uses its own fallback, a private file (mode 0600) in Henge's Application Support. Henge never reads it; its log
records that each step happened and never an argument.

### A toolchain in one process (`tl/loader/tl_tool.h`)

A compiler expects a new process each time: it registers its options once, frees little, and ends with `exit`. Here:

1. Each tool image is loaded once. Its writable data is **snapshotted before its initializers run**, put back before
   each run, and the initializers then run again.
2. Each run has **a malloc zone of its own**, destroyed afterwards. Files, maps, thread keys and C++ streams it left are
   let go.
3. `exit`, `abort` and `_exit` **end the run**, not the app. Its other threads end at their next wait.
4. `posix_spawn`, `posix_spawnp`, `system`, `popen`, pipes, `wait4`, `waitpid` and `kqueue` process events are answered
   inside the process. A "process" is a thread with its own arguments, environment, folder, standard files and a made-up
   process number. **CMake, Ninja and Python's `subprocess` therefore work**, with several tools at once.
5. Several copies of one tool run side by side in separate **namespaces**, so three compilers can work together.

`tools/phone-kit/sh.c` is a small shell made for this: pipes, `&&`, `||`, redirection, here-documents, `if`, `for`,
`while`, `case`, functions, `[[ ]]`, `$(( ))`, `${x%y}`, `$(…)`, and built-in `cp`, `mv`, `rm`, `mkdir`, `ls`, `cat` and
others. The parts of a pipe run one after the other. `tools/phone-kit/devtools.py` answers for the usual developer tools
(`grep`, `sed`, `awk`, `find`, `diff`, `patch`, `make`, `tar`, `curl`, `jq`…): Python versions with the common options.

The program in the terminal may start these too: a shell it starts gets a made-up process number, and its ends, waits,
kills and `kqueue` watches are answered. **Claude Code's own Bash tool works this way.** Any other program is refused.

### Address space

At launch Henge holds about 60 GB of an iPhone app's 64 GB of address space, for Mac apps whose allocators need a big
range. The terminal does not use it, and with it held there may be no room for a compiler. It is given back at the
first tool run. On an iPhone 18 Pro Max the log said there was no room for 512 MB before that, and room after.

### Projects (`ProjectMaker.swift`, `ProjectBuilder.swift`)

A project is a folder with a `henge-project.json`, which the coding program writes itself:

```json
{
  "name": "MyApp", "bundle_id": "com.example.myapp", "version": "1.0", "min_ios": "17.0",
  "sources": ["src/**/*.c", "ios/*.swift"],
  "include": ["include"], "frameworks": ["UIKit", "Metal"],
  "resources": ["assets"], "icon": "icon.png"
}
```

C, C++, Objective-C(++), assembly and Swift (with SwiftUI and macros). Builds are incremental by content hash and by the
compiler's dependency output. A project with its own build system gives `commands` instead
(`cmake -S . -B build/cm`, `ninja -C build/cm`) and names the program they make. `"kind": "tool"` makes a program to run
here. A very large project can come as a kit instead (below).

### Kits: a big project that is already mostly built

A phone can compile a small app from nothing in under a minute. A project of hundreds of files would take far longer,
and much of it never changes. A **kit** is such a project with the slow part already done on a Mac:

| In a kit | What it is |
|---|---|
| `source/` | The sources the build used, and every header they read |
| `build/` | The compiled objects and libraries from the Mac |
| `app/` | What goes in the app besides its program: pictures, data, `Info.plist` |
| `toolchain/` `sdk/` | The `clang`, `ld` and iPhone SDK it was built with |
| `henge-build.json` | Each source with its object and flags, what it depends on, and how to link, pack and run it |

`henge-build.json` holds a hash of every file an object was made from. On the phone, `build` compares them: if you
change one source, **only that one is compiled** (a header: the sources that read it), then everything is linked and
packed into a new IPA. Nothing is compiled when nothing changed. Moving the folder is fine, as paths are relative to it.

How to make one, on a Mac:

1. Build the project for the iPhone the usual way with CMake and Ninja.
2. `python3 tools/phone-kit/make_kit.py SOURCE BUILD TARGET APP KIT` (the source folder, the build folder, Ninja's
   target, the `.app` in the build folder, and the folder to make).
   - `--prebuilt TEXT` leaves out sources whose path has TEXT, such as a huge generated folder; their objects are used.
   - `--run FILE` says how the program runs without a window, so that the coding program can test it.
3. Zip it, send it to the phone, and put the folder in Files › Henge › **Projects**.

| | Project (`henge-project.json`) | Kit (`henge-build.json`) |
|---|---|---|
| Made by | The coding program, on the phone | `make_kit.py`, on a Mac |
| Needs a Mac | Only for the Toolchain, once | Yes, to make the kit |
| First build | Everything is compiled | Only what you changed |
| New source files | Yes | No: make the kit again |
| For | New apps, small and middle-sized projects | Large existing projects |

A kit holds Apple's compiler and SDK, and your project's code: for your own use, like the toolchain. No kits are in this
repository or its releases.

### The tools a coding program gets (`ToolServer.swift`)

| Tool | What it does |
|---|---|
| `build` | Compile what changed, link, pack the unsigned IPA, or return the errors |
| `run_command` | A shell line with the toolchain, and programs built here |
| `run_program` · `program_command` · `stop_program` | Run the build without its window, send it JSON commands, stop it |
| `search` · `find_files` | Regular-expression search |
| `project_help` | The project format and what works, for the program to read first |
| `download` · `github_download` | Bring in source (zip, tar.gz, tar.xz, tar.bz2; submodules too) |
| `github_save` | Commit chosen files to a branch, never forced. Needs GitHub connected |
| GitHub's own tools | Pull requests, issues, code search: GitHub's hosted server, when connected |

**Connecting GitHub** (Mac Apps › the program › GitHub): **Make a token** opens GitHub's page with what is needed filled
in; choose the repositories, generate, and paste it back. Henge checks it and shows who you are connected as. Disconnect
removes it from the iPhone; GitHub's settings revoke it.

**Safety.** The server listens on 127.0.0.1 only, and a new secret is needed at every launch. Folders the tools write to
must be inside Projects. The optional GitHub token is kept in the iPhone's Keychain; while the program runs it is also in
a config file only Henge's own process can read. What the toolchain runs is native code in Henge's process and is **not
sandboxed from Henge**: run only source you trust, and look before allowing a coding program to run a command.

## Developer tools: what is here, and what is missing

The compiler, linker and SDK are in the **toolchain you make yourself**. The rest comes in the IPA and is put into the
toolchain when the terminal opens. They are Henge's own programs, with the common options; an option they lack is an
error. They are not Apple's and not the originals.

| For | Here | Missing or limited |
|---|---|---|
| Compiling | `clang`, `clang++`, `swiftc`, `swift FILE`, `swift -e`, `swift-frontend`, assembler, for arm64 iOS | The simulator, macOS, watchOS; `swift build`, `swift test`, the REPL; other processors have no linker |
| Linking, libraries | `ld`, `ar`, `libtool`, `ranlib`, `lipo`, `install_name_tool` | `strip`, `dsymutil` |
| Building | `xcodebuild`, `xcrun`, `xcode-select`, `cmake`, `ninja`, `make`, `henge-project.json` | `autoconf`/configure, Bazel, CocoaPods, Fastlane |
| Xcode projects | One iPhone app with its static libraries, frameworks, Swift packages of plain source, string catalogs, plain pictures and icon | Storyboards, xibs, colours and data of asset catalogs (no `actool`), Core Data models, app extensions, build scripts, package plugins and binaries, archives, tests |
| Metal | `.metal` source compiled by the app on the device | `metal`, `metallib` |
| Signing | `codesign -s -`: ad hoc, with entitlements, of programs, apps, frameworks and whole IPAs; `-d`, `--verify` | Certificates and provisioning: your sideloader does that |
| Looking inside | `otool`, `nm`, `objdump`, `size`, `strings`, `dwarfdump --uuid`, `vtool -show-build` | `lldb`, Instruments, `sample`, `leaks` |
| Property lists, text | `plutil`, `PlistBuddy`, `xcstringstool`, `agvtool`, `sw_vers` | `genstrings`, `ibtool`, `sips`, `iconutil` |
| Version control | `git`: clone, submodules, status, diff, log, commit and other local commands | `git push`, `merge`, `pull`, `rebase`, LFS: `github_save` sends files, when GitHub is connected |
| Shell | `sh` and about 90 names: `grep`, `rg`, `sed`, `awk`, `find`, `diff`, `patch`, `tar`, `zip`, `curl`, `jq`, `sqlite3`… | Job control; a pipe's parts run one after the other |
| Scripting | Python 3 with its standard library | pip packages with C parts, Node, Ruby, Rust, Go, Java |
| Packages | `brew` says whether a tool is here | Installing with `brew`, `apt`, `npm` |
| Testing | `run_program` (no window), `program_command`, `framecheck` (flicker in a run of frames) | The iOS simulator, XCTest, Swift Testing; touch, sound and looks need the installed app |
| After a crash | `henge_log` and Projects/henge-logs | A debugger |

`project_help` in the terminal is the full reference, written for the coding program.

## Toolkits: adding the tools a project needs

Ask the coding program: "this project needs X, build what you need". It makes the tool in **Projects/Toolkit**:

- **Toolkit/bin/NAME** is a command in every project: a Python or shell script, or a program built on the phone.
- It **stays when Henge is updated** (Toolchain/bin does not).
- Source goes in Toolkit/src with its licence, shared libraries in Toolkit/lib and headers in Toolkit/include, and
  Toolkit/README.md lists what there is.
- Names that are already here come first, so a tool needs a new name.

No toolkits, games or other people's projects are in this repository or its releases.

## Changing Henge itself

iOS does not let a running app change its own signed code, so a change to Henge is always a new IPA to sign and install.

**On the phone.** Unpack **Henge-source.zip** into Files › Henge › Projects and ask for a change there. `build` makes
`Henge.ipa`: the C parts by Ninja, then the Swift. The icons, the JIT helper, the GPU libraries, the terminal's own tools
and some libraries are taken ready-made. Its CLAUDE.md has the rules. Keep the IPA that works: close Henge and open it
again after building it, as the build uses most of the memory set aside for programs.

**With GitHub Actions**, when the phone's way does not work, or from any computer:

1. Fork the repository (or use your own fork) and choose the branch, here `mac-speed`.
2. Change files on github.com, with `github_save` from the terminal, or with git.
3. Actions › **Build Henge IPA** › Run workflow › choose the branch; set "Linux machine build" to `none` unless you want
   one attached. No Mac or Apple account is needed.
4. It makes a release with Henge.ipa, Henge.tipa and Henge-source.zip. Its build number is the run's number plus 100.
5. Sign and install it over the Henge you have.

## Crash logs

A bad memory access anywhere in the app closes Henge, and the terminal's program may say that it was the one that
crashed. Henge first notes whose thread it was and the calls (library and offset), and whether its stack was full. At the
next start this is a file in **Projects/henge-logs**, and the coding program is told to read it (`henge_log`): `crash`,
`previous` run, or `now`. App folder paths and the log's upload address are left out of it.

## Numbers

Single runs, not averages; yours will differ. "iPhone" is an iPhone 18 Pro Max with iOS 27.0.1; its figures were reported
by the coding program on it or are in its log. "Mac" is an Apple silicon Mac running the same toolchain in the same loader.

| What | iPhone | Mac |
|---|---|---|
| Henge itself, from nothing (79 C steps, then the Swift) | 37.7 s | 24.3 s |
| Henge again, one Swift file changed | not measured | 14.2 s |
| Henge again, one C header changed | not measured | 0.9 s |
| A small Xcode project with `xcodebuild`, first time | 17.9 s | not measured |
| The same with nothing changed | 0.0 s | not measured |
| A sample app from nothing (an earlier build) | 28.7 s | not measured |
| `framecheck` on 48 frames of 426x224 | under 1 s | not measured |
| Henge.ipa built this way | 34.8 MB | 34.8 MB |

| Memory | Figure |
|---|---|
| Set aside for program code at launch, iPhone | 1536 MB |
| Of that used by Claude Code | 348 MB |
| After building Henge | 1197 MB |
| iOS's limit for Henge, with the entitlement | 6652 MB |
| Left with the terminal open in the background | 4784 MB |
| Peak of the whole process building Henge, Mac | about 3.3 GB |

There is no comparison with Xcode or with GitHub Actions here: it was not measured.

## What is tested, and what is not

| | State |
|---|---|
| Terminal, sign-in and saved sign-in, Claude Code's Bash tool | Used on an iPhone 18 Pro Max, iOS 27.0.1 |
| `xcodebuild` of a small app, installed and opened | That iPhone |
| `git` clone, `framecheck` | That iPhone |
| Henge built on the phone, signed, installed, opened, JIT on | That iPhone, once |
| Methods that a library adds to Metal's classes (Vulkan through MoltenVK in `run_program`) | That iPhone |
| `swiftc`, `swift`, `codesign`, `plutil`, `PlistBuddy`, `otool`, `size`, `xcstringstool` | On a Mac, several compared with Apple's; **not tried on an iPhone** |
| Toolkit folder, `henge_log`, the crash note, larger thread stacks | On a Mac; **not tried on an iPhone** |
| `download`, `github_download`, `github_save` | Little tried |
| Other iPhones, iPads, older iOS | **Not tried** |

Known gaps:

- **A crash seen on an iPhone while a program ran is not located yet.** This build gives a program's threads larger stacks,
  a possible cause that is not proven, and notes where the next one happens.
- In the background iOS gives no GPU: a program that draws then fails. Keep Henge on screen.
- Programs here share the memory above. One that crashes takes Henge with it.
- Code that is run is not sandboxed from Henge: run only what you trust.

## Trying the code

`make build/test_tl_term build/test_project` builds the Mac-side tests (the terminal and the project tools);
`tests/test_terminal.swift` covers the screen. They run on a Mac, in the same loader.
