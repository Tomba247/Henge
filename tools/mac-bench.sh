#!/bin/sh
# A load test for a virtual Mac on Henge: the same few jobs, round after round, so the numbers show how fast the
# Mac is under load and how it slows as the phone heats (the app's own log, sent beside this, has the thermal state
# and the engine's stats for the same minutes). About 10 minutes; nothing is installed or changed.
#
#   sh tools/mac-bench.sh [ROUNDS]        (from a clone of the repository; prints the report)
#
# Each round: compile the engine's tests from nothing (many short clang processes: what starting apps is like),
# one CPU loop, four at once, 400 processes started one after another, memory copied by the kernel, and a file
# written and read. Seconds for each, with the time of day (UTC) each round began.
ROUNDS=${1:-6}
REPO=${HENGE_REPO:-$(cd "$(dirname "$0")/.." && pwd)}
cd "$REPO" || exit 1
B=/tmp/henge-bench-build
now() { perl -MTime::HiRes=time -e 'printf "%.2f\n", time' 2>/dev/null || date +%s; }
took() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }
spin() { awk 'BEGIN { for (i = 0; i < 12000000; i++) s += i * i % 7; print s }' > /dev/null; }
mem=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
echo "mac-bench: $(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null), $(sysctl -n hw.ncpu 2>/dev/null) CPUs, $((mem >> 20)) MB, commit $(git rev-parse --short HEAD 2>/dev/null)"
echo "started $(date -u '+%Y-%m-%d %H:%M:%S') UTC, $ROUNDS rounds"
echo "round  at(UTC)   compile  cpu1  cpu4  spawn400  memcopy  file   load1"
r=1
while [ "$r" -le "$ROUNDS" ]; do
    at=$(date -u '+%H:%M:%S')
    rm -rf "$B"
    t0=$(now)
    make B="$B" "$B/test_classify" "$B/test_operands" "$B/test_jit" "$B/test_mem" "$B/test_mmu" "$B/test_exc" > /tmp/henge-bench-make.log 2>&1 || echo "  (compile failed: see /tmp/henge-bench-make.log)"
    t1=$(now)
    spin
    t2=$(now)
    spin & spin & spin & spin & wait
    t3=$(now)
    i=0
    while [ $i -lt 400 ]; do /usr/bin/true; i=$((i + 1)); done
    t4=$(now)
    dd if=/dev/zero of=/dev/null bs=1m count=3000 2>/dev/null
    t5=$(now)
    dd if=/dev/zero of=/tmp/henge-bench-file bs=1m count=200 2>/dev/null && sync && dd if=/tmp/henge-bench-file of=/dev/null bs=1m 2>/dev/null
    t6=$(now)
    load=$(sysctl -n vm.loadavg 2>/dev/null | awk '{ print $2 }')
    printf "%-6s %-9s %-8s %-5s %-5s %-9s %-8s %-6s %s\n" "$r" "$at" "$(took "$t0" "$t1")" "$(took "$t1" "$t2")" "$(took "$t2" "$t3")" "$(took "$t3" "$t4")" "$(took "$t4" "$t5")" "$(took "$t5" "$t6")" "$load"
    r=$((r + 1))
done
rm -rf "$B" /tmp/henge-bench-file
echo "ended $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
