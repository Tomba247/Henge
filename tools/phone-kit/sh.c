/* A small POSIX-like shell for Henge's terminal tools (tl/loader/tl_tool.h), where no process can be made: pipelines run
   one part after another through a temporary file, and what is usually in /bin is built in. For build scripts and a
   coding program's commands; no job control. Other names (grep, sed, make) are devtools.py's, beside it. */
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <fnmatch.h>
#include <glob.h>
#include <limits.h>
#include <regex.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <copyfile.h>
#include <ftw.h>

#include <crt_externs.h>
#define environ (*_NSGetEnviron())
typedef struct { int in, out, err; } Io;
static int last, e_flag, x_flag, quitting, loop_go; /* loop_go: 1 break, 2 continue, 3 return */
static char **pos;
static int npos;

typedef struct { char *p; size_t n, cap; } Buf;
static void put(Buf *b, const char *s, size_t n) {
    if (b->n + n + 1 > b->cap) b->p = realloc(b->p, b->cap = (b->n + n + 1) * 2);
    memcpy(b->p + b->n, s, n);
    b->p[b->n += n] = 0;
}
static void putc1(Buf *b, char c) { put(b, &c, 1); }
static void say(int fd, const char *f, ...) {
    va_list a;
    va_start(a, f);
    char *s = NULL;
    int n = vasprintf(&s, f, a);
    va_end(a);
    for (int k = 0; k < n;) {
        ssize_t w = write(fd, s + k, (size_t)(n - k));
        if (w <= 0) break;
        k += (int)w;
    }
    free(s);
}

/* ---- tokens ---- */
typedef struct { char *s; int op; } Tok; /* op: an operator; words keep their quotes */
typedef struct { Tok *t; int n, cap, at; } Toks;
static void add_tok(Toks *t, char *s, int op) {
    if (t->n == t->cap) t->t = realloc(t->t, sizeof(Tok) * (size_t)(t->cap = t->cap * 2 + 64));
    t->t[t->n++] = (Tok){s, op};
}
static const char *skip_brace(const char *p);
static const char *skip_sub(const char *p);
/* A here-document's word after <<: where it ends, the word without quotes, whether its body is taken as it is. */
static const char *here_word(const char *p, char *word, size_t len, int *quoted, int *tabs) {
    size_t n = 0;
    *quoted = 0;
    if ((*tabs = *p == '-')) p++;
    while (*p == ' ' || *p == '\t') p++;
    for (; *p && !strchr(" \t\r\n;&|<>()", *p); p++) {
        if (*p == '\'' || *p == '"') {
            char q = *p++;
            *quoted = 1;
            while (*p && *p != q) { if (n + 1 < len) word[n++] = *p; p++; }
            if (!*p) break;
        } else if (*p == '\\' && p[1]) { *quoted = 1; p++; if (n + 1 < len) word[n++] = *p; }
        else if (n + 1 < len) word[n++] = *p;
    }
    word[n] = 0;
    return p;
}
/* Past its body, which starts at p (a line's start); the body in b when it is wanted. */
static const char *here_body(const char *p, const char *word, int tabs, Buf *b) {
    while (*p) {
        const char *e = strchr(p, '\n');
        size_t n = e ? (size_t)(e - p) : strlen(p);
        const char *l = p;
        size_t k = n;
        if (tabs) while (k && *l == '\t') l++, k--;
        p += n + (e != NULL);
        if (k == strlen(word) && !strncmp(l, word, k)) break;
        if (b) { put(b, l, k); putc1(b, '\n'); }
    }
    return p;
}
static const char *skip_quote(const char *p) { /* at a quote; past its other end */
    char q = *p++;
    for (; *p && *p != q; p++) {
        if (q == '\'') continue;
        if (*p == '\\' && p[1]) p++;
        else if (q == '"' && *p == '$' && p[1] == '{') p = skip_brace(p + 2) - 1;
        else if (q == '"' && *p == '$' && p[1] == '(') p = skip_sub(p + 2) - 1;
    }
    return *p ? p + 1 : p;
}
static const char *skip_brace(const char *p) { /* past the } of ${ */
    while (*p && *p != '}') {
        if (*p == '\\' && p[1]) p += 2;
        else if (*p == '\'' || *p == '"' || *p == '`') p = skip_quote(p);
        else if (*p == '$' && p[1] == '{') p = skip_brace(p + 2);
        else p++;
    }
    return *p ? p + 1 : p;
}
static const char *skip_sub(const char *p) { /* past the ) of $( */
    struct { char word[128]; int tabs; } h[8];
    int d = 1, nh = 0, q;
    while (*p && d) {
        if (*p == '\\' && p[1]) p += 2;
        else if (*p == '\'' || *p == '"' || *p == '`') p = skip_quote(p);
        else if (*p == '$' && p[1] == '{') p = skip_brace(p + 2);
        else if (!strncmp(p, "<<<", 3)) p += 3;
        else if (*p == '<' && p[1] == '<' && nh < 8 && (isalpha((unsigned char)p[2]) || strchr("-'\"\\_ ", p[2]))) { p = here_word(p + 2, h[nh].word, sizeof h[nh].word, &q, &h[nh].tabs); nh++; }
        else if (*p == '\n' && nh) { p++; for (int i = 0; i < nh; i++) p = here_body(p, h[i].word, h[i].tabs, NULL); nh = 0; }
        else { if (*p == '(') d++; else if (*p == ')') d--; p++; }
    }
    return p;
}
static const char *word_end(const char *p) {
    while (*p && !strchr(" \t\r\n;&|<>()", *p)) {
        if (*p == '\\' && p[1]) p += 2;
        else if (*p == '$' && p[1] == '\'') { for (p += 2; *p && *p != '\''; p++) if (*p == '\\' && p[1]) p++; if (*p) p++; }
        else if (*p == '\'' || *p == '"' || *p == '`') p = skip_quote(p);
        else if (*p == '$' && p[1] == '(') p = skip_sub(p + 2);
        else if (*p == '$' && p[1] == '{') p = skip_brace(p + 2);
        else p++;
    }
    return p;
}
static Toks lex(const char *p) {
    static const char *const ops[] = {"&&", "||", ">>", "2>>", "2>&1", "1>&2", ">&2", ">&1", "&>", "&>>", "2>", "1>", ">|", "1>|", ";;", "<<<", "<<", "<", ">", ";", "|", "&", "(", ")", NULL};
    Toks t = {0};
    struct { int at, quoted, tabs; char word[128]; } h[16];
    int nh = 0, in = 0; /* in: inside [[ ]] */
    while (*p) {
        if (*p == '\\' && p[1] == '\n') { p += 2; continue; }
        if (*p == ' ' || *p == '\t' || *p == '\r') { p++; continue; }
        if (*p == '\n') {
            add_tok(&t, strdup("\n"), 1);
            p++;
            for (int i = 0; i < nh; i++) {
                Buf b = {0};
                put(&b, h[i].quoted ? "q" : "e", 1);
                p = here_body(p, h[i].word, h[i].tabs, &b);
                t.t[h[i].at].s = b.p;
            }
            nh = 0;
            continue;
        }
        if (*p == '#') { while (*p && *p != '\n') p++; continue; }
        const char *op = NULL;
        for (int i = 0; ops[i]; i++)
            if (!strncmp(p, ops[i], strlen(ops[i])) && (!op || strlen(ops[i]) > strlen(op))) op = ops[i];
        if (in && op) { add_tok(&t, strdup(!strcmp(op, "&&") ? "-a" : !strcmp(op, "||") ? "-o" : op), 0); p += strlen(op); continue; }
        if (op) {
            add_tok(&t, strdup(op), 1);
            p += strlen(op);
            if (!strcmp(op, "<<") && nh < 16) {
                p = here_word(p, h[nh].word, sizeof h[nh].word, &h[nh].quoted, &h[nh].tabs);
                add_tok(&t, strdup("q"), 0);
                h[nh++].at = t.n - 1;
            }
            continue;
        }
        const char *e = word_end(p);
        if (in && t.n && !strcmp(t.t[t.n - 1].s, "=~")) for (e = p; *e && !strchr(" \t\r\n", *e);) { if (*e == '\\' && e[1]) e += 2; else if (*e == '\'' || *e == '"') e = skip_quote(e); else e++; }
        if (e == p) e++;
        add_tok(&t, strndup(p, (size_t)(e - p)), 0);
        p = e;
        const char *w = t.t[t.n - 1].s;
        int first = t.n < 2 || t.t[t.n - 2].op || !strcmp(t.t[t.n - 2].s, "!") || !strcmp(t.t[t.n - 2].s, "if") || !strcmp(t.t[t.n - 2].s, "while") || !strcmp(t.t[t.n - 2].s, "then") ||
                    !strcmp(t.t[t.n - 2].s, "do") || !strcmp(t.t[t.n - 2].s, "{") || !strcmp(t.t[t.n - 2].s, "elif") || !strcmp(t.t[t.n - 2].s, "until") || !strcmp(t.t[t.n - 2].s, "else");
        if (!strcmp(w, "[[") && first) in = 1;
        else if (!strcmp(w, "]]")) in = 0;
    }
    return t;
}

/* ---- expansion ---- */
static int run_text(const char *text, Io io);
static void capture(const char *cmd, Buf *b) {
    char name[] = "/sh.XXXXXX", path[PATH_MAX];
    const char *tmp = getenv("TMPDIR");
    snprintf(path, sizeof path, "%s%s", tmp ? tmp : "/tmp", name);
    int fd = mkstemp(path);
    if (fd < 0) return;
    unlink(path);
    int was = quitting;
    last = run_text(cmd, (Io){0, fd, 2});
    quitting = was;
    lseek(fd, 0, SEEK_SET);
    char c[4096];
    ssize_t n;
    while ((n = read(fd, c, sizeof c)) > 0) put(b, c, (size_t)n);
    close(fd);
    while (b->n && b->p[b->n - 1] == '\n') b->p[--b->n] = 0;
}
static const char *var(const char *name, char *num) {
    if (!strcmp(name, "?")) { sprintf(num, "%d", last); return num; }
    if (!strcmp(name, "#")) { sprintf(num, "%d", npos > 0 ? npos - 1 : 0); return num; }
    if (!strcmp(name, "$")) return "1";
    if (!strcmp(name, "!")) return "";
    if (!strcmp(name, "RANDOM")) { sprintf(num, "%d", (int)(arc4random() & 32767)); return num; }
    if (isdigit((unsigned char)name[0])) { int i = atoi(name); return i < npos ? pos[i] : ""; }
    return getenv(name);
}
typedef struct { char **v; int n, cap; } Words;
static void add_word(Words *w, char *s) {
    if (w->n + 2 > w->cap) w->v = realloc(w->v, sizeof(char *) * (size_t)(w->cap = w->cap * 2 + 16));
    w->v[w->n++] = s;
    w->v[w->n] = NULL;
}
/* cur: the word so far; a glob character inside quotes is kept as \\x for glob(). */
typedef struct { Buf cur; int any, magic; } Ex;
static char *plain(const char *s) {
    Buf b = {0};
    put(&b, "", 0);
    for (; *s; s++) { if (*s == '\\' && s[1]) s++; putc1(&b, *s); }
    return b.p;
}
static void flush(Words *w, Ex *x) {
    if (x->any) {
        glob_t g;
        if (x->magic && !glob(x->cur.p, 0, NULL, &g)) {
            for (size_t i = 0; i < g.gl_pathc; i++) add_word(w, strdup(g.gl_pathv[i]));
            globfree(&g);
        } else add_word(w, plain(x->cur.p ? x->cur.p : ""));
    }
    free(x->cur.p);
    *x = (Ex){0};
}
static void lit(Ex *x, char c, int quoted) {
    x->any = 1;
    if (quoted ? strchr("*?[]\\", c) != NULL : c == '\\') putc1(&x->cur, '\\');
    if (!quoted && strchr("*?[", c)) x->magic = 1;
    putc1(&x->cur, c);
}
static void value(Words *w, Ex *x, const char *s, int quoted, int split) {
    if (quoted) x->any = 1;
    for (; s && *s; s++) {
        if (!quoted && split && strchr(" \t\n", *s)) { flush(w, x); continue; }
        lit(x, *s, quoted);
    }
}
static char *expand1(const char *p);
static char *expand_pattern(const char *p);
/* $(( )) */
static const char *ap;
static long arith(int min);
static void arith_space(void) { while (*ap && isspace((unsigned char)*ap)) ap++; }
static long arith_one(void) {
    arith_space();
    if (*ap == '(') { ap++; long v = arith(0); arith_space(); if (*ap == ')') ap++; return v; }
    if (*ap == '-') { ap++; return -arith_one(); }
    if (*ap == '+') { ap++; return arith_one(); }
    if (*ap == '!') { ap++; return !arith_one(); }
    if (*ap == '~') { ap++; return ~arith_one(); }
    if (isdigit((unsigned char)*ap)) { char *e; long v = strtol(ap, &e, 0); ap = e; return v; }
    if (*ap == '$') ap++;
    char name[128];
    size_t n = 0;
    while ((isalnum((unsigned char)*ap) || *ap == '_') && n < 127) name[n++] = *ap++;
    name[n] = 0;
    if (!n) { if (*ap) ap++; return 0; }
    char num[32];
    const char *v = var(name, num);
    long now = v ? strtol(v, NULL, 0) : 0, to = now;
    arith_space();
    if (!strncmp(ap, "++", 2)) { ap += 2; to = now + 1; }
    else if (!strncmp(ap, "--", 2)) { ap += 2; to = now - 1; }
    else if (*ap == '=' && ap[1] != '=') { ap++; now = to = arith(0); snprintf(num, sizeof num, "%ld", to); setenv(name, num, 1); return now; }
    else if (*ap && strchr("+-*/%", *ap) && ap[1] == '=') {
        char o = *ap;
        ap += 2;
        long x = arith(0);
        now = to = o == '+' ? now + x : o == '-' ? now - x : o == '*' ? now * x : !x ? 0 : o == '/' ? now / x : now % x;
        snprintf(num, sizeof num, "%ld", to);
        setenv(name, num, 1);
        return now;
    }
    if (to != now) { snprintf(num, sizeof num, "%ld", to); setenv(name, num, 1); }
    return now;
}
static long arith(int min) {
    static const struct { const char *s; int p; } o[] = {{"||", 1}, {"&&", 2}, {"==", 6}, {"!=", 6}, {"<=", 7}, {">=", 7}, {"<<", 8}, {">>", 8}, {"|", 3}, {"^", 4}, {"&", 5}, {"<", 7}, {">", 7}, {"+", 9}, {"-", 9}, {"*", 10}, {"/", 10}, {"%", 10}};
    long v = arith_one();
    for (;;) {
        arith_space();
        int k = -1;
        for (int i = 0; i < (int)(sizeof o / sizeof *o) && k < 0; i++) if (!strncmp(ap, o[i].s, strlen(o[i].s))) k = i;
        if (k < 0 || o[k].p <= min) {
            if (min == 0 && *ap == '?') { ap++; long a = arith(0); arith_space(); if (*ap == ':') ap++; long b = arith(0); return v ? a : b; }
            if (min == 0 && *ap == ',') { ap++; v = arith(0); continue; }
            return v;
        }
        ap += strlen(o[k].s);
        long x = arith(o[k].p);
        switch (k) {
            case 0: v = v || x; break; case 1: v = v && x; break; case 2: v = v == x; break; case 3: v = v != x; break;
            case 4: v = v <= x; break; case 5: v = v >= x; break; case 6: v = v << x; break; case 7: v = v >> x; break;
            case 8: v = v | x; break; case 9: v = v ^ x; break; case 10: v = v & x; break; case 11: v = v < x; break; case 12: v = v > x; break;
            case 13: v += x; break; case 14: v -= x; break; case 15: v *= x; break; case 16: v = x ? v / x : 0; break; default: v = x ? v % x : 0;
        }
    }
}
static long arith_text(const char *t) {
    char *e = expand1(t);
    const char *was = ap;
    ap = e;
    long v = arith(0);
    ap = was;
    free(e);
    return v;
}
/* Where pattern matches in s: n bytes, the shortest or the longest; -1: not. at: from its start, 2: to its end. */
static long match_len(const char *pat, const char *s, int end, int longest) {
    size_t l = strlen(s);
    for (size_t i = 0; i <= l; i++) {
        size_t n = longest ? l - i : i;
        char *part = end ? strdup(s + l - n) : strndup(s, n);
        int ok = !fnmatch(pat, part, 0);
        free(part);
        if (ok) return (long)n;
    }
    return -1;
}
static const char *dollar(const char *p, Words *w, Ex *x, int quoted, int split) { /* after $ */
    char num[32];
    if (*p == '(' && p[1] == '(') {
        const char *e = skip_sub(p + 1);
        if (e[-1] == ')' && e - p >= 4 && e[-2] == ')') {
            char *t = strndup(p + 2, (size_t)(e - p - 4));
            snprintf(num, sizeof num, "%ld", arith_text(t));
            free(t);
            value(w, x, num, quoted, split);
            return e;
        }
    }
    if (*p == '(') {
        const char *e = skip_sub(p + 1);
        char *cmd = strndup(p + 1, (size_t)(e - p - 2));
        Buf b = {0};
        capture(cmd, &b);
        value(w, x, b.p, quoted, split);
        free(cmd), free(b.p);
        return e;
    }
    char name[256] = "", *alt = NULL;
    int kind = 0, colon = 0, twice = 0, length = 0;
    if (*p == '{') {
        const char *e = skip_brace(p + 1) - 1;
        if (*e != '}') return p + strlen(p);
        size_t n = 0;
        p++;
        if (*p == '#' && p + 1 < e && p[1] != '}') { length = 1; p++; }
        for (; p < e && (isalnum((unsigned char)*p) || *p == '_' || (!n && strchr("?#@*!$", *p))) && n < 255;) { name[n++] = *p++; if (strchr("?#@*!$", name[0])) break; }
        name[n] = 0;
        if (p < e) {
            if (*p == ':' ) { colon = 1; p++; if (!strchr("-=+?", *p)) kind = ':'; else kind = *p++; }
            else kind = *p++;
            if ((kind == '#' || kind == '%' || kind == '/') && *p == kind) { twice = 1; p++; }
            alt = strndup(p, (size_t)(e - p));
        }
        p = e + 1;
    } else if (strchr("?#@*$!", *p) && *p) { name[0] = *p++; }
    else if (isdigit((unsigned char)*p)) { name[0] = *p++; }
    else {
        size_t n = 0;
        while ((isalnum((unsigned char)*p) || *p == '_') && n < 255) name[n++] = *p++;
        name[n] = 0;
        if (!n) { lit(x, '$', quoted); return p; }
    }
    if ((!strcmp(name, "@") || !strcmp(name, "*")) && !length && (!kind || (kind == '+' && npos > 1))) {
        for (int i = 1; i < npos; i++) {
            if (i > 1) { if (quoted && name[0] == '*') lit(x, ' ', 1); else flush(w, x); }
            value(w, x, pos[i], quoted || kind, split && !kind);
        }
        free(alt);
        return p;
    }
    const char *v = !strcmp(name, "@") || !strcmp(name, "*") ? (npos > 1 ? pos[1] : NULL) : var(name, num);
    if (length) { char t[32]; snprintf(t, sizeof t, "%zu", !strcmp(name, "@") || !strcmp(name, "*") ? (size_t)(npos > 0 ? npos - 1 : 0) : strlen(v ? v : "")); value(w, x, t, quoted, split); free(alt); return p; }
    int set = v && (*v || !colon);
    if (kind == '-' || kind == '=') {
        if (!set) { v = expand1(alt); if (kind == '=') setenv(name, v, 1); }
    } else if (kind == '+') v = set ? expand1(alt) : "";
    else if (kind == '?' && !set) { say(2, "sh: %s: %s\n", name, *alt ? alt : "not set"); last = 1; quitting = 1; }
    else if (kind == ':' && v) {
        char *two = strchr(alt, ':');
        if (two) *two++ = 0;
        long l = (long)strlen(v), from = arith_text(alt), len = two ? arith_text(two) : l;
        if (from < 0) from = l + from > 0 ? l + from : 0;
        if (from > l) from = l;
        if (len < 0) len = l - from + len;
        if (len < 0) len = 0;
        v = strndup(v + from, (size_t)len);
    } else if ((kind == '#' || kind == '%') && v) {
        char *pat = expand_pattern(alt);
        long n = match_len(pat, v, kind == '%', twice);
        if (n >= 0) v = kind == '#' ? strdup(v + n) : strndup(v, strlen(v) - (size_t)n);
        free(pat);
    } else if (kind == '/' && v) {
        const char *sl = alt;
        while (*sl && *sl != '/') { if (*sl == '\\' && sl[1]) sl += 2; else if (*sl == '\'' || *sl == '"') sl = skip_quote(sl); else if (*sl == '$' && sl[1] == '{') sl = skip_brace(sl + 2); else sl++; }
        char *raw = strndup(alt, (size_t)(sl - alt)), *pat = expand_pattern(raw), *with = *sl ? expand1(sl + 1) : strdup("");
        Buf b = {0};
        put(&b, "", 0);
        for (const char *q = v; *q;) {
            long n = -1;
            for (size_t k = strlen(q); k > 0 && n < 0; k--) { char *part = strndup(q, k); if (!fnmatch(pat, part, 0)) n = (long)k; free(part); }
            if (n > 0) { put(&b, with, strlen(with)); q += n; if (!twice) { put(&b, q, strlen(q)); break; } }
            else putc1(&b, *q++);
        }
        free(raw), free(pat), free(with);
        v = b.p;
    }
    value(w, x, v, quoted, split);
    free(alt);
    return p;
}
static void expand(const char *p, Words *w, int split) {
    Ex x = {0};
    if (*p == '~' && (!p[1] || p[1] == '/')) { value(w, &x, getenv("HOME"), 1, 0); p++; }
    while (*p) {
        if (*p == '\\' && p[1]) { if (p[1] != '\n') lit(&x, p[1], 1); p += 2; }
        else if (*p == '\'') { x.any = 1; for (p++; *p && *p != '\''; p++) lit(&x, *p, 1); if (*p) p++; }
        else if (*p == '"') {
            x.any = 1;
            for (p++; *p && *p != '"';) {
                if (*p == '\\' && p[1] && strchr("\"\\$`\n", p[1])) { if (p[1] != '\n') lit(&x, p[1], 1); p += 2; }
                else if (*p == '$') p = dollar(p + 1, w, &x, 1, 0);
                else lit(&x, *p++, 1);
            }
            if (*p) p++;
        }
        else if (*p == '`') {
            const char *e = strchr(p + 1, '`');
            if (!e) break;
            char *cmd = strndup(p + 1, (size_t)(e - p - 1));
            Buf b = {0};
            capture(cmd, &b);
            value(w, &x, b.p, 0, split);
            free(cmd), free(b.p);
            p = e + 1;
        }
        else if (*p == '$' && p[1] == '\'') {
            x.any = 1;
            for (p += 2; *p && *p != '\''; p++) {
                if (*p != '\\' || !p[1]) { lit(&x, *p, 1); continue; }
                static const char from[] = "ntrabfve0", to[] = "\n\t\r\a\b\f\v\033";
                p++;
                const char *k = strchr(from, *p);
                if (*p == 'x') { char h[3] = {0}; for (int i = 0; i < 2 && isxdigit((unsigned char)p[1]); i++) h[i] = *++p; lit(&x, (char)strtol(h, NULL, 16), 1); }
                else lit(&x, k ? to[k - from] : *p, 1);
            }
            if (*p) p++;
        }
        else if (*p == '$') p = dollar(p + 1, w, &x, 0, split);
        else lit(&x, *p++, 0);
    }
    if (!split) x.magic = 0;
    flush(w, &x);
}
static char *expand1(const char *p) {
    Words w = {0};
    expand(p, &w, 0);
    return w.n ? w.v[0] : strdup("");
}

/* As a pattern for fnmatch: what is quoted stands for itself. */
static char *expand_pattern(const char *p) {
    /* expand() keeps a quoted magic character as \\x until glob(): the same form fnmatch wants. */
    Words w = {0};
    Ex x = {0};
    while (*p) {
        if (*p == '\\' && p[1]) { lit(&x, p[1], 1); p += 2; }
        else if (*p == '\'') { for (p++; *p && *p != '\''; p++) lit(&x, *p, 1); if (*p) p++; }
        else if (*p == '"') {
            for (p++; *p && *p != '"';) {
                if (*p == '\\' && p[1] && strchr("\"\\$`", p[1])) { lit(&x, p[1], 1); p += 2; }
                else if (*p == '$') p = dollar(p + 1, &w, &x, 1, 0);
                else lit(&x, *p++, 1);
            }
            if (*p) p++;
        }
        else if (*p == '$') p = dollar(p + 1, &w, &x, 0, 0);
        else lit(&x, *p++, 0);
    }
    return x.cur.p ? x.cur.p : strdup("");
}
static char *here_text(const char *p) {
    Words w = {0};
    Ex x = {0};
    while (*p) {
        if (*p == '\\' && p[1] && strchr("\\$`\n", p[1])) { if (p[1] != '\n') lit(&x, p[1], 1); p += 2; }
        else if (*p == '$') p = dollar(p + 1, &w, &x, 1, 0);
        else lit(&x, *p++, 1);
    }
    return plain(x.cur.p ? x.cur.p : "");
}

/* ---- built in ---- */
static int fail(Io io, const char *what, const char *who) { say(io.err, "%s: %s: %s\n", what, who, strerror(errno)); return 1; }
static int rm_one(const char *p, const struct stat *s, int f, struct FTW *w) { (void)s, (void)f, (void)w; return remove(p); }
static int mkdirs(char *p) {
    for (char *q = p + 1; *q; q++)
        if (*q == '/') { *q = 0; if (mkdir(p, 0777) && errno != EEXIST) { *q = '/'; return -1; } *q = '/'; }
    return mkdir(p, 0777) && errno != EEXIST ? -1 : 0;
}
static int cat_fd(int from, int to) {
    char b[65536];
    ssize_t n;
    while ((n = read(from, b, sizeof b)) > 0)
        for (ssize_t k = 0; k < n;) { ssize_t w = write(to, b + k, (size_t)(n - k)); if (w <= 0) return 1; k += w; }
    return 0;
}
static int test_one(int n, char **v) {
    struct stat s;
    if (n == 0) return 1;
    if (!strcmp(v[0], "!")) return !test_one(n - 1, v + 1);
    for (int i = 1; i < n; i++) {
        if (!strcmp(v[i], "-a")) return test_one(i, v) || test_one(n - i - 1, v + i + 1);
        if (!strcmp(v[i], "-o")) return test_one(i, v) && test_one(n - i - 1, v + i + 1);
    }
    if (n >= 3 && !strcmp(v[0], "(") && !strcmp(v[n - 1], ")")) return test_one(n - 2, v + 1);
    if (n == 1) return !*v[0];
    if (n == 2) {
        if (!strcmp(v[0], "-z")) return *v[1] != 0;
        if (!strcmp(v[0], "-n")) return *v[1] == 0;
        if (!strcmp(v[0], "-L") || !strcmp(v[0], "-h")) return lstat(v[1], &s) || !S_ISLNK(s.st_mode);
        if (!strcmp(v[0], "-t")) return 1;
        if (!strcmp(v[0], "-v")) return getenv(v[1]) == NULL;
        if (stat(v[1], &s)) return 1;
        if (!strcmp(v[0], "-x")) return !(s.st_mode & 0111);
        if (!strcmp(v[0], "-p")) return !S_ISFIFO(s.st_mode);
        if (!strcmp(v[0], "-f")) return !S_ISREG(s.st_mode);
        if (!strcmp(v[0], "-d")) return !S_ISDIR(s.st_mode);
        if (!strcmp(v[0], "-s")) return s.st_size == 0;
        return 0;
    }
    if (!strcmp(v[1], "=") || !strcmp(v[1], "==")) return strcmp(v[0], v[2]) != 0;
    if (!strcmp(v[1], "!=")) return strcmp(v[0], v[2]) == 0;
    if (!strcmp(v[1], "<")) return strcmp(v[0], v[2]) >= 0;
    if (!strcmp(v[1], ">")) return strcmp(v[0], v[2]) <= 0;
    if (!strcmp(v[1], "-nt") || !strcmp(v[1], "-ot")) {
        struct stat q;
        int x = stat(v[0], &s), y = stat(v[2], &q);
        if (v[1][1] == 'o') return x ? y != 0 : y || s.st_mtimespec.tv_sec >= q.st_mtimespec.tv_sec;
        return y ? x != 0 : x || s.st_mtimespec.tv_sec <= q.st_mtimespec.tv_sec;
    }
    long a = atol(v[0]), b = atol(v[2]);
    const char *o = v[1];
    return !(!strcmp(o, "-eq") ? a == b : !strcmp(o, "-ne") ? a != b : !strcmp(o, "-lt") ? a < b : !strcmp(o, "-le") ? a <= b : !strcmp(o, "-gt") ? a > b : a >= b);
}
static int by_name(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }
static int has(int n, char **v, char c) {
    for (int i = 1; i < n; i++) if (v[i][0] == '-' && v[i][1] && v[i][1] != '-' && strchr(v[i], c)) return 1;
    return 0;
}
static void ls_dir(const char *d, int all, int lng, int rec, Io io) {
    DIR *h = opendir(d);
    if (!h) return;
    Words w = {0};
    for (struct dirent *e; (e = readdir(h));) if (all ? strcmp(e->d_name, ".") && strcmp(e->d_name, "..") : e->d_name[0] != '.') add_word(&w, strdup(e->d_name));
    closedir(h);
    if (w.n) qsort(w.v, (size_t)w.n, sizeof *w.v, by_name);
    for (int i = 0; i < w.n; i++) {
        char p[PATH_MAX];
        struct stat s = {0};
        snprintf(p, sizeof p, "%s/%s", d, w.v[i]);
        stat(p, &s);
        if (lng) say(io.out, "%c %12lld  ", S_ISDIR(s.st_mode) ? 'd' : '-', (long long)s.st_size);
        say(io.out, "%s%s\n", rec ? p : w.v[i], S_ISDIR(s.st_mode) && lng ? "/" : "");
        if (rec && S_ISDIR(s.st_mode)) ls_dir(p, all, lng, rec, io);
    }
}
/* Functions: where each one's body is. */
typedef struct Fn { char *name; Toks *t; int at; struct Fn *next; } Fn;
static Fn *fns;
static Fn *is_function(const char *name) { for (Fn *f = fns; f; f = f->next) if (f->t && !strcmp(f->name, name)) return f; return NULL; }
/* A tool of the toolchain, or devtools.py for a name it answers. */
static int tool_path(const char *name, char *out, size_t len) {
    const char *t = getenv("HENGE_TOOLS");
    if (!t || strchr(name, '/')) return 0;
    snprintf(out, len, "%s/bin/%s", t, name);
    if (!access(out, F_OK)) return 1;
    static const char *const here[] = {"echo", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "ls", "ln", "touch", "pwd", "head", "tail", "wc", "chmod", "printf", "basename", "dirname", "sleep", "uname", "env", "test", "cd", "eval", "read", "export", NULL};
    int dev = 0;
    for (int i = 0; here[i]; i++) if (!strcmp(here[i], name)) { snprintf(out, len, "%s", name); return 1; }
    snprintf(out, len, "%s/bin/devtools.names", t);
    FILE *f = fopen(out, "r");
    char line[128];
    while (f && !dev && fgets(line, sizeof line, f)) { line[strcspn(line, "\r\n")] = 0; dev = !strcmp(line, name); }
    if (f) fclose(f);
    snprintf(out, len, "%s/bin/devtools.py", t);
    return dev ? 2 : 0;
}
/* 0: not built in. */
static int builtin(int n, char **v, Io io, int *rc) {
    const char *c = v[0];
    if (strrchr(c, '/') && (!strncmp(c, "/bin/", 5) || !strncmp(c, "/usr/bin/", 9))) c = strrchr(c, '/') + 1;
    *rc = 0;
#define IS(x) (!strcmp(c, x))
    if (IS(":") || IS("true") || IS("umask") || IS("ulimit") || IS("trap") || IS("wait") || IS("sync") || IS("shopt") || IS("setopt") || IS("alias") || IS("hash") || IS("disown") || IS("bind") || IS("complete") || IS("stty")) return 1;
    if (IS("unalias")) return *rc = 1;
    if (IS("false")) return *rc = 1;
    if (IS("exit")) { quitting = 1; *rc = n > 1 ? atoi(v[1]) : last; return 1; }
    if (IS("return")) { loop_go = 3; *rc = n > 1 ? atoi(v[1]) : last; return 1; }
    if (IS("let")) { long x = 0; for (int i = 1; i < n; i++) x = arith_text(v[i]); *rc = !x; return 1; }
    if (IS("eval")) {
        Buf b = {0};
        put(&b, "", 0);
        for (int i = 1; i < n; i++) { if (i > 1) putc1(&b, ' '); put(&b, v[i], strlen(v[i])); }
        *rc = run_text(b.p, io);
        return 1;
    }
    if (IS("read")) {
        Buf b = {0};
        put(&b, "", 0);
        char ch;
        ssize_t k = 0;
        int raw = has(n, v, 'r');
        while ((k = read(io.in, &ch, 1)) == 1 && ch != '\n') { if (ch == '\\' && !raw) { if (read(io.in, &ch, 1) != 1 || ch == '\n') continue; } putc1(&b, ch); }
        int i = 1;
        while (i < n && v[i][0] == '-') i++;
        const char *ifs = getenv("IFS");
        char *q = b.p;
        if (!ifs) ifs = " \t\n";
        for (; i < n; i++) {
            while (*q && strchr(ifs, *q) && isspace((unsigned char)*q)) q++;
            char *e = q;
            if (i + 1 < n) while (*e && !strchr(ifs, *e)) e++;
            else { e = q + strlen(q); while (e > q && strchr(ifs, e[-1]) && isspace((unsigned char)e[-1])) e--; }
            char c2 = *e;
            *e = 0;
            setenv(v[i], q, 1);
            q = c2 ? e + 1 : e;
        }
        *rc = k != 1;
        free(b.p);
        return 1;
    }
    if (IS("break")) { loop_go = 1; return 1; }
    if (IS("continue")) { loop_go = 2; return 1; }
    if (IS("cd")) { const char *d = n > 1 ? v[1] : getenv("HOME"); if (chdir(d ? d : "/")) *rc = fail(io, "cd", d); else { char p[PATH_MAX]; if (getcwd(p, sizeof p)) setenv("PWD", p, 1); } return 1; }
    if (IS("pwd")) { char p[PATH_MAX]; say(io.out, "%s\n", getcwd(p, sizeof p)); return 1; }
    if (IS("echo")) {
        int i = 1, nl = 1;
        int esc = 0;
        for (; i < n && v[i][0] == '-' && v[i][1] && strspn(v[i] + 1, "neE") == strlen(v[i] + 1); i++) { if (strchr(v[i], 'n')) nl = 0; if (strchr(v[i], 'e')) esc = 1; }
        Buf b = {0};
        put(&b, "", 0);
        for (; i < n; i++) { put(&b, v[i], strlen(v[i])); if (i + 1 < n) putc1(&b, ' '); }
        if (esc) { char *o = b.p; for (char *q = b.p; *q; q++) { static const char from[] = "ntrabfve\\", to[] = "\n\t\r\a\b\f\v\033\\"; const char *k = q[0] == '\\' && q[1] ? strchr(from, q[1]) : NULL; if (k) { *o++ = to[k - from]; q++; } else *o++ = *q; } *o = 0; b.n = strlen(b.p); }
        if (nl) putc1(&b, '\n');
        say(io.out, "%s", b.p);
        free(b.p);
        return 1;
    }
    if (IS("printf")) {
        int a = 2;
        do {
            for (const char *p = n > 1 ? v[1] : ""; *p; p++) {
                if (*p == '\\' && p[1]) { p++; say(io.out, "%c", *p == 'n' ? '\n' : *p == 't' ? '\t' : *p); }
                else if (*p == '%' && p[1] == '%') { p++; say(io.out, "%%"); }
                else if (*p == '%') {
                    char f[32] = "%";
                    size_t k = 1;
                    for (p++; *p && !isalpha((unsigned char)*p) && k < 20; p++) f[k++] = *p;
                    const char *x = a < n ? v[a++] : "";
                    if (strchr("dixXuo", *p)) { f[k++] = 'l'; f[k++] = *p; f[k] = 0; say(io.out, f, strtol(x, NULL, 0)); }
                    else { f[k++] = 's'; f[k] = 0; say(io.out, f, x); }
                } else say(io.out, "%c", *p);
            }
        } while (a < n && a > 2);
        return 1;
    }
    if (IS("export") || IS("readonly") || IS("local") || IS("declare") || IS("typeset")) { for (int i = 1; i < n; i++) { char *e = strchr(v[i], '='); if (e) { *e = 0; setenv(v[i], e + 1, 1); } } return 1; }
    if (IS("unset")) { for (int i = 1; i < n; i++) unsetenv(v[i]); return 1; }
    if (IS("env") && n == 1) { for (char **e = environ; *e; e++) say(io.out, "%s\n", *e); return 1; }
    if (IS("set")) { for (int i = 1; i < n; i++) if (v[i][0] == '-' || v[i][0] == '+') { if (strchr(v[i], 'e')) e_flag = v[i][0] == '-'; if (strchr(v[i], 'x')) x_flag = v[i][0] == '-'; } return 1; }
    if (IS("shift")) { if (npos > 1) { pos++, npos--; } return 1; }
    if (IS("test")) { *rc = test_one(n - 1, v + 1); return 1; }
    if (IS("[[")) {
        /* == takes a pattern and =~ a regular expression; -a and -o stand for && and ||. */
        int res = 0, op = 0, not_ = 0, at = 1;
        for (int i = 1; i <= n - 1; i++) {
            int end = i == n - 1;
            if (!end && strcmp(v[i], "-a") && strcmp(v[i], "-o")) continue;
            int from = at, k;
            not_ = 0;
            while (from < i && !strcmp(v[from], "!")) not_ = !not_, from++;
            k = i - from;
            int x;
            if (k == 3 && (!strcmp(v[from + 1], "==") || !strcmp(v[from + 1], "=") || !strcmp(v[from + 1], "!="))) x = (fnmatch(v[from + 2], v[from], 0) != 0) ^ (v[from + 1][0] == '!');
            else if (k == 3 && !strcmp(v[from + 1], "=~")) { regex_t re; x = regcomp(&re, v[from + 2], REG_EXTENDED | REG_NOSUB) ? 2 : (regexec(&re, v[from], 0, NULL, 0) != 0); }
            else x = test_one(k, v + from);
            if (not_) x = !x;
            res = op == 0 ? x : op == 1 ? res || x : res && x;
            if (!end) op = !strcmp(v[i], "-a") ? 1 : 2;
            at = i + 1;
        }
        *rc = res;
        return 1;
    }
    if (IS("[")) { *rc = test_one(n - 2, v + 1); return 1; }
    if (IS("sleep")) { usleep((useconds_t)(atof(n > 1 ? v[1] : "0") * 1e6)); return 1; }
    if (IS("uname")) { say(io.out, has(n, v, 'm') ? "arm64\n" : "Darwin\n"); return 1; }
    if (IS("nproc")) { say(io.out, "2\n"); return 1; }
    if (IS("mktemp")) { char p[PATH_MAX]; snprintf(p, sizeof p, "%s/tmp.XXXXXX", getenv("TMPDIR") ? getenv("TMPDIR") : "/tmp"); if (has(n, v, 'd')) mkdtemp(p); else { int fd = mkstemp(p); if (fd >= 0) close(fd); } say(io.out, "%s\n", p); return 1; }
    if (IS("basename") && n > 1) { char *b = strrchr(v[1], '/'); b = b ? b + 1 : v[1]; if (n > 2 && strlen(b) > strlen(v[2]) && !strcmp(b + strlen(b) - strlen(v[2]), v[2])) b[strlen(b) - strlen(v[2])] = 0; say(io.out, "%s\n", b); return 1; }
    if (IS("dirname") && n > 1) { char *b = strrchr(v[1], '/'); if (b) *b = 0; say(io.out, "%s\n", b ? (*v[1] ? v[1] : "/") : "."); return 1; }
    if (IS("mkdir")) { for (int i = 1; i < n; i++) if (v[i][0] != '-' && (has(n, v, 'p') ? mkdirs(v[i]) : mkdir(v[i], 0777))) *rc = fail(io, c, v[i]); return 1; }
    if (IS("rmdir")) { for (int i = 1; i < n; i++) if (v[i][0] != '-' && rmdir(v[i])) *rc = fail(io, c, v[i]); return 1; }
    if (IS("touch")) { for (int i = 1; i < n; i++) { int fd = open(v[i], O_CREAT | O_WRONLY, 0666); if (fd < 0) *rc = fail(io, c, v[i]); else { futimens(fd, NULL); close(fd); } } return 1; }
    if (IS("chmod")) { for (int i = 2; i < n; i++) { struct stat s; if (stat(v[i], &s)) { *rc = fail(io, c, v[i]); continue; } chmod(v[i], isdigit((unsigned char)v[1][0]) ? (mode_t)strtol(v[1], NULL, 8) : strchr(v[1], 'x') ? s.st_mode | 0111 : s.st_mode); } return 1; }
    if (IS("rm")) {
        int r = has(n, v, 'r') || has(n, v, 'R'), f = has(n, v, 'f');
        for (int i = 1; i < n; i++) {
            if (v[i][0] == '-') continue;
            struct stat s;
            if (lstat(v[i], &s)) { if (!f) *rc = fail(io, c, v[i]); }
            else if (S_ISDIR(s.st_mode) ? !r || nftw(v[i], rm_one, 16, FTW_DEPTH | FTW_PHYS) : unlink(v[i])) *rc = fail(io, c, v[i]);
        }
        return 1;
    }
    if (IS("cp") || IS("mv") || IS("ln")) {
        int first = 1, k = 0;
        char *files[4096];
        for (int i = first; i < n && k < 4096; i++) if (v[i][0] != '-' || !v[i][1]) files[k++] = v[i];
        if (k < 2) { say(io.err, "%s: needs a source and a destination\n", c); return *rc = 1; }
        struct stat s;
        int dir = !stat(files[k - 1], &s) && S_ISDIR(s.st_mode);
        for (int i = 0; i < k - 1; i++) {
            char to[PATH_MAX], src[PATH_MAX];
            snprintf(src, sizeof src, "%s", files[i]);
            while (strlen(src) > 1 && src[strlen(src) - 1] == '/') src[strlen(src) - 1] = 0;
            char *b = strrchr(src, '/');
            if (dir) snprintf(to, sizeof to, "%s/%s", files[k - 1], b ? b + 1 : src);
            else snprintf(to, sizeof to, "%s", files[k - 1]);
            int bad;
            if (IS("ln")) { if (has(n, v, 'f')) unlink(to); bad = has(n, v, 's') ? symlink(src, to) : link(src, to); }
            else if (IS("mv") && !rename(src, to)) bad = 0;
            else {
                struct stat f;
                bad = stat(src, &f);
                if (!bad && S_ISDIR(f.st_mode) && !has(n, v, 'r') && !has(n, v, 'R') && !has(n, v, 'a') && !IS("mv")) { errno = EISDIR; bad = 1; }
                if (!bad) bad = copyfile(src, to, NULL, (S_ISDIR(f.st_mode) ? COPYFILE_RECURSIVE : 0) | COPYFILE_DATA | COPYFILE_STAT) < 0;
                if (!bad && IS("mv")) nftw(src, rm_one, 16, FTW_DEPTH | FTW_PHYS);
            }
            if (bad) *rc = fail(io, c, src);
        }
        return 1;
    }
    if (IS("cat")) {
        if (n == 1) return cat_fd(io.in, io.out), 1;
        for (int i = 1; i < n; i++) { int fd = open(v[i], O_RDONLY); if (fd < 0) { *rc = fail(io, c, v[i]); continue; } cat_fd(fd, io.out); close(fd); }
        return 1;
    }
    if (IS("head") || IS("tail") || IS("wc")) {
        long k = 10;
        int i = 1, only = IS("wc") && has(n, v, 'l');
        for (; i < n && v[i][0] == '-'; i++) if (!IS("wc")) k = isdigit((unsigned char)v[i][1]) ? atol(v[i] + 1) : v[i][2] ? atol(v[i] + 2) : i + 1 < n ? atol(v[++i]) : 10;
        int fd = i < n ? open(v[i], O_RDONLY) : io.in;
        if (fd < 0) { *rc = fail(io, c, v[i]); return 1; }
        FILE *f = fdopen(dup(fd), "r");
        char *line = NULL;
        size_t cap = 0;
        ssize_t l;
        long count = 0, bytes = 0;
        char **keep = calloc((size_t)(k > 0 ? k : 1), sizeof *keep);
        while (f && (l = getline(&line, &cap, f)) >= 0) {
            bytes += l;
            if (IS("head") && count < k) say(io.out, "%s", line);
            if (IS("tail") && k > 0) { free(keep[count % k]); keep[count % k] = strdup(line); }
            count++;
        }
        if (IS("tail")) for (long j = count > k ? count - k : 0; j < count; j++) say(io.out, "%s", keep[j % k]);
        if (IS("wc")) { if (only) say(io.out, "%ld\n", count); else say(io.out, "%ld %ld\n", count, bytes); }
        if (f) fclose(f);
        if (fd != io.in) close(fd);
        return 1;
    }
    if (IS("ls")) {
        int some = 0;
        for (int i = 1; i < n; i++) {
            if (v[i][0] == '-') continue;
            some = 1;
            struct stat s;
            if (stat(v[i], &s)) *rc = fail(io, c, v[i]);
            else if (S_ISDIR(s.st_mode) && !has(n, v, 'd')) ls_dir(v[i], has(n, v, 'a'), has(n, v, 'l'), has(n, v, 'R'), io);
            else say(io.out, "%s\n", v[i]);
        }
        if (!some) ls_dir(".", has(n, v, 'a'), has(n, v, 'l'), has(n, v, 'R'), io);
        return 1;
    }
    if (IS(".") || IS("source")) {
        int fd = n > 1 ? open(v[1], O_RDONLY) : -1;
        if (fd < 0) { *rc = fail(io, c, n > 1 ? v[1] : ""); return 1; }
        Buf b = {0};
        char t[4096];
        ssize_t k;
        while ((k = read(fd, t, sizeof t)) > 0) put(&b, t, (size_t)k);
        close(fd);
        *rc = run_text(b.p ? b.p : "", io);
        if (loop_go == 3) loop_go = 0;
        return 1;
    }
    if (IS("which") || IS("type") || (IS("command") && n > 1 && (!strcmp(v[1], "-v") || !strcmp(v[1], "-V")))) {
        *rc = 0;
        for (int i = 1; i < n; i++) {
            char where[PATH_MAX];
            if (v[i][0] == '-') continue;
            if (is_function(v[i]) || (strchr(v[i], '/') && !access(v[i], F_OK))) say(io.out, "%s\n", v[i]);
            else if (tool_path(v[i], where, sizeof where)) say(io.out, "%s\n", where);
            else {
                int found = 0;
                for (const char *s = getenv("PATH"); s && *s && !found;) {
                    const char *e = strchr(s, ':');
                    snprintf(where, sizeof where, "%.*s/%s", (int)(e ? e - s : (long)strlen(s)), s, v[i]);
                    if ((found = !access(where, F_OK))) say(io.out, "%s\n", where);
                    s = e ? e + 1 : NULL;
                }
                if (!found) *rc = 1;
            }
        }
        return 1;
    }
    return 0;
}

/* ---- running ---- */
static int spawn(int n, char **v, Io io);
/* ar, as libtool: the archive holds the objects named (nothing of an older one). */
static int ar(int n, char **v, Io io) {
    Words w = {0};
    add_word(&w, "libtool");
    add_word(&w, "-static");
    add_word(&w, "-no_warning_for_no_symbols");
    add_word(&w, "-o");
    int i = 1;
    if (n > 1 && strchr(v[1], 't')) { say(io.err, "ar: listing is not supported here\n"); return 1; }
    for (int k = 2; k < n; k++) add_word(&w, v[k]);
    (void)i;
    return w.n > 5 ? spawn(w.n, w.v, io) : 0;
}
static int spawn(int n, char **v, Io io) {
    const char *short_name = v[0];
    if (!strncmp(v[0], "/usr/bin/", 9) || !strncmp(v[0], "/bin/", 5) || !strncmp(v[0], "/usr/local/bin/", 15) || !strncmp(v[0], "/henge/bin/", 11)) short_name = strrchr(v[0], '/') + 1;
    char dev[PATH_MAX];
    if (tool_path(short_name, dev, sizeof dev) == 2) {
        Words w = {0};
        add_word(&w, "python3");
        add_word(&w, dev);
        add_word(&w, (char *)short_name);
        for (int i = 1; i < n; i++) add_word(&w, v[i]);
        return spawn(w.n, w.v, io);
    }
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    if (io.out == 2) posix_spawn_file_actions_adddup2(&fa, 2, 1);
    if (io.err == 1) posix_spawn_file_actions_adddup2(&fa, 1, 2);
    if (io.in != 0) posix_spawn_file_actions_adddup2(&fa, io.in, 0);
    if (io.out > 2) posix_spawn_file_actions_adddup2(&fa, io.out, 1);
    if (io.err > 2) posix_spawn_file_actions_adddup2(&fa, io.err, 2);
    pid_t pid;
    char path[PATH_MAX];
    int rc = ENOENT;
    if (strchr(v[0], '/')) {
        /* A script runs by its first line. */
        char head[512] = "";
        int fd = open(v[0], O_RDONLY);
        if (fd >= 0) { ssize_t k = read(fd, head, sizeof head - 1); close(fd); if (k > 0) head[k] = 0; }
        if (head[0] == '#' && head[1] == '!') {
            strtok(head, "\r\n");
            Words w = {0};
            for (char *t = strtok(head + 2, " "); t; t = strtok(NULL, " ")) if (!(w.n == 0 && !strcmp(t, "/usr/bin/env"))) add_word(&w, strdup(t));
            for (int i = 0; i < n; i++) add_word(&w, v[i]);
            rc = posix_spawnp(&pid, w.v[0], &fa, NULL, w.v, environ);
        } else rc = posix_spawn(&pid, v[0], &fa, NULL, v, environ);
    } else {
        const char *s = getenv("PATH");
        for (; s && *s && rc == ENOENT;) {
            const char *e = strchr(s, ':');
            snprintf(path, sizeof path, "%.*s/%s", (int)(e ? e - s : (long)strlen(s)), s, v[0]);
            if (!access(path, F_OK)) rc = posix_spawn(&pid, path, &fa, NULL, v, environ);
            s = e ? e + 1 : NULL;
        }
        if (rc == ENOENT) rc = posix_spawnp(&pid, v[0], &fa, NULL, v, environ);
    }
    posix_spawn_file_actions_destroy(&fa);
    if (rc) { say(io.err, "%s: %s\n", v[0], rc == ENOENT ? "not found" : strerror(rc)); return 127; }
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
    return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}
static int is(Toks *t, const char *s) { return t->at < t->n && !strcmp(t->t[t->at].s, s); }
static int is_assign(const char *s) {
    if (!isalpha((unsigned char)*s) && *s != '_') return 0;
    while (isalnum((unsigned char)*s) || *s == '_') s++;
    return *s == '=';
}
static int list(Toks *t, Io io, int go, const char *const *ends);
static int redirect(Toks *t, Io *io, int go, int *opened, int *nopened) { /* 1 took one, -1 failed */
    static const char *const ops[] = {">", "1>", ">>", "2>", "2>>", "&>", "&>>", ">|", "1>|", "<", NULL};
    if (t->at >= t->n || !t->t[t->at].op) return 0;
    const char *o = t->t[t->at].s;
    if (!strcmp(o, "2>&1")) { io->err = io->out; t->at++; return 1; }
    if (!strcmp(o, "1>&2") || !strcmp(o, ">&2")) { io->out = io->err; t->at++; return 1; }
    if (!strcmp(o, ">&1")) { t->at++; return 1; }
    if (!strcmp(o, "<<") || !strcmp(o, "<<<")) {
        int three = o[2] == '<';
        t->at++;
        if (t->at >= t->n) return -1;
        const char *b = t->t[t->at++].s;
        if (!go) return 1;
        char *text = three ? expand1(b) : *b == 'q' ? strdup(b + 1) : here_text(b + 1), path[PATH_MAX];
        snprintf(path, sizeof path, "%s/here.XXXXXX", getenv("TMPDIR") ? getenv("TMPDIR") : "/tmp");
        int fd = mkstemp(path);
        if (fd < 0) return -1;
        unlink(path);
        say(fd, "%s%s", text, three ? "\n" : "");
        lseek(fd, 0, SEEK_SET);
        free(text);
        if (*nopened < 16) opened[(*nopened)++] = fd;
        io->in = fd;
        return 1;
    }
    for (int i = 0; ops[i]; i++) {
        if (strcmp(o, ops[i])) continue;
        t->at++;
        if (t->at >= t->n) return -1;
        char *p = go ? expand1(t->t[t->at].s) : NULL;
        t->at++;
        if (!go) return 1;
        int fd = open(p, *o == '<' ? O_RDONLY : O_WRONLY | O_CREAT | (strstr(o, ">>") ? O_APPEND : O_TRUNC), 0666);
        if (fd < 0) { fail(*io, "sh", p); return -1; }
        if (*nopened < 16) opened[(*nopened)++] = fd;
        if (*o == '<') io->in = fd;
        else { if (*o != '2') io->out = fd; if (*o == '2' || *o == '&') io->err = fd; }
        return 1;
    }
    return 0;
}
static void skip_nl(Toks *t) { while (is(t, "\n") || is(t, ";")) t->at++; }
static void need(Toks *t, const char *s) { if (is(t, s)) t->at++; }
static int tail_at, within;
static int command(Toks *t, Io io, int go);
static int compound(Toks *t, Io io) { within = 1; return command(t, io, 1); }
static int command(Toks *t, Io io, int go) {
    int again = within;
    within = 0;
    int opened[16], nopened = 0, rc = 0;
/* Where a compound command reads and writes comes after it: found by going through it once, run the second time. */
#define TAIL tail_at = t->at; while ((r = redirect(t, &io, 0, opened, &nopened)) > 0) {}
    int r;
    if (go && !again && (is(t, "if") || is(t, "for") || is(t, "while") || is(t, "until") || is(t, "case"))) {
        int start = t->at;
        command(t, io, 0);
        if (t->at > tail_at) {
            t->at = tail_at;
            while ((r = redirect(t, &io, 1, opened, &nopened)) > 0) {}
            int after = t->at;
            t->at = start;
            rc = r < 0 ? 1 : compound(t, io);
            t->at = after;
            for (int i = 0; i < nopened; i++) close(opened[i]);
            return rc;
        }
        t->at = start;
    }
    if (is(t, "if")) {
        static const char *const a[] = {"then", NULL}, *const b[] = {"elif", "else", "fi", NULL};
        t->at++;
        int was = e_flag, done = 0;
        e_flag = 0;
        int c = list(t, io, go, a);
        e_flag = was;
        need(t, "then");
        if (go && c == 0) { rc = list(t, io, 1, b); done = 1; } else list(t, io, 0, b);
        while (is(t, "elif")) {
            t->at++;
            e_flag = 0;
            c = list(t, io, go && !done, a);
            e_flag = was;
            need(t, "then");
            if (go && !done && c == 0) { rc = list(t, io, 1, b); done = 1; } else list(t, io, 0, b);
        }
        if (is(t, "else")) { t->at++; if (go && !done) rc = list(t, io, 1, b); else list(t, io, 0, b); }
        need(t, "fi");
        TAIL
        return rc;
    }
    if (is(t, "for") || is(t, "while") || is(t, "until")) {
        static const char *const d[] = {"do", NULL}, *const e[] = {"done", NULL};
        int isfor = is(t, "for"), until = is(t, "until");
        t->at++;
        Words w = {0};
        const char *name = "";
        int cond = t->at;
        if (isfor) {
            name = t->at < t->n ? t->t[t->at++].s : "";
            if (is(t, "in")) { t->at++; while (t->at < t->n && !t->t[t->at].op) { if (go) expand(t->t[t->at].s, &w, 1); t->at++; } }
            else for (int i = 1; i < npos; i++) add_word(&w, pos[i]);
            skip_nl(t);
        }
        for (int i = 0;; i++) {
            int more = go;
            if (isfor) { more = go && i < w.n; if (more) setenv(name, w.v[i], 1); }
            else { t->at = cond; int was = e_flag; e_flag = 0; int c = list(t, io, go, d); e_flag = was; more = go && (until ? c != 0 : c == 0); }
            int body = t->at;
            need(t, "do");
            int x = list(t, io, more, e);
            if (more) rc = x;
            need(t, "done");
            int after = t->at;
            if (loop_go == 1) { loop_go = 0; more = 0; list((t->at = body, need(t, "do"), t), io, 0, e); need(t, "done"); after = t->at; }
            else if (loop_go == 2) { loop_go = 0; list((t->at = body, need(t, "do"), t), io, 0, e); need(t, "done"); after = t->at; }
            if (!more || quitting || loop_go == 3) { t->at = after; break; }
            if (isfor) t->at = body;
        }
        TAIL
        return rc;
    }
    if (is(t, "case")) {
        static const char *const e[] = {";;", "esac", NULL};
        t->at++;
        char *v = go && t->at < t->n ? expand1(t->t[t->at].s) : NULL;
        t->at++;
        skip_nl(t);
        need(t, "in");
        int done = 0;
        for (;;) {
            while (is(t, "\n") || is(t, ";;") || is(t, ";")) t->at++;
            if (t->at >= t->n || is(t, "esac")) break;
            int hit = 0;
            if (is(t, "(")) t->at++;
            while (t->at < t->n && !is(t, ")")) {
                if (!is(t, "|") && go && !done && !hit) { char *pat = expand_pattern(t->t[t->at].s); hit = !fnmatch(pat, v, 0); free(pat); }
                t->at++;
            }
            need(t, ")");
            if (hit && !done) { done = 1; rc = list(t, io, 1, e); } else list(t, io, 0, e);
        }
        need(t, "esac");
        free(v);
        TAIL
        return rc;
    }
    int fn = is(t, "function");
    if (fn || (t->at + 2 < t->n && !t->t[t->at].op && t->t[t->at + 1].op && !strcmp(t->t[t->at + 1].s, "(") && !strcmp(t->t[t->at + 2].s, ")"))) {
        t->at += fn;
        const char *name = t->at < t->n ? t->t[t->at++].s : "";
        if (is(t, "(")) t->at += 2;
        skip_nl(t);
        if (go) {
            Fn *f = calloc(1, sizeof *f);
            f->name = strdup(name), f->t = malloc(sizeof *t), *f->t = *t, f->at = t->at, f->next = fns, fns = f;
        }
        command(t, io, 0);
        return 0;
    }
    if (is(t, "(") && t->at + 1 < t->n && !strcmp(t->t[t->at + 1].s, "(")) { /* (( )) */
        Buf b = {0};
        put(&b, "", 0);
        int d = 0;
        for (; t->at < t->n; t->at++) {
            if (is(t, "(")) { if (++d <= 2) continue; }
            if (is(t, ")")) { if (--d < 2) { if (!d) { t->at++; break; } continue; } }
            put(&b, t->t[t->at].s, strlen(t->t[t->at].s));
            putc1(&b, ' ');
        }
        if (go) rc = !arith_text(b.p);
        free(b.p);
        return rc;
    }
    if (is(t, "{") || is(t, "(")) {
        int sub = is(t, "(");
        static const char *const a[] = {"}", NULL}, *const b[] = {")", NULL};
        t->at++;
        /* Where it writes comes after it. */
        int start = t->at;
        list(t, io, 0, sub ? b : a);
        need(t, sub ? ")" : "}");
        while ((r = redirect(t, &io, go, opened, &nopened)) > 0) {}
        int after = t->at;
        if (go && r >= 0) {
            char p[PATH_MAX];
            getcwd(p, sizeof p);
            t->at = start;
            rc = list(t, io, 1, sub ? b : a);
            if (sub) { chdir(p); quitting = 0; }
        }
        t->at = after;
        for (int i = 0; i < nopened; i++) close(opened[i]);
        return r < 0 ? 1 : rc;
    }
    if (is(t, "!") ) { t->at++; return !command(t, io, go); }
    Words w = {0}, set = {0};
    while (t->at < t->n) {
        r = redirect(t, &io, go, opened, &nopened);
        if (r < 0) { go = 0; rc = 1; continue; }
        if (r) continue;
        if (t->t[t->at].op) break;
        const char *s = t->t[t->at++].s;
        if (!go) continue;
        if (!w.n && is_assign(s)) { const char *e = strchr(s, '='); char *v = expand1(e + 1), *pair; asprintf(&pair, "%.*s=%s", (int)(e - s), s, v); add_word(&set, pair); }
        else if (w.n && !strcmp(w.v[0], "[[") && strcmp(s, "]]")) { /* neither split nor matched against files */
            const char *before = w.v[w.n - 1];
            add_word(&w, !strcmp(before, "==") || !strcmp(before, "=") || !strcmp(before, "!=") ? expand_pattern(s) : expand1(s));
        }
        else expand(s, &w, 1);
    }
    if (go) {
        int direct = 0;
        while (w.n > 1 && (!strcmp(w.v[0], "builtin") || (!strcmp(w.v[0], "command") && strcmp(w.v[1], "-v") && strcmp(w.v[1], "-V")))) { direct = 1; w.v++, w.n--; if (w.n > 1 && !strcmp(w.v[0], "-p")) w.v++, w.n--; }
        if (w.n == 1 && !strcmp(w.v[0], "exec")) w.n = 0;
        if (w.n && (!strcmp(w.v[0], "env") || !strcmp(w.v[0], "exec") || !strcmp(w.v[0], "command") || !strcmp(w.v[0], "nohup") || !strcmp(w.v[0], "time")) && w.n > 1 && strcmp(w.v[1], "-v")) {
            w.v++, w.n--;
            while (w.n && is_assign(w.v[0])) add_word(&set, *w.v++), w.n--;
        }
        char *old[64];
        for (int i = 0; i < set.n; i++) {
            char *e = strchr(set.v[i], '=');
            *e = 0;
            if (i < 64) { char *o = getenv(set.v[i]); old[i] = o ? strdup(o) : NULL; }
            setenv(set.v[i], e + 1, 1);
        }
        if (x_flag && w.n) { say(io.err, "+"); for (int i = 0; i < w.n; i++) say(io.err, " %s", w.v[i]); say(io.err, "\n"); }
        if (w.n) {
            const char *base = strrchr(w.v[0], '/');
            Fn *f = direct ? NULL : is_function(w.v[0]);
            if (f) {
                char **pv = pos;
                int pn = npos;
                pos = w.v, npos = w.n;
                Toks body = *f->t;
                body.at = f->at;
                rc = command(&body, io, 1);
                if (loop_go == 3) loop_go = 0;
                pos = pv, npos = pn;
            }
            else if (!strcmp(base ? base + 1 : w.v[0], "ar")) rc = ar(w.n, w.v, io);
            else if (!builtin(w.n, w.v, io, &rc)) rc = spawn(w.n, w.v, io);
            for (int i = 0; i < set.n && i < 64; i++) { if (old[i]) setenv(set.v[i], old[i], 1); else unsetenv(set.v[i]); }
        }
    }
    for (int i = 0; i < nopened; i++) close(opened[i]);
    return rc;
}
static int pipeline(Toks *t, Io io, int go) {
    int rc = 0, in = io.in, first = 1;
    for (;;) {
        int start = t->at;
        command(t, io, 0); /* where it ends */
        int more = is(t, "|");
        if (go) {
            t->at = start;
            Io x = io;
            x.in = in;
            char path[PATH_MAX];
            if (more) {
                snprintf(path, sizeof path, "%s/pipe.XXXXXX", getenv("TMPDIR") ? getenv("TMPDIR") : "/tmp");
                x.out = mkstemp(path);
                unlink(path);
            }
            rc = command(t, x, 1);
            if (!first) close(in);
            if (more) { lseek(x.out, 0, SEEK_SET); in = x.out; }
        }
        first = 0;
        if (!more) break;
        t->at++;
        while (is(t, "\n")) t->at++;
    }
    if (go) last = rc;
    return rc;
}
static int list(Toks *t, Io io, int go, const char *const *ends) {
    int rc = 0;
    for (;;) {
        skip_nl(t);
        if (t->at >= t->n) return rc;
        for (int i = 0; ends && ends[i]; i++) if (is(t, ends[i])) return rc;
        if (!ends && is(t, ")")) { t->at++; continue; }
        int was = t->at, run = go && !quitting && !loop_go, chained = 0;
        int x = pipeline(t, io, run);
        if (run) rc = x;
        while (is(t, "&&") || is(t, "||")) {
            chained = is(t, "&&") && rc != 0; /* a failure before && does not end the script */
            int yes = is(t, "&&") ? rc == 0 : rc != 0;
            t->at++;
            while (is(t, "\n")) t->at++;
            run = go && !quitting && !loop_go && yes;
            x = pipeline(t, io, run);
            if (run) rc = x;
        }
        if (go && !quitting && e_flag && rc && !chained) quitting = 1;
        if (is(t, "&")) t->at++;
        if (t->at == was) t->at++;
    }
}
static int run_text(const char *text, Io io) {
    Toks t = lex(text);
    return list(&t, io, 1, NULL);
}

int main(int argc, char **argv) {
    const char *cmd = NULL, *me = strrchr(argv[0], '/');
    if (!strcmp(me ? me + 1 : argv[0], "ar")) return ar(argc, argv, (Io){0, 1, 2});
    me = me ? me + 1 : argv[0];
    if (strcmp(me, "sh") && strcmp(me, "bash") && strcmp(me, "zsh") && strcmp(me, "dash") && *me != '-') { /* by the name of what is built in */
        int rc = 0;
        if (builtin(argc, argv, (Io){0, 1, 2}, &rc)) return rc;
        char dev[PATH_MAX];
        if (tool_path(me, dev, sizeof dev) == 2) { argv[0] = (char *)me; return spawn(argc, argv, (Io){0, 1, 2}); }
    }
    int i = 1;
    int want = 0;
    for (; i < argc && (argv[i][0] == '-' || argv[i][0] == '+') && argv[i][1]; i++) {
        if (!strcmp(argv[i], "--")) { i++; break; }
        if (argv[i][1] == '-') { if (!strcmp(argv[i], "--version")) { say(1, "Henge sh\n"); return 0; } continue; }
        if ((strchr(argv[i], 'o') || strchr(argv[i], 'O')) && i + 1 < argc) { i++; continue; }
        if (strchr(argv[i], 'e')) e_flag = 1;
        if (strchr(argv[i], 'x')) x_flag = 1;
        if (argv[i][0] == '-' && strchr(argv[i], 'c')) want = 1;
    }
    if (want) { cmd = i < argc ? argv[i] : ""; i++; }
    Buf b = {0};
    put(&b, "", 0);
    if (cmd) { put(&b, cmd, strlen(cmd)); pos = argv + i, npos = argc - i; }
    else {
        int fd = i < argc ? open(argv[i], O_RDONLY) : 0;
        if (fd < 0) { say(2, "sh: %s: %s\n", argv[i], strerror(errno)); return 127; }
        char t[4096];
        ssize_t k;
        while ((k = read(fd, t, sizeof t)) > 0) put(&b, t, (size_t)k);
        pos = argv + i, npos = argc - i;
    }
    return run_text(b.p, (Io){0, 1, 2}) & 255;
}
