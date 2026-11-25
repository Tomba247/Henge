/* Part of tl_loader.c: tools run again and again in one process (tl_load_tool, tl_tool_run). A compiler or linker
   expects a fresh process: it registers its options once and frees little. Before each run its writable data is put
   back as it was before its initializers, which then run again; its heap is a zone of its own, destroyed afterwards;
   what it left mapped or open is let go. */
#include <sys/ioctl.h>
#include <malloc/malloc.h>
#include <mach/mach_time.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/mount.h>
#include <sys/clonefile.h>
#include <copyfile.h>
#include <stdatomic.h>
#include <dirent.h>

typedef struct TlToolExit { void (*fn)(void *); void *arg; } TlToolExit;
typedef struct TlToolMap { void *at; size_t len; } TlToolMap;

/* One running tool. */
typedef struct TlToolRun {
    TlImage *exe;
    int ns;
    malloc_zone_t *zone;
    pthread_t thread;
    int argc, code, ended, stuck;
    const char **argv;
    char **env;         /* its environment */
    const char *command; /* the folder tl_tool_command started it in */
    char *cwd;          /* its folder, NULL: the app's */
    char *_Atomic alias; /* where TL_TOOLCHAIN really is, once it has used that name */
    int std[3];         /* what its standard files are; -1: output goes to the host, input is empty */
    void *entry;        /* called instead of the program's main */
    int keeps;          /* ended by an exception, its thread and memory stay (tool_terminate) */
    _Atomic int stop;   /* the program has ended: its other threads end at their next wait */
    _Atomic int kill;   /* it is to end: its main thread too */
    _Atomic int live;   /* threads it started that still run */
    void (*output)(void *, const char *, size_t);
    void *context;
    TlToolExit *exits;
    size_t nexits, capexits;
    TlToolMap *maps;
    size_t nmaps, capmaps;
    unsigned char fds[1024]; /* 1: it opened it; 2: an end of its pipe */
    ino_t fdino[1024];
    pthread_key_t keys[128];
    int nkeys;
    struct TlToolJob *jobs; /* Swift's, for its main actor */
    struct sigaction sigchld;
    _Atomic int chld; /* children that ended, and how many of them a wait of its has been told */
    int chld_seen;
} TlToolRun;

static pthread_mutex_t g_tool_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_tool_cond = PTHREAD_COND_INITIALIZER;
static TlToolRun *g_tool_runs[TL_MAX_NS]; /* what each namespace runs now */
static TlToolRun g_tool_none;             /* for a call from code that belongs to none */
static pthread_key_t g_tool_key;          /* the run a thread belongs to */
static pthread_once_t g_tool_once = PTHREAD_ONCE_INIT;
static int (*g_thread_chdir)(const char *), (*g_thread_fchdir)(int);
static int tool_in_read_first(void *c, char *p, int n);
static FILE *g_tool_in; /* every tool's stdin, unbuffered */
static void tool_terminate_init(void);
static void tool_init(void) {
    pthread_key_create(&g_tool_key, NULL);
    tool_terminate_init();
    g_tool_none.zone = malloc_default_zone();
    g_tool_in = funopen(NULL, tool_in_read_first, NULL, NULL, NULL);
    setvbuf(g_tool_in, NULL, _IONBF, 0);
    g_thread_chdir = dlsym(RTLD_DEFAULT, "pthread_chdir_np");
    g_thread_fchdir = dlsym(RTLD_DEFAULT, "pthread_fchdir_np");
}
/* The run the calling code belongs to: its thread's, or (a system thread) that of the image the call came from. */
static TlToolRun *tool_run_of(const void *caller) {
    TlToolRun *r = pthread_getspecific(g_tool_key);
    if (r) return r;
    TlImage *im = tl_image_containing(caller);
    r = im && im->ns > 0 && im->ns < TL_MAX_NS ? g_tool_runs[im->ns] : NULL;
    return r ? r : &g_tool_none;
}
#define g_tool (*tool_run_of(__builtin_return_address(0)))
static void tool_opened(TlToolRun *r, int fd, int kind) {
    struct stat st;
    if (fd < 3 || fd >= 1024 || r == &g_tool_none || fstat(fd, &st)) return;
    r->fds[fd] = (unsigned char)kind, r->fdino[fd] = st.st_ino;
}
static int tool_pipe(int p[2]) {
    int rc = pipe(p);
    if (!rc) tool_opened(tool_run_of(__builtin_return_address(0)), p[0], 2), tool_opened(tool_run_of(__builtin_return_address(0)), p[1], 2);
    return rc;
}
static int tool_fclose(FILE *f) {
    TlToolRun *r = tool_run_of(__builtin_return_address(0));
    if (f == stdout || f == stderr) return 0;
    int fd = fileno(f);
    if (fd >= 0 && fd < 1024) r->fds[fd] = 0;
    return fclose(f);
}
static void tool_enter_folder(TlToolRun *r) {
    if (r->cwd && g_thread_chdir) g_thread_chdir(r->cwd);
}

/* Each section of the image's writable data that a run may change. Objective-C's and Swift's own stay: their
   runtimes keep pointers into them. */
static void tool_each_section(TlImage *im, void (^f)(uint8_t *addr, size_t size, int zero)) {
    each_section(im, ^(const struct section_64 *sect, uint8_t *addr) {
        uint32_t type = sect->flags & SECTION_TYPE;
        if (!sect->size || strncmp(sect->segname, "__DATA", 6) || !strncmp(sect->segname, "__DATA_CONST", 12)) return;
        if (!strncmp(sect->sectname, "__objc_", 7) || !strncmp(sect->sectname, "__swift", 7)) return;
        if (type == S_THREAD_LOCAL_REGULAR || type == S_THREAD_LOCAL_ZEROFILL || type == S_THREAD_LOCAL_VARIABLES) return;
        if (type == S_MOD_INIT_FUNC_POINTERS || type == S_INIT_FUNC_OFFSETS) return;
        f(addr, (size_t)sect->size, type == S_ZEROFILL || type == S_GB_ZEROFILL);
    });
}

static void tool_snapshot(TlImage *im) {
    if (im->fresh) return;
    __block size_t total = 1;
    tool_each_section(im, ^(uint8_t *addr, size_t size, int zero) { if (!zero) total += size; });
    uint8_t *copy = malloc(total);
    if (!copy) return;
    __block size_t at = 0;
    tool_each_section(im, ^(uint8_t *addr, size_t size, int zero) {
        if (zero) return;
        memcpy(copy + at, addr, size);
        at += size;
    });
    im->fresh = copy;
}

static void tool_restore(TlImage *im) {
    uint8_t *copy = im->fresh;
    if (!copy) return;
    __block size_t at = 0;
    tool_each_section(im, ^(uint8_t *addr, size_t size, int zero) {
        if (zero) { memset(addr, 0, size); return; }
        memcpy(addr, copy + at, size);
        at += size;
    });
    im->initialized = im->visiting = 0;
    /* Threads that outlive the run (dispatch's) start again with new thread-local variables. */
    if (im->tlv_size) {
        pthread_key_t key;
        if (!pthread_key_create(&key, free)) {
            pthread_key_t old = im->tlv_key;
            im->tlv_key = key;
            each_section(im, ^(const struct section_64 *sect, uint8_t *addr) {
                if ((sect->flags & SECTION_TYPE) != S_THREAD_LOCAL_VARIABLES) return;
                for (uint64_t k = 0; k < sect->size / sizeof(TlvDesc); k++) ((TlvDesc *)addr)[k].key = key;
            });
            pthread_key_delete(old);
        }
    }
}

/* ---- what a tool calls ---- */

static void *tool_malloc(size_t n) { return malloc_zone_malloc(g_tool.zone, n); }
static void *tool_calloc(size_t a, size_t b) { return malloc_zone_calloc(g_tool.zone, a, b); }
static void *tool_realloc(void *p, size_t n) { return p ? realloc(p, n) : tool_malloc(n); }
static void *tool_valloc(size_t n) { return malloc_zone_valloc(g_tool.zone, n); }
static void *tool_aligned(size_t a, size_t n) { return malloc_zone_memalign(g_tool.zone, a < 8 ? 8 : a, n ? n : 1); }
static int tool_posix_memalign(void **out, size_t a, size_t n) {
    void *p = tool_aligned(a, n);
    if (!p) return ENOMEM;
    *out = p;
    return 0;
}
static char *tool_strdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *p = tool_malloc(n);
    return p ? memcpy(p, s, n) : NULL;
}
static char *tool_strndup(const char *s, size_t max) {
    size_t n = strnlen(s, max);
    char *p = tool_malloc(n + 1);
    if (!p) return NULL;
    memcpy(p, s, n);
    p[n] = 0;
    return p;
}
static void *tool_new(size_t n) {
    void *p = tool_malloc(n ? n : 1);
    if (!p) abort();
    return p;
}
static void *tool_new_nothrow(size_t n, const void *t) { (void)t; return tool_malloc(n ? n : 1); }
static void *tool_new_aligned(size_t n, size_t a) {
    void *p = tool_aligned(a, n);
    if (!p) abort();
    return p;
}
static void *tool_new_aligned_nothrow(size_t n, size_t a, const void *t) { (void)t; return tool_aligned(a, n); }

static int tool_cxa_atexit(void (*fn)(void *), void *arg, void *dso) {
    (void)dso;
    pthread_mutex_lock(&g_tool_lock);
    if (g_tool.nexits == g_tool.capexits) {
        size_t cap = g_tool.capexits ? g_tool.capexits * 2 : 256;
        TlToolExit *e = realloc(g_tool.exits, cap * sizeof *e);
        if (e) g_tool.exits = e, g_tool.capexits = cap;
    }
    if (g_tool.nexits < g_tool.capexits) g_tool.exits[g_tool.nexits++] = (TlToolExit){fn, arg};
    pthread_mutex_unlock(&g_tool_lock);
    return 0;
}
static void tool_call_plain(void *fn) { ((void (*)(void))fn)(); }
static int tool_atexit(void (*fn)(void)) { return tool_cxa_atexit(tool_call_plain, (void *)fn, NULL); }
static void tool_tlv_atexit(void (*fn)(void *), void *arg) { (void)fn; (void)arg; } /* its memory goes with the zone */
static int tool_zero(void) { return 0; }
static void *tool_null(void) { return NULL; }

static void tool_done(TlToolRun *r, int code, int stuck) {
    pthread_mutex_lock(&g_tool_lock);
    if (!r->ended) r->ended = 1, r->code = code, r->stuck = stuck;
    pthread_cond_broadcast(&g_tool_cond);
    pthread_mutex_unlock(&g_tool_lock);
}
static void tool_run_exits(TlToolRun *r) {
    for (;;) {
        pthread_mutex_lock(&g_tool_lock);
        TlToolExit e = {0};
        if (r->nexits) e = r->exits[--r->nexits];
        pthread_mutex_unlock(&g_tool_lock);
        if (!e.fn) return;
        e.fn(e.arg);
    }
}
static void tool__exit(int code) {
    TlToolRun *r = &g_tool;
    if (r == &g_tool_none) _exit(code);
    if (pthread_equal(pthread_self(), r->thread)) {
        r->code = code;
        pthread_exit(NULL);
    }
    tool_done(r, code, 1);
    for (;;) pause();
}
static void tool_exit(int code) {
    TlToolRun *r = &g_tool;
    if (pthread_equal(pthread_self(), r->thread)) tool_run_exits(r);
    tool__exit(code);
}
static void tool_text_to(TlToolRun *r, int fd, const void *p, size_t n);
static void tool_abort(void) {
    static const char why[] = "the tool aborted\n";
    tool_text_to(&g_tool, 2, why, sizeof why - 1);
    tool__exit(134);
}

/* An exception nobody caught ends the program it was in, not the app. The system keeps one handler for all: the
   terminal's program sets its own (Bun's ends the app), which still gets what is not a tool's. */
int tl_tool_away; /* Henge is in the background: said with what ended a program */
void tl_tool_uncaught(char *out, size_t n, void **at, int *nat);
int tl_tool_guard(void (*fn)(void *), void *arg, char *out, size_t n);
void *tl_tool_set_terminate(void (*fn)(void));
static void (*volatile g_app_terminate)(void);
static int tool_call_stack_run(TlToolRun **out) {
    void *at[64];
    int n = backtrace(at, 64);
    for (int i = 0; i < n; i++) {
        TlImage *im = tl_image_containing(at[i]);
        TlToolRun *r = im && im->ns > 0 && im->ns < TL_MAX_NS ? g_tool_runs[im->ns] : NULL;
        if (r) { *out = r; return 1; }
    }
    return 0;
}
/* Whether the exception was thrown by the program itself, not inside a system library that may hold locks. */
static int tool_own_throw(void) {
    void *at[64];
    int n = backtrace(at, 64);
    for (int i = 0; i < n; i++) {
        if (tl_image_containing(at[i])) return 1;
        Dl_info d;
        if (!dladdr(at[i], &d) || !d.dli_fname) return 0;
        if (d.dli_fbase == (void *)&__dso_handle) continue;
        if (!strstr(d.dli_fname, "/libc++") && !strstr(d.dli_fname, "/libobjc.")) return 0;
    }
    return 0;
}
static void tool_say_uncaught(TlToolRun *r, const char *what) {
    char why[640];
    int n = snprintf(why, sizeof why, "terminated: an exception was not caught%s%s%s\n", what[0] ? ": " : "", what,
                     tl_tool_away ? " (Henge is in the background, where iOS gives it no GPU)" : "");
    if (n >= (int)sizeof why) n = sizeof why - 1;
    tl_log("%s: %.*s", r->exe->path, n - 1, why);
    tool_text_to(r, 2, why, (size_t)n);
}
/* Where the exception came from, as library+offset: what a report needs, without any path. */
static void tool_say_stack(TlToolRun *r, const char *what, void **at, int n) {
    char t[2400];
    int k = snprintf(t, sizeof t, "%s:", what);
    for (int i = 0; i < n && i < 40 && k < (int)sizeof t - 200; i++) {
        TlImage *im = tl_image_containing(at[i]);
        Dl_info d = {0};
        if (!im) dladdr(at[i], &d);
        const char *f = im ? im->path : d.dli_fname ? d.dli_fname : "?", *s = strrchr(f, '/');
        const void *base = im ? (const void *)im->base : d.dli_fbase;
        k += snprintf(t + k, sizeof t - k, " %.40s+0x%lx", s ? s + 1 : f, (unsigned long)((uintptr_t)at[i] - (uintptr_t)base));
        if (!im && d.dli_sname) k += snprintf(t + k, sizeof t - k, "(%.60s)", d.dli_sname);
    }
    tl_log("%s", t);
    tool_text_to(r, 2, t, (size_t)k);
    tool_text_to(r, 2, "\n", 1);
}
static void tool_terminate(void) {
    TlToolRun *r = pthread_getspecific(g_tool_key);
    if (!r && !tool_call_stack_run(&r)) { /* not a tool's */
        if (g_app_terminate) g_app_terminate();
        abort();
    }
    char what[400];
    void *thrown[40], *here[40];
    int nthrown = 40;
    tl_tool_uncaught(what, sizeof what, thrown, &nthrown);
    tool_say_uncaught(r, what);
    tool_say_stack(r, pthread_equal(pthread_self(), r->thread) ? "ended on its first thread" : pthread_main_np() ? "ended on the app's main thread" : "ended on another thread", here, backtrace(here, 40));
    if (nthrown > 0) tool_say_stack(r, "raised at", thrown, nthrown);
    if (pthread_main_np()) { /* the app's: it can neither stay here nor end (blocks on it are guarded, tool_as) */
        if (g_app_terminate) g_app_terminate();
        abort();
    }
    if (!r->keeps && pthread_equal(pthread_self(), r->thread) && tool_own_throw()) {
        r->code = 134;
        pthread_exit(NULL);
    }
    /* The thread stays where it is, for good: it may hold the system's locks (one whose owner is gone ends the app),
       and what it asked of the GPU may still call back. So this copy's memory is not used again either. */
    tool_done(r, 134, 1);
    for (;;) pause();
}
static void *rt_set_terminate(void (*fn)(void)) {
    pthread_once(&g_tool_once, tool_init);
    void *was = (void *)g_app_terminate;
    g_app_terminate = fn;
    return was;
}
static void tool_terminate_init(void) {
    void *was = tl_tool_set_terminate(tool_terminate);
    if (was != (void *)tool_terminate && !g_app_terminate) g_app_terminate = (void (*)(void))was;
}

/* Where a thread of the program waits, it ends when it is time to. */
static void tool__exit(int code);
static void tool_point(void) {
    int main = pthread_equal(pthread_self(), g_tool.thread);
    if (main && g_tool.kill && !g_tool.stop) tool__exit(137);
    if (!main && (g_tool.stop || g_tool.kill)) pthread_exit(NULL);
}
typedef struct TlToolStart { void *(*fn)(void *); void *arg; TlToolRun *run; } TlToolStart;
static void tool_thread_gone(void *x) { ((TlToolRun *)x)->live--; }
static void *tool_thread_start(void *x) {
    TlToolStart t = *(TlToolStart *)x;
    free(x);
    void *r;
    pthread_setspecific(g_tool_key, t.run);
    tool_enter_folder(t.run);
    pthread_cleanup_push(tool_thread_gone, t.run);
    r = t.fn(t.arg);
    pthread_cleanup_pop(1);
    return r;
}
static int tool_pthread_create(pthread_t *out, const pthread_attr_t *a, void *(*fn)(void *), void *arg) {
    TlToolStart *t = malloc(sizeof *t);
    if (!t) return EAGAIN;
    TlToolRun *r = &g_tool;
    *t = (TlToolStart){fn, arg, r};
    r->live++;
    pthread_attr_t big; /* the system's 512 KB for a thread that asks for nothing is little for translated code */
    int own = !a && !pthread_attr_init(&big);
    if (own) pthread_attr_setstacksize(&big, 4 << 20);
    int rc = pthread_create(out, own ? &big : a, tool_thread_start, t);
    if (own) pthread_attr_destroy(&big);
    if (rc) { r->live--; free(t); }
    return rc;
}
static int tool_nanosleep(const struct timespec *t, struct timespec *left) {
    tool_point();
    struct timespec now = *t;
    const struct timespec step = {0, 200000000};
    while (now.tv_sec > 0 || now.tv_nsec > step.tv_nsec) {
        nanosleep(&step, NULL);
        tool_point();
        now.tv_nsec -= step.tv_nsec;
        if (now.tv_nsec < 0) now.tv_sec--, now.tv_nsec += 1000000000;
        if (now.tv_sec < 0) return 0;
    }
    int rc = nanosleep(&now, left);
    tool_point();
    return rc;
}
static int tool_usleep(useconds_t us) { return tool_nanosleep(&(struct timespec){us / 1000000, (long)(us % 1000000) * 1000}, NULL); }
static void tool_sleep_for(const int64_t *ns) {
    if (*ns > 0) tool_nanosleep(&(struct timespec){*ns / 1000000000, (long)(*ns % 1000000000)}, NULL);
    else tool_point();
}
/* A wait without end returns now and then, as POSIX allows one to; the program waits again. */
static int tool_cond_wait(pthread_cond_t *c, pthread_mutex_t *m) {
    const struct timespec step = {0, 200000000};
    int rc = pthread_cond_timedwait_relative_np(c, m, &step);
    if (g_tool.stop || g_tool.kill) {
        pthread_mutex_unlock(m);
        tool_point();
        pthread_mutex_lock(m);
    }
    return rc == ETIMEDOUT ? 0 : rc;
}
static void tool_cv_wait(pthread_cond_t *c, pthread_mutex_t **lock) { tool_cond_wait(c, *lock); }
static int tool_cond_timedwait(pthread_cond_t *c, pthread_mutex_t *m, const struct timespec *t) {
    if (!g_tool.stop && !g_tool.kill) return pthread_cond_timedwait(c, m, t);
    pthread_mutex_unlock(m);
    tool_point();
    pthread_mutex_lock(m);
    return ETIMEDOUT;
}
static int tool_cond_timedwait_rel(pthread_cond_t *c, pthread_mutex_t *m, const struct timespec *t) {
    if (!g_tool.stop && !g_tool.kill) return pthread_cond_timedwait_relative_np(c, m, t);
    pthread_mutex_unlock(m);
    tool_point();
    pthread_mutex_lock(m);
    return ETIMEDOUT;
}
static uint64_t tool_absolute_time(void) { tool_point(); return mach_absolute_time(); }
static int tool_clock_gettime(clockid_t id, struct timespec *t) { tool_point(); return clock_gettime(id, t); }
static uint64_t tool_steady_now(void) { tool_point(); return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }
static int tool_readable(int fd) {
    for (;;) {
        tool_point();
        struct pollfd p = {fd, POLLIN, 0};
        int n = poll(&p, 1, 200);
        if (n > 0 || (n < 0 && errno != EINTR)) return 1;
        int fl = fcntl(fd, F_GETFL);
        if (fl >= 0 && fl & O_NONBLOCK) return 1;
    }
}
static int tool_accept(int fd, struct sockaddr *a, socklen_t *n) {
    tool_readable(fd);
    int c = accept(fd, a, n);
    tool_opened(&g_tool, c, 1);
    return c;
}
static ssize_t tool_recv(int fd, void *p, size_t n, int flags) {
    if (!(flags & MSG_DONTWAIT)) tool_readable(fd);
    return recv(fd, p, n, flags);
}
static int tool_socket(int d, int t, int p) {
    int fd = socket(d, t, p);
    tool_opened(&g_tool, fd, 1);
    return fd;
}
static int tool_select(int n, fd_set *r, fd_set *w, fd_set *e, struct timeval *t) {
    tool_point();
    int rc = select(n, r, w, e, t);
    tool_point();
    return rc;
}

void *tl_tool_stream(int ns, const char *sym); /* tl_cout.cpp */
void tl_tool_stream_reset(int ns);
static int tool_is_out(FILE *f) { return f == stdout || f == stderr; }
/* What fd means to the tool: a file of the app's, or -1 for the host (or, for input, nothing). */
static int tool_fd(TlToolRun *r, int fd) { return fd >= 0 && fd <= 2 ? r->std[fd] : fd; }
static void tool_text_to(TlToolRun *r, int fd, const void *p, size_t n) {
    int to = tool_fd(r, fd);
    if (to >= 0) {
        const char *c = p;
        while (n) {
            ssize_t k = write(to, c, n);
            if (k < 0 && errno == EINTR) continue;
            if (k < 0 && errno == EAGAIN) { usleep(1000); continue; }
            if (k <= 0) return;
            c += k, n -= (size_t)k;
        }
        return;
    }
    static pthread_mutex_t one = PTHREAD_MUTEX_INITIALIZER;
    pthread_mutex_lock(&one);
    if (r->output && n) r->output(r->context, p, n);
    pthread_mutex_unlock(&one);
}
#define tool_text(p, n) tool_text_to(&g_tool, 1, p, n)
static ssize_t tool_write(int fd, const void *p, size_t n) {
    TlToolRun *r = &g_tool;
    int to = tool_fd(r, fd);
    if (to >= 0) return write(to, p, n);
    tool_text_to(r, fd, p, n);
    return (ssize_t)n;
}
static ssize_t tool_writev(int fd, const struct iovec *v, int n) {
    TlToolRun *r = &g_tool;
    int to = tool_fd(r, fd);
    if (to >= 0) return writev(to, v, n);
    ssize_t all = 0;
    for (int i = 0; i < n; i++) tool_text_to(r, fd, v[i].iov_base, v[i].iov_len), all += (ssize_t)v[i].iov_len;
    return all;
}
static ssize_t tool_read(int fd, void *p, size_t n) {
    TlToolRun *r = &g_tool;
    int from = tool_fd(r, fd);
    return from < 0 && fd >= 0 && fd <= 2 ? 0 : read(from, p, n);
}
static int tool_fstat(int fd, struct stat *st) {
    TlToolRun *r = &g_tool;
    int to = tool_fd(r, fd);
    if (to >= 0 || fd < 0 || fd > 2) return fstat(to, st);
    memset(st, 0, sizeof *st);
    st->st_mode = S_IFIFO | 0600, st->st_nlink = 1, st->st_blksize = 4096;
    return 0;
}
void tl_tool_stream_write(int fd, const char *p, size_t n) { tool_text_to(&g_tool, fd, p, n); }
long tl_tool_stream_read(char *p, size_t n) { return (long)tool_read(0, p, n); }
static int tool_in_read_first(void *c, char *p, int n) { (void)c; return (int)tl_tool_stream_read(p, (size_t)n); }
static int tool_getchar(void) { char c; return tl_tool_stream_read(&c, 1) == 1 ? (unsigned char)c : EOF; }
static int tool_fileno(FILE *f) { return f == g_tool_in ? 0 : fileno(f); }
static int tool_isatty(int fd) {
    if (fd >= 0 && fd <= 2) { errno = ENOTTY; return 0; }
    return isatty(fd);
}
static off_t tool_lseek(int fd, off_t off, int how) {
    TlToolRun *r = &g_tool;
    int to = tool_fd(r, fd);
    if (to < 0 && fd >= 0 && fd <= 2) { errno = ESPIPE; return -1; }
    return lseek(to, off, how);
}
static int tool_ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (fd >= 0 && fd <= 2) { errno = ENOTTY; return -1; }
    return ioctl(fd, request, arg);
}
static size_t tool_fwrite(const void *p, size_t s, size_t n, FILE *f) {
    if (!tool_is_out(f)) return fwrite(p, s, n, f);
    tool_text_to(&g_tool, f == stderr ? 2 : 1, p, s * n);
    return n;
}
static int tool_fputs(const char *p, FILE *f) {
    if (!tool_is_out(f)) return fputs(p, f);
    tool_text_to(&g_tool, f == stderr ? 2 : 1, p, strlen(p));
    return 0;
}
static int tool_fputc(int c, FILE *f) {
    if (!tool_is_out(f)) return fputc(c, f);
    char b = (char)c;
    tool_text_to(&g_tool, f == stderr ? 2 : 1, &b, 1);
    return (unsigned char)c;
}
static int tool_putchar(int c) { return tool_fputc(c, stdout); }
static int tool_puts(const char *p) { tool_text(p, strlen(p)); tool_text("\n", 1); return 0; }
static int tool_vfprintf(FILE *f, const char *fmt, va_list ap) {
    if (!tool_is_out(f)) return vfprintf(f, fmt, ap);
    char *b = NULL;
    int n = vasprintf(&b, fmt, ap);
    if (n > 0) tool_text_to(&g_tool, f == stderr ? 2 : 1, b, (size_t)n);
    free(b);
    return n;
}
static int tool_vprintf(const char *fmt, va_list ap) { return tool_vfprintf(stdout, fmt, ap); }
static int tool_fprintf(FILE *f, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = tool_vfprintf(f, fmt, ap);
    va_end(ap);
    return n;
}
static int tool_printf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = tool_vfprintf(stdout, fmt, ap);
    va_end(ap);
    return n;
}

static void tool_keep_map(void *at, size_t len) {
    pthread_mutex_lock(&g_tool_lock);
    if (g_tool.nmaps == g_tool.capmaps) {
        size_t cap = g_tool.capmaps ? g_tool.capmaps * 2 : 1024;
        TlToolMap *m = realloc(g_tool.maps, cap * sizeof *m);
        if (m) g_tool.maps = m, g_tool.capmaps = cap;
    }
    if (g_tool.nmaps < g_tool.capmaps) g_tool.maps[g_tool.nmaps++] = (TlToolMap){at, len};
    pthread_mutex_unlock(&g_tool_lock);
}
/* A whole mapping let go is forgotten; part of one stays listed only for what is left of it. */
static void tool_forget_map(void *at, size_t len) {
    len = (len + TL_PAGE - 1) & ~(TL_PAGE - 1);
    uint8_t *a = at, *b = a + len;
    pthread_mutex_lock(&g_tool_lock);
    for (size_t i = 0; i < g_tool.nmaps; i++) {
        uint8_t *m = g_tool.maps[i].at, *e = m + g_tool.maps[i].len;
        if (b <= m || a >= e) continue;
        if (a <= m && b >= e) g_tool.maps[i--] = g_tool.maps[--g_tool.nmaps];
        else if (a <= m) g_tool.maps[i] = (TlToolMap){b, (size_t)(e - b)};
        else g_tool.maps[i].len = (size_t)(a - m); /* a hole: what follows it is left mapped */
    }
    pthread_mutex_unlock(&g_tool_lock);
}
static void *tool_mmap(void *at, size_t len, int prot, int flags, int fd, off_t off) {
    void *p = mmap(at, len, prot, flags, fd, off);
    if (p != MAP_FAILED) tool_keep_map(p, (len + TL_PAGE - 1) & ~(TL_PAGE - 1));
    return p;
}
static int tool_munmap(void *at, size_t len) {
    tool_forget_map(at, len);
    return munmap(at, len);
}
static kern_return_t tool_vm_allocate(vm_map_t task, vm_address_t *at, vm_size_t len, int flags) {
    kern_return_t kr = vm_allocate(task, at, len, flags);
    if (!kr) tool_keep_map((void *)*at, (len + TL_PAGE - 1) & ~(TL_PAGE - 1));
    return kr;
}
static kern_return_t tool_vm_deallocate(vm_map_t task, vm_address_t at, vm_size_t len) {
    tool_forget_map((void *)at, len);
    return vm_deallocate(task, at, len);
}
/* TL_TOOLCHAIN is the folder the tool's HENGE_TOOLS names. A compiler writes the paths it was given into its module
   cache and drops the cache when they change, and the app's folder changes with each update: under this name they do not.
   What it is told of a file it named so (realpath, F_GETPATH) has this name too. */
#define TL_TOOLCHAIN "/henge/toolchain"
static char **tool_env_find(TlToolRun *r, const char *name, size_t n);
static const char *tool_path(TlToolRun *r, const char *path, char *buf) {
    size_t n = sizeof TL_TOOLCHAIN - 1;
    if (!path || strncmp(path, TL_TOOLCHAIN, n) || (path[n] && path[n] != '/')) return path;
    char *real = r->alias;
    if (!real) {
        char **e = tool_env_find(r, "HENGE_TOOLS", 11), *none = NULL;
        if (!e || !(real = realpath(*e + 12, NULL))) return path;
        if (!atomic_compare_exchange_strong(&r->alias, &none, real)) free(real), real = none;
    }
    return (size_t)snprintf(buf, PATH_MAX, "%s%s", real, path + n) < PATH_MAX ? buf : path;
}
static const char *tool_path_of(const void *caller, const char *path, char *buf) {
    return path && !strncmp(path, TL_TOOLCHAIN, sizeof TL_TOOLCHAIN - 1) ? tool_path(tool_run_of(caller), path, buf) : path;
}
/* A real path in place, under TL_TOOLCHAIN if it lies there (never longer than a container's path). */
static char *tool_path_back(TlToolRun *r, char *path) {
    const char *real = r->alias;
    size_t n = real ? strlen(real) : 0, m = sizeof TL_TOOLCHAIN - 1;
    if (!path || n < m || strncmp(path, real, n) || (path[n] && path[n] != '/')) return path;
    memmove(path + m, path + n, strlen(path + n) + 1);
    memcpy(path, TL_TOOLCHAIN, m);
    return path;
}
#define TOOL_PATH(p) TlToolRun *r = &g_tool; char b1[PATH_MAX]; (p) = tool_path(r, (p), b1)
#define TOOL_PATHS(p, q) TOOL_PATH(p); char b2[PATH_MAX]; (q) = tool_path(r, (q), b2)
static int tool_open(const char *path, int flags, ...) {
    va_list ap;
    va_start(ap, flags);
    int mode = flags & O_CREAT ? va_arg(ap, int) : 0;
    va_end(ap);
    TOOL_PATH(path);
    int fd = open(path, flags, mode);
    tool_opened(r, fd, 1);
    return fd;
}
static int tool_fcntl(int fd, int cmd, ...) {
    va_list ap;
    va_start(ap, cmd);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    int rc = fcntl(fd, cmd, arg);
    if (!rc && cmd == F_GETPATH) tool_path_back(&g_tool, arg);
    return rc;
}
static int tool_access(const char *p, int m) { TOOL_PATH(p); return access(p, m); }
static int tool_stat(const char *p, struct stat *st) { TOOL_PATH(p); return stat(p, st); }
static int tool_lstat(const char *p, struct stat *st) { TOOL_PATH(p); return lstat(p, st); }
static int tool_statfs(const char *p, struct statfs *st) { TOOL_PATH(p); return statfs(p, st); }
static int tool_mkdir(const char *p, mode_t m) { TOOL_PATH(p); return mkdir(p, m); }
static int tool_unlink(const char *p) { TOOL_PATH(p); return unlink(p); }
static int tool_remove(const char *p) { TOOL_PATH(p); return remove(p); }
static DIR *tool_opendir(const char *p) { TOOL_PATH(p); return opendir(p); }
static ssize_t tool_readlink(const char *p, char *to, size_t n) { TOOL_PATH(p); return readlink(p, to, n); }
static int tool_rename(const char *p, const char *q) { TOOL_PATHS(p, q); return rename(p, q); }
static int tool_link(const char *p, const char *q) { TOOL_PATHS(p, q); return link(p, q); }
static int tool_symlink(const char *p, const char *q) { TOOL_PATHS(p, q); return symlink(p, q); }
static int tool_clonefile(const char *p, const char *q, uint32_t f) { TOOL_PATHS(p, q); return clonefile(p, q, f); }
static int tool_copyfile(const char *p, const char *q, copyfile_state_t st, copyfile_flags_t f) { TOOL_PATHS(p, q); return copyfile(p, q, st, f); }
static char *tool_realpath(const char *p, char *to) {
    const char *given = p;
    TOOL_PATH(p);
    char *out = realpath(p, to);
    return p != given ? tool_path_back(r, out) : out;
}
static int tool_close(int fd) {
    if (fd >= 0 && fd <= 2) return 0;
    if (fd >= 0 && fd < 1024) g_tool.fds[fd] = 0;
    return close(fd);
}
static int tool_key_create(pthread_key_t *key, void (*dtor)(void *)) {
    (void)dtor;
    int rc = pthread_key_create(key, NULL);
    pthread_mutex_lock(&g_tool_lock);
    if (!rc && g_tool.nkeys < 128) g_tool.keys[g_tool.nkeys++] = *key;
    pthread_mutex_unlock(&g_tool_lock);
    return rc;
}

/* ---- a tool's environment, folder and files ---- */

static char **tool_env_find(TlToolRun *r, const char *name, size_t n) {
    for (char **e = r->env; e && *e; e++)
        if (!strncmp(*e, name, n) && (*e)[n] == '=') return e;
    return NULL;
}
static char *tool_getenv(const char *name) {
    TlToolRun *r = &g_tool;
    if (!r->env) return getenv(name);
    char **e = tool_env_find(r, name, strlen(name));
    return e ? *e + strlen(name) + 1 : NULL;
}
/* Old strings stay: the tool may hold them. */
static void tool_env_put(TlToolRun *r, char *pair, size_t n) {
    char **e = tool_env_find(r, pair, n);
    if (e) { *e = pair; return; }
    size_t k = 0;
    while (r->env && r->env[k]) k++;
    char **grown = realloc(r->env, (k + 2) * sizeof *grown);
    if (!grown) return;
    grown[k] = pair, grown[k + 1] = NULL;
    r->env = grown;
}
static int tool_setenv(const char *name, const char *value, int over) {
    TlToolRun *r = &g_tool;
    size_t n = strlen(name);
    if (!over && tool_env_find(r, name, n)) return 0;
    char *pair = NULL;
    if (asprintf(&pair, "%s=%s", name, value) < 0) return -1;
    tool_env_put(r, pair, n);
    return 0;
}
static int tool_putenv(char *pair) {
    char *eq = strchr(pair, '=');
    if (eq) tool_env_put(&g_tool, strdup(pair), (size_t)(eq - pair));
    return 0;
}
static int tool_unsetenv(const char *name) {
    TlToolRun *r = &g_tool;
    char **e = tool_env_find(r, name, strlen(name));
    for (; e && *e; e++) *e = e[1];
    return 0;
}
static char ***tool_NSGetEnviron(void) { TlToolRun *r = &g_tool; return &r->env; }
static int *tool_NSGetArgc(void) { TlToolRun *r = &g_tool; return &r->argc; }
static char ***tool_NSGetArgv(void) { TlToolRun *r = &g_tool; return (char ***)&r->argv; }

static int tool_chdir(const char *path) {
    TOOL_PATH(path);
    if (!g_thread_chdir) return 0;
    int rc = g_thread_chdir(path);
    char now[PATH_MAX];
    if (!rc && getcwd(now, sizeof now)) r->cwd = strdup(now); /* for threads it starts; a few bytes are left */
    return rc;
}
static int tool_fchdir(int fd) {
    TlToolRun *r = &g_tool;
    if (!g_thread_fchdir) return 0;
    int rc = g_thread_fchdir(fd);
    char now[PATH_MAX];
    if (!rc && getcwd(now, sizeof now)) r->cwd = strdup(now);
    return rc;
}
static int tool_dup2(int from, int to) {
    TlToolRun *r = &g_tool;
    if (to < 0 || to > 2) return dup2(tool_fd(r, from), to);
    int f = tool_fd(r, from);
    r->std[to] = f >= 0 ? dup(f) : -1;
    tool_opened(r, r->std[to], 1);
    return to;
}
static int tool_dup(int fd) {
    TlToolRun *r = &g_tool;
    int f = tool_fd(r, fd);
    int n = f >= 0 ? dup(f) : open("/dev/null", O_RDWR);
    tool_opened(r, n, 1);
    return n;
}
static int tool_sigaction(int sig, const struct sigaction *act, struct sigaction *old) {
    TlToolRun *r = &g_tool;
    if (old) {
        memset(old, 0, sizeof *old);
        if (sig == SIGCHLD) *old = r->sigchld;
    }
    if (act && sig == SIGCHLD) r->sigchld = *act;
    return 0;
}
static int tool_no_fork(void) { errno = ENOSYS; return -1; }

/* Blocks a tool hands to the system's threads run as its own. */
static void tool_say_uncaught(TlToolRun *r, const char *what);
static void tool_call_block(void *b) { ((dispatch_block_t)b)(); }
static void tool_as(TlToolRun *r, void (^block)(void)) {
    void *was = pthread_getspecific(g_tool_key);
    pthread_setspecific(g_tool_key, r);
    int moved = !was && r->cwd && g_thread_chdir;
    if (moved) g_thread_chdir(r->cwd);
    char what[400];
    if (!pthread_main_np()) block();
    else if (tl_tool_guard(tool_call_block, (void *)block, what, sizeof what)) { /* the app's main thread goes on */
        tool_say_uncaught(r, what);
        tool_done(r, 134, 1);
    }
    if (moved && g_thread_fchdir) g_thread_fchdir(-1); /* back to the app's folder */
    pthread_setspecific(g_tool_key, was);
}
static void tool_dispatch_apply(size_t n, dispatch_queue_t q, void (^block)(size_t)) {
    TlToolRun *r = &g_tool;
    dispatch_apply(n, q, ^(size_t i) { tool_as(r, ^{ block(i); }); });
}
static void tool_dispatch_async(dispatch_queue_t q, dispatch_block_t block) {
    TlToolRun *r = &g_tool;
    dispatch_async(q, ^{ tool_as(r, block); });
}
static void tool_dispatch_group_async(dispatch_group_t g, dispatch_queue_t q, dispatch_block_t block) {
    TlToolRun *r = &g_tool;
    dispatch_group_async(g, q, ^{ tool_as(r, block); });
}
static void tool_dispatch_sync(dispatch_queue_t q, dispatch_block_t block) {
    TlToolRun *r = &g_tool;
    dispatch_sync(q, ^{ tool_as(r, block); });
}
static void tool_dispatch_barrier_async(dispatch_queue_t q, dispatch_block_t block) {
    TlToolRun *r = &g_tool;
    dispatch_barrier_async(q, ^{ tool_as(r, block); });
}

/* ---- tools that start tools ---- */

typedef struct TlToolAction { int kind, fd, from, flags, mode; char *path; struct TlToolAction *next; } TlToolAction;
enum { TOOL_DUP2, TOOL_OPEN, TOOL_CHDIR, TOOL_CLOSE };
static int tool_fa_init(void **fa) { *fa = NULL; return 0; }
static int tool_fa_destroy(void **fa) {
    for (TlToolAction *x = *fa, *n; x; x = n) n = x->next, free(x->path), free(x);
    *fa = NULL;
    return 0;
}
static int tool_fa_add(void **fa, TlToolAction what) {
    TlToolAction *x = malloc(sizeof *x);
    if (!x) return ENOMEM;
    *x = what;
    x->path = what.path ? strdup(what.path) : NULL;
    TlToolAction **at = (TlToolAction **)fa;
    while (*at) at = &(*at)->next;
    *at = x;
    return 0;
}
static int tool_fa_dup2(void **fa, int from, int to) { return tool_fa_add(fa, (TlToolAction){.kind = TOOL_DUP2, .fd = to, .from = from}); }
static int tool_fa_open(void **fa, int fd, const char *path, int flags, mode_t mode) {
    return tool_fa_add(fa, (TlToolAction){.kind = TOOL_OPEN, .fd = fd, .flags = flags, .mode = mode, .path = (char *)path});
}
static int tool_fa_close(void **fa, int fd) { return tool_fa_add(fa, (TlToolAction){.kind = TOOL_CLOSE, .fd = fd}); }
static int tool_fa_inherit(void **fa, int fd) { (void)fa; (void)fd; return 0; }
static int tool_fa_chdir(void **fa, const char *path) { return tool_fa_add(fa, (TlToolAction){.kind = TOOL_CHDIR, .path = (char *)path}); }

typedef struct TlToolChild {
    pid_t pid;
    int done, status;
    TlToolRun *parent;
    _Atomic(TlToolRun *) run;
    _Atomic int kill;
    char *path, *cwd, **argv, **env;
    int argc, std[3];
    void (*ended)(void *, int); /* for the host's own program (tl_tool_spawn): told its status; nobody waits for it here */
    void *context;
    int held[32], nheld; /* its parent's pipes that it would inherit: the other ends see when it has ended */
    struct TlToolChild *next;
} TlToolChild;
static TlToolChild *g_tool_children;
static pid_t g_tool_next_pid = 400001;
static int tool_run_child(TlToolChild *c);


static void *tool_child_thread(void *x) {
    TlToolChild *c = x;
    pthread_setname_np("tool child");
    int code = tool_run_child(c);
    for (int i = 0; i < 3; i++)
        if (c->std[i] >= 0) close(c->std[i]);
    for (int i = 0; i < c->nheld; i++) close(c->held[i]);
    pthread_mutex_lock(&g_tool_lock);
    c->status = c->kill ? SIGKILL : W_EXITCODE(code & 0xFF, 0);
    c->run = NULL;
    c->done = 1;
    /* The parent may have ended; its run is then no longer listed. */
    void (*handler)(int) = NULL;
    for (int i = 0; i < TL_MAX_NS; i++)
        if (g_tool_runs[i] && g_tool_runs[i] == c->parent && !c->parent->ended && (uintptr_t)c->parent->sigchld.sa_handler > 1) handler = c->parent->sigchld.sa_handler, c->parent->chld++;
    if (c->ended)
        for (TlToolChild **at = &g_tool_children; *at; at = &(*at)->next)
            if (*at == c) { *at = c->next; break; }
    pthread_cond_broadcast(&g_tool_cond);
    pthread_mutex_unlock(&g_tool_lock);
    if (handler) handler(SIGCHLD);
    if (c->ended) {
        c->ended(c->context, c->status);
        free(c->path), free(c->cwd);
        free(c);
    }
    return NULL;
}

/* Where a program a tool names is: the path, or the name in the host's folders of tools. */
static int tool_find(const char *name, const char *cwd, char *out, size_t len) {
    const char *base = strrchr(name, '/') ? strrchr(name, '/') + 1 : name;
    if (strchr(name, '/')) {
        if (name[0] == '/' || !cwd) snprintf(out, len, "%s", name);
        else snprintf(out, len, "%s/%s", cwd, name);
        uint32_t magic = 0;
        int fd = open(out, O_RDONLY | O_CLOEXEC);
        if (fd >= 0) {
            ssize_t k = read(fd, &magic, 4);
            close(fd);
            if (k == 4 && (magic == MH_MAGIC_64 || magic == FAT_CIGAM) && strncmp(out, "/usr/bin/", 9) && strncmp(out, "/bin/", 5)) return 1;
        }
    }
    static const char *const also[][2] = {{"cc", "clang"}, {"c++", "clang"}, {"gcc", "clang"}, {"g++", "clang"}, {"clang++", "clang"}, {"bash", "sh"},
                                          {"zsh", "sh"}, {"dash", "sh"}, {"ar", "sh"}, {"ranlib", "libtool"}, {"llvm-ar", "sh"}, {"python", "python3"}, {"nm", "llvm-nm"}, {"objdump", "llvm-objdump"}};
    for (size_t i = 0; i < sizeof also / sizeof *also; i++)
        if (!strcmp(base, also[i][0])) { base = also[i][1]; break; }
    if (!strncmp(base, "python3.", 8)) base = "python3";
    static const char *const in_shell[] = {"uname", "echo", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "ls", "ln", "touch", "pwd", "true", "false", "env",
                                           "sleep", "test", "head", "tail", "wc", "chmod", "printf", "basename", "dirname", "which"};
    char names[PATH_MAX];
    for (size_t i = 0; i < sizeof in_shell / sizeof *in_shell; i++)
        if (!strcmp(base, in_shell[i])) { base = "sh"; break; }
    for (char *const *d = g_host->tool_folders; d && *d; d++) {
        snprintf(out, len, "%s/%s", *d, base);
        if (file_exists(out)) return 1;
    }
    /* Those devtools.py answers (grep, sed, make): the shell starts it for such a name. */
    for (char *const *d = g_host->tool_folders; d && *d; d++) {
        snprintf(names, sizeof names, "%s/devtools.names", *d);
        FILE *f = fopen(names, "r");
        char line[64];
        int is = 0;
        while (f && !is && fgets(line, sizeof line, f)) { line[strcspn(line, "\r\n")] = 0; is = !strcmp(line, base); }
        if (f) fclose(f);
        snprintf(out, len, "%s/sh", *d);
        if (is && file_exists(out)) return 1;
    }
    return 0;
}

static int tool_spawn_for(TlToolRun *r, pid_t *pid, const char *path, void *const *fa, char *const argv[], char *const envp[],
                          void (*ended)(void *, int), void *context) {
    TlToolChild *c = calloc(1, sizeof *c);
    if (!c) return ENOMEM;
    c->ended = ended, c->context = context;
    char found[PATH_MAX], here[PATH_MAX];
    c->parent = r;
    c->cwd = getcwd(here, sizeof here) ? strdup(here) : NULL;
    c->env = copy_strings(envp && envp != environ ? envp : r->env, NULL); /* a tool that names environ itself has the app's */
    c->argv = copy_strings(argv, &c->argc);
    for (int i = 0; i < 3; i++) c->std[i] = ended ? dup(i) : r->std[i] >= 0 ? dup(r->std[i]) : -1;
    for (TlToolAction *x = fa ? *fa : NULL; x; x = x->next) {
        if (x->kind == TOOL_CHDIR) { free(c->cwd); c->cwd = strdup(x->path); continue; }
        if (x->fd < 0 || x->fd > 2 || x->kind == TOOL_CLOSE) continue;
        int now;
        if (x->kind == TOOL_OPEN) now = open(x->path, x->flags, x->mode);
        else if (x->from >= 0 && x->from <= 2) now = c->std[x->from] >= 0 ? dup(c->std[x->from]) : -1;
        else now = dup(x->from);
        if (c->std[x->fd] >= 0) close(c->std[x->fd]);
        c->std[x->fd] = now;
    }
    /* What a patched subprocess module says (it cannot through posix_spawn): the folder. */
    for (char **e = c->env; e && *e; e++)
        if (!strncmp(*e, "TL_SPAWN_CWD=", 13)) {
            if ((*e)[13]) free(c->cwd), c->cwd = strdup(*e + 13);
            for (; *e; e++) *e = e[1]; /* not for what it starts in turn */
            break;
        }
    char from[PATH_MAX];
    if (!tool_find(path, getcwd(from, sizeof from), found, sizeof found)) {
        for (int i = 0; i < 3; i++) if (c->std[i] >= 0) close(c->std[i]);
        tl_log("a tool asked for %s, which is not among the tools here", path);
        return ENOENT;
    }
    for (int fd = 3; fd < 1024 && c->nheld < 32; fd++) {
        if (r->fds[fd] != 2 || fcntl(fd, F_GETFD) & FD_CLOEXEC) continue;
        int closed = 0;
        for (TlToolAction *x = fa ? *fa : NULL; x; x = x->next) closed |= x->kind == TOOL_CLOSE && x->fd == fd;
        if (!closed) c->held[c->nheld++] = dup(fd);
    }
    c->path = strdup(found);
    pthread_mutex_lock(&g_tool_lock);
    c->pid = g_tool_next_pid++;
    c->next = g_tool_children, g_tool_children = c;
    pthread_mutex_unlock(&g_tool_lock);
    if (pid) *pid = c->pid;
    pthread_t t;
    if (pthread_create(&t, NULL, tool_child_thread, c)) {
        c->done = 1, c->status = W_EXITCODE(127, 0);
        if (ended) ended(context, c->status);
        return 0;
    }
    pthread_detach(t);
    return 0;
}
static int tool_posix_spawn(pid_t *pid, const char *path, void *const *fa, const void *attr, char *const argv[], char *const envp[]) {
    (void)attr;
    return tool_spawn_for(&g_tool, pid, path, fa, argv, envp, NULL, NULL);
}
int tl_tool_spawn(pid_t *pid, const char *path, void *const *fa, char *const argv[], char *const envp[], void (*ended)(void *, int), void *context) {
    pthread_once(&g_tool_once, tool_init);
    g_tool_none.std[0] = 0, g_tool_none.std[1] = 1, g_tool_none.std[2] = 2;
    return tool_spawn_for(&g_tool_none, pid, path, fa, argv, envp, ended, context);
}
int tl_tool_exists(const char *name) {
    char found[PATH_MAX];
    return g_host && tool_find(name, NULL, found, sizeof found);
}
/* What the file actions of a program that starts tools are kept in: its posix_spawn_file_actions_t is a pointer. */
static int tool_fa_fchdir(void **fa, int fd) {
    char path[PATH_MAX];
    return fcntl(fd, F_GETPATH, path) ? errno : tool_fa_chdir(fa, path);
}
void *tl_tool_file_action(const char *symbol) {
    static const struct { const char *name; void *fn; } table[] = {
        {"init", (void *)tool_fa_init}, {"destroy", (void *)tool_fa_destroy}, {"adddup2", (void *)tool_fa_dup2}, {"addopen", (void *)tool_fa_open},
        {"addclose", (void *)tool_fa_close}, {"addinherit_np", (void *)tool_fa_inherit}, {"addchdir_np", (void *)tool_fa_chdir},
        {"addchdir", (void *)tool_fa_chdir}, {"addfchdir", (void *)tool_fa_fchdir}, {"addfchdir_np", (void *)tool_fa_fchdir},
    };
    static const char prefix[] = "_posix_spawn_file_actions_";
    if (strncmp(symbol, prefix, sizeof prefix - 1)) return NULL;
    for (size_t i = 0; i < sizeof table / sizeof *table; i++)
        if (!strcmp(symbol + sizeof prefix - 1, table[i].name)) return table[i].fn;
    return NULL;
}

static pid_t tool_waitpid(pid_t pid, int *status, int options) {
    TlToolRun *r = &g_tool;
    pthread_mutex_lock(&g_tool_lock);
    for (;;) {
        int any = 0;
        for (TlToolChild **at = &g_tool_children; *at; at = &(*at)->next) {
            TlToolChild *c = *at;
            if (c->parent != r || (pid > 0 && c->pid != pid)) continue;
            any = 1;
            if (!c->done) continue;
            *at = c->next;
            if (status) *status = c->status;
            pid_t got = c->pid;
            pthread_mutex_unlock(&g_tool_lock);
            free(c->path), free(c->cwd);
            free(c); /* its argv and env stay: what it ran may still hold them */
            return got;
        }
        if (!any) { pthread_mutex_unlock(&g_tool_lock); errno = ECHILD; return -1; }
        if (options & WNOHANG) { pthread_mutex_unlock(&g_tool_lock); return 0; }
        const struct timespec step = {0, 200000000};
        pthread_cond_timedwait_relative_np(&g_tool_cond, &g_tool_lock, &step);
        if (r->kill || r->stop) {
            pthread_mutex_unlock(&g_tool_lock);
            tool_point();
            pthread_mutex_lock(&g_tool_lock);
        }
    }
}
static pid_t tool_wait4(pid_t pid, int *status, int options, struct rusage *ru) {
    if (ru) memset(ru, 0, sizeof *ru);
    pthread_setspecific(g_tool_key, &g_tool);
    return tool_waitpid(pid, status, options);
}
static void tool_kill_run(TlToolRun *r);
static int tool_kill(pid_t pid, int sig) {
    if (sig <= 0) return 0;
    int rc = -1;
    pthread_mutex_lock(&g_tool_lock);
    for (TlToolChild *c = g_tool_children; c; c = c->next)
        if (c->pid == pid) {
            c->kill = 1;
            if (c->run) c->run->kill = 1;
            rc = 0;
        }
    pthread_mutex_unlock(&g_tool_lock);
    if (rc) errno = ESRCH;
    return rc;
}

/* It and what it started: a signal to its group. */
static void tool_kill_tree(TlToolChild *c, int depth) {
    c->kill = 1;
    TlToolRun *r = c->run;
    if (!r) return;
    r->kill = 1;
    for (TlToolChild *x = g_tool_children; x && depth < 16; x = x->next)
        if (x->parent == r && !x->done) tool_kill_tree(x, depth + 1);
}
int tl_tool_kill(pid_t pid) {
    int found = 0;
    pthread_mutex_lock(&g_tool_lock);
    for (TlToolChild *c = g_tool_children; c; c = c->next)
        if (c->pid == pid) tool_kill_tree(c, 0), found = 1;
    pthread_mutex_unlock(&g_tool_lock);
    return found;
}

/* A child's end interrupts it, as SIGCHLD would. */
static int tool_pselect(int n, fd_set *rd, fd_set *wr, fd_set *ex, const struct timespec *t, const sigset_t *mask) {
    TlToolRun *r = &g_tool;
    (void)mask;
    fd_set in[3];
    if (rd) in[0] = *rd;
    if (wr) in[1] = *wr;
    if (ex) in[2] = *ex;
    long long left = t ? t->tv_sec * 1000000000ll + t->tv_nsec : -1;
    for (;;) {
        tool_point();
        if (r->chld != r->chld_seen) { r->chld_seen = r->chld; errno = EINTR; return -1; }
        long long step = left < 0 || left > 20000000 ? 20000000 : left;
        const struct timespec s = {0, step};
        int k = pselect(n, rd, wr, ex, &s, NULL);
        if (k || !step || (left >= 0 && (left -= step) <= 0)) return k;
        if (rd) *rd = in[0];
        if (wr) *wr = in[1];
        if (ex) *ex = in[2];
    }
}
/* What ends a process from inside the system's library. */
static void tool_verr(int code, int with, const char *f, va_list a) {
    int e = errno;
    char b[1024], c[1400];
    vsnprintf(b, sizeof b, f ? f : "", a);
    int n = with ? snprintf(c, sizeof c, "%s: %s\n", b, strerror(e)) : snprintf(c, sizeof c, "%s\n", b);
    tool_text_to(&g_tool, 2, c, (size_t)(n < (int)sizeof c ? n : (int)sizeof c - 1));
    if (code >= 0) tool_exit(code);
}
static void tool_err(int code, const char *f, ...) { va_list a; va_start(a, f); tool_verr(code, 1, f, a); va_end(a); }
static void tool_errx(int code, const char *f, ...) { va_list a; va_start(a, f); tool_verr(code, 0, f, a); va_end(a); }
static void tool_perror(const char *what) {
    char c[1024];
    int n = snprintf(c, sizeof c, "%s: %s\n", what ? what : "", strerror(errno));
    tool_text_to(&g_tool, 2, c, (size_t)(n < (int)sizeof c ? n : (int)sizeof c - 1));
}
static void tool_warn(const char *f, ...) { va_list a; va_start(a, f); tool_verr(-1, 1, f, a); va_end(a); }
static void tool_warnx(const char *f, ...) { va_list a; va_start(a, f); tool_verr(-1, 0, f, a); va_end(a); }
static void tool_assert(const char *fn, const char *file, int line, const char *what) {
    char c[1024];
    int n = snprintf(c, sizeof c, "Assertion failed: (%s), function %s, file %s, line %d.\n", what, fn ? fn : "?", file, line);
    tool_text_to(&g_tool, 2, c, (size_t)(n < (int)sizeof c ? n : (int)sizeof c - 1));
    tool_abort();
}
static int tool_system(const char *command);
static FILE *tool_popen(const char *command, const char *mode);
static int tool_pclose(FILE *f);
/* Swift's main actor for tools: the app's main thread is not theirs (it draws, and may not end), and the runtime's own way
   to wait for its jobs would end the app. Where a tool's code names the main executor it gets the host's executor in its
   place (tl_tool_main_executor), whose jobs its first thread runs while it waits (main with await). */
typedef struct TlToolJob { void *job; struct TlToolJob *next; } TlToolJob;
typedef struct { void *identity, *witness; } TlSwiftExecutor;
static void (*g_swift_job_run)(void *, TlSwiftExecutor);
__attribute__((used)) static void *g_swift_main, *g_swift_switch; /* the app's main executor; the runtime's swift_task_switch */
__attribute__((used)) static TlSwiftExecutor g_swift_ours;
void tl_tool_main_executor(void *identity, void *witness) { g_swift_ours = (TlSwiftExecutor){identity, witness}; }
void tl_tool_main_job(void *job) {
    /* Where a job says its kind and what it runs, and a task the contexts it returns to: part of Swift's ABI. */
    enum { JOB_FLAGS = 0x20, JOB_CODE = 0x38, TASK_CONTEXT = 0x40 };
    TlImage *im = tl_image_containing(*(void **)((char *)job + JOB_CODE));
    void **ctx = *(uint8_t *)((char *)job + JOB_FLAGS) == 0 ? *(void ***)((char *)job + TASK_CONTEXT) : NULL;
    for (int i = 0; !im && ctx && i < 64; i++, ctx = ctx[0]) im = tl_image_containing(ctx[1]); /* in the runtime's code, on a tool's behalf */
    TlToolRun *r = NULL;
    TlToolJob *j = calloc(1, sizeof *j);
    j->job = job;
    pthread_mutex_lock(&g_tool_lock);
    if (im && im->ns > 0 && im->ns < TL_MAX_NS) r = g_tool_runs[im->ns];
    TlToolRun *own = pthread_getspecific(g_tool_key);
    if (!r) r = own;
    if (r) {
        TlToolJob **at = &r->jobs;
        while (*at) at = &(*at)->next;
        *at = j;
        pthread_cond_broadcast(&g_tool_cond);
    } else
        free(j); /* its program has ended */
    pthread_mutex_unlock(&g_tool_lock);
}
static void tool_swift_drain(void) {
    TlToolRun *r = &g_tool;
    for (;;) {
        pthread_mutex_lock(&g_tool_lock);
        TlToolJob *j = r->jobs;
        if (j) r->jobs = j->next;
        else {
            const struct timespec step = {0, 50000000};
            pthread_cond_timedwait_relative_np(&g_tool_cond, &g_tool_lock, &step);
        }
        pthread_mutex_unlock(&g_tool_lock);
        tool_point();
        if (!j) continue;
        void *job = j->job;
        free(j);
        g_swift_job_run(job, g_swift_ours);
    }
}
static TlSwiftExecutor tool_swift_main(void) { return g_swift_ours; }
/* Its arguments: x0 what goes on, x1 and x2 the executor, x22 the async context. */
__attribute__((naked)) static void tool_swift_switch(void) {
    __asm__("adrp x16, _g_swift_main@PAGE\n ldr x16, [x16, _g_swift_main@PAGEOFF]\n cmp x1, x16\n b.ne 1f\n cbz x1, 1f\n"
            "adrp x16, _g_swift_ours@PAGE\n add x16, x16, _g_swift_ours@PAGEOFF\n ldp x1, x2, [x16]\n"
            "1: adrp x16, _g_swift_switch@PAGE\n ldr x16, [x16, _g_swift_switch@PAGEOFF]\n br x16\n");
}
static _Bool (*g_swift_current)(TlSwiftExecutor);
static _Bool (*g_swift_current_flags)(TlSwiftExecutor, uintptr_t);
static TlSwiftExecutor tool_swift_mine(TlSwiftExecutor e) { return e.identity && e.identity == g_swift_main ? g_swift_ours : e; }
static _Bool tool_swift_current(TlSwiftExecutor e) { return g_swift_current(tool_swift_mine(e)); }
static _Bool tool_swift_current_flags(TlSwiftExecutor e, uintptr_t f) { return g_swift_current_flags(tool_swift_mine(e), f); }
/* A task may be made to start on an executor, named in its options: {flags, next, executor}. */
typedef struct { void *task, *context; } TlSwiftTask;
static TlSwiftTask (*g_swift_create)(size_t, void **, void *, void *, void *);
static TlSwiftTask tool_swift_create(size_t flags, void **options, void *type, void *fn, void *ctx) {
    for (void **o = options; o; o = o[1])
        if (g_swift_main && o[2] == g_swift_main) o[2] = g_swift_ours.identity, o[3] = g_swift_ours.witness;
    return g_swift_create(flags, options, type, fn, ctx);
}
/* MainActor as an Actor: what the runtime's own code (task groups) asks for the executor of an actor it is given. */
static const void *g_swift_actor, *g_swift_actor_table[2];
static const void *(*g_swift_table)(const void *, const void *, const void *);
static const void *tool_swift_table(const void *conformance, const void *type, const void *args) {
    return conformance == g_swift_actor ? g_swift_actor_table : g_swift_table(conformance, type, args);
}
static void *tool_swift_symbol(const char *sym) {
    static const char *const names[] = {"_swift_task_asyncMainDrainQueue", "_swift_task_getMainExecutor", "_swift_task_switch", "_swift_task_isCurrentExecutor",
                                        "_swift_task_isCurrentExecutorWithFlags", "_swift_task_create", "_swift_getWitnessTable", "_$sScMScAsWP"};
    static void *const with[] = {(void *)tool_swift_drain, (void *)tool_swift_main, (void *)tool_swift_switch, (void *)tool_swift_current,
                                 (void *)tool_swift_current_flags, (void *)tool_swift_create, (void *)tool_swift_table, (void *)g_swift_actor_table};
    if ((strncmp(sym, "_swift_", 7) && strncmp(sym, "_$sScM", 6)) || !g_swift_ours.identity) return NULL;
    for (size_t i = 0; i < sizeof names / sizeof *names; i++) {
        if (strcmp(sym, names[i])) continue;
        TlSwiftExecutor (*real)(void) = (TlSwiftExecutor (*)(void))dlsym(RTLD_DEFAULT, "swift_task_getMainExecutor");
        g_swift_job_run = (void (*)(void *, TlSwiftExecutor))dlsym(RTLD_DEFAULT, "swift_job_run");
        g_swift_switch = dlsym(RTLD_DEFAULT, "swift_task_switch");
        g_swift_current = (_Bool (*)(TlSwiftExecutor))dlsym(RTLD_DEFAULT, "swift_task_isCurrentExecutor");
        g_swift_current_flags = (_Bool (*)(TlSwiftExecutor, uintptr_t))dlsym(RTLD_DEFAULT, "swift_task_isCurrentExecutorWithFlags");
        g_swift_create = (TlSwiftTask (*)(size_t, void **, void *, void *, void *))dlsym(RTLD_DEFAULT, "swift_task_create");
        if (!real || !g_swift_job_run || !g_swift_switch || !g_swift_create) return NULL;
        g_swift_table = (const void *(*)(const void *, const void *, const void *))dlsym(RTLD_DEFAULT, "swift_getWitnessTable");
        g_swift_actor = dlsym(RTLD_DEFAULT, "$sScMScAsMc");
        if (!g_swift_table || !g_swift_actor) return NULL;
        g_swift_actor_table[0] = g_swift_actor, g_swift_actor_table[1] = (const void *)tool_swift_main;
        g_swift_main = real().identity;
        return dlsym(RTLD_DEFAULT, sym + 1) ? with[i] : NULL;
    }
    return NULL;
}
/* Symbols the host answers for tools (Swift's print, whose own writes to the app's output). */
static struct { const char *name; void *fn; } g_tool_answers[8];
void tl_tool_answer(const char *symbol, void *fn) {
    for (int i = 0; i < 8; i++)
        if (!g_tool_answers[i].name || !strcmp(g_tool_answers[i].name, symbol)) {
            g_tool_answers[i].fn = fn, g_tool_answers[i].name = symbol;
            return;
        }
}
/* From such an answer: to the output of the tool it was called by, found on the stack when its thread is the system's. */
void tl_tool_print(const char *p, size_t n) {
    TlToolRun *r = pthread_getspecific(g_tool_key);
    void **fp = __builtin_frame_address(0);
    for (int i = 0; !r && fp && i < 24; i++) {
        void **up = fp[0];
        TlImage *im = tl_image_containing(fp[1]);
        if (im && im->ns > 0 && im->ns < TL_MAX_NS) r = g_tool_runs[im->ns];
        if (up <= fp || (char *)up - (char *)fp > 1 << 20) break;
        fp = up;
    }
    tool_text_to(r ? r : &g_tool_none, 1, p, n);
}

static void *tool_lookup(const char *sym, int ns) {
    for (int i = 0; i < 8 && g_tool_answers[i].name; i++)
        if (!strcmp(sym, g_tool_answers[i].name)) return g_tool_answers[i].fn;
    void *swift = tool_swift_symbol(sym);
    if (swift) return swift;
    if (!strncmp(sym, "__ZNSt3__1", 10)) {
        void *o = tl_tool_stream(ns, sym);
        if (o) return o;
    }
    if (!strcmp(sym, "___stdinp")) return &g_tool_in;
    static const struct { const char *name; void *fn; } table[] = {
        {"_malloc", (void *)tool_malloc}, {"_calloc", (void *)tool_calloc}, {"_realloc", (void *)tool_realloc},
        {"_reallocf", (void *)tool_realloc}, {"_valloc", (void *)tool_valloc}, {"_aligned_alloc", (void *)tool_aligned},
        {"_posix_memalign", (void *)tool_posix_memalign}, {"_strdup", (void *)tool_strdup}, {"_strndup", (void *)tool_strndup},
        {"__Znwm", (void *)tool_new}, {"__Znam", (void *)tool_new},
        {"__ZnwmRKSt9nothrow_t", (void *)tool_new_nothrow}, {"__ZnamRKSt9nothrow_t", (void *)tool_new_nothrow},
        {"__ZnwmSt11align_val_t", (void *)tool_new_aligned}, {"__ZnamSt11align_val_t", (void *)tool_new_aligned},
        {"__ZnwmSt11align_val_tRKSt9nothrow_t", (void *)tool_new_aligned_nothrow},
        {"__ZnamSt11align_val_tRKSt9nothrow_t", (void *)tool_new_aligned_nothrow},
        {"___cxa_atexit", (void *)tool_cxa_atexit}, {"_atexit", (void *)tool_atexit}, {"__tlv_atexit", (void *)tool_tlv_atexit},
        {"_exit", (void *)tool_exit}, {"__exit", (void *)tool__exit}, {"__Exit", (void *)tool__exit}, {"_abort", (void *)tool_abort},
        {"__ZSt13set_terminatePFvvE", (void *)tool_null},
        /* The app's own signal handling and folder are not a tool's to change. */
        {"_sigaction", (void *)tool_sigaction}, {"_signal", (void *)tool_null}, {"_sigaltstack", (void *)tool_zero},
        {"_chdir", (void *)tool_chdir}, {"_fchdir", (void *)tool_fchdir}, {"_dup2", (void *)tool_dup2}, {"_dup", (void *)tool_dup}, {"_setrlimit", (void *)tool_zero}, {"_setitimer", (void *)tool_zero},
        {"_write", (void *)tool_write}, {"_write$NOCANCEL", (void *)tool_write}, {"_writev", (void *)tool_writev},
        {"_fwrite", (void *)tool_fwrite}, {"_fputs", (void *)tool_fputs}, {"_fputc", (void *)tool_fputc}, {"_putc", (void *)tool_fputc},
        {"_putchar", (void *)tool_putchar}, {"_puts", (void *)tool_puts}, {"_printf", (void *)tool_printf},
        {"_fprintf", (void *)tool_fprintf}, {"_vfprintf", (void *)tool_vfprintf}, {"_vprintf", (void *)tool_vprintf},
        {"_mmap", (void *)tool_mmap}, {"_munmap", (void *)tool_munmap}, {"_vm_allocate", (void *)tool_vm_allocate},
        {"_vm_deallocate", (void *)tool_vm_deallocate}, {"_open", (void *)tool_open}, {"_access", (void *)tool_access}, {"_stat", (void *)tool_stat},
        {"_lstat", (void *)tool_lstat}, {"_statfs", (void *)tool_statfs}, {"_mkdir", (void *)tool_mkdir}, {"_unlink", (void *)tool_unlink},
        {"_remove", (void *)tool_remove}, {"_opendir", (void *)tool_opendir}, {"_readlink", (void *)tool_readlink}, {"_rename", (void *)tool_rename},
        {"_link", (void *)tool_link}, {"_symlink", (void *)tool_symlink}, {"_clonefile", (void *)tool_clonefile}, {"_copyfile", (void *)tool_copyfile},
        {"_realpath$DARWIN_EXTSN", (void *)tool_realpath}, {"_fcntl", (void *)tool_fcntl}, {"_close", (void *)tool_close},
        {"_getenv", (void *)tool_getenv}, {"_setenv", (void *)tool_setenv}, {"_unsetenv", (void *)tool_unsetenv}, {"_putenv", (void *)tool_putenv},
        {"__NSGetEnviron", (void *)tool_NSGetEnviron}, {"__NSGetArgc", (void *)tool_NSGetArgc}, {"__NSGetArgv", (void *)tool_NSGetArgv},
        {"_read", (void *)tool_read}, {"_read$NOCANCEL", (void *)tool_read}, {"_fstat", (void *)tool_fstat}, {"_isatty", (void *)tool_isatty}, {"_fileno", (void *)tool_fileno},
        {"_lseek", (void *)tool_lseek}, {"_ioctl", (void *)tool_ioctl},
        {"_posix_spawn", (void *)tool_posix_spawn}, {"_posix_spawnp", (void *)tool_posix_spawn},
        {"_posix_spawn_file_actions_init", (void *)tool_fa_init}, {"_posix_spawn_file_actions_destroy", (void *)tool_fa_destroy},
        {"_posix_spawn_file_actions_adddup2", (void *)tool_fa_dup2}, {"_posix_spawn_file_actions_addopen", (void *)tool_fa_open},
        {"_posix_spawn_file_actions_addclose", (void *)tool_fa_close}, {"_posix_spawn_file_actions_addinherit_np", (void *)tool_fa_inherit},
        {"_posix_spawn_file_actions_addchdir_np", (void *)tool_fa_chdir}, {"_posix_spawn_file_actions_addchdir", (void *)tool_fa_chdir},
        {"_waitpid", (void *)tool_waitpid}, {"_waitpid$NOCANCEL", (void *)tool_waitpid}, {"_wait4", (void *)tool_wait4}, {"_kill", (void *)tool_kill},
        {"_pselect", (void *)tool_pselect}, {"_pselect$DARWIN_EXTSN", (void *)tool_pselect}, {"_pselect$1050", (void *)tool_pselect}, {"_pipe", (void *)tool_pipe}, {"_fclose", (void *)tool_fclose}, {"_close$NOCANCEL", (void *)tool_close}, {"_perror", (void *)tool_perror}, {"_getchar", (void *)tool_getchar}, {"_err", (void *)tool_err}, {"_errx", (void *)tool_errx}, {"_warn", (void *)tool_warn}, {"_warnx", (void *)tool_warnx}, {"___assert_rtn", (void *)tool_assert},
        {"_system", (void *)tool_system}, {"_popen", (void *)tool_popen}, {"_pclose", (void *)tool_pclose},
        {"_fork", (void *)tool_no_fork}, {"_vfork", (void *)tool_no_fork},
        {"_dispatch_apply", (void *)tool_dispatch_apply}, {"_dispatch_async", (void *)tool_dispatch_async},
        {"_dispatch_group_async", (void *)tool_dispatch_group_async}, {"_dispatch_sync", (void *)tool_dispatch_sync},
        {"_dispatch_barrier_async", (void *)tool_dispatch_barrier_async}, {"_pthread_create", (void *)tool_pthread_create}, {"_nanosleep", (void *)tool_nanosleep},
        {"_usleep", (void *)tool_usleep}, {"_pthread_cond_wait", (void *)tool_cond_wait},
        {"_pthread_cond_timedwait", (void *)tool_cond_timedwait}, {"_pthread_cond_timedwait_relative_np", (void *)tool_cond_timedwait_rel},
        {"__ZNSt3__111this_thread9sleep_forERKNS_6chrono8durationIxNS_5ratioILl1ELl1000000000EEEEE", (void *)tool_sleep_for},
        {"__ZNSt3__118condition_variable4waitERNS_11unique_lockINS_5mutexEEE", (void *)tool_cv_wait},
        {"__ZNSt3__16chrono12steady_clock3nowEv", (void *)tool_steady_now}, {"_mach_absolute_time", (void *)tool_absolute_time},
        {"_clock_gettime", (void *)tool_clock_gettime}, {"_accept", (void *)tool_accept}, {"_recv", (void *)tool_recv},
        {"_socket", (void *)tool_socket}, {"_select", (void *)tool_select}, {"_select$DARWIN_EXTSN", (void *)tool_select},
         {"_setvbuf", (void *)tool_zero}, {"_pthread_key_create", (void *)tool_key_create},
    };
    for (size_t i = 0; i < sizeof table / sizeof *table; i++)
        if (!strcmp(sym, table[i].name)) return table[i].fn;
    return NULL;
}

static pid_t tool_shell(const char *command, int in, int out) {
    void *fa;
    tool_fa_init(&fa);
    if (in >= 0) tool_fa_dup2(&fa, in, 0);
    if (out >= 0) tool_fa_dup2(&fa, out, 1);
    char *argv[] = {"sh", "-c", (char *)command, NULL};
    pid_t pid = -1;
    int rc = tool_posix_spawn(&pid, "/bin/sh", &fa, NULL, argv, g_tool.env);
    tool_fa_destroy(&fa);
    return rc ? -1 : pid;
}
static int tool_system(const char *command) {
    if (!command) return 1;
    int st = 0;
    pid_t p = tool_shell(command, -1, -1);
    if (p < 0) return 127 << 8;
    return tool_waitpid(p, &st, 0) == p ? st : -1;
}
static struct { FILE *f; pid_t pid; } g_tool_popen[64];
static FILE *tool_popen(const char *command, const char *mode) {
    int p[2], reads = mode[0] == 'r';
    if (pipe(p)) return NULL;
    pid_t pid = tool_shell(command, reads ? -1 : p[0], reads ? p[1] : -1);
    close(p[reads]);
    FILE *f = pid > 0 ? fdopen(p[!reads], mode) : NULL;
    if (!f) { close(p[!reads]); return NULL; }
    pthread_mutex_lock(&g_tool_lock);
    for (int i = 0; i < 64; i++)
        if (!g_tool_popen[i].f) { g_tool_popen[i].f = f, g_tool_popen[i].pid = pid; break; }
    pthread_mutex_unlock(&g_tool_lock);
    return f;
}
static int tool_pclose(FILE *f) {
    pid_t pid = 0;
    int st = -1;
    pthread_mutex_lock(&g_tool_lock);
    for (int i = 0; i < 64; i++)
        if (g_tool_popen[i].f == f) pid = g_tool_popen[i].pid, g_tool_popen[i].f = NULL;
    pthread_mutex_unlock(&g_tool_lock);
    fclose(f);
    return pid && tool_waitpid(pid, &st, 0) == pid ? st : -1;
}

/* ---- running one ---- */

static TlImage *tool_load(const char *path, int own_objc, char *err, size_t errlen) {
    pthread_once(&g_tool_once, tool_init);
    if (!g_host) { set_err(err, errlen, "nothing loaded yet"); return NULL; }
    char *argv[] = {(char *)path, NULL};
    TlProc *p = proc_new(0, argv, environ);
    if (!p) { set_err(err, errlen, "too many programs"); return NULL; }
    g_is_tool[p->ns] = 1, g_no_objc[p->ns] = own_objc;
    p->exe = load_library_in(path, p->ns, err, errlen);
    if (!p->exe) { g_procs[p->ns] = NULL; return NULL; }
    return p->exe;
}
TlImage *tl_load_tool(const char *path, char *err, size_t errlen) { return tool_load(path, 0, err, errlen); }

static void tool_thread_end(void *x) { tool_done(x, ((TlToolRun *)x)->code, 0); }
static void *tool_thread(void *x) {
    TlToolRun *r = x;
    pthread_setname_np("tool");
    pthread_setspecific(g_tool_key, r);
    tool_enter_folder(r);
    pthread_cleanup_push(tool_thread_end, r);
    run_initializers(r->exe);
    r->code = r->entry ? ((int (*)(int, const char **))r->entry)(r->argc, r->argv) : tl_run_main(r->exe, r->argc, r->argv, (const char **)r->env);
    tool_run_exits(r);
    pthread_cleanup_pop(1);
    return NULL;
}

/* A program that is built again and again, such as a game without its window: each build takes the place of the one
   before it. Its Objective-C classes are not registered. replacing: the build before it, ended and no longer used. */
TlImage *tl_load_replaceable(const char *path, TlImage *replacing, char *err, size_t errlen) {
    pthread_once(&g_tool_once, tool_init);
    if (!g_host) { set_err(err, errlen, "nothing loaded yet"); return NULL; }
    if (!replacing) {
        g_want_slot = 1;
        TlImage *im = tool_load(path, 1, err, errlen);
        g_want_slot = 0;
        return im;
    }
    int ns = replacing->ns;
    if (g_is_tool[ns] == 2 || g_tool_runs[ns]) {
        set_err(err, errlen, "the build before it still has threads running: start Henge again");
        return NULL;
    }
    pthread_mutex_lock(&g_lock);
    own_categories_gone(ns);
    TlImage *list = NULL;
    for (TlImage **at = &g_images; *at;) {
        TlImage *im = *at;
        if (im->ns != ns) { at = &im->next; continue; }
        *at = im->next;
        im->next = list, list = im;
    }
    g_ns_main[ns] = NULL;
    pthread_mutex_unlock(&g_lock);
    for (TlImage *im = list; im; im = im->next) { /* stays in g_order, matching nothing */
        im->vmsize = 0;
        im->ns = -1;
        if (im->tlv_size) pthread_key_delete(im->tlv_key), im->tlv_key = (pthread_key_t)-1;
        free(im->fresh);
        im->fresh = NULL;
        munmap(im->map, im->maplen);
        close(im->fd);
    }
    g_procs[ns]->exe = NULL;
    g_want_slot = 1;
    TlImage *im = g_procs[ns]->exe = load_library_in(path, ns, err, errlen);
    g_want_slot = 0;
    return im;
}

void tl_tool_stop(TlImage *tool) {
    pthread_mutex_lock(&g_tool_lock);
    for (int i = 0; i < TL_MAX_NS; i++)
        if (g_tool_runs[i] && (!tool || i == tool->ns)) g_tool_runs[i]->kill = 1;
    for (TlToolChild *c = g_tool_children; c && !tool; c = c->next) c->kill = 1;
    pthread_mutex_unlock(&g_tool_lock);
}

void tl_tool_stop_in(const char *cwd) {
    pthread_mutex_lock(&g_tool_lock);
    for (int i = 0; i < TL_MAX_NS; i++)
        if (g_tool_runs[i] && g_tool_runs[i]->command && cwd && !strcmp(g_tool_runs[i]->command, cwd)) g_tool_runs[i]->kill = 1;
    for (int more = 1; more;) {
        more = 0;
        for (TlToolChild *c = g_tool_children; c; c = c->next)
            if (!c->kill && c->parent && c->parent->kill) {
                c->kill = 1, more = 1;
                if (c->run) c->run->kill = 1;
            }
    }
    pthread_mutex_unlock(&g_tool_lock);
}

/* Over the environment base, each of over's "NAME=value". */
static char **tool_env_with(char *const *base, const char **over) {
    int n = 0;
    char **env = copy_strings(base, &n);
    TlToolRun r = {.env = env};
    for (; over && *over; over++) {
        char *eq = strchr(*over, '=');
        if (eq) tool_env_put(&r, strdup(*over), (size_t)(eq - *over));
    }
    return r.env;
}

/* The system's list of zones: one that goes is replaced by the last, which a free() on another thread may miss at that
   moment. So the last is always a new one nobody uses yet, kept for the next run. */
static pthread_mutex_t g_tool_zone_lock = PTHREAD_MUTEX_INITIALIZER;
static malloc_zone_t *g_tool_spare[64];
static int g_tool_nspare;
static malloc_zone_t *tool_zone_take(void) {
    pthread_mutex_lock(&g_tool_zone_lock);
    malloc_zone_t *z = g_tool_nspare ? g_tool_spare[--g_tool_nspare] : malloc_create_zone(0, 0);
    pthread_mutex_unlock(&g_tool_zone_lock);
    return z;
}
static void tool_zone_give(malloc_zone_t *old) {
    pthread_mutex_lock(&g_tool_zone_lock);
    if (g_tool_nspare < 64) {
        g_tool_spare[g_tool_nspare++] = malloc_create_zone(0, 0);
        malloc_destroy_zone(old);
    } /* else: stays; there are never so many runs at once */
    pthread_mutex_unlock(&g_tool_zone_lock);
}

static int tool_run_it(TlToolRun *r) {
    int ns = r->ns;
    r->zone = tool_zone_take();
    clearerr(g_tool_in);
    tl_tool_stream_reset(ns);
    optind = optreset = opterr = 1; /* getopt's, the system's, one for all */
    for (TlImage *im = g_images; im; im = im->next)
        if (im->ns == ns) tool_restore(im);
    pthread_attr_t a;
    pthread_attr_init(&a);
    pthread_attr_setstacksize(&a, 64 << 20);
    pthread_mutex_lock(&g_tool_lock);
    int rc = pthread_create(&r->thread, &a, tool_thread, r);
    while (!rc && !r->ended) pthread_cond_wait(&g_tool_cond, &g_tool_lock);
    pthread_mutex_unlock(&g_tool_lock);
    pthread_attr_destroy(&a);
    if (rc) {
        char why[160];
        snprintf(why, sizeof why, "no thread for the tool: %s\n", strerror(rc));
        tl_log("%s", why);
        tool_text_to(r, 2, why, strlen(why));
    }
    int code = rc ? 127 : r->code & 0xFF;
    r->stop = 1;
    for (int i = 0; i < 300 && r->live > 0; i++) usleep(10000);
    if (!r->stuck && r->live > 0) {
        tl_log("%d threads of %s still run", r->live, r->exe->path);
        r->stuck = 1;
    }
    if (r->stuck) {
        pthread_detach(r->thread);
        tl_log("a tool ended while its threads still ran (%d): this copy of it is not used again", code);
        g_is_tool[ns] = 2; /* its threads still hold its heap: all of it stays, and the run */
        return code;
    }
    if (!rc) pthread_join(r->thread, NULL);
    pthread_mutex_lock(&g_tool_lock);
    g_tool_runs[ns] = NULL;
    /* Children nobody waited for. */
    for (TlToolChild *c = g_tool_children; c; c = c->next)
        if (c->parent == r) c->parent = NULL;
    pthread_mutex_unlock(&g_tool_lock);
    for (size_t i = 0; i < r->nmaps; i++) munmap(r->maps[i].at, r->maps[i].len);
    struct stat st;
    for (int fd = 3; fd < 1024; fd++)
        if (r->fds[fd] && !fstat(fd, &st) && st.st_ino == r->fdino[fd]) close(fd); /* not another's that has its number now */
    for (int i = 0; i < r->nkeys; i++) pthread_key_delete(r->keys[i]);
    tool_zone_give(r->zone);
    free(r->maps);
    free(r->exits);
    free(r->env);
    free(r->alias);
    free(r);
    return code;
}

/* NULL when the tool is in use or no longer usable. */
static TlToolRun *tool_run_new(TlImage *tool) {
    int ns = tool->ns;
    TlToolRun *r = calloc(1, sizeof *r);
    if (!r) return NULL;
    r->exe = tool, r->ns = ns;
    r->std[0] = r->std[1] = r->std[2] = -1;
    pthread_mutex_lock(&g_tool_lock);
    int ok = g_is_tool[ns] == 1 && !g_tool_runs[ns];
    if (ok) g_tool_runs[ns] = r;
    pthread_mutex_unlock(&g_tool_lock);
    if (!ok) { free(r); return NULL; }
    return r;
}

int tl_tool_run_at(TlImage *tool, const char *entry, int argc, const char **argv, const char **env,
                   void (*output)(void *, const char *, size_t), void *context) {
    void *at = entry ? tl_image_local_symbol(tool, entry, NULL) : NULL;
    if (entry && !at) return 127;
    TlToolRun *r = tool_run_new(tool);
    if (!r) return TL_TOOL_STUCK;
    r->argc = argc, r->argv = argv, r->output = output, r->context = context, r->entry = at, r->keeps = 1;
    r->env = tool_env_with(environ, env);
    return tool_run_it(r);
}
int tl_tool_run(TlImage *tool, int argc, const char **argv, void (*output)(void *, const char *, size_t), void *context) {
    return tl_tool_run_at(tool, NULL, argc, argv, NULL, output, context);
}

/* ---- by path: copies of tools kept for the next time ---- */

static int tool_most(const char *path, int argc, const char **argv);
typedef struct TlToolCopy { char *path; dev_t dev; ino_t ino; time_t mtime; off_t size; TlImage *im; int busy, slot; struct TlToolCopy *next; } TlToolCopy;
static TlToolCopy *g_tool_copies;
static pthread_mutex_t g_tool_load_lock = PTHREAD_MUTEX_INITIALIZER; /* one is loaded at a time */

static int tool_same_file(const TlToolCopy *c, const struct stat *st) {
    return c->dev == st->st_dev && c->ino == st->st_ino && c->mtime == st->st_mtime && c->size == st->st_size;
}
/* A copy of the tool at path for one run. At most `most` of them; then one is waited for. */
static TlToolCopy *tool_take(const char *path, int most, char *err, size_t errlen) {
    struct stat st;
    if (stat(path, &st)) { set_err(err, errlen, "cannot find %s", path); return NULL; }
    int tries = 0, slot = !strstr(path, "/toolchain/") && !strstr(path, ".xctoolchain/") && !strstr(path, "/Toolchain/");
    pthread_mutex_lock(&g_tool_lock);
    for (;;) {
        int have = 0, built = 0;
        TlToolCopy *old = NULL, *other = NULL;
        for (TlToolCopy *c = g_tool_copies; c; c = c->next) {
            if (c->slot && c->path[0] && !c->busy && c->im && g_is_tool[c->im->ns] == 1 && strcmp(c->path, path)) built++, other = c;
            if (strcmp(c->path, path) || (c->im && g_is_tool[c->im->ns] == 2)) continue;
            have++;
            if (c->busy) continue;
            if (tool_same_file(c, &st)) {
                c->busy = 1;
                pthread_mutex_unlock(&g_tool_lock);
                return c;
            }
            old = c;
        }
        TlToolCopy *c = old;
        if (!c && !have && slot && built >= 12 && g_slot[other->im->ns].size >= (size_t)st.st_size + ((size_t)5 << 20)) {
            c = other; /* the room of a program built here that has not run for longest */
            free(c->path);
            c->path = strdup(path);
            old = c;
        }
        if (!c && have < most) {
            c = calloc(1, sizeof *c);
            c->path = strdup(path);
            c->busy = 2;
            c->next = g_tool_copies, g_tool_copies = c;
        }
        if (c) {
            c->busy = 1;
            pthread_mutex_unlock(&g_tool_lock);
            pthread_mutex_lock(&g_tool_load_lock);
            c->slot = slot;
            const char *base = strrchr(path, '/');
            TlImage *im = c->im || slot ? tl_load_replaceable(path, c->im, err, errlen) : tool_load(path, !base || strcmp(base, "/ld"), err, errlen);
            pthread_mutex_unlock(&g_tool_load_lock);
            pthread_mutex_lock(&g_tool_lock);
            if (im) {
                c->im = im, c->dev = st.st_dev, c->ino = st.st_ino, c->mtime = st.st_mtime, c->size = st.st_size;
                pthread_mutex_unlock(&g_tool_lock);
                tl_log("tool %s loaded, %zu MB of the region in use", base ? base + 1 : path, g_host->used >> 20);
                return c;
            }
            c->path[0] = 0; /* not usable */
            c->im = NULL;
            if (old) { old = NULL; if (++tries < 2) continue; } /* it has outgrown its room: a new one */
            if (!have || most > tool_most(path, 0, NULL)) { pthread_mutex_unlock(&g_tool_lock); return NULL; }
            most = have; /* no room for another: wait for one */
            continue;
        }
        const struct timespec step = {0, 200000000};
        pthread_cond_timedwait_relative_np(&g_tool_cond, &g_tool_lock, &step);
    }
}
static void tool_give(TlToolCopy *c) {
    pthread_mutex_lock(&g_tool_lock);
    c->busy = 0;
    pthread_cond_broadcast(&g_tool_cond);
    pthread_mutex_unlock(&g_tool_lock);
}

static int tool_is_clang(const char *path) {
    const char *b = strrchr(path, '/');
    return !strncmp(b ? b + 1 : path, "clang", 5);
}
/* Apple's clang starts itself anew to compile one file; it can do without. (For several files it still does.) */
static const char **tool_args(const char *path, int *argc, const char **argv) {
    const char *b = strrchr(path, '/');
    if (!strcmp(b ? b + 1 : path, "python3")) {
        /* -I and -E leave out PYTHONHOME, without which Python does not find its library here: the rest of what they mean. */
        const char **v = calloc((size_t)*argc + 1, sizeof *v);
        int i = 1, n = 1;
        v[0] = argv[0];
        for (; i < *argc && argv[i][0] == '-' && argv[i][1] && argv[i][1] != '-'; i++) {
            const char *o = argv[i];
            size_t k = strcspn(o, "cmWX");
            char *w = malloc(strlen(o) * 2 + 1), *to = w;
            for (size_t j = 0; j < k; j++) {
                if (o[j] == 'I') *to++ = 's', *to++ = 'P';
                else if (o[j] != 'E') *to++ = o[j];
            }
            strcpy(to, o + k);
            if (strcmp(w, "-")) v[n++] = w;
            if (o[k] == 'c' || o[k] == 'm') { i++; break; }
            if (o[k] && !o[k + 1] && i + 1 < *argc) v[n++] = argv[++i]; /* -W and -X take the next */
        }
        for (; i < *argc; i++) v[n++] = argv[i];
        *argc = n;
        return v;
    }
    if (!tool_is_clang(path) || (*argc > 1 && !strncmp(argv[1], "-cc1", 4))) return argv;
    const char **v = calloc((size_t)*argc + 2, sizeof *v); /* stays: the tool may hold it */
    v[0] = argv[0], v[1] = "-fintegrated-cc1";
    for (int i = 1; i < *argc; i++) v[i + 1] = argv[i];
    ++*argc;
    return v;
}
static int tool_most(const char *path, int argc, const char **argv) {
    const char *b = strrchr(path, '/');
    b = b ? b + 1 : path;
    if (!strcmp(b, "ld")) return 1;
    /* One more for what a compiler starts: those that wait for it hold the others. */
    if (tool_is_clang(path)) return (g_host->tool_copies > 0 ? g_host->tool_copies : 2) + (argc > 1 && !strncmp(argv[1], "-cc1", 4));
    return 4;
}
static int tool_run_child(TlToolChild *c) {
    char err[512] = "";
    TlToolRun *parent = c->parent;
    TlToolCopy *t = tool_take(c->path, tool_most(c->path, c->argc, (const char **)c->argv), err, sizeof err);
    TlToolRun *r = t ? tool_run_new(t->im) : NULL;
    if (!r) {
        tl_log("%s: %s", c->path, err);
        if (t) tool_give(t);
        return 127;
    }
    r->argc = c->argc, r->argv = tool_args(c->path, &r->argc, (const char **)c->argv), r->env = copy_strings(c->env, NULL), r->cwd = c->cwd ? strdup(c->cwd) : NULL;
    for (int i = 0; i < 3; i++) r->std[i] = c->std[i];
    if (parent) r->output = parent->output, r->context = parent->context;
    r->kill = c->kill;
    c->run = r;
    int code = tool_run_it(r);
    tool_give(t);
    return code;
}

int tl_tool_command(const char *path, int argc, const char **argv, const char **env, const char *cwd,
                    void (*output)(void *, const char *, size_t), void *context) {
    pthread_once(&g_tool_once, tool_init);
    char found[PATH_MAX], err[512] = "";
    if (!g_host || !tool_find(path, cwd, found, sizeof found)) {
        snprintf(err, sizeof err, "%s: not a program that can run here\n", path);
        output(context, err, strlen(err));
        return 127;
    }
    TlToolCopy *t = tool_take(found, tool_most(found, argc, argv), err, sizeof err);
    TlToolRun *r = t ? tool_run_new(t->im) : NULL;
    if (!r) {
        output(context, err, strlen(err));
        if (t) tool_give(t);
        return 127;
    }
    r->argc = argc, r->argv = tool_args(found, &r->argc, argv), r->output = output, r->context = context, r->env = tool_env_with(environ, env);
    r->cwd = cwd ? strdup(cwd) : NULL, r->command = cwd;
    int code = tool_run_it(r);
    tool_give(t);
    return code;
}
