/* A command-line Mac program in a terminal, without macOS: its standard input and output are a socket that answers
   as a terminal does (isatty, the window size, raw mode), since an iOS app gets no pseudo-terminal of its own. The
   program runs on a thread of this process, one per launch (henge-tl.c). Of other processes it tries to start, a shell
   and the toolchain's tools run on threads here (tl_tool.h); the rest are refused, as iOS does; a web page it opens (/usr/bin/open URL: signing in) is handed to the app. */
#include <dispatch/dispatch.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/event.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

#include "henge-ios-jit.h"
#include "henge-tl.h"
#include "henge-vm.h"
#include "../../../tl/loader/tl_loader.h"

HengeDualMapping *henge_ios_jit_get_mapping(void);
void henge_tl_mark_ran(void); /* henge-tl.c: one program or app per launch */
extern _Atomic int henge_tl_ran;

static TlHost host;
static TlImage *image;
static int host_fd = -1;
static struct termios tio;
static struct winsize size = {40, 80, 0, 0};
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static HengeTlTermOutput on_output;
static HengeTlTermOpen on_open;
static HengeTlTermExit on_exit_cb;
static char **g_argv;
static int g_argc;
static pthread_t main_thread;
static _Atomic int ended;

static void finish(int code) {
    if (atomic_exchange(&ended, 1)) return;
    fprintf(stderr, "henge: term: the program ended with %d\n", code);
    if (on_exit_cb) on_exit_cb(code);
}

static int is_term(int fd) { return fd >= 0 && fd <= 2; }
static int term_isatty(int fd) { return is_term(fd) ? 1 : isatty(fd); }
static int term_tcgetattr(int fd, struct termios *t) {
    if (!is_term(fd)) return tcgetattr(fd, t);
    pthread_mutex_lock(&lock);
    *t = tio;
    pthread_mutex_unlock(&lock);
    return 0;
}
static int term_tcsetattr(int fd, int how, const struct termios *t) {
    if (!is_term(fd)) return tcsetattr(fd, how, t);
    pthread_mutex_lock(&lock);
    tio = *t;
    pthread_mutex_unlock(&lock);
    return 0;
}
static int term_ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *p = va_arg(ap, void *);
    va_end(ap);
    if (is_term(fd)) {
        int known = 1;
        pthread_mutex_lock(&lock);
        switch (request) {
        case TIOCGWINSZ: *(struct winsize *)p = size; break;
        case TIOCGETA: *(struct termios *)p = tio; break;
        case TIOCSETA: case TIOCSETAW: case TIOCSETAF: tio = *(const struct termios *)p; break;
        default: known = 0;
        }
        pthread_mutex_unlock(&lock);
        if (known) return 0;
    }
    return ioctl(fd, request, p);
}
/* The program's exit ends its own thread, not Henge. */
static void term_exit(int code) {
    fflush(NULL);
    finish(code);
    for (;;) {
        if (pthread_equal(pthread_self(), main_thread)) pthread_exit(NULL);
        pause(); /* another of its threads called it: the main one may still be running */
    }
}
/* /usr/bin/security, the Mac's keychain tool, which iOS has not: it runs, finds nothing and can save nothing, so a
   program (Claude Code) keeps its sign-in in its own private file instead. Refusing to start it says only "try again
   later". The "process" has ended when it is returned; the kernel knows no such pid, so a watch on it fails at once
   and the program asks wait4. */
enum { SECURITY_PID = 0x40000000, SECURITY_PIDS = 4096, NOT_FOUND = 44 };
static _Atomic unsigned security_next;
static _Atomic unsigned char security_code[SECURITY_PIDS]; /* 0: no such process; else the exit code + 1 */
static int security_tool(pid_t *pid, const char *path, char *const argv[]) {
    if (!path || !argv || !argv[0]) return 0;
    const char *base = strrchr(path, '/'), *verb = argv[1];
    base = base ? base + 1 : path;
    if (!strcmp(base, "sh") && argv[1] && !strcmp(argv[1], "-c") && argv[2]) {
        const char *cmd = argv[2];
        if (!strncmp(cmd, "/usr/bin/", 9)) cmd += 9;
        if (strncmp(cmd, "security ", 9) || strpbrk(cmd, ";|&\n")) return 0;
        verb = cmd + 9;
    } else if (strcmp(base, "security"))
        return 0;
    int lookup = verb && (!strncmp(verb, "find-", 5) || !strncmp(verb, "delete-", 7));
    unsigned i = atomic_fetch_add(&security_next, 1) % SECURITY_PIDS;
    atomic_store(&security_code[i], (unsigned char)((lookup ? NOT_FOUND : 1) + 1));
    if (pid) *pid = SECURITY_PID + (pid_t)i;
    fprintf(stderr, "henge: term: security %.24s: answered %d (%u)\n", verb ? verb : "", lookup ? NOT_FOUND : 1, i); /* not its arguments: a secret is one */
    return 1;
}

/* The compiler, linker and other tools beside it (tl_tool_command): loaded at their first use and kept. Told before the
   program starts. */
void henge_tool_print(void); /* Swift's, with the arguments of its print (ProjectTools.swift) */
void henge_tool_debug_print(void);
void henge_tool_swift(void);
static char *tool_folders[9], **tool_env;
static int tool_copies;
void henge_tl_tool_setup(const char *const *folders, int copies, const char *const *env) {
    int n = 0, e = 0;
    for (; folders && folders[n] && n < 8; n++) tool_folders[n] = strdup(folders[n]);
    tool_folders[n] = NULL;
    tool_copies = copies;
    while (env && env[e]) e++;
    henge_tool_swift();
    tl_tool_answer("_$ss5print_9separator10terminatoryypd_S2StF", (void *)henge_tool_print);
    tl_tool_answer("_$ss10debugPrint_9separator10terminatoryypd_S2StF", (void *)henge_tool_debug_print);
    tool_env = calloc((size_t)e + 1, sizeof *tool_env);
    for (int i = 0; i < e; i++) tool_env[i] = strdup(env[i]);
}
/* base with the tools' own values in place of its. The array is the caller's, the strings are not. */
static char **env_for_tool(char *const *base) {
    int n = 0, m = 0, k = 0;
    while (base && base[n]) n++;
    while (tool_env && tool_env[m]) m++;
    char **v = calloc((size_t)(n + m) + 1, sizeof *v);
    for (int i = 0; i < n; i++) {
        size_t l = strcspn(base[i], "=") + 1;
        int own = 0;
        for (int j = 0; j < m && !own; j++) own = !strncmp(tool_env[j], base[i], l);
        if (!own) v[k++] = base[i];
    }
    for (int j = 0; j < m; j++) v[k++] = tool_env[j];
    return v;
}

/* The toolchain's tools, when the program starts them (Claude Code's Bash tool starts a shell). */
enum { PROC_PID = SECURITY_PID + SECURITY_PIDS, PROCS = 256, WATCHES = 4 };
typedef struct Watch { int kq, is64; struct kevent e; struct kevent64_s e64; } Watch;
static struct Proc { int used, done, status; unsigned serial; pid_t tool; int nwatch; Watch watch[WATCHES]; } procs[PROCS];
static pthread_mutex_t procs_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t procs_cond = PTHREAD_COND_INITIALIZER;
static pthread_once_t room_once = PTHREAD_ONCE_INIT;
static void give_room(void);
static int is_proc(uint64_t pid) { return pid >= PROC_PID && pid < PROC_PID + PROCS; }
static void proc_ended(void *ctx, int status) {
    struct Proc *p = ctx;
    pthread_mutex_lock(&procs_lock);
    p->done = 1, p->status = status;
    for (int i = 0; i < p->nwatch; i++) {
        Watch *w = &p->watch[i];
        const struct timespec now = {0, 0};
        w->e.flags = w->e64.flags = 0, w->e.fflags = w->e64.fflags = NOTE_TRIGGER;
        if (w->is64) kevent64(w->kq, &w->e64, 1, NULL, 0, 0, &now);
        else kevent(w->kq, &w->e, 1, NULL, 0, &now);
    }
    p->nwatch = 0;
    pthread_cond_broadcast(&procs_cond);
    pthread_mutex_unlock(&procs_lock);
}
static int proc_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *fa, char *const argv[], char *const envp[]) {
    static unsigned next;
    const char *base = path ? strrchr(path, '/') : NULL;
    base = base ? base + 1 : path;
    if (!path || !argv || !strncmp(base, "claude", 6) || (g_argv && !strcmp(path, g_argv[0])) || !tl_tool_exists(path)) return -1;
    pthread_once(&room_once, give_room);
    struct Proc *p = NULL;
    pthread_mutex_lock(&procs_lock);
    for (int i = 0; i < PROCS && !p; i++, next++)
        if (!procs[next % PROCS].used) p = &procs[next % PROCS];
    if (p) memset(p, 0, sizeof *p), p->used = 1;
    pthread_mutex_unlock(&procs_lock);
    if (!p) return EAGAIN;
    pid_t mine = PROC_PID + (pid_t)(p - procs), tool = 0;
    extern char **environ;
    char **env = env_for_tool(envp ? envp : environ);
    int rc = tl_tool_spawn(&tool, path, (void *const *)fa, argv, env, proc_ended, p);
    free(env);
    pthread_mutex_lock(&procs_lock);
    if (rc) p->used = 0;
    else p->tool = tool;
    pthread_mutex_unlock(&procs_lock);
    if (!rc && pid) *pid = mine;
    if (!rc) fprintf(stderr, "henge: term: started %s (%d)\n", base, (int)(p - procs)); /* not its arguments */
    return rc;
}
static pid_t proc_wait(pid_t pid, int *status, int options) {
    struct Proc *p = &procs[pid - PROC_PID];
    pthread_mutex_lock(&procs_lock);
    while (p->used && !p->done && !(options & WNOHANG)) pthread_cond_wait(&procs_cond, &procs_lock);
    pid_t rc = 0;
    if (!p->used) errno = ECHILD, rc = -1;
    else if (p->done) {
        if (status) *status = p->status;
        p->used = 0;
        rc = pid;
    }
    pthread_mutex_unlock(&procs_lock);
    return rc;
}
static int term_kill(pid_t pid, int sig) {
    pid_t one = pid < 0 ? -pid : pid;
    if (is_proc((uint64_t)one)) {
        struct Proc *p = &procs[one - PROC_PID];
        pthread_mutex_lock(&procs_lock);
        int alive = p->used && !p->done;
        pid_t tool = p->tool;
        pthread_mutex_unlock(&procs_lock);
        if (!alive) return errno = ESRCH, -1;
        if (sig) tl_tool_kill(tool);
        return 0;
    }
    if (one >= SECURITY_PID && one < SECURITY_PID + SECURITY_PIDS) return errno = ESRCH, -1;
    int (*next)(pid_t, int) = tl_runtime_lookup("_kill");
    return (next ? next : kill)(pid, sig);
}

static pid_t term_wait4(pid_t pid, int *status, int options, struct rusage *ru) {
    if (pid > 0 && is_proc((uint64_t)pid)) {
        if (ru) memset(ru, 0, sizeof *ru);
        return proc_wait(pid, status, options);
    }
    if (pid >= SECURITY_PID && pid < SECURITY_PID + SECURITY_PIDS) {
        int code = atomic_exchange(&security_code[pid - SECURITY_PID], 0);
        if (!code) return errno = ECHILD, -1;
        if (status) *status = (code - 1) << 8;
        fprintf(stderr, "henge: term: security: told it ended (%d)\n", pid - SECURITY_PID);
        if (code - 1 != NOT_FOUND)
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC), dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
                char file[PATH_MAX];
                struct stat st;
                snprintf(file, sizeof file, "%s/.claude/.credentials.json", getenv("HOME") ? getenv("HOME") : "");
                if (stat(file, &st)) fprintf(stderr, "henge: term: no private sign-in file: %s\n", strerror(errno));
                else fprintf(stderr, "henge: term: private sign-in file: %lld bytes\n", (long long)st.st_size);
            });
        if (ru) memset(ru, 0, sizeof *ru);
        return pid;
    }
    pid_t (*next)(pid_t, int *, int, struct rusage *) = tl_runtime_lookup("_wait4");
    return next ? next(pid, status, options, ru) : wait4(pid, status, options, ru);
}
static pid_t term_waitpid(pid_t pid, int *status, int options) {
    if (pid >= SECURITY_PID && pid < PROC_PID + PROCS) return term_wait4(pid, status, options, NULL);
    pid_t (*next)(pid_t, int *, int) = tl_runtime_lookup("_waitpid");
    return next ? next(pid, status, options) : waitpid(pid, status, options);
}
static int term_spawn(pid_t *pid, const char *path, const posix_spawn_file_actions_t *fa, const posix_spawnattr_t *attr,
                      char *const argv[], char *const envp[]) {
    size_t n = path ? strlen(path) : 0;
    if (n >= 4 && !strcmp(path + n - 4, "open") && (n == 4 || path[n - 5] == '/'))
        for (int i = 1; argv && argv[i]; i++)
            if (!strncmp(argv[i], "https://", 8) && on_open) {
                on_open(argv[i]);
                return EPERM; /* there is no such process to wait for: the program shows the address as well */
            }
    if (security_tool(pid, path, argv)) return 0;
    int rc = proc_spawn(pid, path, fa, argv, envp);
    if (rc >= 0) return rc;
    int (*refuse)(pid_t *, const char *, const void *, const void *, char *const[], char *const[]) = tl_runtime_lookup("_posix_spawn");
    return refuse ? refuse(pid, path, fa, attr, argv, envp) : EPERM;
}

/* A program looks for the tool on its PATH (/henge/bin is first there) before it starts it, and iOS has no such file:
   it sees a file that is there, Henge's own. */
#define SECURITY_PATH "/henge/bin/security"
#define SHELL_PATH "/henge/bin/bash" /* the toolchain's sh */
static const char *seen(const char *path) {
    static char own[PATH_MAX];
    if (!path || (strcmp(path, SECURITY_PATH) && (strcmp(path, SHELL_PATH) || !tl_tool_exists("sh")))) return path;
    uint32_t n = sizeof own;
    if (!own[0] && _NSGetExecutablePath(own, &n)) own[0] = 0;
    return own[0] ? own : path;
}
static int term_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    const char *p = seen(path);
    if (p != path) return open(p, O_RDONLY | O_CLOEXEC);
    int (*next)(const char *, int, ...) = tl_runtime_lookup("_open");
    return (next ? next : open)(path, flags, mode);
}
static int term_access(const char *path, int mode) {
    const char *p = seen(path);
    int (*next)(const char *, int) = tl_runtime_lookup("_access");
    return (next ? next : access)(p, p == path ? mode : F_OK);
}
static int term_stat(const char *path, struct stat *st) {
    int (*next)(const char *, struct stat *) = tl_runtime_lookup("_stat");
    return (next ? next : stat)(seen(path), st);
}

/* A watch on such a process fails as the kernel's does on one that is gone, whatever this kernel would say. */
static int is_security(uint64_t ident, int16_t filter) {
    return filter == EVFILT_PROC && ident >= SECURITY_PID && ident < SECURITY_PID + SECURITY_PIDS;
}
/* A watch on a tool becomes an event of the program's own, set off when the tool ends (proc_ended); it is told it as the
   end of a process. */
static int watch_kevent(int kq, const struct kevent *e, const struct kevent64_s *e64, struct kevent *u, struct kevent64_s *u64) {
    uint64_t ident = e ? e->ident : e64->ident;
    unsigned flags = e ? e->flags : e64->flags;
    struct Proc *p = &procs[ident - PROC_PID];
    if (flags & EV_DELETE) {
        for (int i = 0; i < p->nwatch; i++)
            if (p->watch[i].kq == kq) p->watch[i--] = p->watch[--p->nwatch];
    } else {
        if (!p->used || p->done || p->nwatch == WATCHES) return 0; /* it asks wait4 */
        flags |= EV_ONESHOT;
    }
    if (e) *u = *e, u->filter = EVFILT_USER, u->fflags = 0, u->flags = (unsigned short)flags;
    else *u64 = *e64, u64->filter = EVFILT_USER, u64->fflags = 0, u64->flags = (unsigned short)flags;
    if (!(flags & EV_DELETE)) {
        Watch *w = &p->watch[p->nwatch++];
        w->kq = kq, w->is64 = e == NULL;
        if (e) w->e = *u; else w->e64 = *u64;
    }
    return 1;
}
static int is_tool_proc(uint64_t ident, int16_t filter) { return filter == EVFILT_PROC && is_proc(ident); }
#define FLAGS_COMMA ,
#define KEVENT(name, type, extra_decl, extra, A, B, C, D)                                                                  \
    static int told_##name(type *ev, int n) {                                                                              \
        for (int i = 0; i < n; i++)                                                                                        \
            if (ev[i].filter == EVFILT_USER && is_proc(ev[i].ident)) {                                                     \
                ev[i].filter = EVFILT_PROC;                                                                                \
                ev[i].data = ev[i].flags & EV_ERROR ? ev[i].data : 0;                                                      \
                ev[i].fflags = NOTE_EXIT;                                                                                  \
            }                                                                                                              \
        return n;                                                                                                          \
    }                                                                                                                      \
    static int term_##name(int kq, const type *ch, int nch, type *ev, int nev, extra_decl const struct timespec *t) {      \
        int gone = 0, live = 0;                                                                                            \
        for (int i = 0; i < nch; i++) gone += is_security(ch[i].ident, ch[i].filter), live += is_tool_proc(ch[i].ident, ch[i].filter); \
        if (!gone && !live) return told_##name(ev, name(kq, ch, nch, ev, nev, extra t));                                   \
        if (gone) fprintf(stderr, "henge: term: security: watched\n");                                                     \
        type *rest = calloc((size_t)nch, sizeof *rest);                                                                    \
        int n = 0, out = 0, rc = 0;                                                                                        \
        if (live) pthread_mutex_lock(&procs_lock);                                                                         \
        for (int i = 0; i < nch; i++) {                                                                                    \
            int tool = is_tool_proc(ch[i].ident, ch[i].filter);                                                            \
            if (tool && watch_kevent(kq, A, B, C, D)) { n++; continue; }                                                   \
            if (!tool && !is_security(ch[i].ident, ch[i].filter)) rest[n++] = ch[i];                                       \
            else if (out < nev) ev[out] = ch[i], ev[out].flags = EV_ERROR, ev[out++].data = ESRCH;                         \
            else out = -1;                                                                                                 \
            if (out < 0) break;                                                                                            \
        }                                                                                                                  \
        const struct timespec now = {0, 0};                                                                                \
        /* What it changes is told to the kernel before a tool may end. */                                                 \
        if (n && (live || out)) rc = name(kq, rest, n, out < 0 ? NULL : ev + out, out < 0 ? 0 : nev - out, extra &now);    \
        if (live) pthread_mutex_unlock(&procs_lock);                                                                       \
        free(rest);                                                                                                        \
        if (rc < 0) return rc;                                                                                             \
        if (out < 0) return errno = ESRCH, -1;                                                                             \
        if (out + rc || !nev) return told_##name(ev, out + rc);                                                            \
        return told_##name(ev, name(kq, NULL, 0, ev, nev, extra t));                                                       \
    }
KEVENT(kevent, struct kevent, , , &ch[i], NULL, &rest[n], NULL)
KEVENT(kevent64, struct kevent64_s, unsigned flags FLAGS_COMMA, flags FLAGS_COMMA, NULL, &ch[i], NULL, &rest[n])

/* The loader answers these itself: asked first. */
static void *first_lookup(const char *symbol) {
    void *action = tl_tool_file_action(symbol);
    if (action) return action;
    if (!strcmp(symbol, "_kill")) return (void *)term_kill;
    if (!strcmp(symbol, "_posix_spawn") || !strcmp(symbol, "_posix_spawnp")) return (void *)term_spawn;
    if (!strcmp(symbol, "_wait4")) return (void *)term_wait4;
    if (!strcmp(symbol, "_waitpid")) return (void *)term_waitpid;
    if (!strcmp(symbol, "_open")) return (void *)term_open;
    if (!strcmp(symbol, "_access")) return (void *)term_access;
    if (!strcmp(symbol, "_stat")) return (void *)term_stat;
    if (!strcmp(symbol, "_kevent")) return (void *)term_kevent;
    if (!strcmp(symbol, "_kevent64")) return (void *)term_kevent64;
    return NULL;
}

static void *shim_lookup(const char *install_name, const char *symbol) {
    static const struct { const char *name; void *fn; } table[] = {
        {"_isatty", (void *)term_isatty}, {"_tcgetattr", (void *)term_tcgetattr}, {"_tcsetattr", (void *)term_tcsetattr},
        {"_ioctl", (void *)term_ioctl}, {"_exit", (void *)term_exit}, {"__exit", (void *)term_exit}, {"__Exit", (void *)term_exit},
    };
    if (!strstr(install_name, "libSystem")) return NULL;
    for (size_t i = 0; i < sizeof table / sizeof *table; i++)
        if (!strcmp(symbol, table[i].name)) return table[i].fn;
    return tl_runtime_lookup(symbol);
}

static void log_line(const char *line) { fprintf(stderr, "henge: term: %s\n", line); }

static void *read_thread(void *arg) {
    (void)arg;
    pthread_setname_np("terminal output");
    static char buf[65536];
    for (;;) {
        ssize_t n = read(host_fd, buf, sizeof buf);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        if (on_output) on_output(buf, (size_t)n);
    }
    return NULL;
}

static void *run_thread(void *arg) {
    (void)arg;
    pthread_setname_np("terminal program");
    extern char **environ;
    int code = tl_run_main(image, g_argc, (const char **)g_argv, (const char **)environ);
    fflush(NULL);
    finish(code);
    return NULL;
}

int henge_tl_term_start(const char *path, const char *const *args, const char *const *env, const char *dir, int cols, int rows,
                        HengeTlTermOutput output, HengeTlTermOpen open_url, HengeTlTermExit ended_cb, char *err, size_t errlen) {
    if (henge_tl_ran_program() || atomic_load(&henge_tl_ran))
        return snprintf(err, errlen, "One program per launch: quit Henge (swipe it away) and open it again to run another."), -1;
    int vm = henge_vm_state();
    if (vm == 1 || vm == 3 || vm == 4) return snprintf(err, errlen, "Stop the Mac first: a program and a Mac cannot run at once."), -1;
    HengeDualMapping *jit = henge_ios_jit_get_mapping();
    if (!jit || !jit->rw_addr || !jit->rx_addr) return snprintf(err, errlen, "Turn on JIT first."), -1;
    if (jit->size < (size_t)500 << 20) return snprintf(err, errlen, "The JIT region is too small (Settings > Performance > Code cache: 1 GB or more)."), -1;
    on_output = output, on_open = open_url, on_exit_cb = ended_cb;
    size.ws_col = (unsigned short)cols, size.ws_row = (unsigned short)rows;
    memset(&tio, 0, sizeof tio);
    cfmakeraw(&tio);
    tio.c_iflag |= ICRNL;
    tio.c_oflag |= OPOST | ONLCR;
    tio.c_lflag |= ECHO | ICANON | ISIG | IEXTEN;
    for (int i = 0; env && env[i]; i++) { /* the program may read the process's own environment */
        char *copy = strdup(env[i]), *eq = strchr(copy, '=');
        if (eq) *eq = 0, setenv(copy, eq + 1, 1);
        free(copy);
    }
    if (dir && chdir(dir)) fprintf(stderr, "henge: term: cannot enter %s: %s\n", dir, strerror(errno));
    signal(SIGPIPE, SIG_IGN);
    memset(&host, 0, sizeof host);
    size_t keep = (size_t)16 << 20; /* the region's start stays QEMU's way of finding it; the rest is the program's */
    tl_host_init_region(&host, jit->rx_addr + keep, jit->rw_addr + keep, jit->size - keep);
    host.map_install_name = tl_ios_map_install_name;
    host.shim_lookup = shim_lookup;
    host.first_lookup = first_lookup;
    host.log = log_line;
    host.soft_missing = 1;
    host.no_processes = 1;
    host.ios_kernel_rules = 1;
    host.tool_folders = tool_folders, host.tool_copies = tool_copies;
    atomic_store(&henge_tl_ran, 1); /* from here the JIT region is the program's: no Mac in this launch */
    henge_tl_mark_ran();
    fprintf(stderr, "henge: term: loading %s\n", path);
    image = tl_load(&host, path, err, errlen);
    if (!image) {
        fprintf(stderr, "henge: term: %s\n", err);
        return -1;
    }
    fprintf(stderr, "henge: term: loaded at %p, %zu MB, %zu MB of the region, %d missing symbols\n", tl_image_base(image),
            tl_image_size(image) >> 20, host.used >> 20, tl_missing_count());
    for (int i = 0; i < tl_missing_count() && i < 20; i++) fprintf(stderr, "henge: term: missing %s\n", tl_missing_name(i));
    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv)) return snprintf(err, errlen, "No terminal: %s", strerror(errno)), -1;
    host_fd = sv[0];
    int on = 1;
    setsockopt(host_fd, SOL_SOCKET, SO_NOSIGPIPE, &on, sizeof on);
    fflush(NULL);
    dup2(sv[1], 0); /* standard error stays Henge's log */
    dup2(sv[1], 1);
    close(sv[1]);
    int n = 0;
    while (args && args[n]) n++;
    g_argv = calloc((size_t)n + 2, sizeof *g_argv);
    g_argv[0] = strdup(path);
    for (int i = 0; i < n; i++) g_argv[i + 1] = strdup(args[i]);
    g_argc = n + 1;
    pthread_t reader;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 16u << 20);
    int rc = pthread_create(&reader, NULL, read_thread, NULL) || pthread_create(&main_thread, &attr, run_thread, NULL);
    pthread_attr_destroy(&attr);
    if (rc) return snprintf(err, errlen, "No thread for the program."), -1;
    return 0;
}

void henge_tl_term_write(const char *bytes, size_t len) {
    if (host_fd < 0 || atomic_load(&ended)) return;
    pthread_mutex_lock(&lock);
    int canonical = (tio.c_lflag & ICANON) != 0, echo = (tio.c_lflag & ECHO) != 0;
    pthread_mutex_unlock(&lock);
    if (!canonical) {
        // In one go, so a program reads a key's bytes, or a paste, together.
        for (size_t at = 0; at < len;) {
            ssize_t n = write(host_fd, bytes + at, len - at);
            if (n > 0) at += (size_t)n;
            else if (n < 0 && (errno == EINTR || errno == EAGAIN)) usleep(1000);
            else return;
        }
        return;
    }
    for (size_t i = 0; i < len; i++) {
        char c = bytes[i];
        if (c == '\r') c = '\n';
        if (echo && on_output) {
            if (c == '\n') on_output("\r\n", 2);
            else on_output(&c, 1);
        }
        while (write(host_fd, &c, 1) < 0 && (errno == EINTR || errno == EAGAIN)) usleep(1000);
    }
}

void henge_tl_term_resize(int cols, int rows) {
    pthread_mutex_lock(&lock);
    int changed = size.ws_col != cols || size.ws_row != rows;
    size.ws_col = (unsigned short)cols, size.ws_row = (unsigned short)rows;
    pthread_mutex_unlock(&lock);
    if (changed && host_fd >= 0 && !atomic_load(&ended)) kill(getpid(), SIGWINCH);
}

/* The address space held at launch for a Mac app's allocators (60 of iOS's 64 GB): a tool's file and its threads' stacks
   find no room beside it. Given back at the first tool, when the terminal's program has long made its reservations. */
static void give_room(void) {
    void *try = mmap(NULL, (size_t)512 << 20, PROT_NONE, MAP_ANON | MAP_PRIVATE, -1, 0);
    int before = try != MAP_FAILED;
    if (before) munmap(try, (size_t)512 << 20);
    henge_ios_release_high_room();
    try = mmap(NULL, (size_t)512 << 20, PROT_NONE, MAP_ANON | MAP_PRIVATE, -1, 0);
    int after = try != MAP_FAILED;
    if (after) munmap(try, (size_t)512 << 20);
    fprintf(stderr, "henge: term: room for 512 MB: %s before the holds were released, %s after\n", before ? "yes" : "no", after ? "yes" : "no");
}

int henge_tl_tool_run(const char *path, int argc, const char *const *argv, const char *const *env, const char *cwd,
                      HengeTlToolOutput output, void *context) {
    if (!image) return -1;
    pthread_once(&room_once, give_room);
    return tl_tool_command(path, argc, (const char **)argv, (const char **)env, cwd, output, context);
}

static TlImage *program;
static pthread_mutex_t program_lock = PTHREAD_MUTEX_INITIALIZER;

int henge_tl_program_run(const char *path, const char *argv0, const char *entry, const char *const *env, HengeTlToolOutput output, void *context) {
    if (!image) return -1;
    char err[512] = "";
    pthread_once(&room_once, give_room);
    pthread_mutex_lock(&program_lock);
    TlImage *im = tl_load_replaceable(path, program, err, sizeof err);
    if (im) program = im;
    pthread_mutex_unlock(&program_lock);
    if (!im) {
        output(context, err, strlen(err));
        return -1;
    }
    fprintf(stderr, "henge: term: loaded %s, %zu MB of the region in use\n", path, host.used >> 20);
    const char *argv[] = {argv0, NULL};
    return tl_tool_run_at(im, entry, 1, argv, (const char **)env, output, context);
}

void henge_tl_program_stop(void) { if (program) tl_tool_stop(program); }
void henge_tl_tool_stop(const char *cwd) { tl_tool_stop_in(cwd); }
