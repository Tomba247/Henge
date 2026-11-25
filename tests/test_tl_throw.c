/* An exception a program does not catch ends that program, not the app: test_tl_throw HELLO THROWER (what the terminal
   runs, and tests/tl/throw.cpp). */
#include <CoreFoundation/CoreFoundation.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "henge-ios-jit.h"
#include "henge-tl.h"
#include "jit_region.h"

static HvJitRegion jit;
static HengeDualMapping mapping;
_Atomic int henge_tl_ran;
void henge_ios_release_high_room(void) {}
void henge_tool_print(void) {}
void henge_tool_debug_print(void) {}
void henge_tool_swift(void) {}
int henge_vm_state(void) { return 0; }
HengeDualMapping *henge_ios_jit_get_mapping(void) { return &mapping; }
static char said[4096];
static size_t nsaid;
static void output(void *c, const char *p, size_t n) {
    (void)c;
    if (nsaid + n < sizeof said) memcpy(said + nsaid, p, n), nsaid += n, said[nsaid] = 0;
}
static void drawn(const char *b, size_t n) { (void)b, (void)n; }
static void open_url(const char *u) { (void)u; }
static void ended(int code) { (void)code; }
static int failures;
static char **args;

static void run(const char *how, int want, const char *text) {
    const char *argv[] = {args[2], how, NULL}, *env[] = {NULL};
    nsaid = 0, said[0] = 0;
    int rc = henge_tl_tool_run(args[2], 2, argv, env, "/private/tmp", output, NULL);
    int ok = rc == want && strstr(said, "caught\n") && strstr(said, text);
    fprintf(stderr, "%s %s: %d\n", ok ? "ok  " : "FAIL", how, rc);
    if (!ok) failures++, fprintf(stderr, "%s\n", said);
}
static atomic_int main_alive;
static void *tests(void *x) {
    (void)x;
    run("none", 0, "end");
    for (int i = 0; i < 180; i++) /* its memory is used again: 180 copies would not fit the region */
        run("main", 134, "not caught: from main");
    run("noexcept", 134, "not caught");
    run("thread", 134, "not caught: from a thread");
    run("queue", 134, "not caught: from the main queue");
    dispatch_async(dispatch_get_main_queue(), ^{ atomic_store(&main_alive, 1); });
    for (int i = 0; i < 30 && !atomic_load(&main_alive); i++) usleep(100000);
    if (!atomic_load(&main_alive)) failures++, fprintf(stderr, "FAIL the main thread does not go on\n");
    henge_tl_tool_away(1);
    run("main", 134, "in the background");
    henge_tl_tool_away(0);
    /* An app's program keeps its memory: it is not loaded over. */
    const char *env[] = {NULL};
    nsaid = 0;
    int rc = henge_tl_program_run(args[2], args[2], NULL, env, output, NULL), again = henge_tl_program_run(args[2], args[2], NULL, env, output, NULL);
    if (rc != 134 || again != -1) failures++, fprintf(stderr, "FAIL program %d, again %d: %s\n", rc, again, said);
    run("none", 0, "end");
    fprintf(stderr, "tl throw: %d failures\n", failures);
    fflush(stderr);
    _exit(failures != 0);
}
int main(int argc, char **argv) {
    if (argc < 3) return 2;
    args = argv;
    setenv("HV_JIT_SPLIT", "1", 1); /* the iOS shape */
    if (hv_jit_region_open(&jit, (size_t)1200 << 20)) { fprintf(stderr, "no JIT region\n"); return 2; }
    mapping = (HengeDualMapping){.rx_addr = (uint8_t *)jit.base, .rw_addr = (uint8_t *)jit.base + jit.rw_diff, .size = jit.capacity * 4};
    char err[512] = "";
    const char *a[] = {NULL};
    if (henge_tl_term_start(argv[1], a, a, NULL, 80, 40, drawn, open_url, ended, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
    sleep(1);
    pthread_t t;
    pthread_create(&t, NULL, tests, NULL);
    CFRunLoopRun();
}
