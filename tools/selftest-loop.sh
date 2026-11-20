#!/bin/sh
# Builds and runs the engine's tests (the Makefile's TESTS) each time a branch gets a new commit, and sends the result
# to a log drop (tools/henge-logs-fetch.py reads it). For a Mac that can stay on, such as a virtual Mac in Henge
# itself: an engine change pushed to the branch is tested on real macOS without anyone at the keyboard.
#
#   sh tools/selftest-loop.sh DROP_URL [BRANCH]      (from a clone of the repository; Ctrl+C stops it)
#
# The result goes to DROP_URL/selftest-<commit>.txt. Only the tests are built, not hengevm (which needs libraries a
# plain Mac does not have). The clone is moved to each new commit (detached), so use one kept for this.
DROP=${1:?usage: selftest-loop.sh DROP_URL [BRANCH]}
BRANCH=${2:-claude/linux-runner-crash-flumcd}
REPO=${HENGE_REPO:-$(cd "$(dirname "$0")/.." && pwd)}
# Run from a copy: checking out a new commit can rewrite this file under the shell reading it.
if [ -z "$HENGE_LOOP_COPY" ]; then
    cp "$0" /tmp/henge-selftest-loop.sh && HENGE_LOOP_COPY=1 HENGE_REPO=$REPO exec sh /tmp/henge-selftest-loop.sh "$@"
fi
cd "$REPO" || exit 1
TESTS="test_classify test_jit_region test_operands test_jit test_mem test_mmu test_exc test_devices test_shutdown"
OUT=/tmp/henge-selftest.txt
last=""
echo "selftest-loop: watching $BRANCH in $REPO"
while :; do
    if git fetch -q origin "$BRANCH" 2>/dev/null; then new=$(git rev-parse -q --verify FETCH_HEAD); else new=""; fi
    if [ -n "$new" ] && [ "$new" != "$last" ]; then
        short=$(echo "$new" | cut -c1-7)
        echo "selftest-loop: $(date '+%H:%M:%S') testing $short"
        git checkout -q --detach "$new" 2>/dev/null || git checkout -q -f --detach "$new"
        {
            echo "selftest $new"
            git log -1 --format='%s'
            echo "$(date -u '+%Y-%m-%d %H:%M:%S') UTC, $(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null), $(uname -m), $(clang --version 2>/dev/null | head -1)"
            echo
            fails=0
            for t in $TESTS; do
                t0=$(date +%s)
                if make "build/$t" > /tmp/henge-build.log 2>&1; then
                    t1=$(date +%s)
                    ( ulimit -s 65520; exec "./build/$t" ) > /tmp/henge-run.log 2>&1 &
                    pid=$!
                    n=0 # up to 10 minutes
                    while kill -0 $pid 2>/dev/null && [ $n -lt 600 ]; do sleep 1; n=$((n + 1)); done
                    kill -9 $pid 2>/dev/null
                    wait $pid; rc=$?
                    t2=$(date +%s)
                    if [ $rc = 0 ]; then
                        echo "PASS $t (build $((t1 - t0)) s, run $((t2 - t1)) s): $(tail -1 /tmp/henge-run.log)"
                    else
                        fails=$((fails + 1))
                        echo "FAIL $t (exit $rc, build $((t1 - t0)) s, run $((t2 - t1)) s):"
                        tail -40 /tmp/henge-run.log | sed 's/^/    /'
                    fi
                else
                    fails=$((fails + 1))
                    echo "BUILD-FAILED $t:"
                    grep -E "error|Error" /tmp/henge-build.log | head -20 | sed 's/^/    /'
                fi
            done
            echo
            echo "RESULT $short: $fails failed"
        } > "$OUT" 2>&1
        tail -1 "$OUT"
        curl -sS -m 120 --data-binary "@$OUT" "$DROP/selftest-$short.txt" || echo "selftest-loop: upload failed (kept in $OUT)"
        last=$new
    fi
    sleep 90
done
