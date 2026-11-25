#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Unix tools in one file, for a phone that has python3 but no grep, sed, awk, make and so on.

Run as `python3 devtools.py NAME ARGS...`; `help` lists the names. Standard library only. Nothing here forks: make, xargs,
find -exec and awk pipes run other programs with subprocess, and run the tools of this file directly in this process.
An option that is not supported is an error (exit 2), never ignored when it would change the result.
"""
import os
import re
import sys

CMDS = {}
PROG = 'devtools'
SPAWN = {'make', 'gmake', 'xargs', 'find', 'awk', 'gawk', 'nawk', 'xcrun'}


def command(*names):
    def add(f):
        for n in names:
            CMDS[n] = f
        return f
    return add


class Fail(Exception):
    def __init__(self, msg, code=2):
        self.code = code
        super().__init__(msg)


class Out:
    """Buffered output that keeps nothing behind after a broken pipe."""
    def __init__(self):
        self.b, self.n = [], 0

    def write(self, data):
        self.b.append(data)
        self.n += len(data)
        if self.n > 65536:
            self.flush()

    def flush(self):
        if not self.b:
            return
        data, self.b, self.n = memoryview(b''.join(self.b)), [], 0
        f = getattr(sys.stdout, 'buffer', None)
        if f is None:
            sys.stdout.write(bytes(data).decode('utf-8', 'backslashreplace'))
            return
        f.flush()
        f = getattr(f, 'raw', f)
        while len(data):
            data = data[f.write(data) or 0:]


class Capture(Out):
    def flush(self):
        pass


class State:
    out = Out()
    stdin = None      # bytes file that replaces standard input
    out_set = False


def outb(b):
    State.out.write(b)


def out(s):
    State.out.write(s.encode('utf-8', 'surrogateescape'))


def put(*a):
    out(' '.join(map(str, a)) + '\n')


def err(msg, name=None):
    State.out.flush()
    sys.stderr.write(f'{name or PROG}: {msg}\n'.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace'))
    sys.stderr.flush()


def oserr(e, path=None):
    p = path if path is not None else e.filename
    return f'{p}: {e.strerror}' if p is not None else str(e.strerror or e)


def stdin():
    return State.stdin or sys.stdin.buffer


def data(path='-'):
    if path in ('-', '/dev/stdin'):
        return stdin().read()
    with open(path, 'rb') as f:
        return f.read()


def dec(b):
    return b.decode('utf-8', 'surrogateescape')


def enc(s):
    return s.encode('utf-8', 'surrogateescape')


def text(path='-'):
    return dec(data(path))


def lines(s, sep='\n'):
    r = s.split(sep)
    if r[-1] == '':
        r.pop()
    return r


def texts(files, bad, binary=False):
    """Yield (name, content) for each file; print problems and note them in the list `bad`."""
    for f in files or ['-']:
        try:
            d = data(f)
        except OSError as e:
            err(oserr(e, f))
            bad.append(f)
            continue
        yield f, d if binary else dec(d)


def opts(args, short, long='', stop=False, digits=False):
    """Parse options. short: 'ab:c?' (: value, ? attached optional value). long: 'name=k' gives key k, 'name:' takes a value,
    'name?' an optional =value. Returns [(key, value)], operands. digits: -5 gives ('#', '5')."""
    kind, lmap = {}, {}
    for m in re.finditer(r'(.)([:?]?)', short):
        kind[m[1]] = m[2]
    for w in long.split():
        n, _, k = w.partition('=')
        k = k or n
        if k[-1] in ':?':
            kind[k[:-1]] = k[-1]
            k = k[:-1]
        else:
            kind.setdefault(k, '')
        lmap[n.rstrip(':?')] = k
    res, rest, i = [], [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == '--':
            rest += args[i:]
            break
        if a.startswith('--'):
            n, eq, v = a[2:].partition('=')
            c = [n] if n in lmap else [x for x in lmap if x.startswith(n)]
            if len(c) > 1 and len({lmap[x] for x in c}) == 1:
                c = c[:1]
            if len(c) != 1:
                raise Fail(f'unsupported option {a}')
            k = lmap[c[0]]
            if kind[k] == ':':
                if not eq:
                    if i >= len(args):
                        raise Fail(f'option {a} needs a value')
                    v, i = args[i], i + 1
            elif not eq:
                v = None
            elif kind[k] != '?':
                raise Fail(f'option --{n} takes no value')
            res.append((k, v))
        elif a.startswith('-') and len(a) > 1:
            j = 1
            if digits and a[1:].isdigit():
                res.append(('#', a[1:]))
                continue
            while j < len(a):
                c = a[j]
                j += 1
                if c not in kind or c in '-':
                    raise Fail(f'unsupported option -{c}')
                if kind[c] == ':':
                    if j < len(a):
                        v = a[j:]
                    elif i < len(args):
                        v, i = args[i], i + 1
                    else:
                        raise Fail(f'option -{c} needs a value')
                    res.append((c, v))
                    break
                if kind[c] == '?':
                    res.append((c, a[j:] or None))
                    break
                res.append((c, None))
        else:
            rest.append(a)
            if stop:
                rest += args[i:]
                break
    return res, rest


def integer(s):
    try:
        return int(s)
    except ValueError:
        raise Fail(f'invalid number: {s}') from None


def dispatch(name, args):
    """Run one of our commands; returns its exit code."""
    global PROG
    old, PROG = PROG, name
    try:
        return CMDS[name](name, args) or 0
    except Fail as e:
        err(e)
        return e.code
    except BrokenPipeError:
        raise
    except RecursionError:
        err('input too deeply nested')
        return 2
    except OSError as e:
        err(oserr(e))
        return 2 if name in ('grep', 'egrep', 'fgrep', 'rg', 'diff', 'cmp', 'sort', 'awk', 'make') else 1
    except re.error as e:
        err(f'bad regular expression: {e}')
        return 2
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    finally:
        PROG = old


def call(argv, inp=None, capture=False, cwd=None):
    """Run a command list, ours in this process. Returns (exit code, captured bytes)."""
    import io
    if argv[0] in CMDS and not (capture and argv[0] in SPAWN) and cwd is None:
        so, si = State.out, State.stdin
        if capture:
            State.out = Capture()
        if inp is not None:
            State.stdin = io.BytesIO(inp)
        try:
            rc = dispatch(argv[0], argv[1:])
            return rc, b''.join(State.out.b) if capture else b''
        finally:
            State.out, State.stdin = so, si
    import subprocess
    State.out.flush()
    try:
        p = subprocess.run(argv, input=inp, stdout=subprocess.PIPE if capture else None, cwd=cwd)
    except FileNotFoundError:
        err(f'{argv[0]}: command not found')
        return 127, b''
    except PermissionError:
        err(f'{argv[0]}: permission denied')
        return 126, b''
    return p.returncode if p.returncode >= 0 else 128 - p.returncode, p.stdout or b''


def simple(cmd):
    """The word list of a shell command that needs no shell and is one of ours, else None."""
    if not re.search(r'''[|&;<>()$`\\*?\[#~={}!\n%]''', cmd):
        import shlex
        try:
            w = shlex.split(cmd)
        except ValueError:
            return None
        if w and w[0] in CMDS:
            return w
    return None


def shell(cmd, inp=None, capture=False, env=None):
    w = simple(cmd)
    if w and not (capture and w[0] in SPAWN):
        saved = dict(os.environ)
        if env is not None:
            os.environ.update(env)
        try:
            return call(w, inp, capture)
        finally:
            if env is not None:
                os.environ.clear()
                os.environ.update(saved)
    import subprocess
    State.out.flush()
    p = subprocess.run(cmd, shell=True, input=inp, stdout=subprocess.PIPE if capture else None, env=env)
    return p.returncode if p.returncode >= 0 else 128 - p.returncode, p.stdout or b''


# ---- regular expressions: POSIX basic and extended to Python's ----

CLS = {'alpha': 'a-zA-Z\u00c0-\u024f', 'digit': '0-9', 'alnum': 'a-zA-Z0-9\u00c0-\u024f', 'upper': 'A-Z', 'lower': 'a-z',
       'space': r'\s', 'blank': r' \t', 'punct': r'!-/:-@\[-`{-~', 'xdigit': '0-9a-fA-F', 'cntrl': r'\x00-\x1f\x7f',
       'print': r'\x20-\x7e\u00a0-\U0010ffff', 'graph': r'\x21-\x7e\u00a1-\U0010ffff', 'word': r'\w'}
ESC = {'n': '\n', 't': '\t', 'r': '\r', 'f': '\f', 'v': '\v', 'a': '\a'}


def bracket(p, i, esc):
    """Translate the bracket expression that starts after p[i-1] == '['. esc: 0 none, 1 sed escapes, 2 awk escapes."""
    n, neg, o, first = len(p), False, [], True
    if p[i:i + 1] == '^':
        neg, i = True, i + 1

    def one(i):
        c = p[i]
        if p[i:i + 2] in ('[=', '[.'):
            e = p.find(p[i + 1] + ']', i + 2)
            if e > 0:
                return p[i + 2:e], e + 2
        if c == '\\' and esc and i + 1 < n:
            x = p[i + 1]
            if x in ESC:
                return ESC[x], i + 2
            if esc == 2:
                m = re.compile(r'[0-7]{1,3}').match(p, i + 1)
                return (chr(int(m[0], 8)), m.end()) if m else (x, i + 2)
            if x == '\\' and p[i + 2:i + 3] != ']':
                return '\\', i + 2
        return c, i + 1

    def q(c):
        return '\\' + c if c in '\\]^[-&~|' else c
    while True:
        if i >= n:
            raise Fail('unmatched [ in regular expression')
        if p[i] == ']' and not first:
            break
        first = False
        if p.startswith('[:', i):
            e = p.find(':]', i)
            if e > 0 and p[i + 2:e] in CLS:
                o.append(CLS[p[i + 2:e]])
                i = e + 2
                continue
        c, i = one(i)
        if p[i:i + 1] == '-' and i + 1 < n and p[i + 1] != ']':
            d, i = one(i + 1)
            if d < c:
                raise Fail('invalid range end in regular expression')
            o.append(q(c) + '-' + q(d))
        else:
            o.append(q(c))
    return '[' + '^' * neg + ''.join(o) + ']', i + 1


def rx_source(p, ere, esc=0, multi=False):
    o, last, stack, quant, i, n = [], None, [], False, 0, len(p)
    alts, plain = [[]], True       # for the top-level alternation of plain words

    def atom(s):
        nonlocal last, quant
        last, quant = len(o), False
        o.append(s)
    while i < n:
        c = p[i]
        i += 1
        meta = None
        if c == '\\':
            if i >= n:
                raise Fail('trailing backslash in regular expression')
            c = p[i]
            i += 1
            if not ere and c in '(){}|+?':
                meta = c
            elif c.isdigit() and c != '0':
                plain = False
                atom(f'(?:\\{c})')
                continue
            else:
                z = {'<': r'\b(?=\w)', '>': r'\b(?<=\w)', '`': r'\A', "'": r'\Z', 'w': r'\w', 'W': r'\W', 's': r'\s',
                     'S': r'\S', 'b': r'\b', 'B': r'\B', 'd': r'\d', 'D': r'\D'}.get(c) if not (esc == 2 and c in 'b') else r'\x08'
                if z:
                    plain = False
                if esc == 2 and c == 'y':
                    z = r'\b'
                atom(z or (re.escape(ESC[c]) if c in ESC else re.escape(c)))
                if not z:
                    alts[-1].append(o[-1])
                continue
        elif c in '(){}|+?':
            meta = c if ere else None
        elif c == '*':
            if last is not None:
                meta = '*'
        elif c == '[':
            s, i = bracket(p, i, esc)
            plain = False
            atom(s)
            continue
        elif c == '.':
            plain = False
            atom('.')
            continue
        elif c == '^':
            if ere or not o or o[-1] in ('(', '|'):
                plain = False
                o.append('(?m:^)' if multi else r'\A')
                last = None
                continue
        elif c == '$':
            if ere or i >= n or p.startswith(('\\)', '\\|'), i):
                plain = False
                o.append('(?m:$)' if multi else r'\Z')
                last = None
                continue
        if meta == '(':
            plain = False
            stack.append(len(o))
            o.append('(')
            last = None
            continue
        if meta == ')' and (stack or not ere):
            if not stack:
                raise Fail('unmatched ) in regular expression')
            s = stack.pop()
            o[s:] = [''.join(o[s:]) + ')']
            last, quant = s, False
            continue
        if meta == '|':
            o.append('|')
            alts.append([])
            last = None
            continue
        if meta in ('*', '+', '?') and last is None:
            if ere and c != '*':
                raise Fail('repetition operator with nothing to repeat')
            meta = None
        q = meta
        if meta == '{':
            m = re.compile(r'(\d*)(,?)(\d*)' + ('}' if ere else r'\\}')).match(p, i)
            if m and (m[1] or m[3]) and last is not None:
                q = '{%s%s%s}' % (m[1] or '0', m[2], m[3])
                i = m.end()
            elif ere:
                meta = None
            else:
                raise Fail('invalid repetition count')
        if meta == '}' or meta == ')':
            meta = None
        if meta:
            plain = False
            o[last] = ('(?:%s)' % o[last] if quant else o[last]) + q
            quant = True
            continue
        atom(re.escape(c))
        alts[-1].append(o[-1])
    if stack:
        raise Fail('unmatched ( in regular expression')
    if plain and len(alts) > 1:
        return '|'.join(sorted((''.join(a) for a in alts), key=len, reverse=True))
    return ''.join(o)


def rx(p, ere=False, icase=False, esc=0, multi=False):
    return re.compile(rx_source(p, ere, esc, multi), re.S | (re.I if icase else 0))


def perl(p):
    p = re.sub(r'\(\?<(?=[A-Za-z_])', '(?P<', p)
    p = re.sub(r'\[:(\w+):\]', lambda m: CLS.get(m[1], m[0]), p)
    p = re.sub(r'(?<!\\)((?:\\\\)*)\\([hR])', lambda m: m[1] + {'h': r'[ \t]', 'R': r'(?:\r\n|\n|\r)'}[m[2]], p)
    m = re.search(r'(?<!\\)((?:\\\\)*)\\K', p)
    if m:
        p = f'(?:{p[:m.start()]}{m[1]})(?P<K_>{p[m.end():]})'
    return p


def glob_re(g, icase=False):
    import fnmatch
    return re.compile(fnmatch.translate(g), re.I if icase else 0).match


def path_glob(g):
    """A matcher for rg and tar style globs, where * stops at / and ** does not."""
    o, i = '', 0
    while i < len(g):
        if g.startswith('**/', i):
            o, i = o + '(?:.*/)?', i + 3
        elif g.startswith('**', i):
            o, i = o + '.*', i + 2
        elif g[i] == '*':
            o, i = o + '[^/]*', i + 1
        elif g[i] == '?':
            o, i = o + '[^/]', i + 1
        elif g[i] == '[' and ']' in g[i + 2:]:
            e = g.index(']', i + 2)
            o, i = o + '[' + g[i + 1:e].replace('!', '^', 1 if g[i + 1] == '!' else 0).replace('\\', '\\\\') + ']', e + 1
        elif g[i] == '{' and '}' in g[i:]:
            e = g.index('}', i)
            o, i = o + '(?:' + '|'.join(path_src(x) for x in g[i + 1:e].split(',')) + ')', e + 1
        else:
            o, i = o + re.escape(g[i]), i + 1
    return o


path_src = path_glob


# ---- grep, rg ----

class Ignore:
    """A reasonable part of .gitignore."""
    def __init__(self):
        self.cache = {}

    def rules(self, d):
        if d not in self.cache:
            r = []
            for n in ('.gitignore', '.ignore', '.rgignore'):
                try:
                    t = text(os.path.join(d, n))
                except OSError:
                    continue
                for ln in lines(t):
                    ln = ln.rstrip()
                    if not ln or ln[0] == '#':
                        continue
                    neg = ln[0] == '!'
                    ln = ln[neg:]
                    donly = ln.endswith('/')
                    ln = ln.rstrip('/')
                    anch = '/' in ln
                    r.append((neg, donly, re.compile(('' if anch else '(?:.*/)?') + path_glob(ln.lstrip('/')) + r'\Z')))
            self.cache[d] = r
        return self.cache[d]

    def hit(self, path, isdir):
        res, d, rel = None, os.path.dirname(path), os.path.basename(path)
        while True:
            for neg, donly, r in reversed(self.rules(d or '.')):
                if (isdir or not donly) and r.match(rel):
                    return not neg
            if d in ('', '.', '/'):
                return bool(res)
            rel = os.path.basename(d) + '/' + rel
            d = os.path.dirname(d)


RGTYPES = {'py': '*.py *.pyi', 'c': '*.c *.h', 'cpp': '*.cpp *.cc *.cxx *.hpp *.hh *.h', 'js': '*.js *.jsx *.mjs *.cjs',
           'ts': '*.ts *.tsx *.mts *.cts', 'rust': '*.rs', 'go': '*.go', 'java': '*.java', 'swift': '*.swift', 'sh': '*.sh *.bash *.zsh',
           'md': '*.md *.markdown', 'json': '*.json', 'yaml': '*.yml *.yaml', 'html': '*.html *.htm', 'css': '*.css *.scss',
           'make': 'Makefile makefile GNUmakefile *.mk *.mak', 'toml': '*.toml', 'xml': '*.xml', 'objc': '*.m *.h', 'objcpp': '*.mm *.h',
           'txt': '*.txt', 'cmake': 'CMakeLists.txt *.cmake', 'rb': '*.rb', 'ruby': '*.rb', 'lua': '*.lua', 'asm': '*.s *.S *.asm',
           'sql': '*.sql', 'kotlin': '*.kt *.kts', 'php': '*.php', 'perl': '*.pl *.pm'}


def walk(top, keepdir, follow=False):
    try:
        ents = sorted(os.scandir(top), key=lambda e: e.name)
    except OSError as e:
        err(oserr(e, top))
        return
    for e in ents:
        p = e.name if top == '' else os.path.join(top, e.name)
        if e.is_dir(follow_symlinks=follow):
            if keepdir(p, e.name):
                yield from walk(p, keepdir, follow)
        elif e.is_file(follow_symlinks=True) and (follow or not e.is_symlink()):
            yield p


@command('grep', 'egrep', 'fgrep', 'rg')
def grep(name, args):
    rg = name == 'rg'
    if rg:
        o, rest = opts(args, 'inNlLcowvqhHIFPsxe:f:A:B:C:m:Sg:t:T:u0ar:U', 'ignore-case=i line-number=n no-line-number=N count=c '
            'files-with-matches=l files-without-match=0L only-matching=o word-regexp=w invert-match=v quiet=q with-filename=H no-filename=I '
            'fixed-strings=F pcre2=P no-messages=s line-regexp=x regexp=e file=f after-context=A before-context=B context=C max-count=m '
            'smart-case=S glob=g iglob: type=t type-not=T unrestricted=u null=0 text=a files hidden=. no-ignore no-ignore-vcs=no-ignore '
            'color: no-heading=sortok sort:=sort: max-depth: follow=L column vimgrep replace=r case-sensitive=cs count-matches '
            'no-config=sortok trim')
    else:
        o, rest = opts(args, 'rRnilLcowvqhHEFGPsxye:f:A:B:C:m:aIbZzd:', 'recursive=r dereference-recursive=R line-number=n '
            'ignore-case=i no-ignore-case=noi files-with-matches=l files-without-match=L count=c only-matching=o word-regexp=w '
            'invert-match=v quiet=q silent=q no-filename=h with-filename=H extended-regexp=E fixed-strings=F basic-regexp=G perl-regexp=P '
            'no-messages=s line-regexp=x regexp=e file=f after-context=A before-context=B context=C max-count=m text=a byte-offset=b '
            'null=Z null-data=z include: exclude: exclude-dir: color? colour=color line-buffered=lb directories=d binary-files: '
            'label: initial-tab=T_', digits=True)
    pats, inc, exc, excd, f = [], [], [], [], {}
    for k, v in o:
        if k == 'e':
            pats += v.split('\n')
        elif k == 'f':
            pats += lines(text(v))
        elif k == 'include':
            inc.append(glob_re(v))
        elif k == 'exclude':
            exc.append(glob_re(v))
        elif k == 'exclude-dir':
            excd.append(glob_re(v.rstrip('/')))
        elif k in 'gtT' or k == 'iglob':
            f.setdefault(k, []).append(v)
        elif k in 'EFGP' and not rg:
            f['mode'] = k
        elif k in ('#', 'C'):
            f['A'] = f['B'] = v
        elif k == 'u':
            f['u'] = f.get('u', 0) + 1
        elif k == 'sortok':
            pass
        else:
            f[k] = v if v is not None else True
    if 'T_' in f or f.get('sort') not in (None, 'path', 'none') or f.get('binary-files') not in (None, 'text', 'without-match', 'binary') \
            or 'U' in f:
        raise Fail('unsupported option')
    if f.get('d') not in (None, 'skip', 'recurse', 'read'):
        raise Fail('unsupported -d value')
    if f.get('d') == 'recurse':
        f['r'] = True
    if not rg and 'noi' in f:
        f.pop('i', None)
    if f.get('binary-files') == 'text':
        f['a'] = True
    if f.get('binary-files') == 'without-match':
        f['I'] = True
    if not pats and not any(k in ('e', 'f') for k, _ in o) and 'files' not in f:
        if not rest:
            raise Fail('usage: %s [options] pattern [file...]' % name)
        pats = rest.pop(0).split('\n')
    mode = {'egrep': 'E', 'fgrep': 'F', 'rg': 'R'}.get(name, f.get('mode', 'G'))
    if rg and 'F' in f:
        mode = 'F'
    icase = any(k in f for k in 'iy') or (rg and 'S' in f and not any(re.search('[A-Z]', re.sub(r'\\.', '', p)) for p in pats))
    if 'cs' in f:
        icase = False
    sep = '\0' if 'z' in f else '\n'

    def source(multi):
        ps = []
        for p in pats:
            s = re.escape(p) if mode == 'F' else perl(p) if mode in 'PR' else rx_source(p, mode == 'E', 0, multi)
            if mode == 'G' or mode == 'E':
                s = s if len(pats) == 1 else re.sub(r'\(\?:\\(\d)\)', r'(?:\\\1)', s)
            ps.append(s)
        if mode == 'F':
            ps.sort(key=len, reverse=True)
        s = ps[0] if len(ps) == 1 else '|'.join(f'(?:{p})' for p in ps)
        if not ps:
            s = '(?!)'
        if 'x' in f:
            s = r'(?m:^)(?:%s)(?m:$)' % s if multi else r'\A(?:%s)\Z' % s
        elif 'w' in f:
            s = r'(?<!\w)(?:%s)(?!\w)' % s
        return s
    fl = re.I if icase else 0
    inv = 'v' in f
    r = re.compile(source(False), fl | (re.S if mode not in 'PR' else 0))
    quick = re.compile(source(True), fl) if not inv and mode not in 'PR' and 'z' not in f and 'L' not in f and 'c' not in f else None
    kgroup = 'K_' if 'K_' in r.groupindex else 0
    A, B = integer(f.get('A', 0)), integer(f.get('B', 0))
    maxn = integer(f['m']) if 'm' in f else None
    follow = 'R' in f or (rg and 'L' in f)
    listing = 'l' in f
    unlist = '0L' in f or (not rg and 'L' in f)
    recurse = 'r' in f or 'R' in f or rg
    if rg:
        recurse = True
    color = f.get('color') in ('always', 'ansi')
    red = (lambda s: f'\x1b[01;31m\x1b[K{s}\x1b[m\x1b[K') if color else (lambda s: s)
    state = {'bad': False, 'any': False}
    multi = len(rest) > 1 or (recurse and (not rest or any(os.path.isdir(x) for x in rest)))
    if rg and not rest and 'files' not in f:
        import stat as st
        try:
            m = os.fstat(0).st_mode
            if State.stdin or st.S_ISFIFO(m) or st.S_ISREG(m):
                rest, multi = ['-'], False
        except OSError:
            pass
    if not rg and not rest and recurse:
        f.setdefault('_nodot', True)
    named = ('H' in f or multi) and not (('h' in f and not rg) or (rg and 'I' in f))
    if rg and ('vimgrep' in f or 'column' in f):
        f['n'] = True
        named = named or 'vimgrep' in f
    numbered = 'n' in f and not (rg and 'N' in f)
    nul = '\0' if ('Z' in f or (rg and '0' in f)) else None
    label = f.get('label', '(standard input)' if not rg else '<stdin>')

    ig = Ignore() if rg and 'no-ignore' not in f and 'u' not in f else None
    hidden = rg and ('.' in f or f.get('u', 0) > 1)
    globs = []
    for k in ('g', 'iglob'):
        for g in f.get(k, []):
            neg = g.startswith('!')
            g = g[neg:]
            s = path_glob(g.lstrip('/')) if '/' in g else '(?:.*/)?' + path_glob(g)
            globs.append((neg, re.compile(s + r'(?:/.*)?\Z' if neg else s + r'\Z', re.I if k == 'iglob' else 0)))

    def types(k):
        r = []
        for t in f.get(k, []):
            if t not in RGTYPES:
                raise Fail(f'unrecognized file type: {t}')
            r += [glob_re(g) for g in RGTYPES[t].split()]
        return r
    tin, tout = types('t'), types('T')

    def want(p):
        b = os.path.basename(p)
        if rg:
            if not hidden and b.startswith('.') and not any(not n for n, _ in globs):
                return False
            if ig and ig.hit(p, False):
                return False
            q = p[2:] if p.startswith('./') else p
            if any(neg and g.match(q) for neg, g in globs):
                return False
            pos = [g for neg, g in globs if not neg]
            if pos and not any(g.match(q) for g in pos):
                return False
            return (not tin or any(t(b) for t in tin)) and not any(t(b) for t in tout)
        return (not inc or any(g(b) for g in inc)) and not any(g(b) for g in exc)

    def keepdir(p, b):
        if rg:
            if (b.startswith('.') and not hidden) or (ig and ig.hit(p, True)):
                return False
            if 'max-depth' in f and p.count('/') + (not p.startswith('./')) >= integer(f['max-depth']):
                return False
            q = p[2:] if p.startswith('./') else p
            return not any(neg and g.match(q) for neg, g in globs)
        return not any(g(b) for g in excd)

    def files():
        if not rest and recurse:
            yield from (x for x in walk('.', keepdir, follow) if want(x))
            return
        for p in rest or ['-']:
            if p != '-' and os.path.isdir(p):
                if recurse:
                    yield from (x for x in walk(p, keepdir, follow) if want(x))
                elif f.get('d') != 'skip' and 's' not in f:
                    err(f'{p}: Is a directory')
            elif p == '-' or rg or want(p):
                yield p

    def span(m):
        return m.span(kgroup)

    def show(path, pre, no, ln, sp, off=0):
        c = ':' if sp else '-'
        h = ''
        if named:
            h += path + (nul if nul else c)
        if numbered:
            h += f'{no}{c}'
        if 'b' in f and not rg:
            h += f'{off}{c}'
        return h

    def one(path):
        try:
            raw = data(path)
        except OSError as e:
            if 's' not in f:
                err(oserr(e, path))
            state['bad'] = True
            return
        binary = 'a' not in f and 'z' not in f and b'\0' in raw[:32768]
        if binary and ('I' in f and not rg or rg and multi):
            return
        t = dec(raw)
        disp = label if path == '-' else path[2:] if (rg or f.get('_nodot')) and not rest and path.startswith('./') else path
        if quick and not quick.search(t):
            if unlist:
                put(disp)
            return
        cnt, last, pending, off, nth = 0, 0, 0, 0, 0
        ls = lines(t, sep)
        quiet = listing or unlist or 'c' in f or 'q' in f or 'count-matches' in f
        find = r.search
        for i, ln in enumerate(ls):
            if bool(find(ln)) != inv and (maxn is None or cnt < maxn):
                cnt += 1
                if quiet:
                    if 'count-matches' in f:
                        nth += sum(1 for _ in r.finditer(ln))
                    elif 'c' not in f:
                        break
                    continue
                if binary:
                    put(f'Binary file {disp} matches') if not rg else put(f'binary file matches (found "\\0" byte): {disp}')
                    break
                if 'o' in f:
                    if not inv:
                        for m in r.finditer(ln):
                            a, b = span(m)
                            if a < b:
                                s = m.expand(re.sub(r'\$\{?(\w+)\}?', r'\\g<\1>', f['r'])) if rg and 'r' in f else ln[a:b]
                                h = show(disp, '', i + 1, ln, True, off + len(enc(ln[:a])))
                                out(h + (f'{a + 1}:' if 'column' in f else '') + red(s) + sep)
                else:
                    if (A or B) and state.get('printed') and (not last or i - B > last):
                        put('--')
                    for j in range(max(last, i - B), i):
                        out(show(disp, '', j + 1, ls[j], False) + ls[j] + sep)
                    h = show(disp, '', i + 1, ln, True, off)
                    if 'column' in f or 'vimgrep' in f:
                        h += f'{r.search(ln).start() + 1}:'
                    s = ln
                    if rg and 'r' in f:
                        s = r.sub(re.sub(r'\$\{?(\w+)\}?', r'\\g<\1>', f['r']), s)
                    elif color and not inv:
                        s = r.sub(lambda m: m[0][:span(m)[0] - m.start()] + red(ln[slice(*span(m))]) + ln[span(m)[1]:m.end()], s)
                    if 'trim' in f:
                        s = s.lstrip()
                    out(h + s + sep)
                    last = i + 1
                    pending = A
                    state['printed'] = True
                state['any'] = True
            elif maxn is not None and cnt >= maxn and not pending:
                break
            elif pending and not quiet and 'o' not in f and not binary:
                pending -= 1
                last = i + 1
                out(show(disp, '', i + 1, ln, False) + ln + sep)
            if 'b' in f:
                off += len(enc(ln)) + 1
        if cnt:
            state['any'] = True
        if 'q' in f:
            return
        if listing and cnt or unlist and not cnt:
            out(disp + (nul or '\n'))
            if unlist:
                state['listed'] = True
        elif ('c' in f or 'count-matches' in f) and not listing and not unlist and (cnt or not rg):
            out((disp + (nul or ':') if named else '') + f'{nth or cnt}\n')
    if 'files' in f:
        rest = pats[:1] + rest if pats else rest
        n = 0
        for p in files():
            n += 1
            out((p[2:] if not rest and p.startswith('./') else p) + (nul or '\n'))
        return 0 if n else 1
    if maxn == 0:
        return 1
    for p in files():
        one(p)
        if 'q' in f and state['any']:
            return 0
    ok = state.get('listed', False) if unlist else state['any']
    return 2 if state['bad'] or any(not os.path.lexists(x) for x in rest if x != '-') else 0 if ok else 1


# ---- find ----

@command('find')
def find(name, args):
    import stat as st
    import time
    i, mode, ere = 0, 'P', False
    while i < len(args) and re.fullmatch(r'-[HLPEdxs]+', args[i]) and args[i] not in ('-ls',):
        for c in args[i][1:]:
            if c in 'HLP':
                mode = c
            elif c == 'E':
                ere = True
            elif c not in 'dxs':
                raise Fail(f'unsupported option -{c}', 1)
        i += 1
    depth_first = any(re.fullmatch('-[HLPExs]*d[HLPExsd]*', a) for a in args[:i])
    tops = []
    while i < len(args) and not (args[i].startswith('-') and len(args[i]) > 1) and args[i] not in ('!', '(', ')', ','):
        tops.append(args[i])
        i += 1
    tops = tops or ['.']
    toks, pos = args[i:], 0
    cfg = {'min': 0, 'max': 1 << 30, 'depth': depth_first, 'action': False, 'rc': 0, 'follow': mode == 'L'}
    now, batches = time.time(), []

    class Quit(Exception):
        pass

    def num(s):
        m = re.fullmatch(r'([+-]?)(\d+(?:\.\d+)?)', s)
        if not m:
            raise Fail(f'bad number {s}', 1)
        v = float(m[2])
        return {'+': lambda x: x > v, '-': lambda x: x < v, '': lambda x: x == v}[m[1]]

    def arg():
        nonlocal pos
        pos += 1
        if pos > len(toks):
            raise Fail(f'{toks[-1]}: needs an argument', 1)
        return toks[pos - 1]

    def p_list():
        a = p_or()
        while peek() == ',':
            arg()
            a = (lambda x, y: lambda e: (x(e), y(e))[1])(a, p_or())
        return a

    def peek():
        return toks[pos] if pos < len(toks) else None

    def p_or():
        a = p_and()
        while peek() in ('-o', '-or'):
            arg()
            a = (lambda x, y: lambda e: x(e) or y(e))(a, p_and())
        return a

    def p_and():
        a = p_not()
        while peek() not in (None, '-o', '-or', ')', ','):
            if peek() in ('-a', '-and'):
                arg()
            a = (lambda x, y: lambda e: x(e) and y(e))(a, p_not())
        return a

    def p_not():
        if peek() in ('!', '-not'):
            arg()
            x = p_not()
            return lambda e: not x(e)
        return prim()

    def stat_of(e):
        if e[2] is None:
            e[2] = lst(e[0], e[3])
        return e[2]

    def lst(p, top):
        if cfg['follow'] or (mode == 'H' and top):
            try:
                return os.stat(p)
            except OSError:
                pass
        return os.lstat(p)

    def prim():
        t = arg()
        T = t.lstrip('-')
        if t == '(':
            x = p_list()
            if peek() != ')':
                raise Fail('missing )', 1)
            arg()
            return x
        if t in ('-name', '-iname'):
            g = glob_re(arg(), T[0] == 'i')
            return lambda e: bool(g(e[1]))
        if t in ('-path', '-ipath', '-wholename', '-iwholename'):
            g = glob_re(arg(), T[0] == 'i')
            return lambda e: bool(g(e[0]))
        if t in ('-regex', '-iregex'):
            r = rx(arg(), ere, T[0] == 'i')
            return lambda e: bool(r.fullmatch(e[0]))
        if t in ('-lname', '-ilname'):
            g = glob_re(arg(), T[0] == 'i')
            return lambda e: os.path.islink(e[0]) and bool(g(os.readlink(e[0])))
        if t in ('-type', '-xtype'):
            ks = {'f': st.S_ISREG, 'd': st.S_ISDIR, 'l': st.S_ISLNK, 'p': st.S_ISFIFO, 's': st.S_ISSOCK, 'b': st.S_ISBLK, 'c': st.S_ISCHR}
            w = arg().split(',')
            if any(x not in ks for x in w):
                raise Fail(f'unknown -type {w}', 1)
            return lambda e: any(ks[x](stat_of(e).st_mode) for x in w)
        if t == '-size':
            m = re.fullmatch(r'([+-]?\d+)([bcwkMGT]?)', arg())
            if not m:
                raise Fail('bad -size', 1)
            f, u = num(m[1]), {'': 512, 'b': 512, 'c': 1, 'w': 2, 'k': 1 << 10, 'M': 1 << 20, 'G': 1 << 30, 'T': 1 << 40}[m[2]]
            return lambda e: f(-(-stat_of(e).st_size // u))
        if t in ('-mtime', '-atime', '-ctime', '-mmin', '-amin', '-cmin'):
            a, k, u = arg(), 'st_%stime' % T[0], 60 if t.endswith('min') else 86400
            m = re.fullmatch(r'([+-]?\d+)([smhdw])', a)
            if m:
                a, u = m[1], {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[m[2]]
            f = num(a)
            if a[0] in '-+' and u == 60:
                return lambda e: f((now - getattr(stat_of(e), k)) / u)
            return lambda e: f((now - getattr(stat_of(e), k)) // u)
        if t in ('-newer', '-anewer', '-cnewer', '-newermm'):
            ref = os.stat(arg()).st_mtime_ns
            return lambda e: stat_of(e).st_mtime_ns > ref
        if t == '-newermt':
            ref = when(arg())
            return lambda e: stat_of(e).st_mtime > ref
        if t == '-empty':
            return lambda e: (not os.listdir(e[0])) if st.S_ISDIR(stat_of(e).st_mode) else \
                st.S_ISREG(stat_of(e).st_mode) and stat_of(e).st_size == 0
        if t == '-perm':
            a = arg()
            m = re.fullmatch(r'([-+/]?)([0-7]+)', a)
            if not m:
                m = re.fullmatch(r'([-+/]?)([ugoa]*)([+=])([rwx]+)', a)
                if not m:
                    raise Fail('only octal and u+x style -perm are supported', 1)
                b = sum({'r': 4, 'w': 2, 'x': 1}[c] for c in m[4])
                v = sum(b << {'u': 6, 'g': 3, 'o': 0}[c] for c in ('ugo' if not m[2] or 'a' in m[2] else m[2]))
            else:
                v = int(m[2], 8)
            return (lambda e: stat_of(e).st_mode & v == v) if m[1] == '-' else (lambda e: bool(stat_of(e).st_mode & v)) if m[1] \
                else (lambda e: stat_of(e).st_mode & 0o7777 == v)
        if t == '-links':
            f = num(arg())
            return lambda e: f(stat_of(e).st_nlink)
        if t == '-inum':
            f = num(arg())
            return lambda e: f(stat_of(e).st_ino)
        if t in ('-readable', '-writable', '-executable'):
            m = {'r': os.R_OK, 'w': os.W_OK, 'e': os.X_OK}[T[0]]
            return lambda e: os.access(e[0], m)
        if t in ('-maxdepth', '-mindepth'):
            cfg[T[:3]] = integer(arg())
            return lambda e: True
        if t in ('-depth', '-d'):
            cfg['depth'] = True
            return lambda e: True
        if t == '-follow':
            cfg['follow'] = True
            return lambda e: True
        if t in ('-true', '-xdev', '-mount', '-noleaf', '-ignore_readdir_race'):
            return lambda e: True
        if t == '-false':
            return lambda e: False
        if t == '-prune':
            def prune(e):
                e[4] = True
                return True
            return prune
        cfg['action'] = True
        if t == '-print':
            return lambda e: (out(e[0] + '\n'), True)[1]
        if t == '-print0':
            return lambda e: (out(e[0] + '\0'), True)[1]
        if t == '-ls':
            def ls(e):
                s = stat_of(e)
                put(f'{s.st_ino:9} {s.st_blocks // 2:8} {st.filemode(s.st_mode)} {s.st_nlink:3} {s.st_uid} {s.st_gid} {s.st_size:10} '
                    + time.strftime('%b %e %H:%M ', time.localtime(s.st_mtime)) + e[0])
                return True
            return ls
        if t == '-printf':
            return printf(arg())
        if t == '-quit':
            def quit_(e):
                raise Quit
            return quit_
        if t == '-delete':
            cfg['depth'] = True

            def delete(e):
                if e[0] in ('.', './'):
                    return True
                try:
                    os.rmdir(e[0]) if os.path.isdir(e[0]) and not os.path.islink(e[0]) else os.unlink(e[0])
                    return True
                except OSError as x:
                    err(oserr(x, e[0]))
                    cfg['rc'] = 1
                    return False
            return delete
        if t in ('-exec', '-execdir'):
            cmd = []
            while True:
                a = arg()
                if a == ';' or (a == '+' and cmd and cmd[-1] == '{}'):
                    break
                cmd.append(a)
            indir = t == '-execdir'
            if a == '+':
                b = [cmd[:-1], [], indir]
                batches.append(b)

                def add(e):
                    b[1].append(e[0])
                    if len(b[1]) >= 4000 and not indir:
                        flush(b)
                    return True
                return add

            def ex(e):
                p = './' + e[1] if indir else e[0]
                return call([x.replace('{}', p) for x in cmd], None, cwd=(os.path.dirname(e[0]) or '.') if indir else None)[0] == 0
            return ex
        cfg['action'] = False
        raise Fail(f'unsupported option {t}')

    def flush(b):
        if not b[1]:
            return
        if b[2]:
            for p in b[1]:
                if call(b[0] + ['./' + os.path.basename(p)], None, cwd=os.path.dirname(p) or '.')[0]:
                    cfg['rc'] = 1
        elif call(b[0] + b[1], None)[0]:
            cfg['rc'] = 1
        b[1].clear()

    def printf(fmt):
        fmt = unescape(fmt)

        def run(e):
            s = stat_of(e)
            top = e[5]

            def rep(m):
                w, c = m[1], m[2]
                if c[0] in 'TAC' and len(c) == 2:
                    ts = getattr(s, {'T': 'st_mtime', 'A': 'st_atime', 'C': 'st_ctime'}[c[0]])
                    if c[1] == '@':
                        v = '%.10f' % ts
                    elif c[1] == '+':
                        v = time.strftime('%Y-%m-%d+%H:%M:%S', time.localtime(ts))
                    else:
                        v = time.strftime('%F' if c[1] == 'F' else '%' + c[1], time.localtime(ts))
                else:
                    kind = st.filemode(s.st_mode)[0].replace('-', 'f')
                    v = {'p': e[0], 'f': e[1], 'h': os.path.dirname(e[0]) or '.', 's': s.st_size, 'k': s.st_blocks // 2, 'b': s.st_blocks,
                         'm': '%o' % (s.st_mode & 0o7777), 'M': st.filemode(s.st_mode), 'y': kind, 'd': e[6], 'i': s.st_ino,
                         'n': s.st_nlink, 'U': s.st_uid, 'G': s.st_gid, 'P': e[0][len(top):].lstrip('/'), 'H': top, '%': '%',
                         'l': os.readlink(e[0]) if kind == 'l' else '', 'u': owner(s.st_uid), 'g': group(s.st_gid),
                         't': time.ctime(s.st_mtime)}.get(c)
                    if v is None:
                        raise Fail(f'unsupported -printf %{c}')
                return ('%' + w + 's') % v
            out(re.sub(r'%(-?\d*)([TAC].|.)', rep, fmt))
            return True
        return run
    expr = p_list() if toks else (lambda e: True)
    if pos < len(toks):
        raise Fail(f'unexpected {toks[pos]}', 1)
    act = cfg['action']

    def visit(p, nm, depth, top):
        e = [p, nm, None, depth == 0, False, top, depth]
        try:
            s = stat_of(e)
        except OSError as x:
            err(oserr(x, p))
            cfg['rc'] = 1
            return
        isdir = st.S_ISDIR(s.st_mode)

        def test():
            if depth >= cfg['min']:
                try:
                    if expr(e) and not act:
                        out(p + '\n')
                except (FileNotFoundError, PermissionError) as x:
                    err(oserr(x, p))
                    cfg['rc'] = 1
        if not cfg['depth']:
            test()
        if isdir and depth < cfg['max'] and not e[4]:
            try:
                names = sorted(os.listdir(p))
            except OSError as x:
                err(oserr(x, p))
                cfg['rc'] = 1
                names = []
            for n in names:
                visit(p + n if p.endswith('/') else p + '/' + n, n, depth + 1, top)
        if cfg['depth']:
            test()
    sys.setrecursionlimit(10000)
    try:
        for t in tops:
            visit(t, os.path.basename(t.rstrip('/')) or t, 0, t)
    except Quit:
        pass
    for b in batches:
        flush(b)
    return cfg['rc']


def owner(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def group(gid):
    try:
        import grp
        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return str(gid)


def unescape(s):
    def f(m):
        c = m[1]
        if c[0] in '01234567':
            return chr(int(c, 8))
        if c[0] == 'x' and len(c) > 1:
            return chr(int(c[1:], 16))
        return {'\\': '\\', 'b': '\b', **ESC}.get(c, '\\' + c)
    return re.sub(r'\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|.)', f, s, flags=re.S)


# ---- sed ----

class Sed:
    def __init__(self, ere):
        self.ere, self.cmds, self.last, self.labels, self.files, self.quiet = ere, [], None, {}, {}, False

    def regex(self, pat, icase=False, multi=False):
        if pat == '':
            return None
        return rx(pat, self.ere, icase, 1, multi)

    def delim(self, s, i, isre):
        """Read up to the delimiter s[i-1]; returns (text, next index)."""
        d, o, n, start = s[i - 1], [], len(s), i

        def scan(aware):
            j, o = start, []
            while j < n:
                c = s[j]
                if c == '\\' and j + 1 < n:
                    x = s[j + 1]
                    if x == d and d != '\\':
                        o.append(x if isre != 1 or d not in '.*[]^$+?(){}|' else ('\\' + x if d in '.*[]^$' else x))
                        if isre == 1 and d in '+?(){}|' and self.ere:
                            o[-1] = '\\' + x
                    else:
                        o.append(c + x)
                    j += 2
                    continue
                if c == d:
                    return ''.join(o), j + 1
                if c == '\n' and isre == 1:
                    return None
                if c == '[' and aware and isre == 1:
                    e = j + 1
                    if s[e:e + 1] == '^':
                        e += 1
                    if s[e:e + 1] == ']':
                        e += 1
                    while e < n and s[e] != ']':
                        if s[e] == '[' and s[e + 1:e + 2] in (':', '.', '='):
                            k = s.find(s[e + 1] + ']', e + 2)
                            e = k + 1 if k > 0 else e
                        e += 1
                    if e >= n:
                        return None
                    b = s[j:e + 1]
                    o.append(b.replace('\\' + d, d))
                    j = e + 1
                    continue
                o.append(c)
                j += 1
            return None
        r = scan(True) or scan(False)
        if r is None:
            raise Fail('unterminated command or address in sed script', 1)
        return r

    def parse(self, s):
        i, n, stack = 0, len(s), []

        def ws(i, extra=''):
            while i < n and s[i] in ' \t' + extra:
                i += 1
            return i

        def addr(i):
            if i < n and s[i].isdigit():
                m = re.compile(r'(\d+)(?:\s*~\s*(\d+))?').match(s, i)
                return (('step', int(m[1]), int(m[2])) if m[2] else ('n', int(m[1]))), m.end()
            if i < n and s[i] == '$':
                return ('$',), i + 1
            if i < n and s[i] in '/\\':
                if s[i] == '\\':
                    i += 1
                p, i = self.delim(s, i + 1, 1)
                m = re.compile('[IM]*').match(s, i)
                return ('re', self.regex(p, 'I' in m[0], 'M' in m[0])), m.end()
            return None, i
        while True:
            i = ws(i, '\n;')
            if i >= n:
                break
            a1, i = addr(i)
            a2 = None
            i = ws(i)
            if a1 and i < n and s[i] == ',':
                i = ws(i + 1)
                m = re.compile(r'([+~])(\d+)').match(s, i)
                if m:
                    a2, i = (m[1], int(m[2])), m.end()
                else:
                    a2, i = addr(i)
                    if not a2:
                        raise Fail('unexpected address after , in sed script', 1)
            i = ws(i)
            neg = False
            while i < n and s[i] == '!':
                neg, i = True, ws(i + 1)
            if i >= n:
                raise Fail('missing command in sed script', 1)
            c = s[i]
            i += 1
            cmd = {'c': c, 'a1': a1, 'a2': a2, 'neg': neg, 'on': False, 'end': 0}
            if c == '#':
                e = s.find('\n', i)
                i = n if e < 0 else e
                continue
            if c == '{':
                stack.append(len(self.cmds))
                self.cmds.append(cmd)
                continue
            if c == '}':
                if not stack:
                    raise Fail('unexpected } in sed script', 1)
                self.cmds[stack.pop()]['to'] = len(self.cmds)
            elif c in ':':
                m = re.compile(r'\s*([^\s;}]+)').match(s, i)
                if not m:
                    raise Fail('missing label in sed script', 1)
                self.labels[m[1]] = len(self.cmds)
                i = m.end()
                continue
            elif c in 'btT':
                m = re.compile(r'[ \t]*([^\s;}]*)').match(s, i)
                cmd['label'] = m[1]
                i = m.end()
            elif c in 'aic':
                i = ws(i)
                if i < n and s[i] == '\\':
                    j = ws(i + 1)
                    i = j + 1 if j < n and s[j] == '\n' else i + 1 if j >= n or s[i + 1] in ' \t' else i + 1
                o = []
                while i < n and s[i] != '\n':
                    if s[i] == '\\' and i + 1 < n:
                        i += 1
                        o.append(ESC.get(s[i], s[i]) if s[i] == 't' else s[i])
                    else:
                        o.append(s[i])
                    i += 1
                cmd['text'] = ''.join(o) + '\n'
            elif c in 'rRwW':
                e = s.find('\n', i)
                e = n if e < 0 else e
                cmd['file'] = s[i:e].strip()
                i = e
                if c in 'wW':
                    self.open(cmd['file'])
            elif c == 's':
                p, i = self.delim(s, i + 1, 1)
                d = s[i - 1]
                rep, i = self.delim(s, i, 2)
                m = re.compile(r'[gpiIeMm0-9]*').match(s, i)
                fl, i = m[0], m.end()
                if 'e' in fl:
                    raise Fail('the s command flag e is not supported')
                cmd['re'] = self.regex(p, 'i' in fl.lower(), 'm' in fl.lower())
                cmd['rep'] = self.replacement(rep)
                cmd['g'], cmd['p'] = 'g' in fl, 'p' in fl
                d = re.search(r'\d+', fl)
                cmd['n'] = int(d[0]) if d else 1
                if i < n and s[i] == 'w':
                    e = s.find('\n', i)
                    e = n if e < 0 else e
                    cmd['file'] = s[i + 1:e].strip()
                    self.open(cmd['file'])
                    i = e
            elif c == 'y':
                a, i = self.delim(s, i + 1, 0)
                b, i = self.delim(s, i, 0)
                a, b = unescape(a), unescape(b)
                if len(a) != len(b):
                    raise Fail('strings for y command are different lengths', 1)
                cmd['map'] = str.maketrans(a, b)
            elif c in 'qQlL':
                m = re.compile(r'[ \t]*(\d*)').match(s, i)
                cmd['num'], i = int(m[1] or 0), m.end()
            elif c not in 'dDpPnNhHgGx=zF':
                raise Fail(f'unsupported sed command {c}')
            self.cmds.append(cmd)
            i = ws(i)
            if i < n and s[i] not in '\n;}#':
                raise Fail(f'extra characters after sed command {c}', 1)
        if stack:
            raise Fail('unmatched { in sed script', 1)

    def replacement(self, s):
        o, i = [], 0
        while i < len(s):
            c = s[i]
            if c == '\\' and i + 1 < len(s):
                i += 1
                x = s[i]
                if x.isdigit():
                    o.append(int(x))
                elif x in 'LUElu':
                    o.append((x,))
                elif x in ESC and x != 'a':
                    o.append(ESC[x])
                else:
                    o.append(x)
            elif c == '&':
                o.append(0)
            else:
                o.append(c)
            i += 1
        return o

    def open(self, f):
        if f not in self.files:
            self.files[f] = None if f in ('/dev/stdout', '/dev/stderr') else open(f, 'wb')

    def write(self, f, s):
        if f == '/dev/stdout':
            out(s)
        elif f == '/dev/stderr':
            State.out.flush()
            sys.stderr.write(s)
        else:
            self.files[f].write(enc(s))
            self.files[f].flush()

    def expand(self, rep, m):
        o, mode, once = [], '', ''
        for x in rep:
            if type(x) is tuple:
                if x[0] in 'lu':
                    once = x[0]
                else:
                    mode, once = ('' if x[0] == 'E' else x[0]), ''
                continue
            if type(x) is int:
                if x > m.re.groups:
                    raise Fail(f'invalid reference \\{x} in s command', 1)
                x = m[x] or ''
            if mode:
                x = x.lower() if mode == 'L' else x.upper()
            if once and x:
                x = (x[0].upper() if once == 'u' else x[0].lower()) + x[1:]
                once = ''
            o.append(x)
        return ''.join(o)

    def rx(self, r):
        if r is None:
            r = self.last
            if r is None:
                raise Fail('no previous regular expression', 1)
        self.last = r
        return r

    def run(self, recs, write, sep='\n', name='-'):
        """recs: list of (line, has newline). Returns exit code or None to go on."""
        st = self
        for c in self.cmds:
            if c['c'] in 'btT':
                if c['label'] and c['label'] not in self.labels:
                    raise Fail(f"can't find label {c['label']}", 1)
        pos, no, hold, N = 0, 0, '', len(recs)
        cmds, ncmd = self.cmds, len(self.cmds)
        for c in cmds:
            c['on'] = bool(c['a1'] and c['a1'] == ('n', 0))
        ps, nl, append, sub = '', True, [], False

        def hit(a):
            k = a[0]
            if k == 'n':
                return no == a[1]
            if k == '$':
                return pos >= N
            if k == 're':
                return st.rx(a[1]).search(ps) is not None
            return no >= a[1] and (no - a[1]) % a[2] == 0 if a[2] else no == a[1]

        def selected(c):
            a1, a2 = c['a1'], c['a2']
            if a1 is None:
                return True
            if a2 is None:
                return hit(a1)
            if c['on']:
                if a2[0] == 'n' or a2[0] in '+~':
                    e = a2[1] if a2[0] == 'n' else c['end']
                    if no >= e:
                        c['on'] = False
                    return no <= e or (a1[0] != 'n' and a2[0] == 'n' and hit(a1))
                if hit(a2):
                    c['on'] = False
                return True
            if hit(a1):
                if a2[0] == 'n':
                    c['on'] = a2[1] > no
                elif a2[0] == '+':
                    c['end'] = no + a2[1]
                    c['on'] = a2[1] > 0
                elif a2[0] == '~':
                    c['end'] = -(-no // a2[1]) * a2[1] if a2[1] else no
                    c['on'] = c['end'] > no
                else:
                    c['on'] = a2[0] != '$' or pos < N
                return True
            return False

        def flush():
            for k, v in append:
                if k == 't':
                    write(v)
                else:
                    try:
                        write(text(v))
                    except OSError:
                        pass
            append.clear()

        def emit(s, e=None):
            write(s + (sep if nl or e else ''))
        restart = False
        while True:
            if not restart:
                if pos >= N:
                    return None
                ps, nl = recs[pos]
                pos += 1
                no += 1
            restart, sub, ip, deleted = False, False if not restart else sub, 0, False
            while ip < ncmd:
                c = cmds[ip]
                ip += 1
                k = c['c']
                if selected(c) == c['neg']:
                    if k == '{':
                        ip = c['to'] + 1
                    continue
                if k == 's':
                    r = st.rx(c['re'])
                    want, cnt = c['n'], 0
                    if c['g'] and want == 1 or not r.search(ps):
                        o, at, done = [], 0, False
                        for m in r.finditer(ps):
                            if done and m.start() == m.end() == at:
                                continue
                            o += [ps[at:m.start()], st.expand(c['rep'], m)]
                            at, done = m.end(), True
                        new = ''.join(o) + ps[at:]
                    else:
                        o, at, done = [], 0, False
                        for m in r.finditer(ps):
                            cnt += 1
                            if cnt >= want:
                                o += [ps[at:m.start()], st.expand(c['rep'], m)]
                                at, done = m.end(), True
                                if not c['g']:
                                    break
                        new = ''.join(o) + ps[at:]
                    if done:
                        ps, sub = new, True
                        if c['p']:
                            emit(ps)
                        if 'file' in c:
                            st.write(c['file'], ps + '\n')
                elif k == 'p':
                    emit(ps, pos < N)
                elif k == 'd':
                    deleted = True
                    break
                elif k in 'bt' or k == 'T':
                    if k == 'b' or (k == 't') == sub:
                        ip = st.labels[c['label']] if c['label'] else ncmd
                    if k != 'b':
                        sub = False
                elif k in 'nN':
                    if pos >= N:
                        if not st.quiet:
                            emit(ps)
                        flush()
                        return None
                    if k == 'n':
                        if not st.quiet:
                            emit(ps)
                        flush()
                        ps, nl = recs[pos]
                    else:
                        flush()
                        ps, nl = ps + sep + recs[pos][0], recs[pos][1]
                    pos += 1
                    no += 1
                elif k == 'D':
                    if '\n' in ps:
                        ps = ps.split('\n', 1)[1]
                        restart = True
                    deleted = True
                    break
                elif k == 'P':
                    write(ps.split('\n', 1)[0] + sep)
                elif k == 'h':
                    hold = ps
                elif k == 'H':
                    hold += '\n' + ps
                elif k == 'g':
                    ps = hold
                elif k == 'G':
                    ps += '\n' + hold
                elif k == 'x':
                    ps, hold = hold, ps
                elif k == '=':
                    write(f'{no}\n')
                elif k == 'a':
                    append.append(('t', c['text']))
                elif k == 'i':
                    write(c['text'])
                elif k == 'c':
                    if c['a2'] is None or c['neg'] or not c['on']:
                        write(c['text'])
                    deleted = True
                    break
                elif k == 'r':
                    append.append(('f', c['file']))
                elif k == 'w':
                    st.write(c['file'], ps + '\n')
                elif k == 'W':
                    st.write(c['file'], ps.split('\n', 1)[0] + '\n')
                elif k == 'R':
                    c.setdefault('lines', lines(text(c['file'])) if os.path.exists(c['file']) else [])
                    if c['lines']:
                        append.append(('t', c['lines'].pop(0) + '\n'))
                elif k == 'y':
                    ps = ps.translate(c['map'])
                elif k == 'z':
                    ps = ''
                elif k == 'F':
                    write(name + '\n')
                elif k == 'l':
                    o = ''
                    for b in enc(ps):
                        z = chr(b)
                        o += {'\\': '\\\\', '\n': '\\n', '\t': '\\t', '\r': '\\r', '\a': '\\a', '\b': '\\b', '\f': '\\f', '\v': '\\v'}.get(
                            z) or (z if 32 <= b < 127 else '\\%03o' % b)
                    while len(o) > 69:
                        write(o[:69] + '\\\n')
                        o = o[69:]
                    write(o + '$\n')
                elif k in 'qQ':
                    if k == 'q' and not st.quiet:
                        emit(ps)
                    flush()
                    return c['num']
            if not deleted and not st.quiet:
                emit(ps, pos < N)
            flush()


@command('sed')
def sed(name, args):
    # -i may carry a suffix (GNU) or be followed by one (BSD: -i '' or -i .bak)
    a, out_, seen = list(args), [], False
    i = 0
    while i < len(a):
        x = a[i]
        if x == '--':
            out_ += a[i:]
            break
        if re.fullmatch(r'-[nErsuz]*i', x) and i + 1 < len(a):
            y, after = a[i + 1], a[i + 2:]
            if y == '' or (re.fullmatch(r'\.\w+', y) and after and not os.path.exists(y)):
                out_ += [x + y]
                i += 2
                continue
        elif re.fullmatch(r'-[nErsuz]*[efl]', x) and i + 1 < len(a):
            out_.append(x)
            i += 1
            x = a[i]
        out_.append(x)
        i += 1
    o, rest = opts(out_, 'nErsuze:f:i?I?', 'quiet=n silent=n regexp-extended=E expression=e file=f in-place=i separate=s '
                   'null-data=z unbuffered=u posix=posix')
    script, ere, inplace, quiet, sepa, sep = [], False, None, False, False, '\n'
    for k, v in o:
        if k == 'e':
            script.append(v)
        elif k == 'f':
            t = text(v)
            if t.startswith('#n\n'):
                quiet = True
            script.append(t[:-1] if t.endswith('\n') else t)
        elif k in 'Er':
            ere = True
        elif k in 'iI':
            inplace = v or ''
        elif k == 'n':
            quiet = True
        elif k == 's':
            sepa = True
        elif k == 'z':
            sep = '\0'
    if not script:
        if not rest:
            raise Fail('usage: sed [options] script [file...]', 1)
        script = [rest.pop(0)]
    s = Sed(ere)
    s.quiet = quiet
    s.parse('\n'.join(script))
    if script[0].startswith('#n\n'):
        s.quiet = True

    def recs(t):
        r = [(x, True) for x in lines(t, sep)]
        if r and not t.endswith(sep):
            r[-1] = (r[-1][0], False)
        return r
    bad = []
    if inplace is not None:
        if not rest:
            raise Fail('no input files', 1)
        for f, t in texts(rest, bad):
            o = []
            rc = s.run(recs(t), o.append, sep, f)
            if inplace:
                b = os.path.join(os.path.dirname(f), inplace.replace('*', os.path.basename(f))) if '*' in inplace else f + inplace
                with open(b, 'wb') as h:
                    h.write(enc(t))
            with open(f, 'wb') as h:
                h.write(enc(''.join(o)))
            if rc:
                return rc
    else:
        group_, rc = [], None
        for f, t in texts(rest, bad):
            r = recs(t)
            if sepa:
                rc = s.run(r, out, sep, f)
                if rc is not None:
                    return rc
            else:
                if group_ and not group_[-1][1]:
                    group_[-1] = (group_[-1][0], True)
                group_ += r
        if not sepa:
            rc = s.run(group_, out, sep)
        if rc is not None:
            return rc
    return 1 if bad else 0

# ---- awk: the program is translated to Python functions ----

class SN(str):
    """A string from input: a number too, if it looks like one."""
    __slots__ = ()


class Un(SN):
    __slots__ = ()


UN = Un('')
NUMRE = re.compile(r'[ \t\r\n\f\v]*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)')
NUMFULL = re.compile(r'[ \t\r\n\f\v]*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[ \t\r\n\f\v]*\Z')


class Arr(dict):
    def __missing__(self, k):
        self[k] = UN
        return UN


def num(v):
    if type(v) is float:
        return v
    m = NUMRE.match(v)
    return float(m[1]) if m else 0.0


def truth(v):
    if type(v) is float:
        return v != 0
    if isinstance(v, SN) and NUMFULL.match(v):
        return float(v) != 0
    return v != ''


def awk_cmp(a, b):
    def isnum(v):
        return type(v) is float or (isinstance(v, SN) and (v is UN or NUMFULL.match(v)))
    if isnum(a) and isnum(b):
        a, b = num(a), num(b)
    else:
        a, b = AWK.S(a), AWK.S(b)
    return (a > b) - (a < b)


class AwkNext(Exception):
    pass


class AwkNextFile(Exception):
    pass


class AwkExit(Exception):
    pass


AWK = None
AWKFN = ('length substr index split sub gsub match sprintf sin cos atan2 exp log sqrt int rand srand tolower toupper system close fflush '
         'gensub strftime systime mktime strtonum asort asorti and or xor lshift rshift compl').split()
AWKKW = 'BEGIN END function func if else while for do break continue next nextfile exit return delete in getline print printf'.split()
AWKVARS = 'FS OFS ORS RS NR FNR NF FILENAME SUBSEP CONVFMT OFMT RSTART RLENGTH RT ARGC'.split()


class AwkParser:
    TOK = re.compile(r'''(?P<ws>(?:[ \t\r]|\\\r?\n|\#[^\n]*)+)|(?P<nl>\n)|(?P<num>0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
        |(?P<name>[A-Za-z_]\w*)|(?P<op>\*\*=|\*\*|\+\+|--|\+=|-=|\*=|/=|%=|\^=|==|<=|>=|!=|!~|&&|\|\||>>|[{}()\[\];,+\-*/%^!<>|?:~$=])''', re.X)

    def __init__(self, src):
        self.toks, self.p, self.src = [], 0, src
        self.funcs, self.ctx, self.arrays, self.calls, self.globals = {}, None, set(), [], set()
        self.lex(src)

    def lex(self, s):
        i, n, line = 0, len(s), 1
        while i < n:
            c = s[i]
            prev = self.toks[-1] if self.toks else ('nl', '')
            if c == '"':
                j, o = i + 1, []
                while j < n and s[j] != '"':
                    if s[j] == '\\' and j + 1 < n:
                        m = re.compile(r'\\([0-7]{1,3})').match(s, j)
                        x = s[j + 1]
                        if m:
                            o.append(chr(int(m[1], 8)))
                            j = m.end()
                            continue
                        o.append({'"': '"', '\\': '\\', '/': '/', 'b': '\b', '\n': '', **ESC}.get(x, '\\' + x))
                        j += 2
                        continue
                    if s[j] == '\n':
                        raise Fail(f'line {line}: string not closed')
                    o.append(s[j])
                    j += 1
                if j >= n:
                    raise Fail(f'line {line}: string not closed')
                self.toks.append(('str', ''.join(o), line))
                i = j + 1
                continue
            if c == '/' and not (prev[0] in ('num', 'str', 'name', 'ere') or prev[0] == 'bi' and prev[1] == 'length'
                                 or prev[0] == 'op' and prev[1] in (')', ']', '$', '++', '--')):
                j, o, br = i + 1, [], False
                while j < n and (s[j] != '/' or br):
                    if s[j] == '\n':
                        raise Fail(f'line {line}: regular expression not closed')
                    if s[j] == '\\' and j + 1 < n:
                        o.append('/' if s[j + 1] == '/' else s[j:j + 2])
                        j += 2
                        continue
                    if s[j] == '[' and not br:
                        k = j + 1
                        if s[k:k + 1] == '^':
                            k += 1
                        if s[k:k + 1] == ']':
                            k += 1
                        e = s.find(']', k)
                        while e > 0 and (s[s.rfind('[', k, e):s.rfind('[', k, e) + 2] == '[:' and s[e - 1] == ':'):
                            e = s.find(']', e + 1)
                        if 0 < e and '\n' not in s[j:e]:
                            o.append(s[j:e + 1].replace('\\/', '/'))
                            j = e + 1
                            continue
                    o.append(s[j])
                    j += 1
                if j >= n:
                    raise Fail(f'line {line}: regular expression not closed')
                self.toks.append(('ere', ''.join(o), line))
                i = j + 1
                continue
            m = self.TOK.match(s, i)
            if not m:
                raise Fail(f'syntax error at line {line}: {c}')
            i, k, v = m.end(), m.lastgroup, m[0]
            if k == 'ws':
                line += v.count('\n')
                continue
            if k == 'name':
                if v in AWKKW:
                    k = 'kw'
                    v = 'function' if v == 'func' else v
                elif v in AWKFN:
                    k = 'bi'
                elif s[i:i + 1] == '(':
                    k = 'fn'
            self.toks.append((k, v, line))
            if k == 'nl':
                line += 1
        self.toks += [('nl', '\n', line), ('eof', '', line)]

    # helpers
    def at(self, v, k=0):
        t = self.toks[min(self.p + k, len(self.toks) - 1)]
        return t[1] == v and t[0] in ('op', 'kw')

    def take(self, v):
        if self.at(v):
            self.p += 1
            return True
        return False

    def need(self, v):
        if not self.take(v):
            self.bad()

    def bad(self):
        t = self.toks[self.p]
        raise Fail(f'syntax error at line {t[2]} near {t[1]!r}')

    def nls(self):
        while self.toks[self.p][0] == 'nl':
            self.p += 1

    def program(self):
        rules = []
        while True:
            while self.toks[self.p][0] == 'nl' or self.at(';'):
                self.p += 1
            if self.toks[self.p][0] == 'eof':
                return rules
            if self.take('function'):
                t = self.toks[self.p]
                self.p += 1
                self.need('(')
                params = []
                while not self.take(')'):
                    self.nls()
                    params.append(self.toks[self.p][1])
                    self.p += 1
                    self.nls()
                    self.take(',')
                self.nls()
                self.funcs[t[1]] = (params, None)
                self.ctx = t[1]
                self.funcs[t[1]] = (params, self.block())
                self.ctx = None
                continue
            if self.take('BEGIN'):
                pat = ('BEGIN',)
            elif self.take('END'):
                pat = ('END',)
            elif self.at('{'):
                pat = None
            else:
                a = self.expr()
                pat = ('pat', a)
                if self.take(','):
                    self.nls()
                    pat = ('range', a, self.expr())
            rules.append((pat, self.block() if self.at('{') else None))

    def block(self):
        self.need('{')
        r = []
        while True:
            while self.toks[self.p][0] == 'nl' or self.at(';'):
                self.p += 1
            if self.take('}'):
                return r
            r.append(self.stmt())

    def term(self):
        if self.at('}'):
            return
        if not (self.take(';') or self.toks[self.p][0] == 'nl'):
            self.bad()

    def body(self):
        self.nls()
        if self.take(';'):
            return []
        if self.at('{'):
            return self.block()
        return [self.stmt()]

    def stmt(self):
        if self.at('{'):
            return ('block', self.block())
        if self.take('if'):
            self.need('(')
            c = self.expr()
            self.need(')')
            a, b, save = self.body(), None, self.p
            while self.toks[self.p][0] == 'nl' or self.at(';'):
                self.p += 1
            if self.take('else'):
                b = self.body()
            else:
                self.p = save
            return ('if', c, a, b)
        if self.take('while'):
            self.need('(')
            c = self.expr()
            self.need(')')
            return ('while', c, self.body())
        if self.take('do'):
            b = self.body()
            while self.toks[self.p][0] == 'nl' or self.at(';'):
                self.p += 1
            self.need('while')
            self.need('(')
            c = self.expr()
            self.need(')')
            self.term()
            return ('do', c, b)
        if self.take('for'):
            self.need('(')
            t = self.toks[self.p]
            if t[0] == 'name' and self.at('in', 1) and self.toks[self.p + 2][0] == 'name' and self.at(')', 3):
                arr = self.toks[self.p + 2][1]
                self.p += 4
                self.arr(arr)
                return ('forin', self.var(t[1]), arr, self.body())
            a = None if self.at(';') else self.simple()
            self.need(';')
            self.nls()
            c = None if self.at(';') else self.expr()
            self.need(';')
            self.nls()
            b = None if self.at(')') else self.simple()
            self.need(')')
            return ('for', a, c, b, self.body())
        s = self.simple()
        self.term()
        return s

    def simple(self):
        for k in ('break', 'continue', 'next', 'nextfile'):
            if self.take(k):
                return (k,)
        if self.take('exit'):
            return ('exit', None if self.end() else self.expr())
        if self.take('return'):
            return ('return', None if self.end() else self.expr())
        if self.take('delete'):
            n = self.toks[self.p][1]
            self.p += 1
            self.arr(n)
            if self.take('['):
                k = self.elist(']')
                return ('delete', n, k)
            return ('delete', n, None)
        if self.at('print') or self.at('printf'):
            k = self.toks[self.p][1]
            self.p += 1
            args, save = [], self.p
            if self.take('('):
                args = self.elist(')')
                t = self.toks[self.p]
                if not (self.end() or t[1] in ('>', '>>', '|') and t[0] == 'op'):
                    self.p, args = save, []
            if not args and not self.end() and not self.at('>') and not self.at('>>') and not self.at('|'):
                args = [self.expr(True)]
                while self.take(','):
                    self.nls()
                    args.append(self.expr(True))
            redir = None
            for op in ('>', '>>', '|'):
                if self.take(op):
                    redir = (op, self.concat(True))
                    break
            return (k, args, redir)
        return ('expr', self.expr())

    def end(self):
        return self.toks[self.p][0] in ('nl', 'eof') or self.at(';') or self.at('}')

    def elist(self, close):
        r = []
        self.nls()
        while not self.take(close):
            r.append(self.expr())
            self.nls()
            if not self.at(close):
                self.need(',')
                self.nls()
        return r

    def scope(self, n):
        return (self.ctx, n) if self.ctx and n in self.funcs[self.ctx][0] else (None, n)

    def arr(self, n):
        s = self.scope(n)
        if s[0] is None:
            self.globals.add(n)
        self.arrays.add(s)

    def var(self, n):
        if n not in AWKVARS:
            s = self.scope(n)
            if s[0] is None:
                self.globals.add(n)
        return ('var', n)

    def expr(self, nogt=False):
        c = self.orx(nogt)
        if self.take('?'):
            self.nls()
            a = self.expr(nogt)
            self.nls()
            self.need(':')
            self.nls()
            return ('cond', c, a, self.expr(nogt))
        t = self.toks[self.p]
        if t[0] == 'op' and t[1] in ('=', '+=', '-=', '*=', '/=', '%=', '^=', '**=') and c[0] in ('var', 'idx', 'field'):
            self.p += 1
            self.nls()
            v = self.expr(nogt)
            return ('set', c, v if t[1] == '=' else ('bin', t[1][:-1].replace('**', '^'), c, v))
        if t[0] == 'op' and t[1] == '=' and c[0] in ('not', 'neg', 'cmp', 'match', 'and', 'or', 'concat', 'bin'):
            # the target of an assignment binds to the last operand: !x = y, a && b = c
            if c[-1][0] in ('var', 'idx', 'field'):
                self.p += 1
                return c[:-1] + (('set', c[-1], self.expr(nogt)),)
        return c

    def orx(self, g):
        a = self.andx(g)
        while self.take('||'):
            self.nls()
            a = ('or', a, self.andx(g))
        return a

    def andx(self, g):
        a = self.inx(g)
        while self.take('&&'):
            self.nls()
            a = ('and', a, self.inx(g))
        return a

    def inx(self, g):
        a = self.matchx(g)
        while self.at('in'):
            self.p += 1
            n = self.toks[self.p][1]
            self.p += 1
            self.arr(n)
            a = ('in', a[1] if a[0] == 'group' else [a], n)
        return a

    def matchx(self, g):
        a = self.cmpx(g)
        while self.at('~') or self.at('!~'):
            op = self.toks[self.p][1]
            self.p += 1
            b = self.cmpx(g)
            a = ('match', op, a, b)
        return a

    def cmpx(self, g):
        a = self.concat(g)
        for op in ('<', '<=', '==', '!=', '>=') + (() if g else ('>',)):
            if self.take(op):
                return ('cmp', op, a, self.concat(g))
        while self.at('|') and self.at('getline', 1):
            self.p += 2
            a = ('getline', 'cmd', self.lvalue_opt(), a)
        return a

    def concat(self, g):
        a = self.add(g)
        while True:
            t = self.toks[self.p]
            if t[0] in ('num', 'str', 'ere', 'name', 'fn', 'bi') or (t[0] == 'op' and t[1] in ('$', '!', '(', '++', '--', '-', '+')
                                                                  and t[1] not in ('+', '-')):
                if t[1] == '!' and t[0] == 'op':
                    break
                if self.at('in'):
                    break
                a = ('concat', a, self.add(g))
            else:
                return a
        return a

    def add(self, g):
        a = self.mul(g)
        while self.at('+') or self.at('-'):
            op = self.toks[self.p][1]
            self.p += 1
            a = ('bin', op, a, self.mul(g))
        return a

    def mul(self, g):
        a = self.unary(g)
        while self.at('*') or self.at('/') or self.at('%'):
            op = self.toks[self.p][1]
            self.p += 1
            a = ('bin', op, a, self.unary(g))
        return a

    def unary(self, g):
        if self.take('!'):
            return ('not', self.unary(g))
        if self.take('-'):
            return ('neg', self.unary(g))
        if self.take('+'):
            return ('pos', self.unary(g))
        return self.power(g)

    def power(self, g):
        a = self.postfix(g)
        if self.take('^') or self.take('**'):
            # right associative; the exponent may have a sign
            if self.take('-'):
                return ('bin', '^', a, ('neg', self.power(g)))
            self.take('+')
            return ('bin', '^', a, self.power(g))
        return a

    def postfix(self, g):
        a = self.primary(g)
        if a[0] in ('var', 'idx', 'field'):
            if self.take('++'):
                return ('post', a, '+')
            if self.take('--'):
                return ('post', a, '-')
        return a

    def lvalue_opt(self):
        t = self.toks[self.p]
        if t[0] == 'name' or t[1] == '$' and t[0] == 'op':
            return self.primary(False)
        return None

    def primary(self, g):
        t = self.toks[self.p]
        self.p += 1
        k, v = t[0], t[1]
        if k == 'num':
            return ('num', float(int(v, 16)) if v[:2] in ('0x', '0X') else float(v))
        if k == 'str':
            return ('str', v)
        if k == 'ere':
            return ('ere', v)
        if k == 'name':
            if self.take('['):
                self.arr(v)
                return ('idx', v, self.elist(']'))
            return self.var(v)
        if k == 'fn':
            self.need('(')
            a = self.elist(')')
            for i, x in enumerate(a):
                if x[0] == 'var':
                    self.calls.append((self.scope(x[1]), v, i))
            return ('call', v, a)
        if k == 'bi':
            if self.at('('):
                self.p += 1
                a = self.elist(')')
            elif v == 'length':
                a = []
            else:
                self.bad()
            if v == 'split' and len(a) > 1:
                self.arr(a[1][1])
            if v in ('asort', 'asorti'):
                for x in a:
                    self.arr(x[1])
            if v == 'match' and len(a) > 2:
                self.arr(a[2][1])
            return ('bi', v, a)
        if k == 'kw' and v == 'getline':
            lv = self.lvalue_opt()
            if self.take('<'):
                return ('getline', 'file', lv, self.postfix(True))
            return ('getline', 'main', lv, None)
        if k == 'op':
            if v == '$':
                if self.at('++') or self.at('--'):
                    return ('field', self.primary(g))
                return ('field', ('neg', self.primary(g)) if self.take('-') else self.primary(g))
            if v in ('++', '--'):
                a = self.primary(g)
                if a[0] not in ('var', 'idx', 'field'):
                    self.bad()
                return ('pre', a, v[0])
            if v == '(':
                a = self.elist(')')
                if len(a) == 1:
                    return ('paren', a[0])
                if not self.at('in'):
                    self.bad()
                return ('group', a)
            if v == '-':
                return ('neg', self.primary(g))
        self.p -= 1
        self.bad()


class AwkGen:
    """Python source for a parsed program."""
    def __init__(self, ps, rules):
        self.ps, self.o, self.ind, self.fn, self.loops, self.rx, self.uniq = ps, [], 0, None, [], [], 0
        arrays, change = ps.arrays, True
        arrays |= {(None, 'ARGV'), (None, 'ENVIRON')}
        while change:
            change = False
            for s, f, i in ps.calls:
                if f not in ps.funcs or i >= len(ps.funcs[f][0]):
                    continue
                q = (f, ps.funcs[f][0][i])
                if (s in arrays) != (q in arrays):
                    arrays |= {s, q}
                    change = True
        ps.globals |= {'ARGV', 'ENVIRON'}
        for n, (params, body) in ps.funcs.items():
            self.fn = n
            self.w(f'def f_{n}({", ".join("p_" + p + "=None" for p in params)}):')
            self.ind += 1
            self.globals()
            for p in params:
                self.w(f'if p_{p} is None: p_{p} = {"Arr()" if (n, p) in arrays else "UN"}')
            self.stmts(body)
            self.w('return UN')
            self.ind -= 1
        self.fn = None
        self.part('begin', [r for r in rules if r[0] == ('BEGIN',)])
        main = [r for r in rules if r[0] not in (('BEGIN',), ('END',))]
        self.part('main', main)
        self.part('end', [r for r in rules if r[0] == ('END',)])
        self.has_main, self.has_end = bool(main), any(r[0] == ('END',) for r in rules)

    def w(self, s):
        self.o.append('    ' * self.ind + s)

    def globals(self):
        if self.ps.globals:
            self.w('global ' + ', '.join('v_' + g for g in sorted(self.ps.globals)))
        self.w('pass')

    def part(self, name, rules):
        self.w(f'def rules_{name}():')
        self.ind += 1
        self.globals()
        for i, (pat, body) in enumerate(rules):
            if pat and pat[0] == 'pat':
                self.w(f'if {self.c(pat[1])}:')
            elif pat and pat[0] == 'range':
                self.w(f'if R.rng.get({i}) or {self.c(pat[1])}:')
                self.ind += 1
                self.w(f'R.rng[{i}] = not {self.c(pat[2])}')
                self.ind -= 1
            else:
                self.w('if 1:')
            self.ind += 1
            if body is None:
                self.w('R.prt(R.fget(0) + R.ORS)')
            else:
                self.stmts(body)
            self.ind -= 1
        self.ind -= 1

    def stmts(self, b):
        if not b:
            self.w('pass')
        for s in b:
            getattr(self, 's_' + s[0])(s)

    def suite(self, b):
        self.ind += 1
        self.stmts(b)
        self.ind -= 1

    def s_block(self, s):
        self.stmts(s[1])

    def s_expr(self, s):
        self.w(self.e(s[1]))

    def s_if(self, s):
        self.w(f'if {self.c(s[1])}:')
        self.suite(s[2])
        if s[3] is not None:
            self.w('else:')
            self.suite(s[3])

    def s_while(self, s):
        self.w(f'while {self.c(s[1])}:')
        self.loops.append(None)
        self.suite(s[2])
        self.loops.pop()

    def s_do(self, s):
        self.w('while True:')
        self.loops.append(lambda: self.w(f'if not ({self.c(s[1])}): break'))
        self.suite(s[2])
        self.ind += 1
        self.loops.pop()()
        self.ind -= 1

    def s_for(self, s):
        if s[1]:
            self.stmts([s[1]])
        self.w(f'while {self.c(s[2]) if s[2] else "True"}:')
        self.loops.append((lambda: self.stmts([s[3]])) if s[3] else None)
        self.suite(s[4])
        step = self.loops.pop()
        if step:
            self.ind += 1
            step()
            self.ind -= 1

    def s_forin(self, s):
        self.uniq += 1
        k = f'k{self.uniq}'
        self.w(f'for {k} in list({self.a(s[2])}):')
        self.loops.append(None)
        self.ind += 1
        self.w(f'if {k} not in {self.a(s[2])}: continue')
        self.w(self.assign(s[1], f'SN({k})'))
        self.ind -= 1
        self.suite(s[3])
        self.loops.pop()

    def s_break(self, s):
        self.w('break' if self.loops else 'raise AwkNext')

    def s_continue(self, s):
        if self.loops and self.loops[-1]:
            self.loops[-1]()
        self.w('continue' if self.loops else 'raise AwkNext')

    def s_next(self, s):
        self.w('raise AwkNext' if self.fn else 'return')

    def s_nextfile(self, s):
        self.w('raise AwkNextFile')

    def s_exit(self, s):
        if s[1]:
            self.w(f'R.code = int(num({self.e(s[1])})) & 255')
        self.w('raise AwkExit')

    def s_return(self, s):
        if not self.fn:
            raise Fail('return outside a function')
        self.w(f'return {self.e(s[1]) if s[1] else "UN"}')

    def s_delete(self, s):
        self.w(f'{self.a(s[1])}.clear()' if s[2] is None else f'{self.a(s[1])}.pop({self.key(s[2])}, None)')

    def s_print(self, s):
        a = ' + R.OFS + '.join(f'R.P({self.e(x)})' for x in s[1]) if s[1] else 'R.fget(0)'
        self.w(self.redir(f'{a} + R.ORS', s[2]))

    def s_printf(self, s):
        if not s[1]:
            raise Fail('printf needs a format')
        self.w(self.redir(f'R.fmt({", ".join(self.e(x) for x in s[1])})', s[2]))

    def redir(self, a, r):
        return f'R.prt({a}, {r[0]!r}, S({self.e(r[1])}))' if r else f'R.prt({a})'

    # names
    def a(self, n):
        return 'p_' + n if self.fn and n in self.ps.funcs[self.fn][0] else 'v_' + n

    def key(self, k):
        return ' + R.SUBSEP + '.join(f'S({self.e(x)})' for x in k)

    def assign(self, lv, v):
        if lv[0] == 'var':
            if lv[1] in AWKVARS and not (self.fn and lv[1] in self.ps.funcs[self.fn][0]):
                return f'R.sv({lv[1]!r}, {v})'
            return f'({self.a(lv[1])} := {v})'
        if lv[0] == 'idx':
            return f'R.aset({self.a(lv[1])}, {self.key(lv[2])}, {v})'
        return f'R.fset(num({self.e(lv[1])}), {v})'

    # conditions give Python truth, expressions give awk values
    def c(self, x):
        k = x[0]
        if k == 'cmp':
            return f'(awk_cmp({self.e(x[2])}, {self.e(x[3])}) {x[1]} 0)'
        if k == 'match':
            return f'({"not " if x[1] == "!~" else ""}{self.re(x[3])}.search(S({self.e(x[2])})))'
        if k == 'ere':
            return f'({self.re(x)}.search(R.fget(0)))'
        if k == 'and' or k == 'or':
            return f'({self.c(x[1])} {k} {self.c(x[2])})'
        if k == 'not':
            return f'(not {self.c(x[1])})'
        if k == 'in':
            return f'({self.key(x[1])} in {self.a(x[2])})'
        if k == 'paren':
            return self.c(x[1])
        return f'truth({self.e(x)})'

    def re(self, x):
        while x[0] == 'paren':
            x = x[1]
        if x[0] == 'ere':
            self.rx.append(rx(x[1], True, False, 2))
            return f'RX[{len(self.rx) - 1}]'
        return f'R.dyn(S({self.e(x)}))'

    def n(self, x):
        s = self.e(x)
        return s if x[0] in ('num', 'bin', 'neg', 'pos', 'pre', 'post') else f'num({s})'

    def e(self, x):
        k = x[0]
        if k == 'num':
            return repr(x[1]) if x[1] < 1e300 else f'float("{x[1]}")'
        if k == 'str':
            return repr(x[1])
        if k == 'paren':
            return self.e(x[1])
        if k == 'var':
            if x[1] in AWKVARS and not (self.fn and x[1] in self.ps.funcs[self.fn][0]):
                return 'R.' + x[1]
            return self.a(x[1])
        if k == 'idx':
            return f'{self.a(x[1])}[{self.key(x[2])}]'
        if k == 'field':
            return f'R.fget(num({self.e(x[1])}))'
        if k == 'set':
            return self.assign(x[1], self.e(x[2]))
        if k == 'bin':
            a, b = self.n(x[2]), self.n(x[3])
            if x[1] in '+-*':
                return f'({a} {x[1]} {b})'
            return f'R.{ {"/": "div", "%": "mod", "^": "pow"}[x[1]] }({a}, {b})'
        if k == 'neg':
            return f'(-{self.n(x[1])})'
        if k == 'pos':
            return f'(+{self.n(x[1])})'
        if k == 'pre':
            return f'num({self.assign(x[1], f"{self.n(x[1])} {x[2]} 1.0")})'
        if k == 'post':
            return f'({self.e(("pre", x[1], x[2]))} {"-" if x[2] == "+" else "+"} 1.0)'
        if k == 'concat':
            return f'(S({self.e(x[1])}) + S({self.e(x[2])}))'
        if k == 'cond':
            return f'({self.e(x[2])} if {self.c(x[1])} else {self.e(x[3])})'
        if k in ('cmp', 'match', 'ere', 'and', 'or', 'not', 'in'):
            return f'(1.0 if {self.c(x)} else 0.0)'
        if k == 'call':
            if x[1] not in self.ps.funcs:
                raise Fail(f'calling undefined function {x[1]}')
            if len(x[2]) > len(self.ps.funcs[x[1]][0]):
                raise Fail(f'too many arguments for function {x[1]}')
            o = []
            for a, p in zip(x[2], self.ps.funcs[x[1]][0]):
                o.append(self.a(a[1]) if (x[1], p) in self.ps.arrays and a[0] == 'var' else self.e(a))
            return f'f_{x[1]}({", ".join(o)})'
        if k == 'getline':
            g = f'R.gl_{x[1]}({"S(" + self.e(x[3]) + ")" if x[3] else ""})'
            s = self.assign(x[2], 'R.gval') if x[2] else 'R.set0(R.gval)'
            return f'(({s}, R.grc)[1] if {g} > 0 else R.grc)'
        if k == 'bi':
            return self.builtin(x[1], x[2])
        raise Fail(f'syntax error in {k}')

    def builtin(self, f, a):
        n = len(a)
        if f == 'length':
            if not a:
                return 'float(len(R.fget(0)))'
            if a[0][0] == 'var':
                s = (self.fn, a[0][1]) if self.fn and a[0][1] in self.ps.funcs[self.fn][0] else (None, a[0][1])
                if s in self.ps.arrays:
                    return f'float(len({self.a(a[0][1])}))'
            return f'float(len(S({self.e(a[0])})))'
        need = {'substr': (2, 3), 'index': (2, 2), 'split': (2, 3), 'sub': (2, 3), 'gsub': (2, 3), 'match': (2, 3), 'sprintf': (1, 99),
                'atan2': (2, 2), 'rand': (0, 0), 'srand': (0, 1), 'fflush': (0, 1), 'gensub': (3, 4), 'strftime': (0, 3),
                'systime': (0, 0), 'asort': (1, 2), 'asorti': (1, 2), 'and': (2, 2), 'or': (2, 2), 'xor': (2, 2), 'lshift': (2, 2),
                'rshift': (2, 2)}.get(f, (1, 1))
        if not need[0] <= n <= need[1]:
            raise Fail(f'wrong number of arguments for {f}')
        if f in ('sub', 'gsub'):
            t = a[2] if n > 2 else ('field', ('num', 0.0))
            call_ = f'R.sub({self.re(a[0])}, S({self.e(a[1])}), S({self.e(t)}), {f == "gsub"})'
            if t[0] not in ('var', 'idx', 'field'):
                return f'float({call_})'
            return f'(({self.assign(t, "R.sres")}, R.scnt)[1] if {call_} else 0.0)'
        if f == 'match':
            arr = self.a(a[2][1]) if n > 2 else 'None'
            return f'R.match(S({self.e(a[0])}), {self.re(a[1])}, {arr})'
        if f == 'split':
            sep = 'R.FS' if n < 3 else self.re(a[2]) if a[2][0] == 'ere' else f'S({self.e(a[2])})'
            return f'R.split(S({self.e(a[0])}), {self.a(a[1][1])}, {sep})'
        if f == 'gensub':
            return f'R.gensub({self.re(a[0])}, S({self.e(a[1])}), S({self.e(a[2])}), S({self.e(a[3]) if n > 3 else "R.fget(0)"}))'
        if f in ('asort', 'asorti'):
            return f'R.{f}({", ".join(self.a(x[1]) for x in a)})'
        if f == 'sprintf':
            return f'R.fmt({", ".join(self.e(x) for x in a)})'
        if f in ('tolower', 'toupper'):
            return f'S({self.e(a[0])}).{f[2:]}()'
        if f == 'index':
            return f'float(S({self.e(a[0])}).find(S({self.e(a[1])})) + 1)'
        return f'R.b_{f}({", ".join(self.e(x) for x in a)})'


class Reader:
    def __init__(self, s):
        self.s, self.p = s, 0

    def next(self, rs):
        s, p = self.s, self.p
        if p >= len(s):
            return None, ''
        if rs == '\n':
            e = s.find('\n', p)
            f = e + 1
        elif rs == '':
            m = re.compile(r'\n*').match(s, p)
            p = m.end()
            if p >= len(s):
                return None, ''
            m = re.compile(r'\n\n+').search(s, p)
            if m:
                e, f = m.span()
            else:
                e = len(s) - s.endswith('\n')
                f = len(s)
        elif len(rs) == 1:
            e = s.find(rs, p)
            f = e + 1
        else:
            m = AWK.dyn(rs).search(s, p)
            e, f = m.span() if m and m.end() > m.start() else (-1, 0)
        if e < 0:
            e = f = len(s)
        self.p = f
        return s[p:e], s[e:f]


class AwkRun:
    def __init__(self):
        import math
        self.math = math
        self.FS, self.OFS, self.ORS, self.RS = ' ', ' ', '\n', '\n'
        self.NR = self.FNR = 0.0
        self.FILENAME, self.SUBSEP, self.CONVFMT, self.OFMT, self.RT = '', '\x1c', '%.6g', '%.6g', ''
        self.RSTART, self.RLENGTH, self.ARGC = 0.0, -1.0, 0.0
        self.rng, self.code, self.rxs, self.files, self.readers, self.pipes = {}, 0, {}, {}, {}, {}
        self.rec, self.flds, self.dirty = SN(''), [], False
        self.gval, self.grc, self.main, self.argi, self.ns, self.rand = UN, 0.0, None, 1, None, None

    def S(self, v):
        if type(v) is float:
            return self.numstr(v, self.CONVFMT)
        return v

    def P(self, v):
        return self.numstr(v, self.OFMT) if type(v) is float else v

    def numstr(self, v, f):
        if v == v and abs(v) < 1e16 and v == int(v):
            return str(int(v))
        if v != v:
            return 'nan'
        if v in (float('inf'), float('-inf')):
            return 'inf' if v > 0 else '-inf'
        if v == int(v) and abs(v) < 1e300:
            return str(int(v))
        return f % v if f == '%.6g' else self.fmt(f, v)

    # record and fields; flds is None until needed
    def set0(self, s):
        self.rec, self.flds, self.dirty = SN(s), None, False
        return self.rec

    def load(self):
        if self.flds is None:
            s = self.rec
            if self.FS == ' ':
                self.flds = [SN(x) for x in s.split()] if s.isascii() else [SN(x) for x in re.split(r'[ \t\n]+', s.strip(' \t\n')) if x]
            else:
                self.flds = [SN(x) for x in self.parts(s, self.FS)]

    def parts(self, s, fs):
        if s == '':
            return []
        if not isinstance(fs, str):
            return self.resplit(fs, s)
        if fs == ' ':
            return re.split(r'[ \t\n]+', s.strip(' \t\n')) if s.strip(' \t\n') else []
        if self.RS == '' and fs is self.FS and fs != '\n':
            return self.resplit(self.dyn((re.escape(fs) if len(fs) == 1 else f'({fs})') + '|\n'), s)
        if fs == '':
            return list(s)
        if len(fs) == 1 and fs != '\\':
            return s.split(fs)
        return self.resplit(self.dyn(fs), s)

    def resplit(self, r, s):
        o, p = [], 0
        for m in r.finditer(s):
            if m.end() > m.start():
                o.append(s[p:m.start()])
                p = m.end()
        o.append(s[p:])
        return o

    def fget(self, i):
        if i == 0:
            if self.dirty:
                self.rec, self.dirty = SN(self.OFS.join(self.flds)), False
            return self.rec
        if i < 0:
            raise Fail(f'trying to access out of range field {int(i)}')
        self.load()
        i = int(i)
        return self.flds[i - 1] if i <= len(self.flds) else UN

    def fset(self, i, v):
        if i == 0:
            self.set0(self.S(v))
            return v
        if i < 0 or i > 1e7:
            raise Fail('trying to access out of range field')
        self.load()
        i = int(i)
        while len(self.flds) < i:
            self.flds.append(UN)
        s = self.S(v)
        self.flds[i - 1] = v if isinstance(v, SN) else SN(s) if type(v) is float else AwkStr(s)
        self.dirty = True
        return v

    @property
    def NF(self):
        self.load()
        return float(len(self.flds))

    @NF.setter
    def NF(self, v):
        self.load()
        n = int(num(v))
        self.flds = self.flds[:n] + [UN] * (n - len(self.flds))
        self.dirty = True

    def sv(self, n, v):
        if n == 'FS':
            self.load()
        if n in ('NR', 'FNR', 'NF', 'RSTART', 'RLENGTH', 'ARGC'):
            setattr(self, n, num(v))
        else:
            setattr(self, n, self.S(v))
        return v

    def aset(self, a, k, v):
        a[k] = v
        return v

    def dyn(self, p):
        r = self.rxs.get(p)
        if r is None:
            r = self.rxs[p] = rx(p, True, False, 2)
        return r

    def div(self, a, b):
        if b == 0:
            raise Fail('division by zero')
        return a / b

    def mod(self, a, b):
        if b == 0:
            raise Fail('division by zero in %')
        return self.math.fmod(a, b)

    def pow(self, a, b):
        try:
            r = a ** b
            return r if type(r) is float else float('nan')
        except (OverflowError, ZeroDivisionError):
            return float('inf')

    def fmt(self, f, *args):
        f, args, i = self.S(f), list(args), 0

        def arg():
            nonlocal i
            i += 1
            if i > len(args):
                raise Fail('not enough arguments for printf format')
            return args[i - 1]

        def one(m):
            fl, w, p, c = m[1], m[2], m[3], m[4]
            if c == '%':
                return '%'
            if w == '*':
                w = str(int(num(arg())))
                if w[0] == '-':
                    fl, w = fl + '-', w[1:]
            if p == '.*':
                p = '.' + str(int(num(arg())))
            p = p or ''
            v = arg()
            if c in 'di':
                v = num(v)
                if v != v or abs(v) == float('inf'):
                    return ('%' + fl.replace('0', '') + w + 's') % self.numstr(v, '%.6g')
                return ('%' + fl + w + p + 'd') % int(v)
            if c in 'ouxX':
                return ('%' + fl + w + p + c.replace('u', 'd')) % (int(num(v)) if num(v) >= 0 else int(num(v)) + (1 << 64))
            if c in 'eEfFgGa':
                return ('%' + fl + w + p + c) % num(v)
            if c == 'c':
                v = chr(int(v) & 0x1fffff if v >= 0 and v < 0x110000 else 0xfffd) if type(v) is float else v[:1]
                return ('%' + fl.replace('0', '') + w + 's') % v
            if c == 's':
                return ('%' + fl.replace('0', '').replace('+', '') + w + p + 's') % self.S(v)
            raise Fail(f'bad printf format %{c}')
        return re.sub(r"%([-+ #0']*)(\*|\d*)(\.\*|\.\d*)?(?:hh|h|ll|l|L|q|j|z|t)?(.)", one, f, flags=re.S)

    def prt(self, s, op=None, f=None):
        if op is None or f in ('/dev/stdout', '-') and op != '|':
            out(s)
        elif op == '|':
            self.pipes.setdefault(f, []).append(enc(s))
        elif f == '/dev/stderr':
            State.out.flush()
            sys.stderr.write(s)
        else:
            if f not in self.files:
                try:
                    self.files[f] = open(f, 'wb' if op == '>' else 'ab')
                except OSError as e:
                    raise Fail(f"can't redirect to {oserr(e, f)}") from None
            self.files[f].write(enc(s))

    def got(self, r, rc=None):
        self.gval, self.grc = (SN(r), 1.0) if r is not None else (UN, 0.0)
        return self.grc

    def gl_file(self, f):
        if f not in self.readers:
            if f in self.files:
                self.files[f].flush()
            try:
                self.readers[f] = Reader(text(f))
            except OSError:
                self.grc = -1.0
                return -1.0
        return self.got(self.readers[f].next(self.RS)[0])

    def gl_cmd(self, c):
        k = ('|', c)
        if k not in self.readers:
            self.flush()
            self.readers[k] = Reader(dec(shell(c, None, True)[1]))
        r = self.got(self.readers[k].next(self.RS)[0])
        if r > 0:
            self.NR += 1
        return r

    def gl_main(self):
        return self.got(self.next())

    def next(self):
        while True:
            if self.main is None:
                if not self.open():
                    return None
            r, self.RT = self.main.next(self.RS)
            if r is not None:
                self.NR += 1
                self.FNR += 1
                return r
            self.main = None

    def open(self):
        argv, used = self.ns['v_ARGV'], getattr(self, 'used', False)
        while self.argi < int(self.ARGC):
            a = argv.get(str(self.argi), '')
            self.argi += 1
            a = self.S(a)
            if a == '':
                continue
            m = re.match(r'([A-Za-z_]\w*)=', a)
            if m:
                self.setvar(m[1], a[m.end():])
                continue
            self.used = True
            try:
                self.main = Reader(text(a))
            except OSError as e:
                err(f"can't open file {oserr(e, a)}")
                self.code = 2
                continue
            self.FILENAME, self.FNR = SN(a), 0.0
            return True
        if not used and not getattr(self, 'used', False):
            self.used = True
            self.main = Reader(text('-'))
            return True
        return False

    def setvar(self, n, v):
        v = SN(re.sub(r'\\([0-7]{1,3}|.)', lambda m: chr(int(m[1], 8)) if m[1][0] in '01234567' else
               {'\\': '\\', '"': '"', '/': '/', 'b': '\b', **ESC}.get(m[1], '\\' + m[1]), v, flags=re.S))
        if n in AWKVARS:
            self.sv(n, v)
        else:
            self.ns['v_' + n] = v

    def flush(self):
        State.out.flush()
        for f in self.files.values():
            f.flush()

    def b_close(self, f):
        f, rc = self.S(f), -1.0
        if f in self.pipes:
            self.flush()
            rc = float(shell(f, b''.join(self.pipes.pop(f)))[0])
        if f in self.files:
            self.files.pop(f).close()
            rc = 0.0
        for k in (f, ('|', f)):
            if k in self.readers:
                del self.readers[k]
                rc = 0.0
        return rc

    def b_system(self, c):
        self.flush()
        return float(shell(self.S(c))[0])

    def b_fflush(self, f=None):
        self.flush()
        return 0.0

    def b_substr(self, s, m, n=None):
        s = self.S(s)
        m = num(m)
        e = float('inf') if n is None else self.rnd(m) + self.rnd(num(n))
        m = max(self.rnd(m), 1)
        if e != e or e <= m:
            return ''
        return s[int(m) - 1:None if e > len(s) else int(e) - 1]

    def rnd(self, v):
        return float(round(v)) if abs(v) < 1e15 else v

    def b_int(self, v):
        v = num(v)
        return float(self.math.trunc(v)) if abs(v) < 1e300 else v

    def fn(self, f, *a):
        try:
            return float(f(*a))
        except ValueError:
            return float('nan')
        except OverflowError:
            return float('inf')

    def b_sin(self, v):
        return self.fn(self.math.sin, num(v))

    def b_cos(self, v):
        return self.fn(self.math.cos, num(v))

    def b_atan2(self, a, b):
        return self.math.atan2(num(a), num(b))

    def b_exp(self, v):
        return self.fn(self.math.exp, num(v))

    def b_log(self, v):
        return float('-inf') if num(v) == 0 else self.fn(self.math.log, num(v))

    def b_sqrt(self, v):
        return self.fn(self.math.sqrt, num(v))

    def b_rand(self):
        if self.rand is None:
            self.b_srand(0.0)
        return self.rand.random()

    def b_srand(self, v=None):
        import random
        import time
        old, self.seed = getattr(self, 'seed', 0.0), float(int(time.time())) if v is None else num(v)
        self.rand = random.Random(self.seed)
        return old

    def b_systime(self):
        import time
        return float(int(time.time()))

    def b_strftime(self, f='%a %b %e %H:%M:%S %Z %Y', t=None, utc=UN):
        import time
        t = time.time() if t is None else num(t)
        return time.strftime(self.S(f), time.gmtime(t) if truth(utc) else time.localtime(t))

    def b_mktime(self, s):
        import time
        p = [int(x) for x in self.S(s).split()][:6]
        return float(time.mktime((*p, 0, 0, -1))) if len(p) == 6 else -1.0

    def b_strtonum(self, s):
        s = self.S(s).strip()
        try:
            return float(int(s, 16) if s[:2].lower() == '0x' else int(s, 8) if re.fullmatch('0[0-7]+', s) else num(s))
        except ValueError:
            return 0.0

    def b_and(self, a, b):
        return float(int(num(a)) & int(num(b)))

    def b_or(self, a, b):
        return float(int(num(a)) | int(num(b)))

    def b_xor(self, a, b):
        return float(int(num(a)) ^ int(num(b)))

    def b_lshift(self, a, b):
        return float(int(num(a)) << int(num(b)))

    def b_rshift(self, a, b):
        return float(int(num(a)) >> int(num(b)))

    def b_compl(self, a):
        return float(int(num(a)) ^ ((1 << 53) - 1))

    def sorted(self, vals):
        import functools
        return sorted(vals, key=functools.cmp_to_key(awk_cmp))

    def asort(self, a, b=None):
        v = self.sorted(a.values())
        b = a if b is None else b
        b.clear()
        b.update((str(i + 1), x) for i, x in enumerate(v))
        return float(len(v))

    def asorti(self, a, b=None):
        v = sorted(a)
        b = a if b is None else b
        b.clear()
        b.update((str(i + 1), SN(x)) for i, x in enumerate(v))
        return float(len(v))

    def sub(self, r, rep, s, g):
        parts = re.findall(r'\\\\&|\\&|&|\\\\|\\|[^&\\]+', rep)

        def f(m):
            return ''.join(m[0] if x == '&' else '&' if x == '\\&' else '\\' + m[0] if x == '\\\\&' else x for x in parts)
        if '&' not in rep:
            def f(m):
                return rep
        if not g:
            self.sres, n = r.subn(f, s, 1)
        else:
            o, p, n, last = [], 0, 0, -1
            for m in r.finditer(s):
                if m.start() == m.end() == last:
                    continue
                o += [s[p:m.start()], f(m)]
                p = last = m.end()
                n += 1
            self.sres = ''.join(o) + s[p:]
        self.sres = AwkStr(self.sres)
        self.scnt = float(n)
        return n

    def gensub(self, r, rep, how, s):
        cnt, want = 0, int(num(how)) or 1
        g = how[:1] in 'gG' and how != ''

        def f(m):
            nonlocal cnt
            cnt += 1
            if not g and cnt != want:
                return m[0]
            return re.sub(r'\\([0-9&\\])|&', lambda k: m[0] if k[0] == '&' else (m[int(k[1])] or '') if k[1].isdigit() else k[1], rep)
        return r.sub(f, s)

    def match(self, s, r, arr):
        m = r.search(s)
        if arr is not None:
            arr.clear()
        if not m:
            self.RSTART, self.RLENGTH = 0.0, -1.0
            return 0.0
        self.RSTART, self.RLENGTH = float(m.start() + 1), float(m.end() - m.start())
        if arr is not None:
            for i in range(r.groups + 1):
                if m[i] is not None:
                    arr[str(i)] = SN(m[i])
                    arr[str(i) + self.SUBSEP + 'start'] = float(m.start(i) + 1)
                    arr[str(i) + self.SUBSEP + 'length'] = float(len(m[i]))
        return self.RSTART

    def split(self, s, a, fs):
        p = [SN(x) for x in self.parts(s, fs)]
        a.clear()
        a.update((str(i + 1), x) for i, x in enumerate(p))
        return float(len(p))


class AwkStr(str):
    __slots__ = ()


@command('awk', 'gawk', 'nawk')
def awk(name, args):
    global AWK
    o, rest = opts(args, 'F:v:f:', 'field-separator=F assign=v file=f version re-interval=ri posix=ri', stop=True)
    R, pre, src, saved = AwkRun(), [], [], AWK
    for k, v in o:
        if k == 'F':
            R.FS = '\t' if v == 't' else unescape_awk(v)
        elif k == 'v':
            if not re.match(r'[A-Za-z_]\w*=', v):
                raise Fail(f'invalid variable assignment {v}')
            pre.append(v.split('=', 1))
        elif k == 'f':
            src.append(text(v))
        elif k == 'version':
            put('awk (devtools)')
            return 0
    if not src:
        if not rest:
            raise Fail('usage: awk [-F fs] [-v var=value] [prog | -f progfile] [file...]')
        src = [rest.pop(0)]
    AWK = R
    try:
        ps = AwkParser('\n'.join(src))
        g = AwkGen(ps, ps.program())
        code = '\n'.join(g.o)
        ns = {'R': R, 'S': R.S, 'num': num, 'truth': truth, 'awk_cmp': awk_cmp, 'SN': SN, 'UN': UN, 'Arr': Arr, 'RX': g.rx,
              'AwkNext': AwkNext, 'AwkExit': AwkExit, 'AwkNextFile': AwkNextFile}
        for n in ps.globals:
            ns['v_' + n] = Arr() if (None, n) in ps.arrays else UN
        R.ns = ns
        ns['v_ARGV'].update((str(i), SN(a)) for i, a in enumerate([name] + rest))
        R.ARGC = float(len(rest) + 1)
        ns['v_ENVIRON'].update((k, SN(v)) for k, v in os.environ.items())
        for k, v in pre:
            R.setvar(k, v)
        sys.setrecursionlimit(20000)
        exec(compile(code, '<awk>', 'exec'), ns)
        begin, main, end = ns['rules_begin'], ns['rules_main'], ns['rules_end']
        try:
            try:
                begin()
                if g.has_main or g.has_end:
                    while True:
                        r = R.next()
                        if r is None:
                            break
                        R.set0(r)
                        try:
                            main()
                        except AwkNext:
                            pass
                        except AwkNextFile:
                            R.main = None
            except AwkExit:
                pass
            try:
                if g.has_end:
                    end()
            except AwkExit:
                pass
        except ZeroDivisionError:
            raise Fail('division by zero') from None
        for f in list(R.files) + list(R.pipes):
            R.b_close(f)
        return R.code
    finally:
        for f in R.files.values():
            f.close()
        AWK = saved


def unescape_awk(v):
    return re.sub(r'\\([0-7]{1,3}|.)', lambda m: chr(int(m[1], 8)) if m[1][0] in '01234567' else
                  {'\\': '\\\\', **ESC}.get(m[1], '\\' + m[1]), v)


# ---- diff, patch, cmp ----

def lcs(a, b):
    """Changed blocks [(a0, a1, b0, b1)] between two sequences (Myers' greedy algorithm)."""
    n, m, pre = len(a), len(b), 0
    while pre < n and pre < m and a[pre] == b[pre]:
        pre += 1
    while n > pre and m > pre and a[n - 1] == b[m - 1]:
        n -= 1
        m -= 1
    if n == pre and m == pre:
        return []
    # lines that the other side does not have cannot match
    sa, sb = set(a[pre:n]), set(b[pre:m])
    ia, ib = [k for k in range(pre, n) if a[k] in sb], [k for k in range(pre, m) if b[k] in sa]
    xa, xb = [a[k] for k in ia], [b[k] for k in ib]
    N, M = len(xa), len(xb)
    if not N or not M:
        pairs = []
    else:
        V, trace, found = {1: 0}, [], None
        limit = 1000
        for d in range(min(N + M, limit) + 1):
            trace.append(V.copy())
            for k in range(-d, d + 1, 2):
                x = V[k + 1] if k == -d or (k != d and V[k - 1] < V[k + 1]) else V[k - 1] + 1
                y = x - k
                while x < N and y < M and xa[x] == xb[y]:
                    x += 1
                    y += 1
                V[k] = x
                if x >= N and y >= M:
                    found = d
                    break
            if found is not None:
                break
        pairs = []
        if found is None:
            import difflib
            for t in difflib.SequenceMatcher(None, xa, xb, False).get_matching_blocks():
                pairs += [(ia[t.a + i], ib[t.b + i]) for i in range(t.size)]
        else:
            x, y = N, M
            for d in range(found, 0, -1):
                V, k = trace[d], x - y
                pk = k + 1 if k == -d or (k != d and V[k - 1] < V[k + 1]) else k - 1
                px = V[pk]
                py = px - pk
                while x > px and y > py:
                    x -= 1
                    y -= 1
                    pairs.append((ia[x], ib[y]))
                x, y = px, py
            while x > 0:
                x -= 1
                y -= 1
                pairs.append((ia[x], ib[y]))
            pairs.reverse()
    blocks, i, j = [], pre, pre
    for x, y in pairs + [(n, m)]:
        if x > i or y > j:
            blocks.append([i, x, j, y])
        i, j = x + 1, y + 1
    # slide pure insertions and deletions down as far as they go, as diff tools do
    k = len(blocks) - 1
    while k >= 0:
        bl = blocks[k]
        while True:
            e = blocks[k + 1] if k + 1 < len(blocks) else [len(a), 0, len(b), 0]
            if bl[0] == bl[1] and bl[3] < e[2] and bl[1] < e[0] and b[bl[2]] == b[bl[3]]:
                bl[:] = [x + 1 for x in bl]
            elif bl[2] == bl[3] and bl[1] < e[0] and bl[3] < e[2] and a[bl[0]] == a[bl[1]]:
                bl[:] = [x + 1 for x in bl]
            else:
                break
        if k + 1 < len(blocks) and blocks[k + 1][0] == bl[1] and blocks[k + 1][2] == bl[3]:
            bl[1], bl[3] = blocks[k + 1][1], blocks[k + 1][3]
            del blocks[k + 1]
        k -= 1
    return [tuple(x) for x in blocks]


@command('diff')
def diff(name, args):
    import time
    o, rest = opts(args, 'urRqNwbBiasU:cC:dL:x:pE', 'unified=un? recursive=r brief=q new-file=N ignore-all-space=w '
                   'ignore-space-change=b ignore-blank-lines=B ignore-case=i text=a report-identical-files=s minimal=d label=L '
                   'exclude=x strip-trailing-cr=cr show-c-function=p color? ignore-trailing-space=Z normal context=cn? '
                   'no-dereference=nd ignore-tab-expansion=E speed-large-files=d')
    f, labels, exc, ctx, style = set(), [], [], 3, 'n'
    for k, v in o:
        if k in ('u', 'U', 'un'):
            style = 'u'
            ctx = integer(v) if v else ctx
        elif k in ('c', 'C', 'cn'):
            style = 'c'
            ctx = integer(v) if v else ctx
        elif k == 'L':
            labels.append(v)
        elif k == 'x':
            exc.append(glob_re(v))
        elif k == 'color':
            if v == 'always':
                raise Fail('unsupported option --color=always')
        else:
            f.add(k)
    if 'R' in f or 'E' in f:
        raise Fail('unsupported option')
    if len(rest) != 2:
        raise Fail('usage: diff [options] file1 file2')

    def key(s):
        if 'cr' in f and s.endswith('\r'):
            s = s[:-1]
        if 'w' in f:
            s = re.sub(r'\s+', '', s)
        elif 'b' in f:
            s = re.sub(r'\s+', ' ', s.rstrip())
        elif 'Z' in f:
            s = s.rstrip()
        return s.lower() if 'i' in f else s
    plain = not f & {'cr', 'w', 'b', 'Z', 'i'}

    def get(p):
        if p is None:
            return b'', 0
        d = data(p)
        return d, os.stat(p).st_mtime_ns if p != '-' else time.time_ns()

    def stamp(p, t):
        s, ns = divmod(t, 10 ** 9)
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s)) + f'.{ns:09d} ' + time.strftime('%z', time.localtime(s))

    def rng(a, b, mark=','):
        return f'{b}' if b - a <= 1 else f'{a + 1}{mark}{b}'

    def header(p, q, hp, hq, tp, tq, m1, m2):
        for i, (h, t, mk) in enumerate(((hp, tp, m1), (hq, tq, m2))):
            put(mk + ' ' + labels[i] if i < len(labels) else f'{mk} {h}\t{stamp(h, t)}')

    def files(p, q, hp, hq):
        (d1, t1), (d2, t2) = get(p), get(q)
        if d1 == d2:
            if 's' in f:
                put(f'Files {hp} and {hq} are identical')
            return 0
        if 'a' not in f and (b'\0' in d1[:65536] or b'\0' in d2[:65536]):
            put(f'Binary files {hp} and {hq} differ' if 'q' not in f else f'Files {hp} and {hq} differ')
            return 1
        s1, s2 = dec(d1), dec(d2)
        l1, l2 = lines(s1), lines(s2)
        e1, e2 = bool(l1) and not s1.endswith('\n'), bool(l2) and not s2.endswith('\n')
        k1, k2 = (l1[:], l2[:]) if plain else (list(map(key, l1)), list(map(key, l2)))
        if e1 and not f & {'w', 'b', 'Z'}:
            k1[-1] += '\0'
        if e2 and not f & {'w', 'b', 'Z'}:
            k2[-1] += '\0'
        ids = {}
        bl = lcs([ids.setdefault(x, len(ids)) for x in k1], [ids.setdefault(x, len(ids)) for x in k2])
        if 'B' in f:
            bl = [x for x in bl if any(y.strip('\0') for y in k1[x[0]:x[1]] + k2[x[2]:x[3]])]
        if not bl:
            if 's' in f:
                put(f'Files {hp} and {hq} are identical')
            return 0
        if 'q' in f:
            put(f'Files {hp} and {hq} differ')
            return 1
        nonl = '\\ No newline at end of file\n'

        def ln(mark, ls, i, e):
            out(mark + ls[i] + '\n' + (nonl if e and i == len(ls) - 1 else ''))
        if cmdline:
            put('diff ' + ' '.join(cmdline) + f' {hp} {hq}')
        if style == 'n':
            for a, b, c, d in bl:
                put((rng(a, b) if b > a else str(a)) + ('c' if b > a and d > c else 'd' if b > a else 'a') + (rng(c, d) if d > c else str(c)))
                for i in range(a, b):
                    ln('< ', l1, i, e1)
                if b > a and d > c:
                    put('---')
                for i in range(c, d):
                    ln('> ', l2, i, e2)
            return 1
        groups = []
        for x in bl:
            if groups and x[0] - groups[-1][-1][1] <= 2 * ctx:
                groups[-1].append(x)
            else:
                groups.append([x])
        header(p, q, hp, hq, t1, t2, *(('---', '+++') if style == 'u' else ('***', '---')))
        for g in groups:
            a0, a1 = max(0, g[0][0] - ctx), min(len(l1), g[-1][1] + ctx)
            c0, c1 = max(0, g[0][2] - ctx), min(len(l2), g[-1][3] + ctx)
            c0, c1 = max(c0, g[0][2] - (g[0][0] - a0)), g[-1][3] + (a1 - g[-1][1])
            c1 = min(c1, len(l2))
            fn = ''
            if 'p' in f:
                for i in range(a0 - 1, -1, -1):
                    if re.match(r'[A-Za-z_$]', l1[i]):
                        fn = ' ' + l1[i][:40].rstrip()
                        break
            if style == 'u':
                def ur(a, b):
                    return f'{a + 1}' if b - a == 1 else f'{a + 1 if b > a else a},{b - a}'
                put(f'@@ -{ur(a0, a1)} +{ur(c0, c1)} @@{fn}')
                i = a0
                for a, b, c, d in g:
                    for j in range(i, a):
                        ln(' ', l1, j, e1)
                    for j in range(a, b):
                        ln('-', l1, j, e1)
                    for j in range(c, d):
                        ln('+', l2, j, e2)
                    i = b
                for j in range(i, a1):
                    ln(' ', l1, j, e1)
            else:
                def side(ls, e, lo, hi, x, y, mk, sg):
                    put(f'{mk} {rng(lo, hi) if hi > lo else lo} {mk}')
                    if any(z[y] > z[x] for z in g):
                        marks = {}
                        for z in g:
                            for j in range(z[x], z[y]):
                                marks[j] = '! ' if z[1] > z[0] and z[3] > z[2] else sg
                        for j in range(lo, hi):
                            ln(marks.get(j, '  '), ls, j, e)
                put('***************' + fn)
                side(l1, e1, a0, a1, 0, 1, '***', '- ')
                side(l2, e2, c0, c1, 2, 3, '---', '+ ')
        return 1

    def dirs(p, q):
        rc = 0
        n1, n2 = set(os.listdir(p)) if p and os.path.isdir(p) else set(), set(os.listdir(q)) if q and os.path.isdir(q) else set()
        for n in sorted(n1 | n2):
            if any(x(n) for x in exc):
                continue
            a, b = (f'{x[:-1] if x.endswith("/") and len(x) > 1 else x}/{n}' for x in (p, q))
            if n not in n1 or n not in n2:
                here, w = (a, p) if n in n1 else (b, q)
                if 'N' not in f or (os.path.isdir(here) and 'r' not in f):
                    put(f'Only in {w.rstrip("/") or "/"}: {n}')
                    rc = max(rc, 1)
                elif os.path.isdir(here):
                    rc = max(rc, dirs(a, b))
                else:
                    rc = max(rc, files(a if n in n1 else None, b if n in n2 else None, a, b))
                continue
            rc = max(rc, pair(a, b, True))
        return rc

    def pair(a, b, inner=False):
        d1, d2 = os.path.isdir(a), os.path.isdir(b)
        if d1 and d2:
            if inner and 'r' not in f:
                put(f'Common subdirectories: {a} and {b}')
                return 0
            return dirs(a, b)
        if d1 or d2:
            if not inner:
                a, b = (os.path.join(a, os.path.basename(b)), b) if d1 else (a, os.path.join(b, os.path.basename(a)))
                return files(a, b, a, b)
            put(f'File {a} is a {"directory" if d1 else "regular file"} while file {b} is a {"directory" if d2 else "regular file"}')
            return 1
        try:
            return files(a, b, a, b)
        except OSError as e:
            if 'N' in f and isinstance(e, FileNotFoundError):
                a1, b1 = (a if os.path.exists(a) else None), (b if os.path.exists(b) else None)
                return files(a1, b1, a, b) if a1 or b1 else 2
            err(oserr(e))
            return 2
    isd = os.path.isdir(rest[0]) and os.path.isdir(rest[1])
    cmdline = [a for a in args if a not in rest and a != '--'] if isd else None
    if isd and not cmdline:
        cmdline = None
    return pair(*rest)


@command('patch')
def patch(name, args):
    o, rest = opts(args, 'p:i:Rd:NF:sbEftu', 'strip=p input=i reverse=R directory=d dry-run forward=N fuzz=F quiet=s silent=s backup=b '
                   'remove-empty-files=E force=f batch=t unified=u check=dry-run no-backup-if-mismatch=nb verbose=vb')
    f = {k: (True if v is None else v) for k, v in o}
    if 'd' in f:
        os.chdir(f['d'])
    strip = integer(f['p']) if 'p' in f else None
    fuzzmax = integer(f.get('F', 2))
    dry, rev, quiet = 'dry-run' in f, 'R' in f, 's' in f
    src = text(f['i']) if 'i' in f else text(rest[1]) if len(rest) > 1 else text('-')
    ls = lines(src)

    def pname(s):
        s = s.split('\t')[0].rstrip('\r')
        if not s.startswith('"'):
            s = re.sub(r'\s+\d{4}-\d\d-\d\d.*$', '', s).rstrip()
        else:
            s = unescape(s.strip('"'))
        if s == '/dev/null':
            return None
        if strip is None:
            return os.path.basename(s)
        parts = re.sub('/+', '/', s).split('/')
        return '/'.join(parts[strip:]) if len(parts) > strip else None
    # parse into [(old, new, git info, hunks)], a hunk being (old start, old lines, new start, new lines, pairs)
    todo, i, git, cur = [], 0, {}, None
    HU, HN = re.compile(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@'), re.compile(r'(\d+)(?:,(\d+))?([acd])(\d+)(?:,(\d+))?\s*$')
    while i < len(ls):
        s = ls[i].rstrip('\r') if ls[i].startswith(('diff ', '--- ', '+++ ')) else ls[i]
        if s.startswith('diff --git '):
            git, cur = {}, None
            m = re.match(r'diff --git (\S+) (\S+)$', s)
            if m:
                git = {'a': pname(m[1]), 'b': pname(m[2])}
            i += 1
            while i < len(ls) and not ls[i].startswith(('diff --git', '--- ', '@@ ')):
                t = ls[i]
                for w in ('new file mode', 'deleted file mode', 'old mode', 'new mode'):
                    if t.startswith(w):
                        git[w] = t.split()[-1]
                if t.startswith(('rename from ', 'rename to ', 'copy from ', 'copy to ')):
                    w = t.split(' ', 2)
                    git[w[0] + ' ' + w[1]] = '/'.join(w[2].split('/')[max((strip or 1) - 1, 0):])
                if t.startswith('GIT binary patch'):
                    raise Fail('git binary patches are not supported')
                i += 1
            if any(k not in ('a', 'b') for k in git):
                cur = [git.get('a'), git.get('b'), git, []]
                todo.append(cur)
                continue
            continue
        if s.startswith('--- ') and i + 1 < len(ls) and ls[i + 1].startswith('+++ '):
            a, b = pname(s[4:]), pname(ls[i + 1][4:])
            if cur is not None and cur[2] and not cur[3]:
                cur[0], cur[1] = a, b
            else:
                cur = [a, b, {}, []]
                todo.append(cur)
            # an epoch time stamp stands for a file that is not there
            for x, k in ((s, 'made'), (ls[i + 1], 'gone')):
                if re.search(r'\t(1970-01-01|1969-12-31) \d\d:[03]0:00', x):
                    cur[2][k] = ''
            i += 2
            continue
        if s.startswith('*** ') and i + 1 < len(ls) and ls[i + 1].startswith('--- '):
            raise Fail('context diffs are not supported; use diff -u')
        m = HU.match(s)
        if m:
            if cur is None:
                cur = [None, None, {}, []]
                todo.append(cur)
            n1, n2 = int(m[2] or 1) if m[2] is not None else 1, int(m[4]) if m[4] is not None else 1
            body, i = [], i + 1
            first = i
            while i < len(ls) and (n1 > 0 or n2 > 0 or ls[i].startswith('\\')):
                t = ls[i]
                c = t[:1] or ' '
                if c == '\\':
                    if body:
                        body[-1][2] = True
                elif c in ' -+':
                    body.append([c, t[1:], False])
                    n1 -= c != '+'
                    n2 -= c != '-'
                else:
                    raise Fail(f'malformed patch at line {i + 1}: {t}')
                i += 1
            if n1 or n2:
                raise Fail(f'unexpected end of patch in the hunk at line {first}')
            cur[3].append((int(m[1]), int(m[3]), body))
            continue
        m = HN.match(s)
        if m and i + 1 < len(ls) and ls[i + 1][:2] in ('< ', '> ', '<', '>'):
            if cur is None:
                cur = [None, None, {}, []]
                todo.append(cur)
            body, i = [], i + 1
            while i < len(ls) and (ls[i][:2] in ('< ', '> ') or ls[i] in ('<', '>', '---') or ls[i].startswith('\\ ')):
                t = ls[i]
                if t.startswith('\\') and body:
                    body[-1][2] = True
                elif t != '---':
                    body.append(['-' if t[0] == '<' else '+', t[2:], False])
                i += 1
            cur[3].append((int(m[1]), int(m[4]), body))
            continue
        i += 1
    if not any(t[3] or t[2] for t in todo):
        raise Fail('Only garbage was found in the patch input.')

    def safe(p):
        if p is None or os.path.isabs(p) or '..' in p.split('/'):
            raise Fail(f'refusing to patch outside the current directory: {p}')
        real = os.path.realpath(p)
        if real != p and not real.startswith(os.path.realpath('.').rstrip('/') + '/'):
            raise Fail(f'refusing to patch outside the current directory: {p}')
        return p

    def flip(body):
        o, i = [], 0
        while i < len(body):
            if body[i][0] == ' ':
                o.append(body[i])
                i += 1
                continue
            j = i
            while j < len(body) and body[j][0] != ' ':
                j += 1
            o += [['-', x[1], x[2]] for x in body[i:j] if x[0] == '+'] + [['+', x[1], x[2]] for x in body[i:j] if x[0] == '-']
            i = j
        return o

    def apply(have, hunks):
        """Returns (new lines, [(ok, where, offset, fuzz)])."""
        res, delta, lo, log = list(have), 0, 0, []
        for h in hunks:
            old, _, body = h
            done = None
            for fz in range(fuzzmax + 1):
                lead = 0
                while lead < len(body) and body[lead][0] == ' ':
                    lead += 1
                tail = 0
                while tail < len(body) - lead and body[-1 - tail][0] == ' ':
                    tail += 1
                # fuzz drops outer context lines, the longer side first
                big = max(lead, tail)
                cl, ct = max(0, fz - (big - lead)), max(0, fz - (big - tail))
                cl, ct = min(cl, lead), min(ct, tail)
                if fz and not (cl or ct):
                    break
                b = body[cl:len(body) - ct]
                want = [(x[1], x[2]) for x in b if x[0] != '+']
                at = (old - 1 if want or old else old) + cl + delta
                if not want:
                    at = min(max(old + delta, 0), len(res))
                    done = (at, b, want, fz, cl)
                    break
                p = find_at(res, want, at, lo)
                if p is not None:
                    done = (p, b, want, fz, cl)
                    break
            if done is None:
                log.append((False, old, 0, 0))
                continue
            p, b, want, fz, cl = done
            new, k = [], p
            for x in b:
                if x[0] == ' ':
                    new.append(res[k])
                    k += 1
                elif x[0] == '-':
                    k += 1
                else:
                    new.append((x[1], x[2]))
            res[p:p + len(want)] = new
            base = (old - 1 if want else old if old else 0) + cl
            off = p - base - delta
            log.append((True, p - cl + 1, off, fz))
            grow = len(new) - len(want)
            delta = p - base + grow
            lo = p + len(new)
        return res, log

    def find_at(res, want, at, lo):
        n, m = len(want), len(res)

        for d in range(0, m + 1):
            for p in ((at - d, at + d) if d else (at,)):
                if max(lo, 0) <= p <= m - n and all(res[p + q][0] == want[q][0] for q in range(n)):
                    return p
        return None
    rc = 0
    say = (lambda s: None) if quiet else put
    for a, b, git, hunks in todo:
        if rev:
            a, b = b, a
            hunks = [(h[1], h[0], flip(h[2])) for h in hunks]
            git = {{'made': 'gone', 'gone': 'made', 'new file mode': 'deleted file mode', 'deleted file mode': 'new file mode', 'rename from': 'rename to',
                    'rename to': 'rename from', 'old mode': 'new mode', 'new mode': 'old mode'}.get(k, k): v for k, v in git.items()}
        if rest:
            target = rest[0]
        else:
            cands = [x for x in (a, b) if x]
            if 'rename from' in git:
                cands = [git['rename from']]
            ex = [x for x in cands if os.path.exists(x)]
            target = min(ex, key=lambda x: (x.count('/'), len(os.path.basename(x)), len(x))) if ex else (b or a)
            if target is None:
                err("can't find file to patch")
                rc = 1
                continue
            safe(target)
        dest = safe(git['rename to']) if 'rename to' in git and not rest else target
        creating = (a is None and b is not None) or 'new file mode' in git or 'made' in git or (bool(hunks) and hunks[0][0] == 0 and not os.path.exists(target))
        exists = os.path.exists(target)
        if not exists and not creating:
            err(f"can't find file to patch: {target}")
            rc = 1
            continue
        raw = text(target) if exists else ''
        have = [(x, False) for x in lines(raw)]
        if have and not raw.endswith('\n'):
            have[-1] = (have[-1][0], True)
        say(f'checking file {dest}' if dry else f'patching file {dest}')
        res, log = apply(have, hunks)
        if hunks and (not log[0][0] or creating and exists and raw):
            back, blog = apply(have, [(h[1], h[0], flip(h[2])) for h in hunks])
            if all(x[0] and x[3] == 0 for x in blog):
                say('Reversed (or previously applied) patch detected!  Skipping patch.' if not rev else
                    'Unreversed patch detected!  Skipping patch.')
                say(f'{len(hunks)} out of {len(hunks)} hunks ignored')
                rc = 1
                continue
        bad = []
        for n, (h, (ok, where, off, fz)) in enumerate(zip(hunks, log), 1):
            if not ok:
                say(f'Hunk #{n} FAILED at {where}.')
                bad.append(h)
            elif off or fz:
                say(f'Hunk #{n} succeeded at {where}' + (f' with fuzz {fz}' if fz else '') +
                    (f' (offset {off} line{"" if abs(off) == 1 else "s"})' if off else '') + '.')
        if bad:
            rc = 1
            say(f'{len(bad)} out of {len(hunks)} hunk{"" if len(hunks) == 1 else "s"} FAILED' +
                ('' if dry else f' -- saving rejects to file {dest}.rej'))
        if dry:
            continue
        if bad:
            o = [f'--- {a or target}\n+++ {b or target}\n']
            for h in bad:
                n1, n2 = (sum(x[0] != c for x in h[2]) for c in '+-')
                o.append(f'@@ -{h[0]},{n1} +{h[1]},{n2} @@\n')
                o += [x[0] + x[1] + '\n' + ('\\ No newline at end of file\n' if x[2] else '') for x in h[2]]
            with open(dest + '.rej', 'wb') as w:
                w.write(enc(''.join(o)))
        if ('b' in f or bad and 'nb' not in f) and exists:
            with open(dest + '.orig', 'wb') as w:
                w.write(enc(raw))
        if any(x[0] for x in log) or not hunks:
            if dest != target or os.path.dirname(dest):
                os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
            if (b is None and a is not None or 'deleted file mode' in git or 'gone' in git) and not res and not bad:
                if exists:
                    os.unlink(target)
                    try:        # folders left empty go too
                        d = os.path.dirname(target)
                        while d:
                            os.rmdir(d)
                            d = os.path.dirname(d)
                    except OSError:
                        pass
                continue
            if not res and 'E' in f and exists:
                os.unlink(target)
                continue
            mode = os.stat(target).st_mode & 0o777 if exists else None
            if dest != target or (exists and os.path.islink(target)) or hunks or not exists:
                o = ''.join(x + '\n' for x, _ in res)
                if res and res[-1][1]:
                    o = o[:-1]
                if os.path.islink(dest) and dest != target:
                    os.unlink(dest)
                with open(dest, 'wb') as w:
                    w.write(enc(o))
            if dest != target and exists and 'rename from' in git:
                os.unlink(target)
            m = git.get('new file mode') or git.get('new mode')
            if m:
                os.chmod(dest, int(m, 8) & 0o777)
            elif mode is not None and dest != target:
                os.chmod(dest, mode)
    return rc


@command('cmp')
def cmp(name, args):
    o, rest = opts(args, 'sln:i:b', 'silent=s quiet=s verbose=l bytes=n ignore-initial=i print-bytes=b')
    f = dict(o)
    if len(rest) == 1:
        rest.append('-')
    if len(rest) < 2 or 'b' in f:
        raise Fail('usage: cmp [-s] [-l] [-n bytes] [-i skip] file1 file2')
    d = [data(x) for x in rest[:2]]
    skip = [integer(x) for x in (rest[2:4] or f.get('i', '0').split(':'))]
    d = [x[(skip + skip)[i]:] for i, x in enumerate(d)]
    if 'n' in f:
        d = [x[:integer(f['n'])] for x in d]
    a, b = d
    if a == b:
        return 0
    if 's' in f:
        return 1
    n = min(len(a), len(b))
    if 'l' in f:
        for i in range(n):
            if a[i] != b[i]:
                put(f'{i + 1:6d} {a[i]:3o} {b[i]:3o}')
    else:
        i = next((i for i in range(n) if a[i] != b[i]), None)
        if i is not None:
            put(f'{rest[0]} {rest[1]} differ: char {i + 1}, line {a.count(10, 0, i) + 1}')
            return 1
    if len(a) != len(b):
        err(f'EOF on {rest[0] if len(a) < len(b) else rest[1]}')
    return 1


# ---- sort, uniq, cut, tr and other line tools ----

@command('sort')
def sort(name, args):
    o, rest = opts(args, 'nrugk:t:o:fhVsbzcCdiMmS:T:R', 'numeric-sort=n reverse=r unique=u key=k field-separator=t output=o '
                   'ignore-case=f human-numeric-sort=h version-sort=V stable=s ignore-leading-blanks=b zero-terminated=z check=c '
                   'dictionary-order=d general-numeric-sort=g month-sort=M merge=m buffer-size=S temporary-directory=T parallel: '
                   'random-sort=R')
    f, keys = {}, []
    for k, v in o:
        if k == 'k':
            keys.append(v)
        else:
            f[k] = v if v is not None else True
    sep = '\0' if 'z' in f else '\n'
    t = f.get('t')
    if t is not None:
        t = {'\\t': '\t', '\\0': '\0'}.get(t, t)
        if len(t) != 1:
            raise Fail('the -t separator must be one character')
    mods = {c for c in 'nrfhVbdgMiR' if c in f}
    recs, bad = [], []
    for n, s in texts(rest, bad):
        recs += lines(s, sep)
    if bad:
        return 2

    def field_starts(s):
        if t is None:
            return [m.span() for m in re.finditer(r'[ \t\n]*[^ \t\n]+|[ \t\n]+$', s)]
        p, r = 0, []
        for x in s.split(t):
            r.append((p, p + len(x)))
            p += len(x) + 1
        return r
    KEY = re.compile(r'(\d+)(?:\.(\d+))?([bdfghiMnrVR]*)(?:,(\d+)(?:\.(\d+))?([bdfghiMnrVR]*))?$')

    def extractor(k):
        m = KEY.match(k)
        if not m or m[1] == '0':
            raise Fail(f'invalid key {k}')
        f1, c1, m1, f2, c2, m2 = int(m[1]), int(m[2] or 1), m[3], m[4] and int(m[4]), int(m[5] or 0), m[6] or ''
        own = set(m1 + m2)
        md = own or mods
        b1, b2 = 'b' in m1 or (not own and 'b' in mods), 'b' in m2 or (not own and 'b' in mods)

        def get(s):
            fs = field_starts(s)
            if f1 > len(fs):
                return ''
            a, e = fs[f1 - 1]
            if b1:
                while a < e and s[a] in ' \t':
                    a += 1
            a = min(a + c1 - 1, len(s))
            if f2 is None:
                z = len(s)
            elif f2 > len(fs):
                z = len(s)
            else:
                x, y = fs[f2 - 1]
                if c2:
                    if b2:
                        while x < y and s[x] in ' \t':
                            x += 1
                    z = min(x + c2, y)
                else:
                    z = y
            return s[a:z]
        return get, md

    def numeric(s):
        m = re.compile(r'[ \t]*(-?(?:\d[\d]*\.?\d*|\.\d+))').match(s)
        if not m:
            return 0
        v = m[1]
        return int(v) if v.lstrip('-').isdigit() else float(v)

    def human(s):
        m = re.compile(r'[ \t]*(-?)(\d*\.?\d*)([kKMGTPEZY]?)').match(s)
        v = float(m[2]) if m[2] not in ('', '.') else 0.0
        o = ' KMGTPEZY'.index(m[3].upper()) if m[3] and v else 0
        return (-o, -v) if m[1] else (o, v)

    def version(s):
        return [(0, int(x), '') if x.isdigit() else (1, 0, [(-1 if c == '~' else ord(c) + (0 if c.isalpha() else 256)) for c in x])
                for x in re.findall(r'\d+|\D+', s)]

    def general(s):
        m = re.compile(r'\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[+-]?inf\w*)', re.I).match(s)
        return (1, float(m[1])) if m else (0, 0.0)

    def conv(md):
        if 'n' in md:
            return numeric
        if 'h' in md:
            return human
        if 'g' in md:
            return general
        if 'V' in md:
            return version
        if 'M' in md:
            return lambda s: ('JANFEBMARAPRMAYJUNJULAUGSEPOCTNOVDEC'.find(s.strip()[:3].upper()) // 3 + 1) if len(s.strip()) > 2 else 0
        if 'R' in md:
            import zlib
            salt = os.urandom(4)
            return lambda s: zlib.crc32(salt + enc(s))
        fs = []
        if 'd' in md:
            fs.append(lambda s: ''.join(c for c in s if c.isalnum() or c in ' \t'))
        if 'i' in md:
            fs.append(lambda s: ''.join(c for c in s if c.isprintable()))
        if 'f' in md:
            fs.append(str.upper)

        def go(s):
            for x in fs:
                s = x(s)
            return s
        return go if fs else (lambda s: s)
    ks = []
    for k in keys:
        get, md = extractor(k)
        ks.append(((lambda g, c: lambda s: c(g(s)))(get, conv(md)), 'r' in md))
    if not ks:
        c = conv(mods)
        ks = [((lambda s: c(s.lstrip(' \t'))) if 'b' in mods else c, 'r' in mods)]
    glob_r = 'r' in mods
    if 'c' in f or 'C' in f:
        def order(a, b):
            for g, r in ks:
                x, y = g(a), g(b)
                if x != y:
                    return (-1 if x < y else 1) * (-1 if r else 1)
            if 'u' in f or 's' in f:
                return 0
            return ((a > b) - (a < b)) * (-1 if glob_r else 1)
        for i in range(1, len(recs)):
            z = order(recs[i - 1], recs[i])
            if z > 0 or (z == 0 and 'u' in f):
                if 'c' in f:
                    err(f'{rest[0] if rest else "-"}:{i + 1}: disorder: {recs[i]}')
                return 1
        return 0
    if 'u' not in f and 's' not in f and (keys or mods - {'r'}):
        recs.sort(reverse=glob_r)
    for g, r in reversed(ks):
        recs.sort(key=g, reverse=r)
    if 'u' in f:
        o, last = [], object()
        for s in recs:
            k = [g(s) for g, _ in ks]
            if k != last:
                o.append(s)
                last = k
        recs = o
    res = ''.join(s + sep for s in recs)
    if 'o' in f:
        with open(f['o'], 'wb') as h:
            h.write(enc(res))
    else:
        out(res)
    return 0


@command('uniq')
def uniq(name, args):
    o, rest = opts(args, 'cdDuif:s:w:z', 'count=c repeated=d unique=u ignore-case=i skip-fields=f skip-chars=s check-chars=w '
                   'zero-terminated=z all-repeated=D')
    f = dict(o)
    sep = '\0' if 'z' in f else '\n'

    def key(s):
        if 'f' in f:
            s = re.sub(r'\A(?:[ \t]*[^ \t]+){0,%d}' % integer(f['f']), '', s)
        s = s[integer(f.get('s', 0)):]
        if 'w' in f:
            s = s[:integer(f['w'])]
        return s.lower() if 'i' in f else s
    res, grp, last = [], [], None

    def flush():
        n = len(grp)
        if not n or ('d' in f and n < 2) or ('u' in f and n > 1) or ('D' in f and n < 2):
            return
        for s in grp if 'D' in f else grp[:1]:
            res.append((f'{n:4d} ' if 'c' in f else '') + s + sep)
    for s in lines(text(rest[0] if rest else '-'), sep):
        k = key(s)
        if k != last:
            flush()
            grp, last = [], k
        grp.append(s)
    flush()
    if len(rest) > 1:
        with open(rest[1], 'wb') as h:
            h.write(enc(''.join(res)))
    else:
        out(''.join(res))


@command('cut')
def cut(name, args):
    o, rest = opts(args, 'd:f:c:b:snz', 'delimiter=d fields=f characters=c bytes=b only-delimited=s complement output-delimiter: '
                   'zero-terminated=z')
    f = dict(o)
    mode = next((k for k in 'fcb' if k in f), None)
    if not mode:
        raise Fail('you must specify a list of bytes, characters, or fields', 1)
    d = f.get('d', '\t')
    od = f.get('output-delimiter', d if mode == 'f' else '')
    rs = []
    for p in re.split('[, ]', f[mode]):
        m = re.fullmatch(r'(\d*)(-?)(\d*)', p)
        if not m or not (m[1] or m[3]) or (m[1] == '0'):
            raise Fail(f'invalid list {f[mode]}', 1)
        a = int(m[1] or 1)
        rs.append((a, int(m[3]) if m[3] else (1 << 60 if m[2] else a)))
    comp, bad, sep = 'complement' in f, [], '\0' if 'z' in f else '\n'

    def pick(items):
        return [x for i, x in enumerate(items, 1) if any(a <= i <= b for a, b in rs) != comp]
    for n, s in texts(rest, bad):
        for ln in lines(s, sep):
            if mode == 'f':
                if d not in ln:
                    if 's' not in f:
                        out(ln + sep)
                    continue
                out(od.join(pick(ln.split(d))) + sep)
            elif mode == 'c':
                out((od.join(pick(ln))) + sep)
            else:
                outb(bytes(pick(enc(ln))) + enc(sep))
    return 1 if bad else 0


@command('tr')
def tr(name, args):
    o, rest = opts(args, 'dscCt', 'delete=d squeeze-repeats=s complement=c truncate-set1=t', stop=True)
    f = {k.lower() for k, _ in o}

    def expand(s):
        r, i = [], 0
        ctypes = {'alpha': str.isalpha, 'digit': str.isdigit, 'alnum': str.isalnum, 'space': lambda c: c in ' \t\n\r\f\v',
                  'blank': lambda c: c in ' \t', 'upper': str.isupper, 'lower': str.islower, 'xdigit': lambda c: c in '0123456789abcdefABCDEF',
                  'cntrl': lambda c: c < ' ' or c == '\x7f', 'print': lambda c: ' ' <= c < '\x7f', 'graph': lambda c: ' ' < c < '\x7f',
                  'punct': lambda c: ' ' < c < '\x7f' and not c.isalnum()}

        def ch(i):
            if s[i] == '\\' and i + 1 < len(s):
                m = re.compile(r'\\([0-7]{1,3})').match(s, i)
                if m:
                    return chr(int(m[1], 8)), m.end()
                return {'b': '\b', **ESC}.get(s[i + 1], s[i + 1]), i + 2
            return s[i], i + 1
        while i < len(s):
            m = re.compile(r'\[:(\w+):\]').match(s, i)
            if m and m[1] in ctypes:
                r += [chr(c) for c in range(128) if ctypes[m[1]](chr(c))]
                i = m.end()
                continue
            m = re.compile(r'\[(\\?.)\*(\d*)\]').match(s, i)
            if m:
                c = ch(i + 1)[0]
                r += [c] * int(m[2]) if m[2] else ['*', c]
                if not m[2]:
                    r[-2:] = [('*', c)]
                i = m.end()
                continue
            c, i = ch(i)
            if s[i:i + 1] == '-' and i + 1 < len(s):
                e, i = ch(i + 1)
                if e < c:
                    raise Fail(f'range {c}-{e} is in reverse order', 1)
                r += [chr(x) for x in range(ord(c), ord(e) + 1)]
            else:
                r.append(c)
        return r
    if not rest or (len(rest) < 2 and not f & {'d', 's'}) or (len(rest) > 1 and 'd' in f and 's' not in f):
        raise Fail('wrong number of strings', 1)
    s1, s2 = expand(rest[0]), expand(rest[1]) if len(rest) > 1 else []
    for i, x in enumerate(s2):
        if type(x) is tuple:
            s2[i:i + 1] = [x[1]] * max(0, len(s1) - len(s2) + 1)
    t, set1 = text(), set(s1)
    comp = 'c' in f
    if 'd' in f:
        t = ''.join(c for c in t if (c in set1) == comp) if comp else t.translate({ord(c): None for c in s1})
        sq = set(s2) if 's' in f else set()
    elif len(rest) > 1:
        if not s2:
            raise Fail('string 2 is empty', 1)
        if comp:
            t = ''.join(c if c in set1 else s2[-1] for c in t)
        else:
            if 't' in f:
                s1 = s1[:len(s2)]
            m = {}
            for i, c in enumerate(s1):
                m[ord(c)] = s2[i] if i < len(s2) else s2[-1]
            t = t.translate(m)
        sq = set(s2) if 's' in f else set()
    else:
        sq = None
    if 's' in f:
        o, last = [], None
        for c in t:
            if c == last and ((c in sq) if sq is not None else (c in set1) != comp):
                continue
            o.append(c)
            last = c
        t = ''.join(o)
    out(t)


@command('tee')
def tee(name, args):
    o, rest = opts(args, 'ai', 'append=a ignore-interrupts=i')
    d, rc = data(), 0
    for p in rest:
        try:
            with open(p, 'ab' if ('a', None) in o else 'wb') as h:
                h.write(d)
        except OSError as e:
            err(oserr(e, p))
            rc = 1
    outb(d)
    return rc


@command('xargs')
def xargs(name, args):
    o, rest = opts(args, '0n:I:J:d:rL:P:tps:xa:', 'null=0 max-args=n replace=I delimiter=d no-run-if-empty=r max-lines=L max-procs=P '
                   'verbose=t max-chars=s exit=x arg-file=a', stop=True)
    f = {k: (True if v is None else v) for k, v in o}
    if 'p' in f or 'J' in f:
        raise Fail('unsupported option', 1)
    s = text(f.get('a', '-'))
    if '0' in f or 'd' in f:
        items = lines(s, '\0' if '0' in f else unescape(f['d']))
    elif 'I' in f:
        items = [re.sub(r'''\\(.)|"([^"]*)"|'([^']*)\'''', lambda m: m[1] or m[2] or m[3] or '', x.lstrip()) for x in lines(s) if x.strip()]
    elif 'L' in f:
        items = [x for x in lines(s) if x.strip()]
    else:
        items, cur, i, n = [], None, 0, len(s)
        while i < n:
            c = s[i]
            i += 1
            if c in ' \t\n':
                if cur is not None:
                    items.append(cur)
                    cur = None
                continue
            cur = cur or ''
            if c in '\'"':
                e = s.find(c, i)
                if e < 0 or '\n' in s[i:e]:
                    raise Fail('unterminated quote', 1)
                cur += s[i:e]
                i = e + 1
            elif c == '\\' and i < n:
                cur += s[i]
                i += 1
            else:
                cur += c
        if cur is not None:
            items.append(cur)
    cmd = rest or ['echo']
    runs = []
    if 'I' in f:
        runs = [[a.replace(f['I'], x) for a in cmd] for x in items]
    elif 'L' in f:
        import shlex
        n = integer(f['L'])
        runs = [cmd + [w for x in items[i:i + n] for w in shlex.split(x)] for i in range(0, len(items), n)]
    elif items:
        n, maxc = integer(f.get('n', 5000)), integer(f.get('s', 200000))
        cur, size = [], 0
        for x in items:
            if cur and (len(cur) >= n or size + len(x) + 1 > maxc):
                runs.append(cmd + cur)
                cur, size = [], 0
            cur.append(x)
            size += len(x) + 1
        runs.append(cmd + cur)
    elif 'r' not in f and sys.platform.startswith('linux'):
        runs = [cmd]
    rc = 0
    for r in runs:
        if 't' in f:
            State.out.flush()
            sys.stderr.write(' '.join(r) + '\n')
        if r[0] == 'echo':
            put(*r[1:]) if r[1:2] != ['-n'] else out(' '.join(r[2:]))
            continue
        c = call(r, b'')[0]
        if c in (126, 127):
            return c
        if c == 255:
            return 124
        if c:
            rc = 123 if sys.platform.startswith('linux') else 1
    return rc


@command('comm')
def comm(name, args):
    o, rest = opts(args, '123i', 'output-delimiter: nocheck-order=nc check-order=nc')
    f = dict(o)
    if len(rest) != 2:
        raise Fail('usage: comm [-123i] file1 file2', 1)
    a, b = (lines(text(x)) for x in rest)
    key = str.lower if 'i' in f else str
    d, i, j = f.get('output-delimiter', '\t'), 0, 0
    while i < len(a) or j < len(b):
        if j >= len(b) or i < len(a) and key(a[i]) < key(b[j]):
            c, s, i = 1, a[i], i + 1
        elif i >= len(a) or key(b[j]) < key(a[i]):
            c, s, j = 2, b[j], j + 1
        else:
            c, s, i, j = 3, a[i], i + 1, j + 1
        if str(c) not in f:
            out(d * sum(str(x) not in f for x in range(1, c)) + s + '\n')


@command('paste')
def paste(name, args):
    import itertools
    o, rest = opts(args, 'd:sz', 'delimiters=d serial=s zero-terminated=z')
    f = dict(o)
    ds = [x.replace('\0', '') for x in unescape(f.get('d', '\t'))] or ['']
    sep = '\0' if 'z' in f else '\n'
    ins, n = [], sum(x == '-' for x in rest or ['-'])
    if n:
        std = lines(text('-'), sep)
    k = 0
    for x in rest or ['-']:
        if x == '-':
            ins.append(std[k::n] if 's' not in f else std if k == 0 else [])
            k += 1
        else:
            ins.append(lines(text(x), sep))
    rows = ins if 's' in f else itertools.zip_longest(*ins, fillvalue='')
    for r in rows:
        s = ''
        for i, x in enumerate(r):
            s += (ds[(i - 1) % len(ds)] if i else '') + x
        out(s + sep)


@command('nl')
def nl(name, args):
    o, rest = opts(args, 'b:n:w:s:v:i:f:h:p', 'body-numbering=b number-format=n number-width=w number-separator=s starting-line-number=v '
                   'line-increment=i')
    f = dict(o)
    b, w, s, n, inc = f.get('b', 't'), integer(f.get('w', 6)), f.get('s', '\t'), integer(f.get('v', 1)), integer(f.get('i', 1))
    if any(f.get(k, 'n') != 'n' for k in 'fh'):
        raise Fail('unsupported option')
    r, bad = rx(b[1:]) if b[0] == 'p' else None, []
    for _, t in texts(rest, bad):
        for ln in lines(t):
            if b == 'a' or b == 't' and ln or r and r.search(ln):
                out({'ln': f'{n:<{w}}', 'rn': f'{n:>{w}}', 'rz': f'{n:0{w}}'}[f.get('n', 'rn')] + s + ln + '\n')
                n += inc
            else:
                out(' ' * (w + (len(s))) + ln + '\n')
    return 1 if bad else 0


@command('tac')
def tac(name, args):
    o, rest = opts(args, 's:', 'separator=s')
    bad, sep = [], dict(o).get('s', '\n')
    for _, t in texts(rest, bad):
        p = t.split(sep)
        tail = p.pop()
        out((tail if tail else '') + ''.join(x + sep for x in reversed(p)))
    return 1 if bad else 0


@command('rev')
def rev(name, args):
    _, rest = opts(args, '')
    bad = []
    for _, t in texts(rest, bad):
        out(''.join(x[::-1] + '\n' for x in lines(t)))
    return 1 if bad else 0


@command('fold')
def fold(name, args):
    o, rest = opts(args, 'w:sb', 'width=w spaces=s bytes=b', digits=True)
    f = dict(o)
    w, bad = integer(f.get('w', f.get('#', 80))), []
    if w < 1:
        raise Fail('invalid width', 1)
    for _, t in texts(rest, bad):
        for ln in t.split('\n') if not t.endswith('\n') else lines(t):
            while True:
                col = i = 0
                while i < len(ln):
                    c = (col // 8 + 1) * 8 if ln[i] == '\t' else col + 1
                    if c > w:
                        break
                    col = c
                    i += 1
                if i >= len(ln):
                    break
                i = max(i, 1)
                if 's' in f:
                    k = max(ln.rfind(' ', 0, i), ln.rfind('\t', 0, i))
                    i = k + 1 if k >= 0 else i
                out(ln[:i] + '\n')
                ln = ln[i:]
            out(ln + '\n')
        if not t.endswith('\n') and t:
            b = State.out
            b.b[-1] = b.b[-1][:-1] if b.b else b''
    return 1 if bad else 0


@command('seq')
def seq(name, args):
    from decimal import Decimal, InvalidOperation
    f, i = {}, 0
    while i < len(args) and args[i].startswith('-') and not re.match(r'-[\d.]', args[i]):
        a = args[i]
        if a == '--':
            i += 1
            break
        if a in ('-w', '--equal-width'):
            f['w'] = True
        elif a[:2] in ('-s', '-f', '-t') and a[1] != '-':
            if len(a) > 2:
                f[a[1]] = a[2:]
            else:
                i += 1
                if i >= len(args):
                    raise Fail(f'option {a} needs a value')
                f[a[1]] = args[i]
        elif a.startswith(('--separator=', '--format=')):
            f[a[2]] = a.split('=', 1)[1]
        else:
            raise Fail(f'unsupported option {a}')
        i += 1
    v = args[i:]
    try:
        n = [Decimal(x) for x in v]
    except InvalidOperation:
        raise Fail('invalid number', 1) from None
    if not 1 <= len(n) <= 3:
        raise Fail('usage: seq [-w] [-s sep] [-f format] [first [increment]] last')
    first, last = (n[0] if len(n) > 1 else Decimal(1)), n[-1]
    inc = n[1] if len(n) > 2 else Decimal(1) if 'f' not in f and first <= last or first <= last else Decimal(-1) \
        if not sys.platform.startswith('linux') else Decimal(1)
    if inc == 0:
        raise Fail('increment is zero', 1)
    dec_ = max([max(0, -x.as_tuple().exponent) for x in (first, inc)])
    r = []
    while (inc > 0 and first <= last) or (inc < 0 and first >= last):
        r.append(f['f'] % float(first) if 'f' in f else f'{first:.{dec_}f}')
        first += inc
    if 'w' in f and r:
        w = max(map(len, r))
        r = [x.zfill(w) for x in r]
    sep = unescape(f.get('s', '\n'))
    if r:
        out(sep.join(r) + unescape(f.get('t', '\n')))

# ---- date, file information, small utilities ----

def when(s):
    """Seconds since the epoch for an ISO date, @seconds or now."""
    import datetime as dt
    s = s.strip()
    if s.startswith('@'):
        return float(s[1:])
    if s in ('now', ''):
        return dt.datetime.now().timestamp()
    if s in ('today', 'yesterday', 'tomorrow'):
        return (dt.datetime.now() + dt.timedelta({'today': 0, 'yesterday': -1, 'tomorrow': 1}[s])).timestamp()
    m = re.fullmatch(r'([+-]?\d+) (second|minute|hour|day|week)s?( ago)?', s)
    if m:
        u = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400, 'week': 604800}[m[2]]
        return dt.datetime.now().timestamp() + int(m[1]) * u * (-1 if m[3] else 1)
    t = re.sub(r' ?(UTC|GMT|Z)$', '+00:00', s)
    t = re.sub(r' (?=[+-]\d\d:?\d\d$)', '', t)
    if re.fullmatch(r'\d\d:\d\d(:\d\d)?', t):
        t = dt.date.today().isoformat() + 'T' + t
    try:
        return dt.datetime.fromisoformat(t).timestamp()
    except ValueError:
        raise Fail(f"invalid date '{s}'", 1) from None


@command('date')
def date(name, args):
    import time
    o, rest = opts(args, 'ur:d:I?Rjf:n', 'utc=u universal=u reference=r date=d iso-8601=I rfc-email=R rfc-3339:')
    f = dict(o)
    t = time.time()
    if 'u' in f:
        os.environ['TZ'] = 'UTC0'
        if hasattr(time, 'tzset'):
            time.tzset()
    if 'd' in f:
        t = when(f['d'])
    elif 'r' in f:
        t = float(f['r']) if f['r'].isdigit() else os.stat(f['r']).st_mtime
    elif 'f' in f:
        if not rest:
            raise Fail('usage: date -j -f format string [+format]')
        try:
            t = time.mktime(time.strptime(rest.pop(0), f['f']))
        except ValueError as e:
            raise Fail(str(e), 1) from None
    tm = time.localtime(t)
    if rest and not rest[0].startswith('+'):
        raise Fail('setting the date is not supported')
    z = time.strftime('%z', tm)
    zc = z[:3] + ':' + z[3:]
    if 'R' in f:
        fmt = '%a, %d %b %Y %H:%M:%S %z'
    elif 'I' in f or 'rfc-3339' in f:
        k = f.get('I', f.get('rfc-3339')) or 'date'
        if k not in ('date', 'hours', 'minutes', 'seconds', 'ns'):
            raise Fail(f'invalid precision {k}')
        fmt = {'date': '%Y-%m-%d', 'hours': '%Y-%m-%dT%H', 'minutes': '%Y-%m-%dT%H:%M', 'seconds': '%Y-%m-%dT%H:%M:%S',
               'ns': '%Y-%m-%dT%H:%M:%S,%N'}[k]
        if 'I' not in f:
            fmt = fmt.replace('T', ' ').replace(',', '.')
        if k != 'date':
            fmt += zc
    else:
        fmt = rest[0][1:] if rest else '%a %b %e %H:%M:%S %Z %Y'
    ns = f'{int(round((t % 1) * 1e9)) % 10 ** 9:09d}'

    def rep(m):
        fl, c = m[1], m[2]
        if c == '%':
            return '%'
        s = {'N': ns, 's': str(int(t // 1)), '3N': ns[:3], '6N': ns[:6], '9N': ns, ':z': zc}.get(c)
        if s is None:
            s = time.strftime('%' + c, tm)
        if fl == '-':
            s = s.lstrip('0 ') or '0'
        elif fl == '_':
            s = re.sub('^0+(?=.)', lambda m: ' ' * len(m[0]), s)
        elif fl == '^':
            s = s.upper()
        return s
    put(re.sub(r'%([-_0^]?)(\d?N|:z|.)', rep, fmt))


def hsize(n, si=1024):
    u = 0
    v = float(n)
    while v >= si and u < 8:
        v /= si
        u += 1
    if not u:
        return f'{int(v)}'
    import math
    return (f'{math.ceil(v * 10) / 10:.1f}' if v < 9.95 else f'{math.ceil(v)}') + 'BKMGTPEZY'[u]


@command('du')
def du(name, args):
    o, rest = opts(args, 'shkmgad:cbxLHP0l', 'summarize=s human-readable=h max-depth=d total=c apparent-size all=a bytes=b exclude: '
                   'block-size: null=0 one-file-system=x')
    f, exc = {}, []
    for k, v in o:
        if k == 'exclude':
            exc.append(glob_re(v))
        else:
            f[k] = v
    if 'block-size' in f or 'L' in f:
        raise Fail('unsupported option', 1)
    apparent = 'apparent-size' in f or 'b' in f
    unit = 1 if 'b' in f else 1 << 20 if 'm' in f else 1 << 30 if 'g' in f else 1024 if 'k' in f or sys.platform.startswith('linux') else 512
    depth = 0 if 's' in f else integer(f['d']) if 'd' in f else 1 << 30
    seen, total, end, rc = set(), 0, '\0' if '0' in f else '\n', 0

    def show(n, p):
        out((hsize(n) if 'h' in f else str(-(-n // unit))) + '\t' + p + end)

    def size(p, d):
        nonlocal rc
        s = os.lstat(p)
        if s.st_nlink > 1 and not os.path.isdir(p):
            if (s.st_dev, s.st_ino) in seen:
                return 0
            seen.add((s.st_dev, s.st_ino))
        n = s.st_size if apparent else s.st_blocks * 512
        if os.path.isdir(p) and not os.path.islink(p):
            try:
                for x in sorted(os.listdir(p)):
                    if any(g(x) for g in exc):
                        continue
                    q = p.rstrip('/') + '/' + x
                    try:
                        n += size(q, d + 1)
                    except FileNotFoundError:
                        pass
            except PermissionError as e:
                err(oserr(e, p))
                rc = 1
            if d <= depth:
                show(n, p)
        elif d == 0 or 'a' in f and d <= depth:
            show(n, p)
        return n
    for p in rest or ['.']:
        try:
            total += size(p, 0)
        except FileNotFoundError as e:
            err(oserr(e, p))
            rc = 1
    if 'c' in f:
        show(total, 'total')
    return rc


@command('df')
def df(name, args):
    o, rest = opts(args, 'hkmPHi', 'human-readable=h')
    f = dict(o)
    if 'i' in f or 'H' in f:
        raise Fail('unsupported option', 1)
    unit, lab = (1 << 20, '1M-blocks') if 'm' in f else (1024, '1K-blocks')
    if 'h' in f:
        lab = 'Size'
    rows = [['Filesystem', lab, 'Used', 'Avail', 'Use%', 'Mounted on']]
    for p in rest or ['.']:
        s = os.statvfs(p)
        m = os.path.realpath(p)
        while not os.path.ismount(m):
            m = os.path.dirname(m)
        size, free, avail = s.f_blocks * s.f_frsize, s.f_bfree * s.f_frsize, s.f_bavail * s.f_frsize
        used = size - free
        z = hsize if 'h' in f else lambda n: str(n // unit)
        rows.append(['-', z(size), z(used), z(avail), f'{-(-used * 100 // (used + avail)) if used + avail else 0}%', m])
    w = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        put(r[0].ljust(w[0]), *(r[i].rjust(w[i]) for i in range(1, 5)), r[5])


@command('stat')
def stat(name, args):
    import stat as st
    import time
    o, rest = opts(args, 'c:f:LnqF:t:lsxr', 'format=c printf: dereference=L')
    f = dict(o)
    if set(f) & set('lsxrF'):
        raise Fail('unsupported option', 1)
    if not rest:
        raise Fail('missing operand', 1)
    KIND = {'-': 'regular file', 'd': 'directory', 'l': 'symbolic link', 'p': 'fifo', 's': 'socket', 'c': 'character special file',
            'b': 'block special file'}
    BK = {'-': 'Regular File', 'd': 'Directory', 'l': 'Symbolic Link', 'p': 'Fifo File', 's': 'Socket', 'c': 'Character Device',
          'b': 'Block Device'}

    def human(ns):
        s, n = divmod(ns, 10 ** 9)
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s)) + f'.{n:09d} ' + time.strftime('%z', time.localtime(s))
    rc = 0
    for p in rest:
        try:
            s = os.stat(p) if 'L' in f else os.lstat(p)
        except OSError as e:
            if 'q' not in f:
                err(f'cannot stat {oserr(e, p)}')
            rc = 1
            continue
        mode = st.filemode(s.st_mode)
        k = mode[0]
        tf = f.get('t', '%b %e %H:%M:%S %Y')
        if 'c' in f or 'printf' in f:
            kind = 'regular empty file' if k == '-' and not s.st_size else KIND[k]
            q = {'n': p, 'N': f"'{p}'" + (f" -> '{os.readlink(p)}'" if k == 'l' else ''), 's': s.st_size, 'b': s.st_blocks, 'B': 512,
                 'a': f'{s.st_mode & 0o7777:o}', 'A': mode, 'f': f'{s.st_mode:x}', 'F': kind, 'u': s.st_uid, 'g': s.st_gid,
                 'U': owner(s.st_uid), 'G': group(s.st_gid), 'i': s.st_ino, 'h': s.st_nlink, 'd': s.st_dev, 'o': s.st_blksize, '%': '%',
                 'X': int(s.st_atime), 'Y': int(s.st_mtime), 'Z': int(s.st_ctime), 'x': human(s.st_atime_ns), 'y': human(s.st_mtime_ns),
                 'z': human(s.st_ctime_ns), 'W': int(getattr(s, 'st_birthtime', 0)), 'w': human(int(getattr(s, 'st_birthtime', 0) * 1e9)),
                 'm': '/'}

            def rep(m):
                if m[2] not in q:
                    raise Fail(f'unsupported format %{m[2]}')
                return ('%' + m[1] + 's') % q[m[2]]
            t = re.sub(r'%(-?\d*)(.)', rep, f.get('c') or f.get('printf', ''))
            out(t + '\n' if 'c' in f else unescape(t))
        elif 'f' in f:
            b = getattr(s, 'st_birthtime', s.st_ctime)
            q = {'z': s.st_size, 'b': s.st_blocks, 'k': s.st_blksize, 'l': s.st_nlink, 'i': s.st_ino, 'd': s.st_dev, 'N': p,
                 'Y': os.readlink(p) if k == 'l' else '', 'u': s.st_uid, 'g': s.st_gid, 'Su': owner(s.st_uid), 'Sg': group(s.st_gid),
                 'p': f'{s.st_mode:o}', 'Lp': f'{s.st_mode & 0o777:o}', 'OLp': f'{s.st_mode & 0o777:o}', 'A': f'{s.st_mode & 0o7777:o}',
                 'Mp': f'{s.st_mode >> 9 & 7:o}', 'Sp': mode,
                 'Hp': f'{s.st_mode >> 12:o}', 'HT': BK[k], 'T': {'-': '', 'd': '/', 'l': '@', 'p': '|', 's': '='}.get(k, ''), '%': '%',
                 'n': '\n', 't': '\t', 'm': int(s.st_mtime), 'a': int(s.st_atime), 'c': int(s.st_ctime), 'B': int(b)}
            for c, v in (('m', s.st_mtime), ('a', s.st_atime), ('c', s.st_ctime), ('B', b)):
                q['S' + c] = time.strftime(tf, time.localtime(v))

            def repb(m):
                if m[2] not in q:
                    raise Fail(f'unsupported format %{m[2]}')
                return ('%' + m[1] + 's') % q[m[2]]
            out(re.sub(r'%(-?\d*)((?:[SOXDUHLM]+)?[a-zA-Z%])', repb, f['f']) + ('' if 'n' in f else '\n'))
        else:
            link = f' -> {os.readlink(p)}' if k == 'l' else ''
            put(f'  File: {p}{link}\n  Size: {s.st_size:<10}\tBlocks: {s.st_blocks:<10} IO Block: {s.st_blksize:<6} {KIND[k]}\n'
                f'Device: {s.st_dev}\tInode: {s.st_ino:<11} Links: {s.st_nlink}\n'
                f'Access: ({s.st_mode & 0o7777:04o}/{mode})  Uid: ({s.st_uid}/{owner(s.st_uid)})   Gid: ({s.st_gid}/{group(s.st_gid)})\n'
                f'Access: {human(s.st_atime_ns)}\nModify: {human(s.st_mtime_ns)}\nChange: {human(s.st_ctime_ns)}')
    return rc


@command('file')
def file(name, args):
    o, rest = opts(args, 'bLhiI', 'brief=b dereference=L mime=I mime-type')
    f, rc = {k for k, _ in o}, 0
    magic = [(b'\x7fELF', 'ELF', 'application/x-executable'), (b'\xcf\xfa\xed\xfe', 'Mach-O 64-bit', 'application/x-mach-binary'),
             (b'\xce\xfa\xed\xfe', 'Mach-O', 'application/x-mach-binary'), (b'\xca\xfe\xba\xbe', 'Mach-O universal binary',
                                                                           'application/x-mach-binary'),
             (b'\x89PNG\r\n\x1a\n', 'PNG image data', 'image/png'), (b'\xff\xd8\xff', 'JPEG image data', 'image/jpeg'),
             (b'GIF8', 'GIF image data', 'image/gif'), (b'%PDF-', 'PDF document', 'application/pdf'),
             (b'PK\x03\x04', 'Zip archive data', 'application/zip'), (b'\x1f\x8b', 'gzip compressed data', 'application/gzip'),
             (b'BZh', 'bzip2 compressed data', 'application/x-bzip2'), (b'\xfd7zXZ\0', 'XZ compressed data', 'application/x-xz'),
             (b'\x28\xb5\x2f\xfd', 'Zstandard compressed data', 'application/zstd'), (b'7z\xbc\xaf\x27\x1c', '7-zip archive data',
                                                                                   'application/x-7z-compressed'),
             (b'!<arch>\n', 'current ar archive', 'application/x-archive'), (b'SQLite format 3\0', 'SQLite 3.x database',
                                                                          'application/vnd.sqlite3'),
             (b'bplist', 'Apple binary property list', 'application/octet-stream'), (b'OggS', 'Ogg data', 'application/ogg'),
             (b'ID3', 'Audio file with ID3', 'audio/mpeg'), (b'fLaC', 'FLAC audio', 'audio/flac'), (b'MZ', 'MS-DOS executable',
                                                                                                 'application/x-dosexec'),
             (b'\0asm', 'WebAssembly (wasm) binary module', 'application/wasm'), (b'<?xml', 'XML 1.0 document text', 'text/xml'),
             (b'BM', 'PC bitmap', 'image/bmp'), (b'RIFF', 'RIFF (little-endian) data', 'application/octet-stream')]

    def kind(p):
        if os.path.islink(p) and 'L' not in f:
            return f'symbolic link to {os.readlink(p)}', 'inode/symlink'
        if os.path.isdir(p):
            return 'directory', 'inode/directory'
        with open(p, 'rb') as h:
            d = h.read(65536)
        if not d:
            return 'empty', 'inode/x-empty'
        for m, t, mm in magic:
            if d.startswith(m):
                if m == b'\x89PNG\r\n\x1a\n' and len(d) > 25:
                    t += f', {int.from_bytes(d[16:20])} x {int.from_bytes(d[20:24])}'
                if m == b'\xcf\xfa\xed\xfe' and len(d) > 16:
                    t += ' ' + {1: 'object', 2: 'executable', 6: 'dynamically linked shared library', 8: 'bundle'}.get(
                        int.from_bytes(d[12:16], 'little'), '') + ' ' + {0x100000c: 'arm64', 0x1000007: 'x86_64'}.get(
                        int.from_bytes(d[4:8], 'little'), '')
                return t, mm
        if d[257:262] == b'ustar':
            return 'POSIX tar archive', 'application/x-tar'
        if d.startswith(b'#!'):
            i = d[2:].split(b'\n')[0].decode('latin-1').strip()
            return f'{i} script text executable', 'text/x-script'
        if b'\0' in d:
            return 'data', 'application/octet-stream'
        try:
            d.decode('ascii')
            k = 'ASCII text'
        except UnicodeDecodeError:
            try:
                d.decode('utf-8') if len(d) < 65536 else d[:-4].decode('utf-8')
                k = 'Unicode text, UTF-8 text'
            except UnicodeDecodeError:
                k = 'ISO-8859 text'
        return k + (', with CRLF line terminators' if b'\r\n' in d else ''), 'text/plain'
    for p in rest:
        try:
            t, m = kind(p)
        except OSError as e:
            t = m = f'cannot open ({e.strerror})'
        if f & {'I', 'i', 'mime-type'}:
            t = m
        put(t if 'b' in f else f'{p}: {t}')
    return rc


@command('realpath')
def realpath(name, args):
    o, rest = opts(args, 'qems', 'relative-to: canonicalize-existing=e canonicalize-missing=m quiet=q strip=s no-symlinks=s')
    f, rc = dict(o), 0
    for p in rest or (['.'] if not sys.platform.startswith('linux') else []):
        q = os.path.abspath(p) if 's' in f else os.path.realpath(p)
        if 'm' not in f and not os.path.exists(q if 'e' in f else os.path.dirname(q)) or ('e' in f and not os.path.exists(q)) \
                or ('m' not in f and not os.path.lexists(q) and not sys.platform.startswith('linux') and 's' not in f):
            if 'q' not in f:
                err(f'{p}: No such file or directory')
            rc = 1
            continue
        put(os.path.relpath(q, os.path.realpath(f['relative-to'])) if 'relative-to' in f else q)
    return rc


@command('readlink')
def readlink(name, args):
    o, rest = opts(args, 'fnemqs', 'canonicalize=f canonicalize-existing=e canonicalize-missing=m no-newline=n')
    f, rc = dict(o), 0
    for p in rest:
        if f.keys() & set('fem'):
            q = os.path.realpath(p)
            if 'm' not in f and not os.path.exists(os.path.dirname(q)) or 'e' in f and not os.path.exists(q):
                rc = 1
                continue
        else:
            try:
                q = os.readlink(p)
            except OSError:
                rc = 1
                continue
        out(q + ('' if 'n' in f and len(rest) == 1 else '\n'))
    return rc


@command('mktemp')
def mktemp(name, args):
    import random
    o, rest = opts(args, 'dqut:p:', 'directory=d quiet=q dry-run=u tmpdir? suffix:')
    f = dict(o)
    base = f.get('p') or f.get('tmpdir') or os.environ.get('TMPDIR') or '/tmp'
    t = rest[0] if rest else (f['t'] + ('' if 'X' in f['t'] else '.XXXXXXXXXX')) if 't' in f else 'tmp.XXXXXXXXXX'
    if not rest or 'p' in f or 'tmpdir' in f or 't' in f:
        t = os.path.join(base, t)
    m = re.search(r'X{3,}(?=[^X/]*$)', t)
    if not m:
        raise Fail(f'too few X in template {t}', 1)
    for _ in range(100):
        p = t[:m.start()] + ''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=len(m[0]))) \
            + t[m.end():] + (f.get('suffix') or '')
        try:
            if 'u' not in f:
                os.mkdir(p, 0o700) if 'd' in f else os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
            elif os.path.lexists(p):
                continue
            put(p)
            return 0
        except FileExistsError:
            pass
    raise Fail('no unused name found', 1)


@command('tree')
def tree(name, args):
    o, rest = opts(args, 'aL:dfI:P:hsFnNi', 'noreport dirsfirst prune')
    f = {k: v for k, v in o}
    if set(f) & {'prune'}:
        raise Fail('unsupported option --prune')
    ig, pat = [[glob_re(x) for x in f[k].split('|')] if k in f else None for k in 'IP']
    nd = nf = 0

    def label(p, n):
        s = os.lstat(p)
        r = p if 'f' in f else n
        if 'h' in f or 's' in f:
            r = f'[{hsize(s.st_size) if "h" in f else s.st_size:>{4 if "h" in f else 11}}]  ' + r
        if os.path.islink(p):
            return r + ' -> ' + os.readlink(p)
        if 'F' in f:
            r += '/' if os.path.isdir(p) else '*' if s.st_mode & 0o111 else ''
        return r

    def go(d, pre, depth):
        nonlocal nd, nf
        if 'L' in f and depth > integer(f['L']):
            return
        try:
            ns = sorted(os.listdir(d))
        except OSError:
            return
        o = []
        for n in ns:
            p = d.rstrip('/') + '/' + n
            isd = os.path.isdir(p) and not os.path.islink(p)
            if ('a' not in f and n.startswith('.')) or (ig and any(g(n) for g in ig)) or ('d' in f and not isd) or \
                    (pat and not isd and not any(g(n) for g in pat)):
                continue
            o.append((n, p, isd))
        if 'dirsfirst' in f:
            o.sort(key=lambda x: not x[2])
        for i, (n, p, isd) in enumerate(o):
            last = i == len(o) - 1
            out(('' if 'i' in f else pre + ('└── ' if last else '├── ')) + label(p, n) + '\n')
            if isd:
                nd += 1
                go(p, pre + ('    ' if last else '│   '), depth + 1)
            else:
                nf += 1
    for p in rest or ['.']:
        put(p)
        go(p, '', 1)
    if 'noreport' not in f:
        put(f'\n{nd} director{"y" if nd == 1 else "ies"}' + ('' if 'd' in f else f', {nf} file{"" if nf == 1 else "s"}'))


@command('expr')
def expr(name, args):
    i = 0

    def peek():
        return args[i] if i < len(args) else None

    def bad():
        raise Fail('syntax error')

    def num_(s):
        if not re.fullmatch(r'-?\d+', str(s)):
            raise Fail('non-integer argument')
        return int(s)

    def level(ops, sub, f):
        def go():
            nonlocal i
            a = sub()
            while peek() in ops:
                op = args[i]
                i += 1
                a = f(op, a, sub())
            return a
        return go

    def mat(s, p):
        m = rx(p).match(str(s))
        if re.search(r'\\\(', p):
            return (m[1] or '') if m else ''
        return str(m.end() if m else 0)

    def prim():
        nonlocal i
        t = peek()
        if t is None:
            bad()
        i += 1
        if t == '(':
            v = orx()
            if peek() != ')':
                bad()
            i += 1
            return v
        if t == 'length' and peek() is not None:
            return str(len(prim()))
        if t == 'match' and peek() is not None:
            return mat(prim(), prim())
        if t == 'substr' and peek() is not None:
            s, p, n = prim(), num_(prim()), num_(prim())
            return s[p - 1:p - 1 + n] if p > 0 and n > 0 else ''
        if t == 'index' and peek() is not None:
            s, cs = prim(), prim()
            return str(next((k for k, c in enumerate(s, 1) if c in cs), 0))
        return t

    def mulf(op, a, b):
        a, b = num_(a), num_(b)
        if op == '*':
            return str(a * b)
        if not b:
            raise Fail('division by zero')
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
        return str(q if op == '/' else a - b * q)

    def cmpf(op, a, b):
        if re.fullmatch(r'-?\d+', a) and re.fullmatch(r'-?\d+', b):
            a, b = int(a), int(b)
        return str(int({'=': a == b, '==': a == b, '!=': a != b, '<': a < b, '>': a > b, '<=': a <= b, '>=': a >= b}[op]))
    nul = ('', '0')
    ma = level((':',), prim, lambda op, a, b: mat(a, b))
    mu = level(('*', '/', '%'), ma, mulf)
    ad = level(('+', '-'), mu, lambda op, a, b: str(num_(a) + num_(b) if op == '+' else num_(a) - num_(b)))
    co = level(('=', '==', '!=', '<', '>', '<=', '>='), ad, cmpf)
    an = level(('&',), co, lambda op, a, b: a if a not in nul and b not in nul else '0')
    orx = level(('|',), an, lambda op, a, b: a if a not in nul else b if b not in nul else '0')
    v = orx()
    if i != len(args):
        bad()
    put(v)
    return 1 if v in nul or re.fullmatch('-?0+', v) else 0


@command('base64')
def base64(name, args):
    import base64 as b64
    o, rest = opts(args, 'dDw:i:o:hb:', 'decode=d wrap=w input=i output=o break=b ignore-garbage=ig')
    f = dict(o)
    d = data(f.get('i') or (rest[0] if rest else '-'))
    if 'd' in f or 'D' in f:
        d = re.sub(rb'[^A-Za-z0-9+/=]' if 'ig' in f else rb'\s', b'', d)
        try:
            r = b64.b64decode(d, validate=True)
        except ValueError:
            raise Fail('invalid input', 1) from None
    else:
        r = b64.b64encode(d)
        w = integer(f.get('w', f.get('b', 76 if sys.platform.startswith('linux') else 0)))
        if w:
            r = b'\n'.join(r[i:i + w] for i in range(0, len(r), w))
        if r and not ('w' in f and w == 0):
            r += b'\n'
    if 'o' in f and f['o'] != '-':
        with open(f['o'], 'wb') as h:
            h.write(r)
    else:
        outb(r)


@command('xxd')
def xxd(name, args):
    o, rest = opts([re.sub(r'^-(ps|plain)$', '-p', a) for a in args], 'prl:s:c:g:iub', 'revert=r')
    f = dict(o)
    if 'r' in f:
        t, r = text(rest[0] if rest else '-'), bytearray()
        if 'p' in f:
            r += bytes.fromhex(re.sub(r'\s+', '', t))
        else:
            for ln in lines(t):
                m = re.match(r'\s*([0-9a-fA-F]+):\s?(.*)', ln)
                if not m:
                    continue
                h = re.match(r'(?:[0-9a-fA-F]{2}(?: ?|(?=\s)))*', m[2].split('  ')[0])[0].replace(' ', '')
                a = int(m[1], 16)
                if len(r) < a:
                    r += bytes(a - len(r))
                b = bytes.fromhex(h)
                r[a:a + len(b)] = b
        if len(rest) > 1:
            with open(rest[1], 'wb') as h:
                h.write(r)
        else:
            outb(bytes(r))
        return 0
    d, skip = data(rest[0] if rest else '-'), int(f.get('s', '0'), 0)
    skip = max(len(d) + skip, 0) if skip < 0 else skip
    d = d[skip:]
    if 'l' in f:
        d = d[:int(f['l'], 0)]
    up = 'u' in f
    o = []
    if 'p' in f:
        c = integer(f.get('c', 30))
        o = [d[i:i + c].hex() for i in range(0, len(d), c)] if c else [d.hex()]
    elif 'i' in f:
        c = integer(f.get('c', 12))
        rows = ['  ' + ', '.join(f'0x{x:02x}' for x in d[i:i + c]) for i in range(0, len(d), c)]
        n = re.sub(r'\W', '_', rest[0]) if rest and rest[0] != '-' else None
        if n and n[0].isdigit():
            n = '__' + n
        o = ([f'unsigned char {n}[] = {{'] if n else []) + [',\n'.join(rows)] + ([f'}};\nunsigned int {n}_len = {len(d)};'] if n else [])
    else:
        bits = 'b' in f
        c, g = integer(f.get('c', 6 if bits else 16)), integer(f.get('g', 1 if bits else 2))
        for i in range(0, len(d), c):
            r = d[i:i + c]
            h = [f'{x:08b}' if bits else f'{x:02x}' for x in r]
            h = [x.upper() if up else x for x in h]
            hs = ''.join(x + (' ' if g and (k + 1) % g == 0 else '') for k, x in enumerate(h))
            w = c * (8 if bits else 2) + ((c + g - 1) // g if g else 0)
            o.append(f'{i + skip:08x}: {hs.ljust(w)} ' + ''.join(chr(x) if 32 <= x < 127 else '.' for x in r))
    s = ''.join(x + '\n' for x in o)
    if len(rest) > 1:
        with open(rest[1], 'w') as h:
            h.write(s)
    else:
        out(s)


@command('od')
def od(name, args):
    o, rest = opts(args, 'cxbdoA:t:N:j:vaw:', 'format=t address-radix=A read-bytes=N skip-bytes=j output-duplicates=v')
    f, types = {}, []
    for k, v in o:
        if k == 't':
            types += re.findall(r'[acdfoux](?:\d+|[CSIL])?z?', v)
        elif k in 'cxbdoa':
            types.append({'c': 'c', 'x': 'x2', 'b': 'o1', 'd': 'u2', 'o': 'o2', 'a': 'a'}[k])
        else:
            f[k] = v
    types = types or ['o2']
    d, rad = b''.join(data(x) for x in rest or ['-']), f.get('A', 'o')
    skip = int(f.get('j', '0'), 0)
    d = d[skip:]
    if 'N' in f:
        d = d[:int(f['N'], 0)]
    W = integer(f.get('w', 16))

    def addr(n):
        return {'o': f'{n:07o}', 'd': f'{n:07d}', 'x': f'{n:07x}', 'n': ''}[rad]
    CH = {0: '\\0', 7: '\\a', 8: '\\b', 9: '\\t', 10: '\\n', 11: '\\v', 12: '\\f', 13: '\\r'}
    import struct
    mac = not sys.platform.startswith('linux')
    last, star = None, False
    for i in range(0, len(d), W):
        r = d[i:i + W]
        if r == last and 'v' not in f and len(r) == W:
            if not star:
                put('*')
            star = True
            continue
        last, star = r, False
        for n, t in enumerate(types):
            k, sz = t[0], t[1:].rstrip('z')
            sz = {'C': 1, 'S': 2, 'I': 4, 'L': 8, '': 1 if k in 'ac' else 8 if k == 'f' else 4}.get(sz) or int(sz)
            if k in 'ac':
                cells = [f'{CH.get(x) or (chr(x) if 32 <= x < 127 else f"{x:03o}"):>4}' for x in r]
                if k == 'a':
                    nm = 'nul soh stx etx eot enq ack bel bs ht nl vt ff cr so si dle dc1 dc2 dc3 dc4 nak syn etb can em sub esc fs gs rs us sp'
                    cells = [f'{(nm.split() + [chr(c) for c in range(33, 127)] + ["del"])[x & 127]:>4}' for x in r]
            else:
                p = r + bytes(-len(r) % sz)
                w = {'x': sz * 2, 'o': {1: 3, 2: 6, 4: 11, 8: 22}[sz], 'u': {1: 3, 2: 5, 4: 10, 8: 20}[sz],
                     'd': {1: 4, 2: 6, 4: 11, 8: 20}[sz], 'f': 15 if sz == 4 else 24}[k]
                cells = []
                for q in range(0, len(p), sz):
                    b = p[q:q + sz]
                    if k == 'f':
                        v = f'{struct.unpack("<f" if sz == 4 else "<d", b)[0]:.{8 if sz == 4 else 17}g}'
                        cells.append(' ' + v.rjust(w))
                        continue
                    v = int.from_bytes(b, 'little', signed=k == 'd')
                    s = f'{v:0{w}{k}}' if k in 'xo' else str(v).rjust(w)
                    cells.append(' ' + s)
                if mac:
                    pad = {1: 4, 2: 8, 4: 16, 8: 32}[sz] if k != 'f' else 0
                    if k in 'xoud' and len(types) == 1:
                        cells = [c.strip().rjust(pad if sz > 1 else 4) if k != 'o' or sz != 8 else c for c in cells]
            out((addr(i + skip) if n == 0 else ' ' * len(addr(0))) + ''.join(cells) + '\n')
    if rad != 'n':
        put(addr(len(d) + skip))


# ---- checksums, compression ----

@command('md5', 'md5sum', 'shasum', 'sha1sum', 'sha256sum', 'sha512sum')
def checksum(name, args):
    import hashlib
    if name == 'md5':
        o, rest = opts(args, 'qrs:')
        f = dict(o)
        if 's' in f:
            put(hashlib.md5(enc(f['s'])).hexdigest() if 'q' in f else f'MD5 ("{f["s"]}") = {hashlib.md5(enc(f["s"])).hexdigest()}')
            return 0
        bad = []
        for p, d in texts(rest, bad, True):
            h = hashlib.md5(d).hexdigest()
            put(h if 'q' in f or not rest else f'{h} {p}' if 'r' in f else f'MD5 ({p}) = {h}')
        return 1 if bad else 0
    o, rest = opts(args, 'a:cbtqsw', 'algorithm=a check=c binary=b text=t quiet=q status=s tag strict warn=w ignore-missing')
    f = dict(o)
    alg = {'md5sum': 'md5', 'sha1sum': 'sha1', 'sha256sum': 'sha256', 'sha512sum': 'sha512'}.get(name) or 'sha' + f.get('a', '1')
    if alg not in ('md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512'):
        raise Fail('unknown algorithm')
    bad = []

    def digest(d):
        return hashlib.new(alg, d).hexdigest()
    if 'c' in f:
        wrong = miss = 0
        for p, t in texts(rest, bad):
            for ln in lines(t):
                m = re.match(r'\\?([0-9a-fA-F]{32,128}) [ *](.*)', ln) or re.match(r'\w+ \((?P<f>.*)\) = (?P<h>[0-9a-f]+)$', ln)
                if not m:
                    if ln.strip():
                        wrong += 1
                        err(f'improperly formatted line: {ln}')
                    continue
                h, n = (m['h'], m['f']) if 'f' in m.groupdict() else (m[1], m[2])
                try:
                    ok = hashlib.new({32: 'md5', 40: 'sha1', 56: 'sha224', 64: 'sha256', 96: 'sha384', 128: 'sha512'}[len(h)],
                                     data(n)).hexdigest() == h.lower()
                except OSError:
                    if 'ignore-missing' in f:
                        continue
                    miss += 1
                    put(f'{n}: FAILED open or read')
                    continue
                wrong += not ok
                if 's' not in f and not (ok and 'q' in f):
                    put(f'{n}: {"OK" if ok else "FAILED"}')
        if wrong and 's' not in f:
            err(f'WARNING: {wrong} computed checksum{"s" if wrong > 1 else ""} did NOT match')
        return 1 if wrong or miss or bad else 0
    for p, d in texts(rest, bad, True):
        put(f'{alg.upper()} ({p}) = {digest(d)}' if 'tag' in f else f'{digest(d)} {"*" if "b" in f else " "}{p}')
    return 1 if bad else 0


def codec(kind):
    """compress(data, level), decompress(data), suffix."""
    if kind == 'gzip':
        import gzip
        return (lambda d, n: gzip.compress(d, n, mtime=0)), gzip.decompress, '.gz'
    if kind == 'bzip2':
        import bz2
        return (lambda d, n: bz2.compress(d, n)), bz2.decompress, '.bz2'
    import lzma
    return (lambda d, n: lzma.compress(d, preset=n)), lzma.decompress, '.xz'


@command('gzip', 'gunzip', 'zcat', 'gzcat', 'bzip2', 'bunzip2', 'bzcat', 'xz', 'unxz', 'xzcat')
def compress(name, args):
    import zlib
    kind = 'gzip' if 'z' in name[:3] and name[0] in 'gz' or name == 'gunzip' else 'bzip2' if name.startswith('b') else 'xz'
    o, rest = opts(args, 'cdfkvq0123456789ntlrS:T:ze', 'stdout=c to-stdout=c decompress=d uncompress=d force=f keep=k verbose=v quiet=q '
                   'best=9 fast=1 test=t no-name=n threads=T compress=z extreme=e list=l recursive=r')
    f, level = {}, None
    for k, v in o:
        f[k] = v
        if k.isdigit():
            level = int(k)
    if f.keys() & {'l', 'r', 'S'}:
        raise Fail('unsupported option')
    comp, dec_, suf = codec(kind)
    un = 'd' in f or 'un' in name or name.endswith('cat') or 't' in f
    tostd = 'c' in f or name.endswith('cat')
    level = level if level is not None else 6 if kind != 'bzip2' else 9
    if kind == 'bzip2' and level == 0:
        raise Fail('unsupported option -0')
    rc = 0
    for p in rest or ['-']:
        try:
            d = data(p)
        except OSError as e:
            err(oserr(e, p))
            rc = 1
            continue
        if un:
            try:
                r = dec_(d) if d else b''
            except (OSError, EOFError, zlib.error, ValueError) as e:
                err(f'{p}: {e or "corrupt data"}')
                rc = 1
                continue
            except Exception as e:     # lzma.LZMAError, imported late
                err(f'{p}: {e}')
                rc = 1
                continue
        else:
            r = comp(d, level)
        if 't' in f:
            continue
        if tostd or p == '-':
            outb(r)
            continue
        if un:
            m = {'.tgz': '.tar', '.tbz': '.tar', '.tbz2': '.tar', '.txz': '.tar'}.get(os.path.splitext(p)[1])
            if not p.endswith(suf) and not m:
                err(f'{p}: unknown suffix -- ignored')
                rc = 1
                continue
            q = p[:-len(suf)] if p.endswith(suf) else os.path.splitext(p)[0] + m
        else:
            if p.endswith(suf) and 'f' not in f:
                err(f'{p} already has {suf} suffix -- unchanged')
                rc = 1
                continue
            q = p + suf
        if os.path.exists(q) and 'f' not in f:
            err(f'{q} already exists; use -f to overwrite')
            rc = 1
            continue
        st = os.stat(p)
        with open(q, 'wb') as h:
            h.write(r)
        os.chmod(q, st.st_mode & 0o777)
        os.utime(q, ns=(st.st_atime_ns, st.st_mtime_ns))
        if 'v' in f:
            State.out.flush()
            sys.stderr.write(f'{p}: {"created" if not un else "expanded to"} {q}\n')
        if 'k' not in f:
            os.unlink(p)
    return rc


# ---- tar, zip ----

@command('tar')
def tar(name, args):
    import io
    import tarfile
    args = list(args)
    if args and not args[0].startswith('-'):
        # old style: tar xzfC FILE DIR
        key, vals, exp = args.pop(0), [], []
        for c in key:
            exp.append('-' + c)
            if c in 'fCTb' and args:
                exp.append(args.pop(0))
        args = exp + args
    cdirs, o, rest = [], [], []
    # -C is positional when creating, so the operands keep the folder they were given in
    i, cur = 0, '.'
    while i < len(args):
        a = args[i]
        if a == '-C' or a == '--directory':
            i += 1
            cur = os.path.join(cur, args[i]) if i < len(args) else cur
        elif a.startswith('--directory='):
            cur = os.path.join(cur, a[12:])
        elif a == '--':
            rest += [(cur, x) for x in args[i + 1:]]
            break
        elif a.startswith('--'):
            r = opts([a] + args[i + 1:i + 2], '', LONGTAR)
            o += r[0]
            if r[0] and r[0][0][1] is not None and '=' not in a:
                i += 1
        elif a.startswith('-') and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                j += 1
                if c in 'fCTXbI':
                    v = a[j:]
                    if not v:
                        i += 1
                        if i >= len(args):
                            raise Fail(f'option -{c} needs a value')
                        v = args[i]
                    if c == 'C':
                        cur = os.path.join(cur, v)
                    else:
                        o.append((c, v))
                    break
                if c not in 'cxtrzjJvOkpPhmanoSlUZ':
                    raise Fail(f'unsupported option -{c}')
                o.append((c, None))
        else:
            rest.append((cur, a))
        i += 1
    f, exc = {}, []
    for k, v in o:
        if k == 'exclude':
            exc.append(v)
        elif k == 'X':
            exc += lines(text(v))
        else:
            f[k] = v
    if f.keys() & {'r', 'Z', 'I', 'n', 'l'}:
        raise Fail('unsupported option')
    modes = [m for m in 'cxt' if m in f]
    if len(modes) != 1:
        raise Fail('give exactly one of -c -x -t')
    mode, fn, strip = modes[0], f.get('f', '-'), integer(f.get('strip', 0))
    if 'T' in f:
        rest += [(cur, x) for x in lines(text(f['T'])) if x]
    ex = [re.compile('(?:.*/)?' * (not x.startswith('/')) + re.sub(r'\[\^/\]\*', '.*', path_glob(x.rstrip('/'))) + r'(?:/.*)?\Z', re.S)
          for x in exc]

    def excluded(n):
        return any(e.match(n[2:] if n.startswith('./') else n) or e.match(n) for e in ex)
    z = 'gz' if 'z' in f else 'bz2' if 'j' in f else 'xz' if 'J' in f else 'zst' if 'zstd' in f else None
    if mode == 'c':
        if 'a' in f and not z:
            z = {'.gz': 'gz', '.tgz': 'gz', '.bz2': 'bz2', '.tbz': 'bz2', '.xz': 'xz', '.txz': 'xz', '.zst': 'zst'}.get(os.path.splitext(fn)[1])
        if not rest:
            raise Fail('cowardly refusing to create an empty archive')
        buf = io.BytesIO() if fn == '-' else open(fn, 'wb')
        rc = 0
        skip = os.path.realpath(fn) if fn != '-' else None
        with tarfile.open(fileobj=buf, mode='w:' + (z or ''), format=tarfile.GNU_FORMAT if f.get('format') == 'gnu' else tarfile.PAX_FORMAT,
                          dereference='h' in f, **({'compresslevel': 6} if z == 'gz' else {})) as t:
            def add(d, n):
                nonlocal rc
                p = os.path.join(d, n)
                if excluded(n) or (skip and os.path.realpath(p) == skip):
                    return
                try:
                    ti = t.gettarinfo(p, n)
                    if ti is None:
                        return
                    if 'numeric-owner' in f:
                        ti.uname = ti.gname = ''
                    if 'v' in f:
                        State.out.flush()
                        sys.stderr.write(f'a {n}\n' if not sys.platform.startswith('linux') else n + '\n')
                    if ti.isreg():
                        with open(p, 'rb') as h:
                            t.addfile(ti, h)
                    else:
                        t.addfile(ti)
                    if ti.isdir() and 'no-recursion' not in f:
                        for x in sorted(os.listdir(p)):
                            add(d, n.rstrip('/') + '/' + x)
                except OSError as e:
                    err(oserr(e, n))
                    rc = 1
            for d, n in rest:
                add(d, n)
        if fn == '-':
            outb(buf.getvalue())
        else:
            buf.close()
        return rc
    try:
        src = io.BytesIO(data('-')) if fn == '-' else open(fn, 'rb')
    except OSError as e:
        raise Fail(f'cannot open {oserr(e, fn)}') from None
    rc, want, seen = 0, [x.rstrip('/') for _, x in rest], set()

    def picked(n):
        if not want:
            return True
        ok = False
        for w in want:
            if n.rstrip('/') == w or n.startswith(w + '/') or ('wildcards' in f or not sys.platform.startswith('linux')) and \
                    re.match(re.sub(r'\[\^/\]\*', '.*', path_glob(w)) + r'(?:/.*)?\Z', n.rstrip('/')):
                seen.add(w)
                ok = True
        return ok
    dest = cur
    if mode == 'x' and not os.path.isdir(dest):
        raise Fail(f'{dest}: no such directory')
    try:
        with src, tarfile.open(fileobj=src, mode='r:*') as t:
            dirs = []
            for m in t:
                n = m.name
                if not picked(n) or excluded(n):
                    continue
                if strip:
                    p = re.sub('/+', '/', n).split('/')
                    if len(p) <= strip:
                        continue
                    n = '/'.join(p[strip:])
                    if not n:
                        continue
                    link = m.linkname
                    if m.islnk():
                        link = '/'.join(link.split('/')[strip:])
                    m = m.replace(name=n, linkname=link, deep=False)
                if mode == 't':
                    d = '/' if m.isdir() else ''
                    if 'v' in f:
                        import stat as st
                        import time
                        kind = st.S_IFDIR if m.isdir() else st.S_IFLNK if m.issym() else st.S_IFREG
                        put(f'{st.filemode(kind | m.mode)} {m.uname or m.uid}/{m.gname or m.gid} {m.size:>9} '
                            + time.strftime('%Y-%m-%d %H:%M ', time.localtime(m.mtime)) + n + d
                            + (f' -> {m.linkname}' if m.issym() else f' link to {m.linkname}' if m.islnk() else ''))
                    else:
                        put(n + d)
                    continue
                if 'O' in f:
                    if m.isreg():
                        outb(t.extractfile(m).read())
                    continue
                target = os.path.join(dest, n)
                if 'k' in f and os.path.lexists(target) and not m.isdir():
                    continue
                if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
                    err(f'{n}: special file skipped')
                    continue
                if os.path.isabs(n) or '..' in n.split('/'):
                    err(f'{n}: refused, outside the target folder')
                    rc = 2
                    continue
                if 'v' in f:
                    State.out.flush()
                    sys.stderr.write(f'x {n}\n') if not sys.platform.startswith('linux') else put(n)
                try:
                    if os.path.lexists(target) and not m.isdir() and (os.path.islink(target) or not os.path.isdir(target)):
                        if os.path.realpath(os.path.dirname(target)).startswith(os.path.realpath(dest)):
                            os.unlink(target)
                    if 'm' in f:
                        import time
                        m = m.replace(mtime=time.time(), deep=False)
                    if m.isdir():
                        dirs.append(m.replace(deep=False))
                        m = m.replace(mode=m.mode | 0o700, deep=False)
                    t.extract(m, dest, filter='data', set_attrs=True, numeric_owner=True)
                    if m.isreg() and m.mode & 0o7777:
                        os.chmod(target, m.mode & 0o777)
                except tarfile.FilterError as e:
                    err(f'{n}: refused, outside the target folder ({type(e).__name__})')
                    rc = 2
                except OSError as e:
                    err(oserr(e, n))
                    rc = 2
            for m in reversed(dirs):
                try:
                    p = os.path.join(dest, m.name)
                    if not os.path.islink(p):
                        os.utime(p, (m.mtime, m.mtime))
                except OSError:
                    pass
    except (tarfile.TarError, EOFError, OSError) as e:
        if isinstance(e, BrokenPipeError):
            raise
        raise Fail(f'{fn}: {e}') from None
    for w in want:
        if w not in seen:
            err(f'{w}: Not found in archive')
            rc = 2
    return rc


LONGTAR = ('create=c extract=x get=x list=t file=f: gzip=z bzip2=j xz=J zstd verbose=v exclude: strip-components=strip: '
           'to-stdout=O keep-old-files=k preserve-permissions=p dereference=h touch=m auto-compress=a files-from=T: exclude-from=X: '
           'wildcards no-recursion numeric-owner no-same-owner=o no-same-permissions=o format: overwrite=ow no-mac-metadata=o '
           'no-xattrs=o no-acls=o')


@command('zip')
def zip_(name, args):
    import zipfile
    o, rest = opts(args, 'rqj0123456789yx@oXDuvmd', 'recurse-paths=r quiet=q junk-paths=j symlinks=y exclude=x')
    f = {k for k, _ in o}
    level = next((int(k) for k, _ in o if k.isdigit()), 6)
    if f & {'u', 'm', 'd', 'o'}:
        raise Fail('unsupported option')
    exc = []
    if 'x' in f:
        # everything after -x is a pattern
        k = next(i for i, a in enumerate(args) if a in ('-x', '--exclude') or re.fullmatch(r'-\w*x', a))
        exc = [x for x in args[k + 1:] if not x.startswith('-')]
        rest = opts(args[:k], 'rqj0123456789yx@oXDuvmd', 'recurse-paths=r quiet=q junk-paths=j symlinks=y')[1]
    if not rest:
        raise Fail('usage: zip [-rqj0-9y] archive file... [-x pattern...]')
    arc = rest[0] if '.' in os.path.basename(rest[0]) or rest[0] == '-' else rest[0] + '.zip'
    import io
    names = rest[1:] + (lines(text('-')) if '@' in f else [])
    buf = io.BytesIO() if arc == '-' else None
    ex = [glob_re(x) for x in exc]
    items, rc = [], 0

    def add(p):
        nonlocal rc
        n = re.sub(r'^(\./)+|^/+', '', re.sub('/+', '/', p))
        if any(g(n) or g(p) for g in ex):
            return
        if os.path.isdir(p) and not ('y' in f and os.path.islink(p)):
            if 'j' not in f and 'D' not in f and n not in ('', '.'):
                items.append((p, n.rstrip('/') + '/'))
            if 'r' in f:
                for x in sorted(os.listdir(p)):
                    add(os.path.join(p, x))
        elif os.path.lexists(p):
            if buf is None and os.path.realpath(p) == os.path.realpath(arc):
                return
            items.append((p, os.path.basename(n) if 'j' in f else n))
        else:
            err(f'warning: name not matched: {p}')
            rc = 12
    for p in names:
        add(p)
    if not items:
        raise Fail('nothing to do', 12)
    # entries that are replaced are dropped from the old archive
    old = []
    if buf is None and os.path.exists(arc):
        new = {n for _, n in items}
        with zipfile.ZipFile(arc) as z:
            old = [(i, z.read(i)) for i in z.infolist() if i.filename not in new]
    tmp = buf if buf is not None else arc + '.tmp~'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED if level else zipfile.ZIP_STORED, compresslevel=level or None) as z:
        for i, d in old:
            z.writestr(i, d)
        for p, n in items:
            if os.path.islink(p) and 'y' in f:
                i = zipfile.ZipInfo(n)
                i.create_system, i.external_attr = 3, 0o120777 << 16
                z.writestr(i, os.readlink(p))
            else:
                z.write(p, n)
            if 'q' not in f:
                i = z.getinfo(n)
                pc = round(100 * (1 - i.compress_size / i.file_size)) if i.file_size else 0
                State.out.write(enc(f'  adding: {n} ({"deflated" if pc else "stored"} {pc}%)\n')) if buf is None else None
    if buf is not None:
        outb(buf.getvalue())
    else:
        os.replace(tmp, arc)
    return rc


@command('unzip')
def unzip(name, args):
    import stat as st
    import time
    import zipfile
    o, rest = opts(args, 'd:lonqpjtvxZ1aC', '')
    f = {}
    for k, v in o:
        f[k] = v
    exc = []
    if 'x' in f:
        k = args.index('-x') if '-x' in args else None
        if k is None:
            raise Fail('give -x on its own')
        tail = args[k + 1:]
        if '-d' in tail:
            j = tail.index('-d')
            f['d'] = tail[j + 1]
            tail = tail[:j] + tail[j + 2:]
        exc, rest = tail, opts(args[:k], 'd:lonqpjtvxZ1aC', '')[1]
    if 'a' in f:
        raise Fail('unsupported option -a')
    if not rest:
        raise Fail('usage: unzip [-lptqonj] archive [file...] [-x file...] [-d dir]', 10)
    arc = rest[0]
    if not os.path.exists(arc) and os.path.exists(arc + '.zip'):
        arc += '.zip'
    fl = re.I if 'C' in f else 0
    want = [re.compile(re.sub(r'\[\^/\]\*', '.*', path_glob(x)) + r'\Z', fl) for x in rest[1:]]
    ex = [re.compile(re.sub(r'\[\^/\]\*', '.*', path_glob(x)) + r'\Z') for x in exc]
    try:
        z = zipfile.ZipFile(__import__('io').BytesIO(data('-')) if arc == '-' else arc)
    except FileNotFoundError:
        raise Fail(f'cannot find or open {rest[0]}', 9) from None
    except zipfile.BadZipFile as e:
        raise Fail(f'{arc}: {e}', 9) from None
    infos = [i for i in z.infolist() if (not want or any(w.match(i.filename) for w in want))
             and not any(w.match(i.filename) for w in ex)]
    if want and not infos:
        raise Fail(f'caution: filename not matched:  {rest[1]}', 11)
    quiet = 'q' in f
    if 'Z' in f:
        for i in infos:
            put(i.filename)
        return 0
    if 'l' in f or 'v' in f and len(rest) > 0:
        if not quiet:
            put(f'Archive:  {arc}')
        put('  Length      Date    Time    Name')
        put('---------  ---------- -----   ----')
        for i in infos:
            d = i.date_time
            put(f'{i.file_size:9d}  {d[1]:02d}-{d[2]:02d}-{d[0]} {d[3]:02d}:{d[4]:02d}   {i.filename}')
        put('---------                     -------')
        put(f'{sum(i.file_size for i in infos):9d}                     {len(infos)} file{"" if len(infos) == 1 else "s"}')
        return 0
    if 't' in f:
        b = z.testzip()
        put(f'{arc}: {"bad CRC in " + b if b else "No errors detected in compressed data"}.')
        return 2 if b else 0
    dest, rc = f.get('d', '.'), 0
    if 'p' in f:
        for i in infos:
            if not i.is_dir():
                outb(z.read(i))
        return 0
    if not quiet:
        put(f'Archive:  {arc}')
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    for i in infos:
        n = os.path.basename(i.filename) if 'j' in f else i.filename
        if 'j' in f and i.is_dir():
            continue
        n = n.replace('\\', '/') if i.create_system == 0 else n
        p = os.path.join(dest, n)
        real = os.path.realpath(p) if not n.endswith('/') else os.path.realpath(p.rstrip('/'))
        if n.startswith('/') or '..' in n.split('/') or not (real + '/').startswith(root.rstrip('/') + '/'):
            err(f'{n}: refused, outside the target folder')
            rc = 2
            continue
        mode = i.external_attr >> 16
        if i.is_dir():
            os.makedirs(p, exist_ok=True)
            if not quiet:
                put(f'   creating: {p}')
            continue
        if os.path.lexists(p) and 'o' not in f:
            if 'n' not in f:
                err(f'{p} exists; skipped (use -o to overwrite)')
                rc = 1
            continue
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        try:
            d = z.read(i)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:
            err(f'{n}: {e}')
            rc = 2
            continue
        if os.path.lexists(p):
            os.unlink(p)
        if st.S_ISLNK(mode):
            link = dec(d)
            t = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(p)), link))
            if os.path.isabs(link) or not (t + '/').startswith(root.rstrip('/') + '/'):
                err(f'{n}: link refused, outside the target folder')
                rc = 2
                continue
            os.symlink(link, p)
        else:
            with open(p, 'wb') as h:
                h.write(d)
            if mode & 0o777:
                os.chmod(p, mode & 0o777)
            try:
                t = time.mktime((*i.date_time, 0, 0, -1))
                os.utime(p, (t, t))
            except (ValueError, OverflowError):
                pass
        if not quiet:
            put(f'{"  inflating" if i.compress_type else " extracting"}: {p}  ')
    return rc


# ---- curl, wget ----

class Http:
    """One request with urllib; redirects are followed by hand so that credentials stay with their host."""
    def __init__(self, method, url, headers, body, insecure=False, timeout=None, cacert=None):
        import http.client
        import ssl
        import urllib.error
        import urllib.request as ur
        self.url = url
        if not re.match(r'https?://', url, re.I):
            raise Fail(f'unsupported protocol in {url}', 1)

        class NoRedirect(ur.HTTPRedirectHandler):
            def redirect_request(self, *a):
                return None
        ctx = ssl.create_default_context(cafile=cacert) if url[:5].lower() == 'https' else None
        if ctx and insecure:
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        hs = [ur.ProxyHandler(ur.getproxies_environment()), NoRedirect()] + ([ur.HTTPSHandler(context=ctx)] if ctx else [])
        op = ur.build_opener(*hs)
        op.addheaders = []
        r = ur.Request(url, data=body, method=method)
        for k, v in headers.items():
            if v is not None:
                r.add_unredirected_header(k, v)
        try:
            self.r = op.open(r, timeout=timeout)
        except urllib.error.HTTPError as e:
            self.r = e
        except urllib.error.URLError as e:
            self.fail(e.reason)
        except (OSError, http.client.HTTPException) as e:
            self.fail(e)
        except ValueError as e:
            raise Fail(f'bad URL: {e}', 3) from None
        self.status, self.headers = self.r.status, self.r.headers

    def fail(self, e):
        import socket
        import ssl
        host = re.sub(r'^https?://([^/]*@)?([^/?#]*).*', r'\2', self.url)
        if isinstance(e, socket.gaierror):
            raise Fail(f'(6) Could not resolve host: {host}', 6)
        if isinstance(e, TimeoutError):
            raise Fail('(28) Operation timed out', 28)
        if isinstance(e, ssl.SSLCertVerificationError):
            raise Fail(f'(60) SSL certificate problem: {e.verify_message}', 60)
        if isinstance(e, ssl.SSLError):
            raise Fail(f'(35) SSL error: {e.reason}', 35)
        if isinstance(e, ConnectionRefusedError):
            raise Fail(f'(7) Failed to connect to {host}: Connection refused', 7)
        if isinstance(e, ConnectionResetError):
            raise Fail('(56) Connection reset by peer', 56)
        raise Fail(f'(7) Failed to connect to {host}: {e}', 7)


def set_header(h, k, v):
    for x in list(h):
        if x.lower() == k.lower():
            del h[x]
    h[k] = v


@command('curl')
def curl(name, args):
    import time
    import urllib.parse as up
    o, urls = opts(args, 'X:d:H:o:OLsSfIiku:w:A:e:b:GT:m:vD:#gnN46ZJa0', 'request=X data=d data-ascii=d data-raw: data-binary: data-urlencode: '
                   'header=H output=o remote-name=O location=L silent=s show-error=S fail=f head=I include=i insecure=k user=u write-out=w '
                   'user-agent=A referer=e get=G json: max-time=m connect-timeout: retry: retry-delay: compressed verbose=v '
                   'dump-header=D progress-bar=# create-dirs fail-with-body=fb url: cacert: globoff=g no-progress-meter=s no-buffer=N '
                   'ipv4=4 ipv6=6 http1.1=0 http2=0 proto: tlsv1.2=0 oauth2-bearer: upload-file=T cookie=b remote-header-name=J '
                   'retry-connrefused=0 retry-all-errors=0 max-redirs: proto-redir: remote-name-all location-trusted url-query: output-dir: '
                   'continue-at: append=a retry-max-time:')
    f, hs, body, outs, dflt = {}, {}, [], [], 'application/x-www-form-urlencoded'
    for k, v in o:
        if k == 'H':
            if v.startswith('@'):
                raise Fail('unsupported -H @file')
            a, c, b = v.partition(':')
            if not c and a.endswith(';'):
                set_header(hs, a[:-1], '')
            else:
                set_header(hs, a.strip(), b.strip() or None)
        elif k in ('d', 'data-binary', 'json'):
            if v.startswith('@'):
                b = data(v[1:])
                body.append(re.sub(rb'[\r\n]', b'', b) if k == 'd' else b)
            else:
                body.append(enc(v))
            if k == 'json':
                f['json'] = True
        elif k == 'data-raw':
            body.append(enc(v))
        elif k == 'data-urlencode':
            n, eq, x = v.partition('=')
            if '@' in v and (not eq or v.index('@') < v.index('=')):
                n, _, p = v.partition('@')
                x, eq = text(p), '='
            elif not eq:
                n, x = '', v
            body.append(enc((n + '=' if n else '') + up.quote(x, safe='')))
        elif k in 'oO':
            outs.append((k, v))
        elif k == 'url':
            urls.append(v)
        else:
            f[k] = v if v is not None else True
    bad = sorted(f.keys() & {'v', 'n', 'J', 'remote-name-all', 'location-trusted', 'url-query', 'continue-at', 'a'})
    if bad:
        raise Fail('unsupported option ' + ', '.join(bad))
    if not urls:
        raise Fail('no URL specified')
    if 'b' in f:
        if '=' not in f['b']:
            raise Fail('cookie files are not supported')
        set_header(hs, 'Cookie', f['b'])
    low = {k.lower() for k in hs}
    for k, v in (('User-Agent', f.get('A', 'devtools-curl/1')), ('Accept', 'application/json' if 'json' in f else '*/*'), ('Referer', f.get('e'))):
        if k.lower() not in low and v:
            hs[k] = v
    if 'u' in f:
        import base64 as b64
        u = f['u'] if ':' in f['u'] else f['u'] + ':'
        hs['Authorization'] = 'Basic ' + b64.b64encode(enc(u)).decode()
    if 'oauth2-bearer' in f:
        hs['Authorization'] = 'Bearer ' + f['oauth2-bearer']
    if 'compressed' in f:
        hs['Accept-Encoding'] = 'gzip, deflate'
    body = (b'' if 'json' in f else b'&').join(body) if body else None
    if 'T' in f:
        body = data(f['T'])
    rc = 0
    silent = 's' in f
    for n, url in enumerate(urls):
        if '://' not in url:
            url = 'http://' + url
        url = up.quote(url, safe="%/:=&?~#+!$,;'@()*[]{}|")
        h, b = dict(hs), body
        if 'G' in f and b is not None:
            url += ('&' if '?' in url else '?') + dec(b)
            b = None
        if b is not None and 'T' not in f and 'content-type' not in low:
            h['Content-Type'] = 'application/json' if 'json' in f else dflt
        method = f.get('X') or ('HEAD' if 'I' in f else 'PUT' if 'T' in f else 'POST' if b is not None else 'GET')
        t0, tries, start, red = time.time(), integer(f.get('retry', 0)), url, 0
        kind, dest = outs[n] if n < len(outs) else ('', None)
        if kind == 'O':
            dest = up.unquote(os.path.basename(up.urlsplit(url).path))
            if not dest or '/' in dest or dest in ('.', '..'):
                err('Remote file name has no length!')
                rc = 23
                continue
        if dest and 'output-dir' in f:
            dest = os.path.join(f['output-dir'], dest)
        try:
            heads = []
            while True:
                tmo = f.get('connect-timeout') or f.get('m')
                try:
                    q = Http(method, url, h, b, 'k' in f, float(f.get('m') or tmo) if tmo else None, f.get('cacert'))
                except Fail as e:
                    if tries > 0 and e.code in (6, 7, 28, 56):
                        tries -= 1
                        time.sleep(float(f.get('retry-delay', 1)))
                        continue
                    raise
                if tries > 0 and q.status in (408, 429, 500, 502, 503, 504):
                    tries -= 1
                    time.sleep(float(f.get('retry-delay', 1)))
                    continue
                hd = f'HTTP/{"1.1" if getattr(q.r, "version", 11) != 10 else "1.0"} {q.status} {q.r.reason}\r\n' + \
                    ''.join(f'{k}: {v}\r\n' for k, v in q.headers.items()) + '\r\n'
                heads.append(hd)
                loc = q.headers.get('Location')
                if 'L' in f and loc and q.status in (301, 302, 303, 307, 308):
                    red += 1
                    if red > integer(f.get('max-redirs', 50)):
                        raise Fail('(47) Maximum redirects followed', 47)
                    q.r.close()
                    new = up.urljoin(url, loc)
                    if up.urlsplit(new)[:2] != up.urlsplit(url)[:2]:
                        h.pop('Authorization', None)
                        for k in [k for k in h if k.lower() in ('cookie', 'authorization')]:
                            del h[k]
                    if q.status in (301, 302, 303) and method == 'POST' and 'X' not in f or q.status == 303 and method != 'HEAD' and 'X' not in f:
                        method, b = 'GET', None
                        h.pop('Content-Type', None)
                    url = new
                    continue
                break
            code = q.status
            if 'D' in f:
                if f['D'] == '-':
                    out(''.join(heads))
                else:
                    with open(f['D'], 'wb') as x:
                        x.write(enc(''.join(heads)))
            failed = code >= 400 and ('f' in f or 'fb' in f)
            size = 0
            if not (failed and 'f' in f):
                sink = None
                if dest and dest != '-':
                    if 'create-dirs' in f:
                        os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
                    sink = open(dest, 'wb')
                wr = sink.write if sink else outb
                try:
                    if 'I' in f or 'i' in f:
                        wr(enc(''.join(heads)))
                    if method != 'HEAD':
                        ce = (q.headers.get('Content-Encoding') or '').lower() if 'compressed' in f else ''
                        if ce in ('gzip', 'deflate'):
                            import zlib
                            raw = q.r.read()
                            try:
                                d = zlib.decompress(raw, 47)
                            except zlib.error:
                                d = zlib.decompress(raw, -15)
                            size = len(raw)
                            wr(d)
                        else:
                            while True:
                                d = q.r.read(65536)
                                if not d:
                                    break
                                size += len(d)
                                wr(d)
                                if not sink:
                                    State.out.flush()
                            want = q.headers.get('Content-Length')
                            if want and want.isdigit() and size < int(want):
                                raise Fail('(18) transfer closed with bytes remaining', 18)
                finally:
                    if sink:
                        sink.close()
            if failed:
                if not silent or 'S' in f:
                    err(f'(22) The requested URL returned error: {code}')
                rc = 22
        except Fail as e:
            if not silent or 'S' in f:
                err(e)
            rc, code, size, q = e.code, 0, 0, None
        except (OSError, __import__('http.client').client.HTTPException) as e:
            if isinstance(e, BrokenPipeError):
                raise
            rc, code, size, q = (28 if isinstance(e, TimeoutError) else 56 if not getattr(e, 'filename', None) else 23), 0, 0, None
            if not silent or 'S' in f:
                err(f'({rc}) {e}')
        if 'w' in f:
            t = text(f['w'][1:]) if f['w'].startswith('@') else f['w']
            vals = {'http_code': f'{code:03d}', 'response_code': f'{code:03d}', 'size_download': str(size), 'url_effective': url,
                    'time_total': f'{time.time() - t0:.6f}', 'content_type': (q.headers.get('Content-Type') or '') if q else '',
                    'num_redirects': str(red), 'url': start, 'method': method, 'exitcode': str(rc), 'filename_effective': dest or '',
                    'redirect_url': up.urljoin(url, q.headers['Location']) if q and q.headers.get('Location') else '',
                    'scheme': url.split(':')[0], 'header_json': '{}'}

            def rep(m):
                if m[1] not in vals:
                    raise Fail(f'unsupported -w variable {m[1]}')
                return vals[m[1]]
            out(re.sub(r'%\{(\w+)\}', rep, unescape(t)).replace('%%', '%'))
    return rc


@command('wget')
def wget(name, args):
    import urllib.parse as up
    o, urls = opts([{'-nv': '-q', '-nc': '--no-clobber'}.get(a, a) for a in args], 'O:qP:cT:t:S', 'output-document=O quiet=q no-verbose=q '
                   'directory-prefix=P no-check-certificate=k header: user-agent: timeout=T tries=t show-progress=q progress: no-clobber '
                   'post-data: max-redirect:')
    f, hs = {}, {'User-Agent': 'devtools-wget/1', 'Accept': '*/*'}
    for k, v in o:
        if k == 'header':
            a, _, b = v.partition(':')
            set_header(hs, a.strip(), b.strip())
        else:
            f[k] = v if v is not None else True
    if f.keys() & {'c', 'S', 'post-data'}:
        raise Fail('unsupported option')
    if not urls:
        raise Fail('missing URL', 1)
    if 'user-agent' in f:
        hs['User-Agent'] = f['user-agent']
    rc = 0
    for url in urls:
        if '://' not in url:
            url = 'http://' + url
        n = f.get('O') or os.path.join(f.get('P', '.'), up.unquote(os.path.basename(up.urlsplit(url).path)).replace('/', '_') or 'index.html')
        n = n[2:] if n.startswith('./') else n
        if 'no-clobber' in f and n != '-' and os.path.exists(n):
            err(f'File {n} already there; not retrieving.')
            continue
        h = dict(hs)
        try:
            for _ in range(integer(f.get('max-redirect', 20)) + 1):
                q = Http('GET', url, h, None, 'k' in f, float(f['T']) if 'T' in f else None)
                loc = q.headers.get('Location')
                if q.status in (301, 302, 303, 307, 308) and loc:
                    new = up.urljoin(url, loc)
                    if up.urlsplit(new)[:2] != up.urlsplit(url)[:2]:
                        for k in [k for k in h if k.lower() in ('cookie', 'authorization')]:
                            del h[k]
                    url = new
                    continue
                break
            if q.status >= 300:
                err(f'{url}: ERROR {q.status}: {q.r.reason}')
                rc = 8
                continue
            d = q.r.read()
        except Fail as e:
            err(f'{url}: {e}')
            rc = 5 if e.code == 60 else 4
            continue
        except Exception as e:     # a broken transfer: OSError or an http.client error
            err(f'{url}: {e}')
            rc = 4
            continue
        if n == '-':
            outb(d)
        else:
            os.makedirs(os.path.dirname(n) or '.', exist_ok=True)
            with open(n, 'wb') as w:
                w.write(d)
            if 'q' not in f:
                State.out.flush()
                sys.stderr.write(f'{url}: saved {n} [{len(d)}]\n')
    return rc


# ---- sqlite3 ----

@command('sqlite3')
def sqlite3_(name, args):
    import sqlite3
    f, rest, cmds, i = {'mode': 'list', 'header': False, 'sep': '|', 'null': ''}, [], [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if not a.startswith('-') or a == '-':
            rest.append(a)
            continue
        a = a.lstrip('-')
        if a in ('csv', 'json', 'line', 'column', 'list', 'tabs', 'table', 'markdown', 'quote'):
            f['mode'] = a
        elif a in ('header', 'noheader'):
            f['header'] = a == 'header'
        elif a in ('separator', 'cmd', 'nullvalue'):
            if i >= len(args):
                raise Fail(f'option -{a} needs a value', 1)
            if a == 'cmd':
                cmds.append(args[i])
            else:
                f['sep' if a == 'separator' else 'null'] = unescape(args[i])
            i += 1
        elif a in ('readonly', 'bail'):
            f[a] = True
        elif a == 'version':
            put(sqlite3.sqlite_version)
            return 0
        elif a not in ('batch', 'interactive'):
            raise Fail(f'unsupported option -{a}', 1)
    if f['mode'] in ('table', 'markdown', 'quote'):
        raise Fail(f'unsupported mode {f["mode"]}')
    path = rest[0] if rest else ':memory:'
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro' if 'readonly' in f else path, uri='readonly' in f, isolation_level=None)
    except sqlite3.Error as e:
        raise Fail(str(e), 1) from None
    db.text_factory = lambda b: b.decode('utf-8', 'surrogateescape')

    def s(v):
        if v is None:
            return f['null']
        if isinstance(v, bytes):
            return dec(v)
        if isinstance(v, float):
            r = f'{v:.15g}'
            return r if any(c in r for c in '.en') else r + '.0'
        return str(v)

    def show(cur):
        if cur.description is None:
            return
        cols, rows, m = [d[0] for d in cur.description], cur.fetchall(), f['mode']
        if not rows:
            return
        if m == 'json':
            import json

            def j(v):
                return dec(v) if isinstance(v, bytes) else v
            put('[' + ',\n'.join('{' + ','.join(json.dumps(c, ensure_ascii=False) + ':' + json.dumps(j(v), ensure_ascii=False)
                                              for c, v in zip(cols, r)) + '}' for r in rows) + ']')
        elif m == 'line':
            w = max(map(len, cols))
            put('\n\n'.join('\n'.join(f'{c:>{max(w, 5)}} = {s(v)}' for c, v in zip(cols, r)) for r in rows))
        elif m == 'column':
            t = [[s(v) for v in r] for r in rows]
            w = [max(len(x) for x in c) for c in zip(cols, *t)]
            for r in [cols, ['-' * x for x in w]] + t:
                put('  '.join(x.ljust(n) for x, n in zip(r, w)).rstrip())
        elif m == 'csv':
            def q(v):
                v = s(v)
                return '"' + v.replace('"', '""') + '"' if re.search(r'[",\n\r\' ]', v) else v
            for r in ([cols] if f['header'] else []) + rows:
                out(','.join(map(q, r)) + '\r\n')
        else:
            sep = '\t' if m == 'tabs' else f['sep']
            for r in ([cols] if f['header'] else []) + rows:
                put(sep.join(map(s, r)))

    def dot(c):
        w = c.split()
        k, a = w[0], w[1:]
        if k in ('.quit', '.exit'):
            return True
        if k == '.tables':
            n = [r[0] for r in db.execute("select name from sqlite_master where type in ('table','view') and name not like 'sqlite_%' "
                                          "and name like ? order by 1", (a[0] if a else '%',))]
            if n:
                wd = max(map(len, n)) + 2
                cols = max(1, 80 // wd)
                rows = -(-len(n) // cols)
                for i in range(rows):
                    put(''.join(x.ljust(wd) for x in n[i::rows]).rstrip())
        elif k == '.schema':
            for r in db.execute("select sql from sqlite_master where sql is not null and name not like 'sqlite_%' and tbl_name like ? "
                                "order by rowid", (a[0] if a else '%',)):
                put(r[0] + ';')
        elif k in ('.headers', '.header'):
            f['header'] = a[:1] in (['on'], ['yes'], ['1'])
        elif k == '.mode' and a and a[0] in ('csv', 'json', 'line', 'column', 'list', 'tabs'):
            f['mode'] = a[0]
        elif k == '.separator' and a:
            f['sep'] = unescape(a[0].strip('"'))
        elif k == '.nullvalue' and a:
            f['null'] = a[0].strip('"')
        elif k in ('.indexes', '.indices'):
            for r in db.execute("select name from sqlite_master where type='index' order by 1"):
                put(r[0])
        elif k == '.databases':
            for r in db.execute('pragma database_list'):
                put(f'{r[1]}: {r[2] or chr(34) * 2} r/w')
        elif k == '.dump':
            put('PRAGMA foreign_keys=OFF;')
            for x in db.iterdump():
                put(x)
        elif k == '.read' and a:
            return run(text(a[0]))
        else:
            raise Fail(f'unsupported command {c}', 1)

    def stmt(q):
        try:
            show(db.execute(q))
        except (sqlite3.Error, sqlite3.Warning) as e:
            raise Fail(f'Error: {e}', 1) from None
    bad = 0

    def run(t):
        nonlocal bad
        buf = ''
        for ln in lines(t):
            if not buf.strip() and ln.lstrip().startswith('.'):
                if dot(ln.strip()):
                    return True
                continue
            # a line may hold several statements
            for part in re.split('(;)', ln):
                buf += part
                if part == ';' and sqlite3.complete_statement(buf):
                    try:
                        if buf.strip(' \t\n;'):
                            stmt(buf)
                    except Fail as e:
                        err(e)
                        bad = 1
                        if 'bail' in f:
                            raise
                    buf = ''
            buf += '\n'
        if re.sub(r'--[^\n]*|/\*.*?\*/', '', buf, flags=re.S).strip():
            stmt(buf)
    try:
        for c in cmds:
            run(c)
        if len(rest) > 1:
            for q in rest[1:]:
                if run(q):
                    break
        else:
            run(text('-'))
    finally:
        db.close()
    return bad

# ---- jq: every filter yields (path, value) pairs; path is None when nobody needs it ----

class JqError(Exception):
    def __init__(self, v):
        self.v = v
        super().__init__(v if isinstance(v, str) else repr(v))


class JqBreak(Exception):
    pass


class JqParser:
    TOK = re.compile(r'''(?P<ws>(?:\s|\#[^\n]*)+)|(?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|(?P<field>\.[A-Za-z_]\w*)
        |(?P<var>\$(?:__loc__|[A-Za-z_]\w*))|(?P<fmt>@[a-z0-9]+)|(?P<id>[A-Za-z_]\w*(?:::\w+)*)
        |(?P<op>\?//|//=|\|=|\+=|-=|\*=|/=|%=|==|!=|<=|>=|//|\.\.|[-+*/%=<>|,.:;()\[\]{}?])''', re.X)

    def __init__(self, s):
        self.t, e = self.lex(s, 0, False)
        self.p = 0

    def lex(self, s, i, nested):
        t, depth = [], 0
        while i < len(s):
            if s[i] == '"':
                parts, i = self.string(s, i + 1)
                t.append(('str', parts))
                continue
            m = self.TOK.match(s, i)
            if not m:
                raise Fail(f'syntax error at {s[i:i + 10]!r}', 3)
            if nested and m[0] == ')' and not depth:
                return t, i + 1
            depth += (m[0] == '(') - (m[0] == ')')
            i = m.end()
            if m.lastgroup != 'ws':
                t.append((m.lastgroup, m[0]))
        if nested:
            raise Fail('syntax error: unterminated string interpolation', 3)
        return t, i

    def string(self, s, i):
        parts, b = [], ''
        while True:
            if i >= len(s):
                raise Fail('syntax error: unterminated string', 3)
            c = s[i]
            if c == '"':
                return parts + [b], i + 1
            if c == '\\':
                x = s[i + 1:i + 2]
                if x == '(':
                    t, i = self.lex(s, i + 2, True)
                    parts += [b, t]
                    b = ''
                    continue
                if x == 'u':
                    b += chr(int(s[i + 2:i + 6], 16))
                    i += 6
                    if '\ud800' <= b[-1] <= '\udbff' and s[i:i + 2] == '\\u':
                        b = b[:-1] + chr(0x10000 + ((ord(b[-1]) - 0xd800) << 10) + int(s[i + 2:i + 6], 16) - 0xdc00)
                        i += 6
                    continue
                if x not in '"\\/bfnrt' or not x:
                    raise Fail('syntax error: bad escape in string', 3)
                b += {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}.get(x, x)
                i += 2
                continue
            b += c
            i += 1

    def peek(self, k=0):
        return self.t[self.p + k] if self.p + k < len(self.t) else ('end', '')

    def at(self, v, kind=('op', 'id')):
        t = self.peek()
        return t[0] in kind and t[1] == v

    def take(self, v):
        if self.at(v):
            self.p += 1
            return True
        return False

    def need(self, v):
        if not self.take(v):
            raise Fail(f'syntax error: expected {v}, found {self.peek()[1] or "end"}', 3)

    def program(self):
        if not self.t:
            return ('id',)
        e = self.pipe()
        if self.p < len(self.t):
            raise Fail(f'syntax error: unexpected {self.peek()[1]}', 3)
        return e

    def pipe(self):
        if self.take('def'):
            n, ps = self.peek()[1], []
            self.p += 1
            if self.take('('):
                while True:
                    ps.append(self.peek()[1])
                    self.p += 1
                    if not self.take(';'):
                        break
                self.need(')')
            self.need(':')
            b = self.pipe()
            self.need(';')
            return ('def', n, ps, b, self.pipe())
        if self.take('label'):
            n = self.peek()[1]
            self.p += 1
            self.need('|')
            return ('label', n, self.pipe())
        a = self.comma()
        if self.at('as'):
            self.p += 1
            pat = self.pattern()
            self.need('|')
            return ('as', a, pat, self.pipe())
        if self.take('|'):
            return ('pipe', a, self.pipe())
        return a

    def pattern(self):
        t = self.peek()
        self.p += 1
        if t[0] == 'var':
            return ('v', t[1])
        if t[1] == '[':
            o = [self.pattern()]
            while self.take(','):
                o.append(self.pattern())
            self.need(']')
            return ('a', o)
        if t[1] == '{':
            o = []
            while True:
                k = self.peek()
                self.p += 1
                if k[0] == 'var':
                    o.append((('lit', k[1][1:]), self.pattern() if self.take(':') else ('v', k[1])))
                else:
                    if k[1] == '(':
                        key = self.pipe()
                        self.need(')')
                    else:
                        key = self.strnode(k[1]) if k[0] == 'str' else ('lit', k[1])
                    self.need(':')
                    o.append((key, self.pattern()))
                if not self.take(','):
                    break
            self.need('}')
            return ('o', o)
        raise Fail('syntax error in pattern', 3)

    def comma(self):
        a = self.alt()
        while self.take(','):
            a = ('comma', a, self.alt())
        return a

    def alt(self):
        a = self.assign()
        if self.take('//'):
            return ('alt', a, self.alt())
        return a

    def assign(self):
        a = self.orx()
        t = self.peek()
        if t[0] == 'op' and t[1] in ('=', '|=', '+=', '-=', '*=', '/=', '%=', '//='):
            self.p += 1
            return ('assign', t[1], a, self.alt())
        return a

    def orx(self):
        a = self.andx()
        while self.take('or'):
            a = ('or', a, self.andx())
        return a

    def andx(self):
        a = self.cmpx()
        while self.take('and'):
            a = ('and', a, self.cmpx())
        return a

    def cmpx(self):
        a = self.add()
        t = self.peek()
        if t[0] == 'op' and t[1] in ('==', '!=', '<', '<=', '>', '>='):
            self.p += 1
            return ('bin', t[1], a, self.add())
        return a

    def add(self):
        a = self.mul()
        while self.at('+') or self.at('-'):
            op = self.peek()[1]
            self.p += 1
            a = ('bin', op, a, self.mul())
        return a

    def mul(self):
        a = self.unary()
        while self.at('*') or self.at('/') or self.at('%'):
            op = self.peek()[1]
            self.p += 1
            a = ('bin', op, a, self.unary())
        return a

    def unary(self):
        if self.take('-'):
            return ('bin', '-', ('lit', 0), self.unary())
        return self.postfix()

    def strnode(self, parts, fmt=None):
        if len(parts) == 1:
            return ('lit', parts[0])
        o = []
        for x in parts:
            if isinstance(x, str):
                o.append(x)
            else:
                q = JqParser.__new__(JqParser)
                q.t, q.p = x, 0
                o.append(q.program())
        return ('interp', o, fmt)

    def postfix(self):
        a = self.primary()
        while True:
            t = self.peek()
            if t[0] == 'field':
                self.p += 1
                a = ('index', a, ('lit', t[1][1:]))
            elif t == ('op', '.') and self.peek(1)[0] == 'str':
                a = ('index', a, self.strnode(self.peek(1)[1]))
                self.p += 2
            elif t == ('op', '.') and self.peek(1) == ('op', '['):
                self.p += 1
            elif t == ('op', '['):
                self.p += 1
                a = self.subscript(a)
            elif t == ('op', '?'):
                self.p += 1
                a = ('try', a, None)
            elif t == ('id', 'as') and self.peek(1)[0] in ('var', 'op') and a[0] != 'as':
                self.p += 1
                pat = self.pattern()
                self.need('|')
                return ('as', a, pat, self.pipe())
            else:
                return a

    def subscript(self, a):
        if self.take(']'):
            return ('iter', a)
        if self.take(':'):
            e = self.pipe()
            self.need(']')
            return ('slice', a, None, e)
        e = self.pipe()
        if self.take(':'):
            f = None if self.at(']') else self.pipe()
            self.need(']')
            return ('slice', a, e, f)
        self.need(']')
        return ('index', a, e)

    def primary(self):
        k, v = self.peek()
        self.p += 1
        if k == 'num':
            return ('lit', float(v) if re.search('[.eE]', v) else int(v))
        if k == 'str':
            return self.strnode(v)
        if k == 'fmt':
            if self.peek()[0] == 'str':
                self.p += 1
                return self.strnode(self.t[self.p - 1][1], v)
            return ('call', v, [])
        if k == 'field':
            return ('index', ('id',), ('lit', v[1:]))
        if k == 'var':
            return ('var', v)
        if k == 'op':
            if v == '.':
                if self.peek()[0] == 'str':
                    self.p += 1
                    return ('index', ('id',), self.strnode(self.t[self.p - 1][1]))
                return ('id',)
            if v == '..':
                return ('call', 'recurse', [])
            if v == '(':
                e = self.pipe()
                self.need(')')
                return e
            if v == '[':
                if self.take(']'):
                    return ('lit', [])
                e = self.pipe()
                self.need(']')
                return ('array', e)
            if v == '{':
                return self.object()
            if v == '-':
                return ('bin', '-', ('lit', 0), self.postfix())
        if k == 'id':
            if v == 'if':
                arms = []
                c = self.pipe()
                self.need('then')
                arms.append((c, self.pipe()))
                while self.take('elif'):
                    c = self.pipe()
                    self.need('then')
                    arms.append((c, self.pipe()))
                e = self.pipe() if self.take('else') else ('id',)
                self.need('end')
                return ('if', arms, e)
            if v == 'try':
                b = self.postnopipe()
                return ('try', b, self.postnopipe() if self.take('catch') else ('call', 'empty', []))
            if v in ('reduce', 'foreach'):
                src = self.postfix_noas()
                self.need('as')
                pat = self.pattern()
                self.need('(')
                a = [self.pipe()]
                while self.take(';'):
                    a.append(self.pipe())
                self.need(')')
                return (v, src, pat, a)
            if v == 'break':
                n = self.peek()[1]
                self.p += 1
                return ('break', n)
            if v in ('true', 'false', 'null'):
                return ('lit', {'true': True, 'false': False, 'null': None}[v])
            args = []
            if self.take('('):
                args.append(self.pipe())
                while self.take(';'):
                    args.append(self.pipe())
                self.need(')')
            return ('call', v, args)
        raise Fail(f'syntax error: unexpected {v or "end"}', 3)

    def postnopipe(self):
        return self.postfix()

    def postfix_noas(self):
        # like postfix, but `as` belongs to reduce/foreach
        a = self.primary()
        while True:
            t = self.peek()
            if t[0] == 'field':
                self.p += 1
                a = ('index', a, ('lit', t[1][1:]))
            elif t == ('op', '['):
                self.p += 1
                a = self.subscript(a)
            elif t == ('op', '?'):
                self.p += 1
                a = ('try', a, None)
            else:
                return a

    def object(self):
        o = []
        while not self.take('}'):
            k, v = self.peek()
            self.p += 1
            dflt = None
            if k == 'var':
                key, dflt = ('lit', v[1:]), ('var', v)
            elif k == 'str' or k == 'fmt':
                if k == 'fmt':
                    self.p += 1
                    v, key = None, self.strnode(self.t[self.p - 1][1], v)
                else:
                    key = self.strnode(v)
                dflt = ('index', ('id',), key)
            elif k == 'op' and v == '(':
                key = self.pipe()
                self.need(')')
            elif k == 'id' or k == 'num':
                key = ('lit', v)
                dflt = ('index', ('id',), key)
            else:
                raise Fail(f'syntax error in object: {v}', 3)
            if self.take(':'):
                val = self.objval()
            elif dflt is None:
                raise Fail('syntax error: object value missing', 3)
            else:
                val = dflt
            o.append((key, val))
            if not self.at('}'):
                self.need(',')
        return ('object', o)

    def objval(self):
        a = self.alt_nocomma()
        while self.take('|'):
            a = ('pipe', a, self.alt_nocomma())
        return a

    def alt_nocomma(self):
        return self.alt()


def jtype(v):
    return 'null' if v is None else 'boolean' if isinstance(v, bool) else 'number' if isinstance(v, (int, float)) else \
        'string' if isinstance(v, str) else 'array' if isinstance(v, list) else 'object'


def jkey(v):
    """A sort key that orders any JSON values the way jq does."""
    t = jtype(v)
    if t == 'array':
        return (5, [jkey(x) for x in v])
    if t == 'object':
        ks = sorted(v)
        return (6, ks, [jkey(v[k]) for k in ks])
    return {'null': (0, 0), 'boolean': (1 + bool(v), 0), 'number': (3, v), 'string': (4, v)}[t]


def jnum(v):
    return int(v) if isinstance(v, float) and v.is_integer() and abs(v) < 1e17 else v


def jdump(v, indent=None, sort=False, asc=False, tab=False):
    import json

    def fix(x):
        if isinstance(x, float):
            if x != x:
                return None
            if x in (float('inf'), float('-inf')):
                return 1.7976931348623157e308 * (1 if x > 0 else -1)
            return jnum(x)
        if isinstance(x, list):
            return [fix(y) for y in x]
        if isinstance(x, dict):
            return {k: fix(y) for k, y in x.items()}
        return x
    return json.dumps(fix(v), ensure_ascii=asc, sort_keys=sort, indent='\t' if tab else indent or None,
                      separators=(',', ':') if not indent and not tab else (',', ': '))


class Jq:
    def __init__(self, inputs, named):
        self.inputs, self.named = inputs, named

    def vals(self, n, v, env):
        for _, x in self.ev(n, None, v, env):
            yield x

    def child(self, p, v, k):
        """(path, value) for v[k]."""
        t = jtype(v)
        np = None if p is None else p + (k,) if not isinstance(k, dict) else p + (('slice', k.get('start'), k.get('end')),)
        if isinstance(k, dict) and t in ('array', 'null', 'string'):
            return self.slice(p, v, k.get('start'), k.get('end'))
        if t == 'object' and isinstance(k, str):
            return np, v.get(k)
        if t == 'array' and jtype(k) == 'number':
            i = int(k // 1)
            return np, v[i] if -len(v) <= i < len(v) else None
        if t == 'null' and jtype(k) in ('string', 'number', 'null'):
            return np, None
        if t == 'array' and isinstance(k, list):
            return None, [i for i in range(len(v) - len(k) + 1) if k and v[i:i + len(k)] == k]
        if isinstance(k, str):
            raise JqError(f'Cannot index {t} with "{k}"')
        raise JqError(f'Cannot index {t} with {jtype(k)}')

    def slice(self, p, v, a, b):
        np = None if p is None else p + (('slice', a, b),)
        if v is None:
            return np, None
        if not isinstance(v, (list, str)):
            raise JqError(f'Cannot index {jtype(v)} with object')
        import math
        a = None if a is None else math.floor(a)
        b = None if b is None else math.ceil(b)
        return np, v[a:b]

    def iterate(self, p, v):
        if isinstance(v, list):
            for i, x in enumerate(v):
                yield (None if p is None else p + (i,)), x
        elif isinstance(v, dict):
            for k, x in v.items():
                yield (None if p is None else p + (k,)), x
        else:
            raise JqError(f'Cannot iterate over {jtype(v)}' + ('' if v is None else f' ({jdump(v)[:11]})'))

    def ev(self, n, p, v, env):
        return getattr(self, 'n_' + n[0])(n, p, v, env)

    def n_id(self, n, p, v, env):
        yield p, v

    def n_lit(self, n, p, v, env):
        yield None, n[1]

    def n_var(self, n, p, v, env):
        if n[1] in env[0]:
            yield None, env[0][n[1]]
        elif n[1] == '$ENV':
            yield None, dict(os.environ)
        elif n[1] == '$__loc__':
            yield None, {'file': '<top-level>', 'line': 1}
        else:
            raise Fail(f'{n[1]} is not defined', 3)

    def n_pipe(self, n, p, v, env):
        for q, x in self.ev(n[1], p, v, env):
            yield from self.ev(n[2], q if p is not None else None, x, env)

    def n_comma(self, n, p, v, env):
        yield from self.ev(n[1], p, v, env)
        yield from self.ev(n[2], p, v, env)

    def n_index(self, n, p, v, env):
        for q, x in self.ev(n[1], p, v, env):
            for k in self.vals(n[2], v, env):
                yield self.child(q, x, k)

    def n_slice(self, n, p, v, env):
        for q, x in self.ev(n[1], p, v, env):
            for b in self.vals(n[3], v, env) if n[3] else (None,):
                for a in self.vals(n[2], v, env) if n[2] else (None,):
                    yield self.slice(q, x, a, b)

    def n_iter(self, n, p, v, env):
        for q, x in self.ev(n[1], p, v, env):
            yield from self.iterate(q, x)

    def n_try(self, n, p, v, env):
        try:
            yield from self.ev(n[1], p, v, env)
        except JqError as e:
            if n[2]:
                yield from self.ev(n[2], None, e.v, env)

    def n_array(self, n, p, v, env):
        yield None, list(self.vals(n[1], v, env))

    def n_object(self, n, p, v, env):
        def go(i, o):
            if i == len(n[1]):
                yield None, dict(o)
                return
            for k in self.vals(n[1][i][0], v, env):
                if not isinstance(k, str):
                    raise JqError('Object keys must be strings')
                for x in self.vals(n[1][i][1], v, env):
                    yield from go(i + 1, {**o, k: x})
        yield from go(0, {})

    def n_interp(self, n, p, v, env):
        def go(i, s):
            if i == len(n[1]):
                yield None, s
                return
            x = n[1][i]
            if isinstance(x, str):
                yield from go(i + 1, s + x)
            else:
                for y in self.vals(x, v, env):
                    yield from go(i + 1, s + (self.format(n[2], y) if n[2] else self.tostring(y)))
        yield from go(0, '')

    def n_if(self, n, p, v, env):
        def go(i):
            if i == len(n[1]):
                yield from self.ev(n[2], p, v, env)
                return
            for c in self.vals(n[1][i][0], v, env):
                if c is not None and c is not False:
                    yield from self.ev(n[1][i][1], p, v, env)
                else:
                    yield from go(i + 1)
        yield from go(0)

    def n_alt(self, n, p, v, env):
        hit = False
        try:
            for q, x in self.ev(n[1], p, v, env):
                if x is not None and x is not False:
                    hit = True
                    yield q, x
        except JqError:
            pass
        if not hit:
            yield from self.ev(n[2], p, v, env)

    def n_and(self, n, p, v, env):
        for a in self.vals(n[1], v, env):
            if a is None or a is False:
                yield None, False
            else:
                for b in self.vals(n[2], v, env):
                    yield None, b is not None and b is not False

    def n_or(self, n, p, v, env):
        for a in self.vals(n[1], v, env):
            if a is not None and a is not False:
                yield None, True
            else:
                for b in self.vals(n[2], v, env):
                    yield None, b is not None and b is not False

    def n_bin(self, n, p, v, env):
        for b in self.vals(n[3], v, env):
            for a in self.vals(n[2], v, env):
                yield None, self.op(n[1], a, b)

    def op(self, o, a, b):
        ta, tb = jtype(a), jtype(b)
        if o == '==':
            return jkey(a) == jkey(b)
        if o == '!=':
            return jkey(a) != jkey(b)
        if o in ('<', '<=', '>', '>='):
            x, y = jkey(a), jkey(b)
            return x < y if o == '<' else x <= y if o == '<=' else x > y if o == '>' else x >= y
        both = ta == tb == 'number'
        if o == '+':
            if a is None or b is None:
                return b if a is None else a
            if both or ta == tb == 'string' or ta == tb == 'array':
                return a + b
            if ta == tb == 'object':
                return {**a, **b}
        elif o == '-':
            if both:
                return a - b
            if ta == tb == 'array':
                ks = [jkey(x) for x in b]
                return [x for x in a if jkey(x) not in ks]
        elif o == '*':
            if both:
                return a * b
            if ta == 'number' and tb == 'string':
                a, b, ta, tb = b, a, tb, ta
            if ta == 'string' and tb == 'number':
                return a * max(1, int(b)) if b > 0 else None
            if ta == tb == 'object':
                def merge(x, y):
                    r = dict(x)
                    for k, z in y.items():
                        r[k] = merge(r[k], z) if isinstance(z, dict) and isinstance(r.get(k), dict) else z
                    return r
                return merge(a, b)
        elif o == '/':
            if both:
                if b == 0:
                    raise JqError(f'number ({jdump(a)}) and number (0) cannot be divided because the divisor is zero')
                return a // b if isinstance(a, int) and isinstance(b, int) and a % b == 0 else a / b
            if ta == tb == 'string':
                return (a.split(b) if b else list(a)) if a else []
        elif o == '%' and both:
            a, b = int(a), int(b)
            if b == 0:
                raise JqError(f'number ({a}) and number (0) cannot be divided because the divisor is zero')
            return abs(a) % abs(b) * (-1 if a < 0 else 1)
        w = {'+': 'added', '-': 'subtracted', '*': 'multiplied', '/': 'divided', '%': 'divided'}[o]
        raise JqError(f'{ta} ({jdump(a)[:11]}) and {tb} ({jdump(b)[:11]}) cannot be {w}')

    def bind(self, pat, v, env):
        if pat[0] == 'v':
            yield ({**env[0], pat[1]: v}, env[1])
            return

        def go(items, i, e):
            if i == len(items):
                yield e
                return
            k, q = items[i]
            for key in self.vals(k, v, e):
                for e2 in self.bind(q, self.child(None, v, key)[1], e):
                    yield from go(items, i + 1, e2)
        yield from go([(('lit', i), q) for i, q in enumerate(pat[1])] if pat[0] == 'a' else pat[1], 0, env)

    def n_as(self, n, p, v, env):
        for x in self.vals(n[1], v, env):
            for e in self.bind(n[2], x, env):
                yield from self.ev(n[3], p, v, e)

    def n_reduce(self, n, p, v, env):
        for acc in self.vals(n[3][0], v, env):
            for x in self.vals(n[1], v, env):
                for e in self.bind(n[2], x, env):
                    last = None
                    for last in self.vals(n[3][1], acc, e):
                        pass
                    acc = last
            yield None, acc

    def n_foreach(self, n, p, v, env):
        for acc in self.vals(n[3][0], v, env):
            for x in self.vals(n[1], v, env):
                for e in self.bind(n[2], x, env):
                    for acc in self.vals(n[3][1], acc, e):
                        if len(n[3]) > 2:
                            yield from ((None, y) for y in self.vals(n[3][2], acc, e))
                        else:
                            yield None, acc

    def n_def(self, n, p, v, env):
        fs = dict(env[1])
        e = (env[0], fs)
        fs[(n[1], len(n[2]))] = ('fn', n[2], n[3], e)
        yield from self.ev(n[4], p, v, e)

    def n_label(self, n, p, v, env):
        tag = JqBreak()
        try:
            yield from self.ev(n[2], p, v, ({**env[0], '*' + n[1]: tag}, env[1]))
        except JqBreak as b:
            if b is not tag:
                raise

    def n_break(self, n, p, v, env):
        raise env[0]['*' + n[1]]

    def n_assign(self, n, p, v, env):
        op = n[1]
        if op == '|=':
            r, dels = v, []
            for q in [q for q, _ in self.ev(n[2], (), v, env)]:
                new = next(self.vals(n[3], self.getp(r, q), env), self)
                if new is self:
                    dels.append(q)
                else:
                    r = self.setp(r, q, new)
            yield None, self.delps(r, dels)
            return
        for x in self.vals(n[3], v, env):
            r = v
            for q in [q for q, _ in self.ev(n[2], (), v, env)]:
                if q is None:
                    raise JqError('Invalid path expression')
                old = self.getp(r, q)
                if op == '=':
                    new = x
                elif op == '//=':
                    new = old if old is not None and old is not False else x
                else:
                    new = self.op(op[0], old, x)
                r = self.setp(r, q, new)
            yield None, r

    def getp(self, v, p):
        if p is None:
            raise JqError('Invalid path expression')
        for k in p:
            if v is None:
                return None
            v = self.slice(None, v, k[1], k[2])[1] if isinstance(k, tuple) else self.child(None, v, k)[1]
        return v

    def setp(self, v, p, x):
        if not p:
            return x
        k = p[0]
        if isinstance(k, tuple):
            v = v or []
            s = slice(k[1], k[2])
            new = self.setp(v[s], p[1:], x)
            if not isinstance(new, list):
                raise JqError('A slice of an array can only be assigned another array')
            r = list(v)
            r[s] = new
            return r
        if isinstance(k, str):
            if v is None:
                v = {}
            if not isinstance(v, dict):
                raise JqError(f'Cannot index {jtype(v)} with "{k}"')
            return {**v, k: self.setp(v.get(k), p[1:], x)}
        if v is None:
            v = []
        if not isinstance(v, list):
            raise JqError(f'Cannot index {jtype(v)} with number')
        k, r = int(k), list(v)
        if k < 0:
            k += len(r)
            if k < 0:
                raise JqError('Out of bounds negative array index')
        r += [None] * (k + 1 - len(r))
        r[k] = self.setp(r[k], p[1:], x)
        return r

    def delps(self, v, ps):
        def order(p):
            return [jkey(k if not isinstance(k, tuple) else k[1] or 0) for k in p]
        for p in sorted(ps, key=order, reverse=True):
            v = self.delp(v, p)
        return v

    def delp(self, v, p):
        if v is None or not p:
            return None
        k = p[0]
        if len(p) > 1:
            sub = self.getp(v, p[:1])
            return v if sub is None else self.setp(v, p[:1], self.delp(sub, p[1:]))
        if isinstance(k, tuple):
            r = list(v)
            del r[slice(k[1], k[2])]
            return r
        if isinstance(v, dict):
            if not isinstance(k, str):
                raise JqError('Cannot delete field at object index of array')
            return {a: b for a, b in v.items() if a != k}
        if isinstance(v, list) and jtype(k) == 'number':
            return v[:k] + v[k + 1:] if 0 <= k < len(v) else v[:k] + v[len(v) + k + 1:] if -len(v) <= k < 0 else v
        raise JqError(f'Cannot delete field at index of {jtype(v)}')

    def tostring(self, v):
        return v if isinstance(v, str) else jdump(v)

    def format(self, f, v):
        if f in ('@text', '@json'):
            return self.tostring(v) if f == '@text' else jdump(v)
        if f in ('@csv', '@tsv'):
            if not isinstance(v, list):
                raise JqError(f'{jtype(v)} ({jdump(v)[:11]}) cannot be {f[1:]}-formatted, only an array can be')
            o = []
            for x in v:
                if isinstance(x, (list, dict)):
                    raise JqError(f'{jtype(x)} ({jdump(x)[:11]}) is not valid in a csv row')
                if f == '@csv':
                    o.append('"' + x.replace('"', '""') + '"' if isinstance(x, str) else '' if x is None else jdump(x))
                else:
                    o.append(('' if x is None else self.tostring(x)).replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n')
                             .replace('\r', '\\r'))
            return (',' if f == '@csv' else '\t').join(o)
        s = self.tostring(v)
        if f == '@html':
            return ''.join({'<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&#39;', '"': '&quot;'}.get(c, c) for c in s)
        if f == '@uri':
            from urllib.parse import quote
            return quote(s, safe='~')
        if f == '@sh':
            q = [x if isinstance(x, str) else jdump(x) for x in (v if isinstance(v, list) else [v])]
            return ' '.join("'" + y.replace("'", "'\\''") + "'" if isinstance(x, str) else y
                            for x, y in zip(v if isinstance(v, list) else [v], q))
        if f in ('@base64', '@base64d'):
            import base64 as b64
            return b64.b64encode(enc(s)).decode() if f == '@base64' else b64.b64decode(s + '=' * (-len(s) % 4)).decode('utf-8', 'replace')
        raise Fail(f'{f} is not a valid format', 3)

    def n_call(self, n, p, v, env):
        name, args = n[1], n[2]
        d = env[1].get((name, len(args)))
        if d:
            if d[0] == 'clos':
                yield from self.ev(d[1], p, v, d[2])
                return
            _, params, body, e = d
            fs, vs = dict(e[1]), dict(e[0])

            def go(i):
                if i == len(params):
                    yield from self.ev(body, p, v, (vs, fs))
                    return
                q = params[i]
                if q.startswith('$'):
                    for x in self.vals(args[i], v, env):
                        vs[q] = x
                        fs[(q[1:], 0)] = ('clos', ('lit', x), env)
                        yield from go(i + 1)
                else:
                    fs[(q, 0)] = ('clos', args[i], env)
                    yield from go(i + 1)
            fs[(name, len(args))] = d
            yield from go(0)
            return
        if name.startswith('@'):
            yield None, self.format(name, v)
            return
        f = getattr(self, f'g_{name}_{len(args)}', None)
        if f:       # gets the unevaluated arguments and yields pairs
            yield from f(p, v, env, *args)
            return
        f = getattr(self, f'f_{name}', None)
        if f is None or f.__code__.co_argcount - 2 != len(args):
            if name in self.DEFS and len(args) == 0:
                yield from self.ev(self.defined(name), p, v, env)
                return
            raise Fail(f'{name}/{len(args)} is not defined', 3)

        def prod(i, acc):
            if i < 0:
                yield None, f(v, *acc)
                return
            for x in self.vals(args[i], v, env):
                yield from prod(i - 1, [x] + acc)
        yield from prod(len(args) - 1, [])

    DEFS = {'arrays': 'select(type=="array")', 'objects': 'select(type=="object")', 'strings': 'select(type=="string")',
            'numbers': 'select(type=="number")', 'booleans': 'select(type=="boolean")', 'nulls': 'select(type=="null")',
            'values': 'select(.!=null)', 'scalars': 'select(type|.!="array" and .!="object")',
            'iterables': 'select(type|.=="array" or .=="object")', 'todate': 'strftime("%Y-%m-%dT%H:%M:%SZ")',
            'fromdate': 'fromdateiso8601', 'fromdateiso8601': 'strptime("%Y-%m-%dT%H:%M:%SZ")|mktime'}
    CACHE = {}

    def defined(self, n):
        if n not in self.CACHE:
            self.CACHE[n] = JqParser(self.DEFS[n]).program()
        return self.CACHE[n]

    # functions with unevaluated arguments
    def g_empty_0(self, p, v, env):
        return
        yield

    def g_select_1(self, p, v, env, f):
        for c in self.vals(f, v, env):
            if c is not None and c is not False:
                yield p, v

    def g_map_1(self, p, v, env, f):
        yield None, [x for _, i in self.iterate(None, v) for x in self.vals(f, i, env)]

    def g_map_values_1(self, p, v, env, f):
        if isinstance(v, dict):
            r = {}
            for k, x in v.items():
                y = next(self.vals(f, x, env), self)
                if y is not self:
                    r[k] = y
            yield None, r
        else:
            yield None, [y for _, x in self.iterate(None, v) for y in [next(self.vals(f, x, env), self)] if y is not self]

    def g_recurse_0(self, p, v, env):
        yield p, v
        if isinstance(v, (list, dict)):
            for q, x in self.iterate(p, v):
                yield from self.g_recurse_0(q, x, env)

    def g_recurse_1(self, p, v, env, f):
        yield p, v
        for q, x in self.ev(f, p, v, env):
            yield from self.g_recurse_1(q, x, env, f)

    def g_recurse_2(self, p, v, env, f, c):
        yield p, v
        for q, x in self.ev(f, p, v, env):
            if any(y is not None and y is not False for y in self.vals(c, x, env)):
                yield from self.g_recurse_2(q, x, env, f, c)

    def g_path_1(self, p, v, env, f):
        for q, _ in self.ev(f, (), v, env):
            if q is None:
                raise JqError('Invalid path expression')
            yield None, [k if not isinstance(k, tuple) else {'start': k[1], 'end': k[2]} for k in q]

    def g_paths_0(self, p, v, env):
        for q, x in self.g_recurse_0((), v, env):
            if q:
                yield None, list(q)

    def g_paths_1(self, p, v, env, f):
        for q, x in self.g_recurse_0((), v, env):
            if q and any(y is not None and y is not False for y in self.vals(f, x, env)):
                yield None, list(q)

    def g_leaf_paths_0(self, p, v, env):
        for q, x in self.g_recurse_0((), v, env):
            if q and not isinstance(x, (list, dict)):
                yield None, list(q)

    def g_getpath_1(self, p, v, env, f):
        for q in self.vals(f, v, env):
            try:
                x = self.getp(v, q)
            except JqError:
                x = None
            yield (None if p is None else p + tuple(q)), x

    def g_del_1(self, p, v, env, f):
        yield None, self.delps(v, [q for q, _ in self.ev(f, (), v, env)])

    def g_to_entries_0(self, p, v, env):
        it = v.items() if isinstance(v, dict) else enumerate(v) if isinstance(v, list) else self.need(v, 'object')
        yield None, [{'key': k, 'value': x} for k, x in it]

    def g_with_entries_1(self, p, v, env, f):
        ent = [y for e in next(self.g_to_entries_0(None, v, env))[1] for y in self.vals(f, e, env)]
        yield None, self.f_from_entries(ent)

    def keyed(self, v, f, env):
        self.need(v, 'array')
        return sorted(((jkey(list(self.vals(f, x, env))), x) for x in v), key=lambda t: t[0])

    def g_sort_by_1(self, p, v, env, f):
        yield None, [x for _, x in self.keyed(v, f, env)]

    def groups(self, v, f, env):
        o, last = [], None
        for k, x in self.keyed(v, f, env):
            if o and k == last:
                o[-1].append(x)
            else:
                o.append([x])
            last = k
        return o

    def g_group_by_1(self, p, v, env, f):
        yield None, self.groups(v, f, env)

    def g_unique_by_1(self, p, v, env, f):
        yield None, [g[0] for g in self.groups(v, f, env)]

    def g_min_by_1(self, p, v, env, f):
        k = self.keyed(v, f, env)
        yield None, k[0][1] if k else None

    def g_max_by_1(self, p, v, env, f):
        k = self.keyed(v, f, env)
        yield None, k[-1][1] if k else None

    def g_any_0(self, p, v, env):
        yield None, any(x is not None and x is not False for _, x in self.iterate(None, v))

    def g_all_0(self, p, v, env):
        yield None, all(x is not None and x is not False for _, x in self.iterate(None, v))

    def g_any_1(self, p, v, env, f):
        yield None, any(y is not None and y is not False for _, x in self.iterate(None, v) for y in self.vals(f, x, env))

    def g_all_1(self, p, v, env, f):
        yield None, all(y is not None and y is not False for _, x in self.iterate(None, v) for y in self.vals(f, x, env))

    def g_any_2(self, p, v, env, g, f):
        yield None, any(y is not None and y is not False for x in self.vals(g, v, env) for y in self.vals(f, x, env))

    def g_all_2(self, p, v, env, g, f):
        yield None, all(y is not None and y is not False for x in self.vals(g, v, env) for y in self.vals(f, x, env))

    def g_first_1(self, p, v, env, f):
        for x in self.ev(f, p, v, env):
            yield x
            return

    def g_first_0(self, p, v, env):
        yield self.child(p, v, 0)

    def g_last_0(self, p, v, env):
        yield self.child(p, v, -1)

    def g_last_1(self, p, v, env, f):
        r = None
        for r in self.ev(f, p, v, env):
            pass
        if r:
            yield r

    def g_nth_1(self, p, v, env, f):
        for i in self.vals(f, v, env):
            yield self.child(p, v, i)

    def g_nth_2(self, p, v, env, i, f):
        for n in self.vals(i, v, env):
            for k, x in enumerate(self.ev(f, p, v, env)):
                if k == n:
                    yield x
                    break

    def g_limit_2(self, p, v, env, n, f):
        for k in self.vals(n, v, env):
            if k > 0:
                for i, x in enumerate(self.ev(f, p, v, env), 1):
                    yield x
                    if i >= k:
                        break

    def g_until_2(self, p, v, env, c, f):
        while next(self.vals(c, v, env)) in (None, False):
            v = next(self.vals(f, v, env))
        yield None, v

    def g_while_2(self, p, v, env, c, f):
        while next(self.vals(c, v, env)) not in (None, False):
            yield None, v
            v = next(self.vals(f, v, env))

    def g_repeat_1(self, p, v, env, f):
        while True:
            yield None, v
            v = next(self.vals(f, v, env))

    def g_isempty_1(self, p, v, env, f):
        yield None, next(self.ev(f, None, v, env), None) is None

    def g_error_0(self, p, v, env):
        raise JqError(v)
        yield

    def g_error_1(self, p, v, env, f):
        raise JqError(next(self.vals(f, v, env)))
        yield

    def g_range_1(self, p, v, env, f):
        for b in self.vals(f, v, env):
            yield from self.rng(0, b, 1)

    def g_range_2(self, p, v, env, a, b):
        for x in self.vals(a, v, env):
            for y in self.vals(b, v, env):
                yield from self.rng(x, y, 1)

    def g_range_3(self, p, v, env, a, b, c):
        for x in self.vals(a, v, env):
            for y in self.vals(b, v, env):
                for z in self.vals(c, v, env):
                    yield from self.rng(x, y, z)

    def rng(self, a, b, s):
        while (s > 0 and a < b) or (s < 0 and a > b):
            yield None, a
            a += s

    def g_input_0(self, p, v, env):
        x = next(self.inputs, self)
        if x is self:
            raise JqError('No more inputs')
        yield None, x

    def g_inputs_0(self, p, v, env):
        for x in self.inputs:
            yield None, x

    def g_env_0(self, p, v, env):
        yield None, dict(os.environ)

    def g_debug_0(self, p, v, env):
        State.out.flush()
        sys.stderr.write('["DEBUG:",' + jdump(v) + ']\n')
        yield p, v

    def g_scan_1(self, p, v, env, f):
        for r in self.vals(f, v, env):
            for m in self.re(r, '').finditer(v):
                yield None, list(m.groups()) if m.re.groups else m[0]

    def g_match_1(self, p, v, env, f):
        for r in self.vals(f, v, env):
            r, fl = (r[0], (r + [None])[1]) if isinstance(r, list) else (r, None)
            yield from self.matches(v, r, fl)

    def g_match_2(self, p, v, env, f, g):
        for r in self.vals(f, v, env):
            for fl in self.vals(g, v, env):
                yield from self.matches(v, r, fl)

    def matches(self, v, r, fl):
        c = self.re(r, fl)
        names = {i: n for n, i in c.groupindex.items()}
        for m in c.finditer(self.need(v, 'string')) if 'g' in (fl or '') else filter(None, [c.search(self.need(v, 'string'))]):
            yield None, {'offset': m.start(), 'length': m.end() - m.start(), 'string': m[0], 'captures': [
                {'offset': m.start(i), 'length': m.end(i) - m.start(i), 'string': m[i], 'name': names.get(i)} for i in range(1, c.groups + 1)]}

    def g_capture_1(self, p, v, env, f):
        for r in self.vals(f, v, env):
            m = self.re(r, '').search(v)
            if m:
                yield None, m.groupdict()

    def subst(self, v, env, f, rep, fl):
        for r in self.vals(f, v, env):
            o, at = [], 0
            c = self.re(r, fl)
            for m in c.finditer(self.need(v, 'string')) if 'g' in fl else filter(None, [c.search(v)]):
                o += [v[at:m.start()], next(self.vals(rep, m.groupdict(), env), '')]
                at = m.end()
            yield None, ''.join(o) + v[at:]

    def g_sub_2(self, p, v, env, f, rep):
        yield from self.subst(v, env, f, rep, '')

    def g_gsub_2(self, p, v, env, f, rep):
        yield from self.subst(v, env, f, rep, 'g')

    def g_sub_3(self, p, v, env, f, rep, fl):
        yield from self.subst(v, env, f, rep, next(self.vals(fl, v, env)))

    def g_gsub_3(self, p, v, env, f, rep, fl):
        yield from self.subst(v, env, f, rep, 'g' + next(self.vals(fl, v, env)))

    def g_add_1(self, p, v, env, f):
        yield None, self.f_add(list(self.vals(f, v, env)))

    def g_walk_1(self, p, v, env, f):
        def go(x):
            if isinstance(x, list):
                x = [z for y in x for z in go(y)]
            elif isinstance(x, dict):
                x = {k: z for k, y in x.items() for z in list(go(y))[:1]}
            yield from self.vals(f, x, env)
        for x in go(v):
            yield None, x

    def g_IN_1(self, p, v, env, f):
        yield None, any(jkey(x) == jkey(v) for x in self.vals(f, v, env))

    def g_INDEX_1(self, p, v, env, f):
        yield None, {self.tostring(next(self.vals(f, x, env))): x for _, x in self.iterate(None, v)}

    def g_join_1(self, p, v, env, f):
        for s in self.vals(f, v, env):
            o = []
            for _, x in self.iterate(None, v):
                if isinstance(x, (list, dict)):
                    raise JqError(f'string and {jtype(x)} cannot be added')
                o.append('' if x is None else self.tostring(x))
            yield None, s.join(o)

    def g_combinations_0(self, p, v, env):
        import itertools
        for x in itertools.product(*v):
            yield None, list(x)

    def g_halt_0(self, p, v, env):
        raise SystemExit(0)
        yield

    def g_split_1(self, p, v, env, f):
        for s in self.vals(f, v, env):
            yield None, self.op('/', self.need(v, 'string'), s)

    def g_split_2(self, p, v, env, f, g):
        for s in self.vals(f, v, env):
            for fl in self.vals(g, v, env):
                yield None, self.re(s, fl).split(v)

    def g_test_1(self, p, v, env, f):
        for r in self.vals(f, v, env):
            r, fl = (r[0], (r + [None])[1]) if isinstance(r, list) else (r, None)
            yield None, bool(self.re(r, fl).search(self.need(v, 'string')))

    def g_test_2(self, p, v, env, f, g):
        for r in self.vals(f, v, env):
            for fl in self.vals(g, v, env):
                yield None, bool(self.re(r, fl).search(self.need(v, 'string')))

    def g_flatten_0(self, p, v, env):
        yield None, self.flat(v, 1 << 30)

    def g_flatten_1(self, p, v, env, f):
        for d in self.vals(f, v, env):
            yield None, self.flat(v, d)

    def flat(self, v, d):
        o = []
        for _, x in self.iterate(None, v):
            o += self.flat(x, d - 1) if isinstance(x, list) and d > 0 else [x]
        return o

    def g_halt_error_0(self, p, v, env):
        State.out.flush()
        sys.stderr.write(v if isinstance(v, str) else jdump(v) + '\n')
        raise SystemExit(5)
        yield

    def re(self, r, fl):
        x = 0
        for c in fl or '':
            x |= {'i': re.I, 'x': re.X, 's': re.S, 'g': 0, 'n': 0, 'l': 0}.get(c, 0)
        return re.compile(perl(self.need(r, 'string')), x)

    def need(self, v, t):
        if jtype(v) != t:
            raise JqError(f'{jtype(v)} ({jdump(v)[:11]}) is not a{"n" if t[0] in "ao" else ""} {t}' if t != 'string' else
                          f'{jtype(v)} ({jdump(v)[:11]}) cannot be matched, as it is not a string')
        return v

    # plain functions: value in, value out
    def f_not(self, v):
        return v is None or v is False

    def f_length(self, v):
        if isinstance(v, bool):
            raise JqError(f'boolean ({jdump(v)}) has no length')
        return 0 if v is None else abs(v) if isinstance(v, (int, float)) else len(v)

    def f_utf8bytelength(self, v):
        return len(enc(v))

    def f_keys(self, v):
        if isinstance(v, dict):
            return sorted(v)
        if isinstance(v, list):
            return list(range(len(v)))
        raise JqError(f'{jtype(v)} ({jdump(v)[:11]}) has no keys')

    def f_keys_unsorted(self, v):
        return list(v) if isinstance(v, dict) else self.f_keys(v)

    def f_has(self, v, k):
        if isinstance(v, dict) and isinstance(k, str):
            return k in v
        if isinstance(v, list) and jtype(k) == 'number':
            return 0 <= k < len(v)
        raise JqError(f'Cannot check whether {jtype(v)} has a {jtype(k)} key')

    def f_in(self, v, o):
        return self.f_has(o, v)

    def f_add(self, v):
        r = None
        xs = [x for _, x in self.iterate(None, v)]
        if xs and all(isinstance(x, str) for x in xs):
            return ''.join(xs)
        if xs and all(isinstance(x, list) for x in xs):
            return [y for x in xs for y in x]
        for x in xs:
            r = self.op('+', r, x)
        return r

    def f_type(self, v):
        return jtype(v)

    def f_from_entries(self, v):
        r = {}
        for e in v:
            k = next((e[x] for x in ('key', 'k', 'name', 'Name', 'Key', 'K') if e.get(x) is not None and e.get(x) is not False), None)
            if k is None:
                k = False if e.get('key') is False else None
            val = next((e[x] for x in ('value', 'v', 'Value', 'V') if x in e), None)
            r[k if isinstance(k, str) else 'null' if k is None else jdump(k)] = val
        return r

    def f_sort(self, v):
        return sorted(self.need(v, 'array'), key=jkey)

    def f_unique(self, v):
        o = []
        for x in self.f_sort(v):
            if not o or jkey(o[-1]) != jkey(x):
                o.append(x)
        return o

    def f_min(self, v):
        return min(v, key=jkey) if v else None

    def f_max(self, v):
        return max(reversed(v), key=jkey) if v else None

    def f_reverse(self, v):
        return [] if v is None else v[::-1]

    def f_tostring(self, v):
        return self.tostring(v)

    def f_tonumber(self, v):
        if jtype(v) == 'number':
            return v
        try:
            return int(v) if re.fullmatch(r'-?\d+', v) else float(v)
        except (ValueError, TypeError):
            raise JqError(f'{jtype(v)} ({jdump(v)[:11]}) cannot be parsed as a number') from None

    def f_tojson(self, v):
        return jdump(v)

    def f_fromjson(self, v):
        import json
        try:
            return json.loads(v)
        except ValueError as e:
            raise JqError(f'{e} (while parsing {v!r})') from None

    def f_ascii_downcase(self, v):
        return re.sub('[A-Z]+', lambda m: m[0].lower(), self.need(v, 'string'))

    def f_ascii_upcase(self, v):
        return re.sub('[a-z]+', lambda m: m[0].upper(), self.need(v, 'string'))

    def f_startswith(self, v, s):
        return self.need(v, 'string').startswith(s)

    def f_endswith(self, v, s):
        return self.need(v, 'string').endswith(s)

    def f_ltrimstr(self, v, s):
        return v.removeprefix(s) if isinstance(v, str) and isinstance(s, str) else v

    def f_rtrimstr(self, v, s):
        return v.removesuffix(s) if isinstance(v, str) and isinstance(s, str) else v

    def f_trim(self, v):
        return self.need(v, 'string').strip()

    def f_ltrim(self, v):
        return self.need(v, 'string').lstrip()

    def f_rtrim(self, v):
        return self.need(v, 'string').rstrip()

    def f_explode(self, v):
        return [ord(c) for c in v]

    def f_implode(self, v):
        return ''.join(map(chr, v))

    def f_contains(self, v, b):
        if isinstance(v, dict) and isinstance(b, dict):
            return all(k in v and self.f_contains(v[k], x) for k, x in b.items())
        if isinstance(v, list) and isinstance(b, list):
            return all(any(jtype(y) == jtype(x) and self.f_contains(y, x) for y in v) for x in b)
        if isinstance(v, str) and isinstance(b, str):
            return b in v
        if jtype(v) == jtype(b):
            return v == b
        raise JqError(f'{jtype(v)} ({jdump(v)[:11]}) and {jtype(b)} ({jdump(b)[:11]}) cannot have their containment checked')

    def f_inside(self, v, b):
        return self.f_contains(b, v)

    def f_indices(self, v, x):
        if v is None:
            return None
        if isinstance(v, str):
            return [i for i in range(len(v)) if x and v.startswith(x, i)]
        x = x if isinstance(x, list) else [x]
        return [i for i in range(len(v) - len(x) + 1) if x and v[i:i + len(x)] == x]

    def f_index(self, v, x):
        return next(iter(self.f_indices(v, x) or []), None)

    def f_rindex(self, v, x):
        return next(reversed(self.f_indices(v, x) or []), None)

    def f_floor(self, v):
        import math
        return math.floor(v)

    def f_ceil(self, v):
        import math
        return math.ceil(v)

    def f_round(self, v):
        import math
        return math.floor(abs(v) + 0.5) * (1 if v >= 0 else -1)

    def f_fabs(self, v):
        return abs(v)

    def f_abs(self, v):
        return abs(v)

    def f_sqrt(self, v):
        return v ** 0.5

    def f_pow(self, v, a, b):
        return a ** b

    def f_log(self, v):
        import math
        return math.log(v)

    def f_infinite(self, v):
        return float('inf')

    def f_nan(self, v):
        return float('nan')

    def f_transpose(self, v):
        n = max(map(len, v), default=0)
        return [[r[i] if i < len(r) else None for r in v] for i in range(n)]

    def f_setpath(self, v, p, x):
        return self.setp(v, p, x)

    def f_delpaths(self, v, ps):
        return self.delps(v, ps)

    def f_now(self, v):
        import time
        return time.time()

    def f_strftime(self, v, f):
        import time
        if isinstance(v, list):
            import calendar
            v = calendar.timegm((v[0], v[1] + 1, *[int(x) for x in v[2:6]], 0, 0, 0))
        return time.strftime(f, time.gmtime(v))

    def f_gmtime(self, v):
        import time
        t = time.gmtime(v)
        return [t[0], t[1] - 1, t[2], t[3], t[4], t[5] + v % 1, (t[6] + 1) % 7, t[7] - 1]

    def f_mktime(self, v):
        import calendar
        return calendar.timegm((v[0], v[1] + 1, *[int(x) for x in v[2:6]], 0, 0, 0))

    def f_strptime(self, v, f):
        import time
        try:
            t = time.strptime(v, f)
        except ValueError as e:
            raise JqError(str(e)) from None
        return [t[0], t[1] - 1, t[2], t[3], t[4], t[5], (t[6] + 1) % 7, t[7] - 1]

    def f_splits(self, v, r):
        return self.re(r, '').split(v)

    def f_ascii(self, v):
        return chr(v)

    def f_input_line_number(self, v):
        return 0


@command('jq')
def jq(name, args):
    import json
    f, pos, var, named, i, indent, rest_mode = set(), [], {}, {}, 0, 2, None
    extra = []

    def nxt(k=1):
        nonlocal i
        v = args[i:i + k]
        if len(v) < k:
            raise Fail('option needs more values')
        i += k
        return v

    def load(s):
        try:
            return json.loads(s)
        except ValueError as e:
            raise Fail(f'invalid JSON text: {e}') from None
    L = {'raw-output': 'r', 'compact-output': 'c', 'slurp': 's', 'null-input': 'n', 'exit-status': 'e', 'join-output': 'j', 'raw-input': 'R',
         'sort-keys': 'S', 'ascii-output': 'a', 'monochrome-output': 'M', 'color-output': 'C', 'from-file': 'f', 'tab': 't', 'unbuffered': 'u',
         'raw-output0': '0'}
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith('--') and len(a) > 2:
            k = a[2:]
            if k in ('arg', 'argjson', 'slurpfile', 'rawfile'):
                n, v = nxt(2)
                var['$' + n] = v if k == 'arg' else load(v) if k == 'argjson' else text(v) if k == 'rawfile' else list(jdocs(text(v)))
                named[n] = var['$' + n]
            elif k == 'indent':
                indent = integer(nxt()[0])
            elif k in ('args', 'jsonargs'):
                rest_mode = k
            elif k in L:
                f.add(L[k])
            else:
                raise Fail(f'unsupported option {a}')
        elif a == '--':
            pos += args[i:]
            break
        elif a.startswith('-') and len(a) > 1 and a[1] not in '.([' and not (rest_mode and pos):
            for c in a[1:]:
                if c not in 'rcsnejRSaMCfu':
                    raise Fail(f'unsupported option -{c}')
                f.add(c)
        elif rest_mode and pos:
            extra.append(a if rest_mode == 'args' else load(a))
        else:
            pos.append(a)
    if not pos:
        raise Fail('usage: jq [options] filter [file...]')
    if 'C' in f:
        raise Fail('unsupported option -C')
    src = text(pos[0]) if 'f' in f else pos[0]
    prog = JqParser(src).program()
    files = pos[1:]
    var['$named'] = named
    var['$__prog_args'] = extra
    var['$ARGS'] = {'positional': extra, 'named': named}
    state = {'bad': 0}

    def inputs():
        bad = []
        whole = ''.join(t for _, t in texts(files, bad)) if files else None
        if bad:
            state['bad'] = 2
        t = whole if whole is not None else text()
        if 'R' in f:
            if 's' in f:
                yield t
            else:
                yield from lines(t)
            return
        if 's' in f:
            yield list(jdocs(t))
        else:
            yield from jdocs(t)
    it = inputs()
    q = Jq(it, named)
    sys.setrecursionlimit(20000)
    last = q
    rc = 0
    while True:
        try:
            if 'n' in f:
                v = None
            else:
                v = next(it, q)
                if v is q:
                    break
            for r in q.vals(prog, v, (var, {})):
                last = r
                if ('r' in f or 'j' in f or '0' in f) and isinstance(r, str):
                    s = r
                else:
                    s = jdump(r, None if 'c' in f else indent, 'S' in f, 'a' in f, 't' in f)
                out(s + ('' if 'j' in f else '\0' if '0' in f else '\n'))
        except JqError as e:
            err('error: ' + (e.v if isinstance(e.v, str) else jdump(e.v) + ' (not a string)'))
            rc = 5
        except JqBreak:
            rc = 5
        except (TypeError, AttributeError, KeyError, OverflowError) as e:
            err(f'error: wrong type of value ({e})')
            rc = 5
        if 'n' in f:
            break
    if rc or state['bad']:
        return rc or state['bad']
    if 'e' in f:
        return 4 if last is q else 1 if last in (None, False) and not isinstance(last, (int, float)) or last is False else 0
    return 0


def jdocs(t):
    import json
    d, i, n = json.JSONDecoder(strict=False), 0, len(t)
    ws = re.compile(r'[\s\ufeff]*')
    while True:
        i = ws.match(t, i).end()
        if i >= n:
            return
        try:
            v, i = d.raw_decode(t, i)
        except ValueError as e:
            raise Fail(f'parse error: {e}') from None
        yield v


# ---- make ----

class MakeError(Exception):
    pass


class Rule:
    def __init__(self, loc):
        self.deps, self.order, self.recipe, self.loc, self.double = [], [], None, loc, False


class Make:
    INF = 1 << 80
    BUILTIN = '''
CC = cc
CXX = c++
AR = ar
AS = as
LD = ld
CPP = $(CC) -E
ARFLAGS = rv
RM = rm -f
LEX = lex
YACC = yacc
COMPILE.c = $(CC) $(CFLAGS) $(CPPFLAGS) $(TARGET_ARCH) -c
COMPILE.cc = $(CXX) $(CXXFLAGS) $(CPPFLAGS) $(TARGET_ARCH) -c
COMPILE.cpp = $(COMPILE.cc)
COMPILE.C = $(COMPILE.cc)
COMPILE.s = $(AS) $(ASFLAGS) $(TARGET_MACH)
COMPILE.S = $(CC) $(ASFLAGS) $(CPPFLAGS) $(TARGET_MACH) -c
LINK.o = $(CC) $(LDFLAGS) $(TARGET_ARCH)
LINK.c = $(CC) $(CFLAGS) $(CPPFLAGS) $(LDFLAGS) $(TARGET_ARCH)
LINK.cc = $(CXX) $(CXXFLAGS) $(CPPFLAGS) $(LDFLAGS) $(TARGET_ARCH)
OUTPUT_OPTION = -o $@
'''
    RULES = '''
%.o: %.c
	$(COMPILE.c) $(OUTPUT_OPTION) $<
%.o: %.cc
	$(COMPILE.cc) $(OUTPUT_OPTION) $<
%.o: %.cpp
	$(COMPILE.cpp) $(OUTPUT_OPTION) $<
%.o: %.C
	$(COMPILE.C) $(OUTPUT_OPTION) $<
%.o: %.s
	$(COMPILE.s) -o $@ $<
%.o: %.S
	$(COMPILE.S) -o $@ $<
%: %.o
	$(LINK.o) $^ $(LOADLIBES) $(LDLIBS) -o $@
%: %.c
	$(LINK.c) $^ $(LOADLIBES) $(LDLIBS) -o $@
%: %.cc
	$(LINK.cc) $^ $(LOADLIBES) $(LDLIBS) -o $@
%: %.cpp
	$(LINK.cc) $^ $(LOADLIBES) $(LDLIBS) -o $@
%: %.sh
	cat $< >$@
	chmod a+x $@
'''

    def __init__(self, flags, cmdvars, incdirs):
        self.fl, self.incdirs = flags, incdirs
        self.vars, self.rules, self.patterns, self.phony, self.default = {}, {}, [], set(), None
        self.exports, self.unexport, self.tvars, self.ptvars, self.flags, self.vpath = set(), set(), {}, [], {}, []
        self.scope, self.auto, self.files, self.missing, self.done, self.ran, self.failed = {}, {}, [], [], {}, False, set()
        self.where, self.active, self.deps_of, self.export_all, self.cur, self.second = '', [], {}, False, None, False
        self.readstack = []
        for k, v in os.environ.items():
            if k not in ('MAKEFLAGS', 'SHELL', 'MAKELEVEL', 'CURDIR'):
                self.vars[k] = [':', v, 'environment']
        self.env_names = set(self.vars)
        for ln in lines(self.BUILTIN):
            if ln:
                k, _, v = ln.partition(' = ')
                if k not in self.vars:
                    self.vars[k] = ['=', v, 'default']
        self.vars['MAKE'] = [':', 'make', 'default']
        self.vars['SHELL'] = [':', '/bin/sh', 'default']
        self.vars['.SHELLFLAGS'] = [':', '-c', 'default']
        self.vars['CURDIR'] = [':', os.getcwd(), 'file']
        self.vars['MAKE_VERSION'] = [':', '3.81', 'default']
        self.vars['MAKELEVEL'] = [':', os.environ.get('MAKELEVEL', '0'), 'environment']
        self.vars['MAKEFILE_LIST'] = [':', '', 'file']
        self.vars['.DEFAULT_GOAL'] = [':', '', 'file']
        for k, op, v in cmdvars:
            self.vars[k] = [':' if op == ':=' else '=', self.expand(v) if op == ':=' else v, 'command line']

    # variables
    def get(self, n):
        if n in self.auto:
            return self.auto[n]
        if len(n) == 2 and n[1] in 'DF' and n[0] in self.auto:
            o = []
            for x in self.auto[n[0]].split():
                d, b = os.path.split(x)
                o.append((d or '.') if n[1] == 'D' else b)
            return ' '.join(o)
        v = self.scope.get(n)
        if v is None or (n in self.vars and self.vars[n][2] == 'command line' and v[2] != 'override'):
            v = self.vars.get(n, v)
        if v is None:
            return ''
        if v[0] == ':':
            return v[1]
        if n in self.active:
            raise MakeError(f"Recursive variable '{n}' references itself (eventually).  Stop.")
        self.active.append(n)
        try:
            return self.expand(v[1])
        finally:
            self.active.pop()

    def set(self, n, op, v, origin='file', scope=None):
        d = self.vars if scope is None else scope
        old = d.get(n) or (self.vars.get(n) if scope is not None else None)
        if self.vars.get(n, (0, 0, ''))[2] in ('command line',) and origin not in ('override', 'command line'):
            return
        if old and old[2] == 'override' and origin == 'file':
            return
        if old and old[2] == 'environment' and 'e' in self.fl and origin == 'file':
            return
        if op == '?=':
            if old:
                return
            op = '='
        if op == '+=':
            if not old or (old[2] == 'default' and old[1] == ''):
                op = '='
            else:
                base = old[1]
                d[n] = [old[0], base + (' ' if base else '') + (self.expand(v) if old[0] == ':' else v), origin]
                return
        if op == '!=':
            d[n] = [':', self.sh(self.expand(v)), origin]
        elif op == '=':
            d[n] = ['=', v, origin]
        else:
            d[n] = [':', self.expand(v), origin]

    def expand(self, s):
        if '$' not in s:
            return s
        o, i, n = [], 0, len(s)
        while True:
            j = s.find('$', i)
            if j < 0 or j + 1 >= n:
                o.append(s[i:] if j < 0 else s[i:j])
                return ''.join(o)
            o.append(s[i:j])
            c = s[j + 1]
            if c == '$':
                o.append('$')
                i = j + 2
            elif c in '({':
                e = self.close(s, j + 1)
                o.append(self.ref(s[j + 2:e]))
                i = e + 1
            else:
                o.append(self.get(c))
                i = j + 2

    def close(self, s, i):
        a, b, d = s[i], {'(': ')', '{': '}'}[s[i]], 0
        for k in range(i, len(s)):
            if s[k] == a:
                d += 1
            elif s[k] == b:
                d -= 1
                if not d:
                    return k
        raise MakeError(f'{self.where}: *** unterminated variable reference.  Stop.')

    def split(self, s, limit=None):
        """Split function arguments at commas outside of nested parentheses."""
        o, d, p = [], [], 0
        for k, c in enumerate(s):
            if c in '({':
                d.append(c)
            elif c in ')}' and d and {'(': ')', '{': '}'}[d[-1]] == c:
                d.pop()
            elif c == ',' and not d and (limit is None or len(o) < limit - 1):
                o.append(s[p:k])
                p = k + 1
        return o + [s[p:]]

    def ref(self, s):
        m = re.match(r'([a-z][a-z-]*)(?:[ \t]+|$)', s)
        if m and hasattr(self, 'fn_' + m[1].replace('-', '_')) and (len(s) > m.end(1)):
            return getattr(self, 'fn_' + m[1].replace('-', '_'))(s[m.end():])
        # substitution reference: name:a=b, with the colon outside of nested references
        d = 0
        for k, c in enumerate(s):
            if c in '({':
                d += 1
            elif c in ')}':
                d -= 1
            elif c == ':' and not d and '=' in s[k:]:
                a, _, b = s[k + 1:].partition('=')
                a, b, name_ = self.expand(a), self.expand(b), self.expand(s[:k])
                if '%' not in a:
                    a, b = '%' + a, '%' + b
                return self.patsubst(a, b, self.get(name_))
        return self.get(self.expand(s))

    def patsubst(self, a, b, t):
        pre, pc, suf = a.partition('%')
        o = []
        for w in t.split():
            if not pc:
                o.append(b if w == a else w)
            elif w.startswith(pre) and w.endswith(suf) and len(w) >= len(pre) + len(suf):
                o.append(b.replace('%', w[len(pre):len(w) - len(suf)], 1))
            else:
                o.append(w)
        return ' '.join(o)

    def match(self, pats, w):
        for p in pats:
            pre, pc, suf = p.partition('%')
            if (w == p) if not pc else w.startswith(pre) and w.endswith(suf) and len(w) >= len(pre) + len(suf):
                return True
        return False

    def args(self, s, n):
        a = [self.expand(x) for x in self.split(s, n)]
        if len(a) < n:
            raise MakeError(f'{self.where}: *** insufficient number of arguments to function.  Stop.')
        return a

    def fn_subst(self, s):
        a, b, t = self.args(s, 3)
        return t.replace(a, b) if a else t + b

    def fn_patsubst(self, s):
        a, b, t = self.args(s, 3)
        return self.patsubst(a.strip(), b.strip(), t)

    def fn_strip(self, s):
        return ' '.join(self.expand(s).split())

    def fn_findstring(self, s):
        a, t = self.args(s, 2)
        return a if a in t else ''

    def fn_filter(self, s):
        a, t = self.args(s, 2)
        return ' '.join(w for w in t.split() if self.match(a.split(), w))

    def fn_filter_out(self, s):
        a, t = self.args(s, 2)
        return ' '.join(w for w in t.split() if not self.match(a.split(), w))

    def fn_sort(self, s):
        return ' '.join(sorted(set(self.expand(s).split())))

    def fn_word(self, s):
        n, t = self.args(s, 2)
        t, n = t.split(), int(n)
        return t[n - 1] if 0 < n <= len(t) else ''

    def fn_wordlist(self, s):
        a, b, t = self.args(s, 3)
        return ' '.join(t.split()[int(a) - 1:int(b)])

    def fn_words(self, s):
        return str(len(self.expand(s).split()))

    def fn_firstword(self, s):
        return ' '.join(self.expand(s).split()[:1])

    def fn_lastword(self, s):
        return ' '.join(self.expand(s).split()[-1:])

    def each(self, s, f):
        return ' '.join(x for x in map(f, self.expand(s).split()) if x is not None)

    def fn_dir(self, s):
        return self.each(s, lambda w: w[:w.rfind('/') + 1] or './')

    def fn_notdir(self, s):
        return self.each(s, lambda w: w[w.rfind('/') + 1:])

    def fn_suffix(self, s):
        return self.each(s, lambda w: (lambda b: b[b.rfind('.'):] if '.' in b else None)(w[w.rfind('/') + 1:]))

    def fn_basename(self, s):
        return self.each(s, lambda w: w[:w.rfind('.')] if '.' in w[w.rfind('/') + 1:] else w)

    def fn_addprefix(self, s):
        a, t = self.args(s, 2)
        return ' '.join(a + w for w in t.split())

    def fn_addsuffix(self, s):
        a, t = self.args(s, 2)
        return ' '.join(w + a for w in t.split())

    def fn_join(self, s):
        import itertools
        a, b = self.args(s, 2)
        return ' '.join(x + y for x, y in itertools.zip_longest(a.split(), b.split(), fillvalue=''))

    def fn_wildcard(self, s):
        return ' '.join(x for p in self.expand(s).split() for x in self.glob(p))

    def glob(self, p):
        import glob
        if not any(c in p for c in '*?['):
            return [p] if os.path.lexists(p) else []
        return sorted(glob.glob(p))

    def fn_realpath(self, s):
        return self.each(s, lambda w: os.path.realpath(w) if os.path.exists(w) else None)

    def fn_abspath(self, s):
        return self.each(s, os.path.abspath)

    def fn_foreach(self, s):
        v, l, t = self.split(s, 3)
        v, o, saved = self.expand(v).strip(), [], dict(self.scope)
        for w in self.expand(l).split():
            self.scope[v] = [':', w, 'override']
            o.append(self.expand(t))
        self.scope = saved
        return ' '.join(o)

    def fn_call(self, s):
        a = [self.expand(x) for x in self.split(s)]
        n, saved = a[0].strip(), self.auto
        if hasattr(self, 'fn_' + n.replace('-', '_')) and n not in self.vars:
            return getattr(self, 'fn_' + n.replace('-', '_'))(','.join(x.replace('$', '$$') for x in a[1:]))
        self.auto = {**saved, **{str(i): '' for i in range(1, 10)}, '0': n, **{str(i): x for i, x in enumerate(a[1:], 1)}}
        act, self.active = self.active, [x for x in self.active if x != n]
        try:
            v = self.scope.get(n) or self.vars.get(n)
            return '' if v is None else v[1] if v[0] == ':' else self.expand(v[1])
        finally:
            self.auto, self.active = saved, act

    def fn_if(self, s):
        a = self.split(s, 3)
        if self.expand(a[0].strip()).strip() if len(a) > 1 else False:
            return self.expand(a[1])
        return self.expand(a[2]) if len(a) > 2 else ''

    def fn_or(self, s):
        for x in self.split(s):
            v = self.expand(x.strip())
            if v:
                return v
        return ''

    def fn_and(self, s):
        v = ''
        for x in self.split(s):
            v = self.expand(x.strip())
            if not v:
                return ''
        return v

    def fn_value(self, s):
        v = self.vars.get(self.expand(s).strip())
        return v[1] if v else ''

    def fn_origin(self, s):
        n = self.expand(s).strip()
        v = self.scope.get(n) or self.vars.get(n)
        return 'automatic' if n in self.auto else v[2] if v else 'undefined'

    def fn_flavor(self, s):
        v = self.vars.get(self.expand(s).strip())
        return 'undefined' if not v else 'simple' if v[0] == ':' else 'recursive'

    def fn_shell(self, s):
        return self.sh(self.expand(s))

    def fn_info(self, s):
        put(self.expand(s))
        return ''

    def fn_warning(self, s):
        err(self.expand(s), self.where)
        return ''

    def fn_error(self, s):
        raise MakeError(f'{self.where}: *** {self.expand(s)}.  Stop.')

    def fn_eval(self, s):
        cur = self.cur
        self.cur = None
        self.parse(self.expand(s), self.where.split(':')[0] or 'eval')
        self.cur = cur
        return ''

    def shvar(self):
        v = self.get('SHELL')
        return None if os.path.basename(v) in ('sh', 'bash', 'zsh', 'dash', 'ksh') else v

    def sh(self, c):
        rc, o = self.execute(c, True)
        self.vars['.SHELLSTATUS'] = [':', str(rc), 'file']
        return re.sub(r'\r?\n', ' ', dec(o).rstrip('\n'))

    def environ(self):
        e = dict(os.environ)
        for n, v in list(self.vars.items()) + list(self.scope.items()):
            if n in self.unexport:
                e.pop(n, None)
            elif n in self.exports or v[2] == 'command line' or (n in self.env_names and n != 'MAKEFLAGS') or \
                    (self.export_all and v[2] in ('file', 'override') and re.fullmatch(r'\w+', n)):
                if re.fullmatch(r'\w+', n) and n not in self.active:
                    e[n] = self.get(n)
        fl = ''.join(sorted(c for c in self.fl if c in 'ksiBnerRq'))
        cv = ' '.join(f'{n}={self.quote(v[1])}' for n, v in self.vars.items() if v[2] == 'command line')
        e['MAKEFLAGS'] = fl + (' -- ' + cv if cv else '')
        e['MAKELEVEL'] = str(int(os.environ.get('MAKELEVEL', '0') or 0) + 1)
        e.pop('MFLAGS', None)
        return e

    def quote(self, s):
        return re.sub(r'([\s\\])', r'\\\1', s)

    def execute(self, c, capture=False):
        env, sh = self.environ(), self.shvar()
        if capture:
            # $(shell) sees the plain environment
            env = dict(os.environ)
        if sh is None:
            w = None if capture else simple(c)
            if w and w[0] in ('make', 'gmake'):
                saved, cwd = dict(os.environ), os.getcwd()
                os.environ.clear()
                os.environ.update(env)
                try:
                    return dispatch('make', w[1:]), b''
                finally:
                    os.chdir(cwd)
                    os.environ.clear()
                    os.environ.update(saved)
            return shell(c, None, capture, env)
        import subprocess
        State.out.flush()
        p = subprocess.run([sh] + self.get('.SHELLFLAGS').split() + [c], env=env, stdout=subprocess.PIPE if capture else None)
        return p.returncode if p.returncode >= 0 else 128 - p.returncode, p.stdout or b''

    # reading makefiles
    def read(self, path):
        t = text(path)
        self.files.append(path)
        self.vars['MAKEFILE_LIST'][1] += ' ' + path
        cur, self.cur = self.cur, None
        self.parse(t, path)
        self.cur = cur

    def comment(self, s):
        if '#' not in s:
            return s
        o, i = [], 0
        depth = 0
        while i < len(s):
            c = s[i]
            if c == '\\' and s[i + 1:i + 2] == '#':
                o.append('#')
                i += 2
                continue
            if c == '$' and s[i + 1:i + 2] in ('(', '{'):
                depth += 1
                o.append(s[i:i + 2])
                i += 2
                continue
            if c in ')}' and depth:
                depth -= 1
            if c == '#' and not depth:
                break
            o.append(c)
            i += 1
        return ''.join(o)

    def cond(self, s):
        """Value of an ifeq/ifneq/ifdef/ifndef line, or None."""
        m = re.match(r'(ifeq|ifneq)\s*(.*)$', s, re.S)
        if m:
            a = m[2].strip()
            if a.startswith('('):
                e = self.close(a, 0)
                p = self.split(a[1:e], 2)
                if len(p) != 2:
                    raise MakeError(f'{self.where}: *** invalid syntax in conditional.  Stop.')
                x, y = p[0].strip(), p[1].strip()
            else:
                q = re.match(r'''("[^"]*"|'[^']*')\s*("[^"]*"|'[^']*')\s*$''', a)
                if not q:
                    raise MakeError(f'{self.where}: *** invalid syntax in conditional.  Stop.')
                x, y = q[1][1:-1], q[2][1:-1]
            return (self.expand(x) == self.expand(y)) == (m[1] == 'ifeq')
        m = re.match(r'(ifdef|ifndef)\s+(.*)$', s, re.S)
        n = self.expand(m[2]).strip()
        v = self.vars.get(n)
        return bool(v and v[1]) == (m[1] == 'ifdef')

    def parse(self, t, path):
        ls, i = lines(t), 0
        conds = []           # [active, already taken, parent active]
        self.cur = None
        on = True
        while i < len(ls):
            raw = ls[i].rstrip('\r')
            no = i + 1
            i += 1
            if raw.startswith('\t') and (self.cur is not None or not on):
                if not on:
                    while raw.endswith('\\') and i < len(ls):
                        raw = ls[i]
                        i += 1
                    continue
                while raw.endswith('\\') and (len(raw) - len(raw.rstrip('\\'))) % 2 and i < len(ls):
                    nx = ls[i].rstrip('\r')
                    raw += '\n' + (nx[1:] if nx.startswith('\t') else nx)
                    i += 1
                for r in self.cur:
                    if r.recipe is None:
                        r.recipe = []
                        r.loc = f'{path}:{no}'
                    r.recipe.append(raw[1:])
                continue
            while raw.endswith('\\') and (len(raw) - len(raw.rstrip('\\'))) % 2 and i < len(ls):
                raw = raw[:-1].rstrip() + ' ' + ls[i].rstrip('\r').lstrip()
                i += 1
            self.where = f'{path}:{no}'
            s = raw.strip(' ') if not raw.startswith('\t') else raw.strip()
            w = s.split(None, 1)[0] if s.strip() else ''
            # conditionals
            if re.match(r'(ifeq|ifneq|ifdef|ifndef)\b', s):
                v = self.cond(self.comment(s)) if on else False
                conds.append([on and v, v, on])
                on = on and v
                continue
            if w == 'else' or re.match(r'else\s*(#|$)', s):
                if not conds:
                    raise MakeError(f"{self.where}: *** extraneous 'else'.  Stop.")
                c = conds[-1]
                rest = self.comment(s[4:]).strip()
                if c[1] or not c[2]:
                    c[0] = False
                elif rest:
                    v = self.cond(rest)
                    c[0] = c[1] = v
                else:
                    c[0] = c[1] = True
                on = c[0] and c[2]
                continue
            if re.match(r'endif\b', s):
                if not conds:
                    raise MakeError(f"{self.where}: *** extraneous 'endif'.  Stop.")
                on = conds.pop()[2]
                continue
            if re.match(r'(override\s+|export\s+)*define\b', s):
                m = re.match(r'((?:override\s+|export\s+)*)define\s+(.*?)\s*(\?=|\+=|:{1,3}=|!=|=)?\s*$', self.comment(s))
                body, depth = [], 1
                while i < len(ls):
                    x = ls[i].rstrip('\r')
                    i += 1
                    if re.match(r'\s*define\b', x):
                        depth += 1
                    if re.match(r'\s*endef\b', x):
                        depth -= 1
                        if not depth:
                            break
                    body.append(x)
                else:
                    raise MakeError(f"{self.where}: *** missing 'endef', unterminated 'define'.  Stop.")
                if on and m:
                    n = self.expand(m[2]).strip()
                    op = m[3] or '='
                    self.set(n, ':=' if op.endswith(':=') or op.startswith(':') else op, '\n'.join(body),
                             'override' if 'override' in m[1] else 'file')
                    if 'export' in m[1]:
                        self.exports.add(n)
                continue
            if not on:
                continue
            if not s.strip():
                continue
            if s.lstrip().startswith('#'):
                continue
            self.line(s, path, no)
        if conds:
            raise MakeError(f"{path}: *** missing 'endif'.  Stop.")

    ASSIGN = re.compile(r'(:{1,3}=|\?=|\+=|!=|=)')

    def assignment(self, s):
        """(name, op, value) when s is a variable assignment."""
        d, k = 0, 0
        while k < len(s):
            c = s[k]
            if c == '$' and s[k + 1:k + 2] in ('(', '{'):
                try:
                    k = self.close(s, k + 1) + 1
                except MakeError:
                    return None
                continue
            m = self.ASSIGN.match(s, k)
            if m:
                n = s[:k].strip()
                if not n or re.search(r'\s', re.sub(r'\$[({].*?[)}]', '', n)):
                    return None
                return n, (':=' if m[0][0] == ':' else m[0]), s[m.end():].lstrip()
            if c in ':#':
                return None
            k += 1
        return None

    def line(self, s, path, no):
        m = re.match(r'(-include|sinclude|include)\s+(.*)$', s)
        if m:
            self.cur = None
            for f in self.expand(self.comment(m[2])).split():
                fs = self.glob(f) or [f]
                for x in fs:
                    p = next((q for q in [x] + [os.path.join(d, x) for d in self.incdirs] if os.path.isfile(q)), None)
                    if p:
                        self.read(p)
                    else:
                        self.missing.append((x, m[1] == 'include', self.where))
                    self.where = f'{path}:{no}'
            return
        origin, rest, exported = 'file', s, False
        while True:
            m = re.match(r'(override|export|private)\s+(?=\S)', rest)
            if not m:
                break
            a = self.assignment(rest)
            if a and a[0] == m[1]:
                break
            if m[1] == 'override':
                origin = 'override'
            elif m[1] == 'export':
                exported = True
            rest = rest[m.end():]
        m = re.match(r'undefine\s+(.*)$', rest)
        if m:
            self.vars.pop(self.expand(m[1]).strip(), None)
            return
        m = re.match(r'(unexport|export)\s*(.*)$', rest) if not self.assignment(rest) else None
        if m and re.match(r'(unexport|export)(\s|$)', rest):
            ns = self.expand(self.comment(m[2])).split()
            (self.unexport if m[1] == 'unexport' else self.exports).update(ns)
            if not ns and m[1] == 'export':
                self.export_all = True
            return
        a = self.assignment(rest)
        if a:
            self.cur = None
            n = self.expand(a[0])
            v = self.comment(a[2])
            self.set(n, a[1], v, origin)
            if exported:
                self.exports.add(n)
            return
        if exported:
            self.exports.update(self.expand(self.comment(rest)).split())
            return
        m = re.match(r'vpath(?:\s+(\S+)(?:\s+(.*))?)?$', rest)
        if m:
            if m[1] and m[2]:
                self.vpath.append((m[1], re.split(r'[:\s]+', self.expand(m[2]).strip())))
            elif m[1]:
                self.vpath = [x for x in self.vpath if x[0] != m[1]]
            else:
                self.vpath = []
            return
        self.rule(rest, path, no)

    def outside(self, s, chars):
        k = 0
        while k < len(s):
            if s[k] == '$' and s[k + 1:k + 2] in ('(', '{'):
                k = self.close(s, k + 1) + 1
                continue
            if s[k] == '$':
                k += 2
                continue
            if s[k] in chars:
                return k
            k += 1
        return -1

    def rule(self, s, path, no):
        # a recipe after ; keeps its # and $
        colon = self.outside(s, ':')
        semi, inline = self.outside(s, ';#'), None
        if semi >= 0 and s[semi] == ';':
            s, inline = s[:semi], s[semi + 1:].lstrip()
            colon = self.outside(s, ':')
        else:
            s = self.comment(s)
        if colon >= 0:
            left, right = s[:colon], s[colon + 1:]
            # target-specific variable
            m = re.match(r'\s*((?:override|export|private)\s+)*', right)
            a = self.assignment(right[m.end():]) if not right.startswith('=') else None
            if a:
                if inline is not None:
                    a = (a[0], a[1], a[2] + ';' + inline)
                for t in self.expand(left).split():
                    item = (self.expand(a[0]), a[1], a[2], 'override' if 'override' in (m[0] or '') else 'file')
                    if 'export' in (m[0] or ''):
                        self.exports.add(item[0])
                    if '%' in t:
                        self.ptvars.append((t, item))
                    else:
                        self.tvars.setdefault(self.norm(t), []).append(item)
                self.cur = None
                return
            # the second expansion is only wanted for prerequisites
            ex = self.expand(left) + ':' + (right if self.second else self.expand(right))
        else:
            ex = self.expand(s)
            if not ex.strip():
                return
            if ':' not in ex:
                a = re.match(r'\s+', s)
                raise MakeError(f'{path}:{no}: *** missing separator.  Stop.')
            if '\n' in ex:
                self.parse(ex, path)
                return
            if self.assignment(ex):
                n, op, v = self.assignment(ex)
                self.set(n, op, v)
                return
        left, _, right = ex.partition(':')
        grouped = left.endswith('&')
        left = left.rstrip('&')
        double = right.startswith(':')
        right = right.lstrip(':') if double else right
        if ';' in right and inline is None and not self.second:
            right, _, inline = right.partition(';')
        targets = [self.norm(y) for x in left.split() for y in (self.glob(x) if any(c in x for c in '*?[') else [x])]
        static = None
        if not self.second and ':' in right:
            static, _, right = right.partition(':')
            static = static.strip()
        elif self.second:
            k = self.outside(right, ':')
            if k >= 0:
                static, right = self.expand(right[:k]).strip(), right[k + 1:]
        if self.second:
            k = self.outside(right, '|')
            deps, order = ([right[:k], right[k + 1:]] if k >= 0 else [right, ''])
        else:
            a, _, b = right.partition('|')
            deps, order = self.words(a), self.words(b)
        loc = f'{path}:{no}'
        self.cur = []
        if not targets:
            return
        recipe = [inline] if inline is not None else None
        # special targets
        if targets[0].startswith('.') and targets[0][1:].isupper() or targets[0] in ('.DELETE_ON_ERROR',):
            for t in targets:
                d = deps if not self.second else self.words(self.expand(deps))
                if t == '.PHONY':
                    self.phony.update(d)
                elif t == '.SECONDEXPANSION':
                    self.second = True
                elif t == '.EXPORT_ALL_VARIABLES':
                    self.export_all = True
                elif t == '.SUFFIXES':
                    if not d:
                        self.flags['.SUFFIXES'] = ['']
                elif t in ('.SILENT', '.IGNORE', '.ONESHELL', '.DELETE_ON_ERROR', '.PRECIOUS', '.SECONDARY', '.INTERMEDIATE', '.POSIX',
                           '.NOTPARALLEL', '.LOW_RESOLUTION_TIME', '.NOTINTERMEDIATE'):
                    self.flags.setdefault(t, []).extend(d or ['*'])
                elif t == '.DEFAULT':
                    r = Rule(loc)
                    r.recipe = recipe
                    self.default = r
                    self.cur = [r]
            return
        if any('%' in t for t in targets) and static is None:
            r = Rule(loc)
            r.deps, r.order, r.recipe, r.targets = deps, order, recipe, targets
            r.double = double
            self.patterns = [p for p in self.patterns if not (p.targets == targets and p.deps == deps)]
            self.patterns.append(r)
            self.cur = [r]
            return
        # old style suffix rules, such as .c.o:
        m = re.fullmatch(r'(\.[A-Za-z0-9_+]+)(\.[A-Za-z0-9_+]+)?', targets[0]) if len(targets) == 1 and not deps and static is None else None
        if m and m[0] not in ('.gitignore',):
            r = Rule(loc)
            r.targets, r.deps, r.recipe = ['%' + (m[2] or '')], ['%' + m[1]], recipe
            self.patterns.append(r)
            self.cur = [r]
            return
        for t in targets:
            if t not in self.rules or double:
                r = Rule(loc)
                r.double = double
                self.rules.setdefault(t, []).append(r)
            r = self.rules[t][-1]
            if not self.get('.DEFAULT_GOAL') and (not t.startswith('.') or '/' in t):
                self.vars['.DEFAULT_GOAL'] = [':', t, 'file']
            d, od = deps, order
            if static is not None:
                pre, pc, suf = static.partition('%')
                if not (pc and t.startswith(pre) and t.endswith(suf) and len(t) >= len(pre) + len(suf)):
                    raise MakeError(f"{loc}: *** target '{t}' doesn't match the target pattern.  Stop.")
                stem = t[len(pre):len(t) - len(suf)] if suf else t[len(pre):]
                r.stem = stem
                if self.second:
                    d, od = d.replace('%', stem), od.replace('%', stem)
                else:
                    d, od = [x.replace('%', stem, 1) for x in d], [x.replace('%', stem, 1) for x in od]
            if self.second:
                r.lazy = getattr(r, 'lazy', []) + [(d, od)]
            else:
                r.deps = r.deps + d
                r.order = r.order + od
            if recipe is not None:
                r.recipe = list(recipe)
                r.loc = loc
            elif r.recipe is not None and not double:
                # a later rule with its own recipe replaces the first one
                old = r
                r = Rule(loc)
                r.__dict__.update(old.__dict__)
                r.old, r.recipe = old.recipe, None
                self.rules[t][-1] = r
            if grouped:
                r.group = targets
            self.cur.append(r)

    def norm(self, t):
        while t.startswith('./') and len(t) > 2:
            t = t[2:].lstrip('/')
        return t

    def words(self, s):
        return [self.norm(y) for x in s.split() for y in ((self.glob(x) or [x]) if any(c in x for c in '*?[') else [x])]

    # building
    def find(self, t):
        if os.path.exists(t) or t.startswith('/'):
            return t
        for pat, ds in self.vpath:
            if self.match([pat], t):
                for d in ds:
                    if os.path.exists(os.path.join(d, t)):
                        return os.path.join(d, t)
        vp = self.get('VPATH')
        if vp:
            for d in re.split(r'[:\s]+', vp.strip()):
                if os.path.exists(os.path.join(d, t)):
                    return os.path.join(d, t)
        return t

    def mtime(self, t):
        try:
            return os.stat(t).st_mtime_ns
        except OSError:
            return None

    def implicit(self, t, depth=0, stack=()):
        """(rule, stem, deps, order) of the pattern rule that can make t."""
        if t in self.phony or depth > 4:
            return None
        best = []
        for i, r in enumerate(self.patterns):
            for p in r.targets:
                pre, _, suf = p.partition('%')
                base, d = t, ''
                if '/' not in p and '/' in t:
                    d, base = t[:t.rfind('/') + 1], t[t.rfind('/') + 1:]
                if base.startswith(pre) and base.endswith(suf) and len(base) > len(pre) + len(suf):
                    stem = base[len(pre):len(base) - len(suf)]
                    best.append((len(stem), i, r, d, stem))
                    break
        best.sort(key=lambda x: x[:2])
        for chain in (False, True):
            for _, i, r, d, stem in best:
                if r in stack or r.recipe is None:
                    continue
                if self.second and isinstance(r.deps, str):
                    saved, self.auto = self.auto, {'@': t, '*': d + stem}
                    deps, order = ([self.norm(x.replace('%', d + stem, 1)) for x in self.words(self.expand(q))] for q in (r.deps, r.order))
                    self.auto = saved
                else:
                    deps, order = ([self.norm((d if '%' in x and '/' not in x[:x.index('%')] else '') + x.replace('%', stem, 1)
                                              if '%' in x else x) for x in q] for q in (r.deps, r.order))
                ok = True
                for x in deps + order:
                    if x in self.rules or x in self.phony or os.path.exists(self.find(x)):
                        continue
                    # match-anything rules do not take part in chains
                    if chain and not (r.targets == ['%'] and depth) and x != t and self.implicit(x, depth + 1, stack + (r,)):
                        continue
                    ok = False
                    break
                if ok:
                    if r.targets == ['%'] and depth and not all(os.path.exists(self.find(x)) for x in deps):
                        continue
                    return r, d + stem, deps, order
        return None

    def scoped(self, t):
        items = [i for p, i in self.ptvars if self.match([p], t)] + self.tvars.get(t, [])
        for n, op, v, o in items:
            self.set(n, op, v, o, self.scope)

    def update(self, t, parent=None, top=False):
        """Bring t up to date; returns its time (INF when it counts as new)."""
        if t in self.done:
            return self.done[t]
        if t in self.deps_of:
            err(f'Circular {parent} <- {t} dependency dropped.', 'make')
            return 0
        saved = self.scope
        self.scope = dict(saved)
        self.deps_of[t] = True
        try:
            self.scoped(t)
            r = self._update(t, parent, top)
        finally:
            self.scope = saved
            del self.deps_of[t]
        self.done[t] = r
        return r

    def _update(self, t, parent, top):
        rs = self.rules.get(t)
        phony = t in self.phony
        if rs and rs[0].double:
            res = []
            for r in rs:
                res.append(self.one(t, phony, r, r.deps, r.order, r.recipe, r.deps and None, False, len(r.deps) == 0))
            return max(res)
        r = rs[0] if rs else None
        if r is not None and hasattr(r, 'lazy'):
            stem, saved = getattr(r, 'stem', ''), self.auto
            self.auto = {'@': t, '*': stem}
            for d, od in r.lazy:
                r.deps = r.deps + self.words(self.expand(d))
                r.order = r.order + self.words(self.expand(od))
            self.auto = saved
            del r.lazy
        deps, order = (list(r.deps), list(r.order)) if r else ([], [])
        recipe = r.recipe if r else None
        if recipe is None and r is not None and hasattr(r, 'old'):
            recipe = r.old
        stem = getattr(r, 'stem', None)
        imp = None
        if recipe is None and not phony:
            imp = self.implicit(t)
            if imp:
                ir, stem, d, od = imp
                deps, order, recipe = d + [x for x in deps if x not in d], od + order, ir.recipe
                r2 = Rule(ir.loc)
                r2.targets = ir.targets
                r = r2
        path = self.find(t)
        if r is None:
            m = self.mtime(path)
            if m is not None:
                return m
            if self.default and self.default.recipe is not None:
                return self.one(t, False, self.default, [], [], self.default.recipe, None, top)
            msg = f"*** No rule to make target '{t}'" + (f", needed by '{parent}'" if parent else '')
            if 'k' in self.fl:
                err(msg + '.', 'make')
                self.failed.add(t)
                return self.INF
            raise MakeError(msg + '.  Stop.')
        return self.one(t, phony, r, deps, order, recipe, stem, top)

    def one(self, t, phony, r, deps, order, recipe, stem, top, always=False):
        path = t if phony else self.find(t)
        mine = None if phony else self.mtime(path)
        newer, need, bad = [], phony or mine is None or 'B' in self.fl or always, False
        for d in dict.fromkeys(deps):
            m = self.update(d, t)
            if d in self.failed:
                bad = True
            if mine is None or m > mine or 'B' in self.fl:
                newer.append(d)
                need = True
        for d in dict.fromkeys(order):
            if d not in deps:
                self.update(d, t)
                bad = bad or d in self.failed
        if bad:
            self.failed.add(t)
            err(f"Target '{t}' not remade because of errors.", 'make') if top else None
            return self.INF
        # an existing intermediate-free target, or nothing to run
        if not need:
            return mine
        if not recipe:
            return self.INF if mine is None or phony or newer and any(self.done.get(d) == self.INF for d in newer) else mine
        if path != t:
            path = t     # a target found through VPATH is rebuilt here
        full = [self.find(d) for d in deps]
        uniq = list(dict.fromkeys(full))
        auto = {'@': path, '<': full[0] if full else '', '^': ' '.join(uniq), '+': ' '.join(full), '?': ' '.join(self.find(d) for d in newer),
                '|': ' '.join(dict.fromkeys(self.find(d) for d in order)), '*': stem if stem is not None else self.guess(t), '%': ''}
        ok = self.run(t, r, recipe, auto)
        for x in getattr(r, 'group', []):
            if x != t:
                self.done[x] = self.INF
        tg = getattr(r, 'targets', [])
        if len(tg) > 1 and stem is not None:
            for p in tg:
                x = p.replace('%', stem, 1)
                if x != t:
                    self.done[x] = self.mtime(x) or self.INF
        if not ok:
            self.failed.add(t)
            if '.DELETE_ON_ERROR' in self.flags and not phony and self.mtime(t) not in (None, mine):
                err(f"*** Deleting file '{t}'", 'make')
                os.unlink(t)
            return self.INF
        if phony or 'n' in self.fl:
            return self.INF
        m = self.mtime(t)
        return self.INF if m is None else m

    def guess(self, t):
        m = re.match(r'(.*)\.(o|c|cc|cpp|s|S|a|h|y|l)$', t)
        return m[1] if m else ''

    def run(self, t, r, recipe, auto):
        saved, self.auto = self.auto, auto
        self.where = r.loc
        try:
            cmds = []
            for raw in recipe:
                plus = re.search(r'\$[({]MAKE[)}]', raw) is not None
                for x in re.split(r'(?<!\\)\n', self.expand(raw)):
                    cmds.append((x, plus))
            if '.ONESHELL' in self.flags and cmds:
                cmds = [('\n'.join(x for x, _ in cmds), any(p for _, p in cmds))]
            for c, plus in cmds:
                self.ranq = True
                silent = 's' in self.fl or t in self.flags.get('.SILENT', []) or '*' in self.flags.get('.SILENT', [])
                ignore = 'i' in self.fl or t in self.flags.get('.IGNORE', []) or '*' in self.flags.get('.IGNORE', [])
                c = c.lstrip()
                while c and c[0] in '@-+ \t':
                    silent, ignore, plus = silent or c[0] == '@', ignore or c[0] == '-', plus or c[0] == '+'
                    c = c[1:]
                if not c.strip():
                    continue
                if 'q' in self.fl:
                    raise SystemExit(1)
                self.ran = True
                if 'n' in self.fl or not silent:
                    put(c)
                if 'n' in self.fl and not plus:
                    continue
                rc, _ = self.execute(c)
                if rc:
                    lv = os.environ.get('MAKELEVEL', '0')
                    me = 'make' if lv in ('0', '') else f'make[{lv}]'
                    if ignore:
                        err(f'[{r.loc}: {t}] Error {rc} (ignored)', me)
                        continue
                    err(f'*** [{r.loc}: {t}] Error {rc}', me)
                    if 'k' in self.fl:
                        return False
                    raise MakeError('')
            return True
        finally:
            self.auto = saved


def make_once(fl, cmdvars, incdirs, files, goals, round_=0):
    mk = Make(fl, cmdvars, incdirs)
    mk.vars['MAKECMDGOALS'] = [':', ' '.join(goals), 'default']
    fls = ''.join(sorted(c for c in fl if c in 'ksiBnerRq'))
    mk.vars['MAKEFLAGS'] = [':', fls, 'default']
    for f in files:
        try:
            mk.read(f)
        except OSError as e:
            raise MakeError(f'{f}: {e.strerror}') from None
    if 'r' not in fl:
        own = mk.patterns
        mk.patterns = []
        goal, sec, ph = mk.vars['.DEFAULT_GOAL'], mk.second, mk.missing
        mk.second = False
        mk.parse(mk.RULES, '<builtin>')
        mk.second, mk.missing, mk.vars['.DEFAULT_GOAL'] = sec, ph, goal
        # a pattern rule without recipe cancels the built-in one
        mk.patterns = [p for p in own] + [p for p in mk.patterns if not any(q.targets == p.targets and q.deps == p.deps for q in own)]
    remade = False
    for f, must, where in mk.missing:
        f = mk.norm(f)
        if f in mk.rules or mk.implicit(f):
            mk.update(f)
            remade = remade or os.path.exists(f)
        if must and not os.path.exists(f):
            raise MakeError(f'{where}: {f}: No such file or directory\nmake: *** No rule to make target \'{f}\'.  Stop.')
    if not remade:
        for f in mk.files:
            f = mk.norm(f)
            if (f in mk.rules and (mk.rules[f][0].recipe or mk.rules[f][0].deps)) and f not in mk.phony and round_ < 5:
                before = mk.mtime(f)
                mk.update(f)
                remade = remade or mk.mtime(f) != before
    if remade and round_ < 5:
        return make_once(fl, cmdvars, incdirs, files, goals, round_ + 1)
    mk.done.clear()
    mk.ran = False
    goals = goals or mk.get('.DEFAULT_GOAL').split()
    if not goals:
        raise MakeError('*** No targets.  Stop.')
    for g in goals:
        mk.ran = False
        g = mk.norm(g)
        mk.update(g, None, True)
        if not mk.ran and g not in mk.failed and 's' not in fl and 'q' not in fl:
            r = mk.rules.get(g)
            if g in mk.phony or (r and not r[0].recipe and not mk.implicit(g)) or not (r or mk.implicit(g)):
                put(f"make: Nothing to be done for '{g}'.")
            elif not getattr(mk, 'ranq', False) or os.path.exists(g):
                put(f"make: '{g}' is up to date.")
    return 2 if mk.failed else 0


@command('make', 'gmake')
def make(name, args):
    env = os.environ.get('MAKEFLAGS', '')
    pre = []
    if env:
        a, _, b = env.partition(' -- ') if ' -- ' in env else (env, '', '')
        if env.startswith('-- '):
            a, b = '', env[3:]
        for w in a.split():
            if w.startswith('--'):
                continue
            pre += ['-' + c for c in w.lstrip('-') if c in 'ksiBnerRq']
        pre += [re.sub(r'\\(.)', r'\1', x) for x in re.findall(r'(?:\\.|[^\s\\])+', b)]
    j = []
    i = 0
    while i < len(args):        # -j takes an optional number
        if re.fullmatch(r'-j\d*|--jobs(=\d+)?|-[ksBinwrRe]*j\d*', args[i]):
            m = re.match(r'-([ksBinwrRe]+)j', args[i])
            if m:
                j.append('-' + m[1])
            if args[i].endswith('j') and i + 1 < len(args) and args[i + 1].isdigit():
                i += 1
        else:
            j.append(args[i])
        i += 1
    o, rest = opts(pre + j, 'C:f:nskBiI:werRqSl:O?b', 'directory=C file=f makefile=f just-print=n dry-run=n recon=n silent=s quiet=s '
                   'keep-going=k always-make=B ignore-errors=i include-dir=I print-directory=w no-print-directory=nw '
                   'environment-overrides=e no-builtin-rules=r no-builtin-variables=R question=q stop=S no-keep-going=S version trace '
                   'output-sync=O? warn-undefined-variables=wu load-average=l:')
    fl, dirs, files, inc, nw = set(), [], [], [], False
    for k, v in o:
        if k == 'C':
            dirs.append(v)
        elif k == 'f':
            files.append(v)
        elif k == 'I':
            inc.append(v)
        elif k == 'version':
            put('make (devtools), GNU Make 3.81 style')
            return 0
        elif k == 'trace':
            raise Fail('unsupported option --trace')
        elif k in ('nw',):
            nw = True
        else:
            fl.add(k)
    if 'S' in fl:
        fl.discard('k')
    if 'R' in fl:
        fl.add('r')
    goals, cv = [], []
    for a in rest:
        m = re.match(r'([^\s=:+?!]+)\s*(:{0,3}=|\+=|\?=)(.*)$', a, re.S)
        if m:
            cv.append((m[1], ':=' if m[2][0] == ':' else '=', m[3]))
        else:
            goals.append(a)
    cwd = os.getcwd()
    lv = os.environ.get('MAKELEVEL', '0')
    me = 'make' if lv in ('0', '') else f'make[{lv}]'
    for d in dirs:
        try:
            os.chdir(d)
        except OSError as e:
            os.chdir(cwd)
            err(f'*** {d}: {e.strerror}.  Stop.', me)
            return 2
    say = (dirs or 'w' in fl or me != 'make') and not nw and 's' not in fl
    if say:
        put(f"{me}: Entering directory '{os.getcwd()}'")
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 20000))
    try:
        if not files:
            files = [f for f in ('GNUmakefile', 'makefile', 'Makefile') if f in os.listdir('.')][:1]
            if not files:
                raise MakeError(f"*** No rule to make target '{goals[0]}'.  Stop." if goals else
                                '*** No targets specified and no makefile found.  Stop.')
        return make_once(fl, cv, inc, files, goals)
    except MakeError as e:
        if str(e):
            m = re.match(r'(\S+:\d+): (.*)', str(e), re.S)
            err(m[2], m[1]) if m else err(e, me)
        return 2
    except SystemExit as e:
        return e.code
    finally:
        if say:
            put(f"{me}: Leaving directory '{os.getcwd()}'")
        os.chdir(cwd)


# ---- xcodebuild, xcrun, xcode-select: Henge's own, for simple iPhone app projects ----
# An Xcode project becomes a henge-project.json beside it, which Henge's builder builds (HENGE_BUILD: its port and secret).

def xc_tools():
    t = os.environ.get('HENGE_TOOLS')
    if not t:
        raise Fail('HENGE_TOOLS is not set: this works in Henge\'s terminal', 1)
    return t


def xc_sdk():
    import json
    sdk = xc_tools() + '/sdk/iPhoneOS.sdk'
    try:
        with open(sdk + '/SDKSettings.json') as f:
            return sdk, str(json.load(f)['Version'])
    except (OSError, ValueError, KeyError):
        return sdk, '27.0'


def xc_sdk_ok(name):
    if not re.fullmatch(r'iphoneos[\d.]*|.*/iPhoneOS[\d.]*\.sdk', name):
        raise Fail(f'SDK "{name}" is not here: only iphoneos is', 1)


def pbx_parse(s):
    """An old-style property list: dictionaries, lists and strings."""
    tok = re.compile(r'\s+|//[^\n]*|/\*.*?\*/|"((?:[^"\\]|\\.)*)"|([A-Za-z0-9_$./:+~@%-]+)|([{}()=;,])|<([0-9A-Fa-f\s]*)>', re.S)
    esc = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v'}
    toks, i = [], 0
    while i < len(s):
        m = tok.match(s, i)
        if not m:
            raise Fail(f'the project file cannot be read at {s[i:i + 30]!r}', 1)
        if m[1] is not None:
            toks.append((1, re.sub(r'\\(U[0-9A-Fa-f]{4}|[0-7]{1,3}|.)', lambda k: chr(int(k[1][1:], 16)) if len(k[1]) == 5 else chr(int(k[1], 8)) if k[1][0] in '01234567' else esc.get(k[1], k[1]), m[1], flags=re.S)))
        elif m[2] is not None:
            toks.append((1, m[2]))
        elif m[3]:
            toks.append((0, m[3]))
        elif m[4] is not None:
            toks.append((1, m[4]))
        i = m.end()
    at = 0

    def need(c):
        nonlocal at
        if toks[at] != (0, c):
            raise Fail(f'the project file has {toks[at][1]!r} where {c!r} belongs', 1)
        at += 1

    def value():
        nonlocal at
        kind, t = toks[at]
        at += 1
        if kind:
            return t
        if t == '{':
            d = {}
            while toks[at] != (0, '}'):
                k = value()
                need('=')
                d[k] = value()
                need(';')
            at += 1
            return d
        if t == '(':
            a = []
            while toks[at] != (0, ')'):
                a.append(value())
                if toks[at] == (0, ','):
                    at += 1
            at += 1
            return a
        raise Fail(f'the project file has an unexpected {t!r}', 1)
    try:
        return value()
    except IndexError:
        raise Fail('the project file ends too early', 1) from None


def xc_words(v):
    if isinstance(v, list):
        return [w for x in v for w in xc_words(x)]
    import shlex
    try:
        return shlex.split(v)
    except ValueError:
        return v.split()


def xc_xcconfig(path, seen=()):
    """The settings of an .xcconfig file, its includes first."""
    out = []
    if path in seen:
        return out
    for ln in text(path).splitlines():
        m = re.match(r'\s*#include(\?)?\s*"(.*)"', ln)
        if m:
            p = os.path.join(os.path.dirname(path), m[2])
            if os.path.exists(p):
                out += xc_xcconfig(p, seen + (path,))
            elif not m[1]:
                raise Fail(f'{path}: {m[2]} is not there', 1)
            continue
        m = re.match(r'\s*([A-Za-z_]\w*(?:\[[^\]]*\])*)\s*=\s*(.*?)\s*;?\s*$', re.sub(r'//.*', '', ln))
        if m:
            out.append((m[1], m[2]))
    return out


class XcProject:
    def __init__(self, path, config, over):
        self.path, self.root = path, os.path.dirname(os.path.abspath(path))
        o = pbx_parse(text(os.path.join(path, 'project.pbxproj')))
        self.o = o.get('objects', {})
        self.main = self.o[o['rootObject']]
        self.notes, self.bad = [], []
        self.where, self.parent = {}, {}
        self.base = os.path.normpath(os.path.join(self.root, self.main.get('projectDirPath', '')))
        self.walk(self.main['mainGroup'], self.base)
        self.targets = [self.o[t] | {'id': t} for t in self.main['targets'] if t in self.o]
        names = self.configs(self.main)
        self.config = config or next((n for n in ('Release', *names) if n in names), 'Release')
        if self.config not in names:
            raise Fail(f'there is no configuration "{self.config}": {", ".join(names)}', 1)
        self.over = over

    def note(self, s):
        if s not in self.notes:
            self.notes.append(s)

    def fail(self, s):
        if s not in self.bad:
            self.bad.append(s)

    def walk(self, i, here):
        g = self.o.get(i)
        if not g:
            return
        tree, p = g.get('sourceTree', '<group>'), g.get('path', '')
        at = {'<group>': os.path.join(here, p), 'SOURCE_ROOT': os.path.join(self.base, p), '<absolute>': p}.get(tree)
        self.where[i] = os.path.normpath(at) if at else None
        for c in g.get('children', []):
            self.walk(c, at or here)

    def configs(self, holder):
        c = self.o.get(holder.get('buildConfigurationList'), {})
        return {self.o[k]['name']: self.o[k] for k in c.get('buildConfigurations', []) if k in self.o}

    def settings(self, target):
        """The target's settings, each level over the one below it."""
        name = target['name']
        levels = [{'TARGET_NAME': name, 'TARGETNAME': name, 'PROJECT_NAME': os.path.splitext(os.path.basename(self.path))[0], 'SRCROOT': self.base, 'SOURCE_ROOT': self.base,
                   'PROJECT_DIR': self.base, 'PROJECT_FILE_PATH': os.path.abspath(self.path), 'CONFIGURATION': self.config, 'PRODUCT_NAME': '$(TARGET_NAME)', 'SDKROOT': 'iphoneos', 'PLATFORM_NAME': 'iphoneos',
                   'EXECUTABLE_NAME': '$(PRODUCT_NAME)', 'PRODUCT_MODULE_NAME': '$(PRODUCT_NAME:c99extidentifier)', 'ARCHS': 'arm64', 'CURRENT_ARCH': 'arm64',
                   'DEVELOPMENT_LANGUAGE': self.main.get('developmentRegion', 'en'), 'PRODUCT_BUNDLE_PACKAGE_TYPE': 'APPL',
                   'CURRENT_PROJECT_VERSION': '1', 'MARKETING_VERSION': '1.0'}]
        for h in (self.main, target):
            c = self.configs(h).get(self.config)
            if not c:
                continue
            ref = c.get('baseConfigurationReference')
            f = self.where.get(ref)
            anchor = c.get('baseConfigurationReferenceAnchor')
            if anchor and c.get('baseConfigurationReferenceRelativePath'):
                f = os.path.join(self.where.get(anchor) or '', c['baseConfigurationReferenceRelativePath'])
            if f:
                levels.append(self.level(xc_xcconfig(f)))
            levels.append(self.level(c.get('buildSettings', {}).items()))
        levels.append(self.level(self.over.items()))
        s = {}
        for lv in levels:
            for k, v in lv.items():
                old = s.get(k, '')

                def inherit(x):
                    return re.sub(r'\$[({]inherited[)}]', lambda m: old if isinstance(old, str) else ' '.join(old), x)
                if isinstance(v, list):
                    n = []
                    for x in v:
                        if re.fullmatch(r'\$[({]inherited[)}]', x):
                            n += old if isinstance(old, list) else xc_words(old)
                        else:
                            n.append(inherit(x))
                    s[k] = n
                else:
                    s[k] = list(old) if isinstance(old, list) and re.fullmatch(r'\$[({]inherited[)}]', v) else inherit(v)
        return s

    def level(self, items):
        import fnmatch
        plain, special = {}, {}
        for k, v in items:
            b, _, cond = k.partition('[')
            if not cond:
                plain[b] = v
            elif all(fnmatch.fnmatch({'sdk': 'iphoneos27.0', 'arch': 'arm64', 'config': self.config}.get(a, ''), p) for a, p in re.findall(r'(\w+)=([^\],]*)', cond)):
                special[b] = v
        return plain | special

    def expand(self, v, s, deep=0):
        if isinstance(v, list):
            return [self.expand(x, s, deep) for x in v]
        if deep > 20:
            return ''

        def one(m):
            n, *mods = (m[1] or m[2]).split(':')
            x = self.expand(s.get(n, ''), s, deep + 1)
            x = ' '.join(x) if isinstance(x, list) else x
            for k in mods:
                if k.startswith('default='):
                    x = x or k[8:]
                elif k in ('c99extidentifier', 'identifier'):
                    x = re.sub(r'\W', '_', x)
                elif k == 'rfc1034identifier':
                    x = re.sub(r'[^A-Za-z0-9.-]', '-', x)
                elif k in ('lower', 'upper'):
                    x = getattr(x, k)()
            return x
        for _ in range(5):
            n = re.sub(r'\$\(([^()$]+)\)|\$\{([^{}$]+)\}', one, v)
            if n == v:
                break
            v = n
        return v

    def rel(self, p):
        return os.path.relpath(os.path.join(self.base, p), self.root)

    def pick(self, name):
        apps = [t for t in self.targets if t.get('productType') == 'com.apple.product-type.application']
        if name:
            t = next((t for t in self.targets if t['name'] == name), None)
            if not t:
                raise Fail(f'there is no target "{name}": {", ".join(t["name"] for t in self.targets)}', 1)
            if t not in apps:
                raise Fail(f'"{name}" is not an app: only iPhone apps are built here ({t.get("productType", "no product type")})', 1)
            return t
        if len(apps) != 1:
            raise Fail(f'name the app with -target: {", ".join(t["name"] for t in apps) or "there is none"}', 1)
        return apps[0]

    def synced(self, target):
        """The files of the folders Xcode 16 and later keeps in step with the disk: (path, kind)."""
        for g in target.get('fileSystemSynchronizedGroups', []):
            top = self.where.get(g)
            if top is None or not os.path.isdir(top):
                self.fail(f'the folder {self.o.get(g, {}).get("path", g)} is not there')
                continue
            gone, only = set(), set()
            for e in self.o[g].get('exceptions', []):
                e = self.o.get(e, {})
                if e.get('target') == target['id']:
                    gone |= set(e.get('membershipExceptions', []))
                    if e.get('additionalCompilerFlagsByRelativePath'):
                        self.fail('compiler flags for single files: ' + ', '.join(e['additionalCompilerFlagsByRelativePath']))
                elif not e.get('target') and e.get('membershipExceptions'):
                    only |= set(e['membershipExceptions'])
            whole = set(self.o[g].get('explicitFolders', []))
            for r, ds, fs in os.walk(top):
                ds.sort()
                for d in list(ds):
                    rel = os.path.relpath(os.path.join(r, d), top)
                    if d.startswith('.') or rel in gone:
                        ds.remove(d)
                    elif rel in whole or os.path.splitext(d)[1] in ('.xcassets', '.bundle', '.xcdatamodeld', '.xcframework', '.framework', '.scnassets', '.icon', '.docc', '.xcodeproj', '.playground'):
                        ds.remove(d)
                        yield os.path.join(r, d)
                for f in sorted(fs):
                    if not f.startswith('.') and os.path.relpath(os.path.join(r, f), top) not in gone:
                        yield os.path.join(r, f)

    def refs(self):
        """The packages the project names: local folders and addresses."""
        out = []
        for i in self.main.get('packageReferences', []):
            r = self.o.get(i, {})
            if r.get('isa') == 'XCLocalSwiftPackageReference':
                out.append(('path', os.path.join(self.base, r.get('relativePath', '')), None))
            elif r.get('isa') == 'XCRemoteSwiftPackageReference':
                q = r.get('requirement', {})
                k = q.get('kind', '')
                w = {'upToNextMajorVersion': ('from', q.get('minimumVersion', '0')), 'upToNextMinorVersion': ('upToNextMinor', q.get('minimumVersion', '0')), 'versionRange': ('from', q.get('minimumVersion', '0')),
                     'exactVersion': ('exact', q.get('version', '0')), 'branch': ('branch', q.get('branch', 'main')), 'revision': ('revision', q.get('revision', ''))}.get(k, ('any', ''))
                out.append(('url', r.get('repositoryURL', ''), w))
        for i, f in self.o.items():
            w = self.where.get(i)
            if w and f.get('isa') == 'PBXFileReference' and os.path.exists(os.path.join(w, 'Package.swift')) and ('path', w, None) not in out:
                out.append(('path', w, None))
        return out

    def convert(self, target, mods):
        """The henge-project.json for a target."""
        needs, public = [], []
        is_lib = target.get('productType') != 'com.apple.product-type.application'

        def product(n):
            if not mods.packages:
                for kind, at, w in self.refs():
                    mods.package(at) if kind == 'path' else mods.remote(at, w)
            needs.extend(mods.product(n))
        raw = self.settings(target)

        def get(k, d=''):
            v = self.expand(raw.get(k, d), raw)
            return ' '.join(v) if isinstance(v, list) else v

        def many(k):
            out = []
            for w in xc_words(raw.get(k, '')):
                m = re.fullmatch(r'\$[({](\w+)[)}]', w)
                if m and m[1] != k and isinstance(raw.get(m[1], []), list):
                    out += many(m[1])
                elif w:
                    out.append(self.expand(w, raw))
            return [w for w in out if w]
        yes = lambda k, d='NO': get(k, d) == 'YES'
        sdk = get('SDKROOT')
        if sdk not in ('iphoneos', 'auto') and 'iphoneos' not in many('SUPPORTED_PLATFORMS'):
            raise Fail(f'"{target["name"]}" is for {sdk}: only iPhone apps are built here', 1)
        name = get('PRODUCT_NAME')
        p = {'name': name, 'bundle_id': get('PRODUCT_BUNDLE_IDENTIFIER') or 'com.example.' + re.sub(r'[^a-z0-9]', '', name.lower()), 'version': get('MARKETING_VERSION'),
             'min_ios': get('IPHONEOS_DEPLOYMENT_TARGET') or '17.0'}
        src, res, fw, libs, icons, heads = [], [], [], [], [], set()

        def source(f, flags=''):
            e = os.path.splitext(f)[1].lower()
            if [x for x in xc_words(flags) if not x.startswith('-W') and x != '-w']:
                self.fail(f'compiler flags for single files: {os.path.basename(f)} ({flags})')
            if not os.path.exists(f):
                self.fail(f'{self.rel(f)} is not there')
            elif e in ('.c', '.s', '.cpp', '.cc', '.cxx', '.c++', '.m', '.mm', '.swift', '.metal'):
                src.append(self.rel(f))
                if e == '.metal':
                    self.note(f'{os.path.basename(f)} is copied as text: the app must compile it itself (no default.metallib)')
            elif e in ('.xcdatamodeld', '.xcdatamodel', '.intentdefinition', '.mlmodel', '.mlpackage', '.y', '.l', '.rcproject', '.storyboard', '.xib'):
                self.fail(f'{os.path.basename(f)}: {e} files cannot be compiled here')
            elif e not in ('.h', '.hpp', '.hh', '.pch', '.md', '.docc'):
                self.fail(f'{os.path.basename(f)}: {e} files cannot be compiled here')

        def resource(f):
            b, e = os.path.basename(f), os.path.splitext(f)[1].lower()
            if not os.path.exists(f):
                self.fail(f'{self.rel(f)} is not there')
            elif e == '.xcassets':
                self.assets(f, res, icons, get('ASSETCATALOG_COMPILER_APPICON_NAME', 'AppIcon'))
            elif e in ('.storyboard', '.xib'):
                if 'launch' in b.lower():
                    self.note(f'{b} is left out: a plain launch screen is used')
                else:
                    self.fail(f'{b}: storyboards cannot be compiled here')
            elif e in ('.xcdatamodeld', '.xcdatamodel', '.mlmodel', '.mlpackage', '.scnassets', '.sks', '.rcproject', '.intentdefinition'):
                self.fail(f'{b}: {e} files cannot be compiled here')
            elif e in ('.xcstrings', '.icon'):
                self.note(f'{b} is left out: {e} files cannot be compiled here')
            else:
                d = os.path.basename(os.path.dirname(f))
                to = d + '/' + b if d.endswith('.lproj') else b
                res.append(self.rel(f) + ('' if to == b else ':' + to))

        built = {self.o.get(t.get('productReference'), {}).get('path'): t for t in self.targets if t.get('productType') in ('com.apple.product-type.library.static', 'com.apple.product-type.framework', 'com.apple.product-type.framework.static')}

        def framework(f, w=None):
            b, e = os.path.splitext(os.path.basename(f))
            if w is None and os.path.basename(f) in built:
                needs.extend(mods.library(built[os.path.basename(f)]))
                return
            if w and e in ('.a', '.dylib', '.tbd') and os.path.exists(w):
                libs.append(self.rel(w))
            elif e == '.framework' and (w is None or '/System/Library/' in w or not os.path.exists(w)):
                fw.append(b)
            elif e in ('.tbd', '.dylib') and w is None:
                libs.append(b[3:] if b.startswith('lib') else b)
            else:
                self.fail(f'{os.path.basename(f)}: only system frameworks and .a libraries of the project can be linked here')

        info_file = get('INFOPLIST_FILE')
        info_file = os.path.join(self.base, info_file) if info_file else None
        for f in self.synced(target):
            e = os.path.splitext(f)[1].lower()
            if e in ('.h', '.hpp', '.hh', '.inc', '.modulemap', '.pch'):
                heads.add(os.path.dirname(f))
            elif e in ('.c', '.s', '.cpp', '.cc', '.cxx', '.c++', '.m', '.mm', '.swift', '.metal', '.xcdatamodeld', '.intentdefinition', '.mlmodel', '.mlpackage', '.y', '.l'):
                source(f)
            elif e in ('.a', '.framework', '.xcframework', '.dylib', '.tbd'):
                framework(f, f)
            elif e in ('.entitlements', '.xcconfig', '.docc', '.xcodeproj', '.playground') or (info_file and os.path.normpath(info_file) == os.path.normpath(f)):
                continue
            else:
                resource(f)
        for ph in target.get('buildPhases', []):
            ph = self.o.get(ph, {})
            isa = ph.get('isa')
            if isa == 'PBXShellScriptBuildPhase':
                self.note(f'the script "{ph.get("name", "Run Script")}" is not run: if it makes files the app needs, run it first')
                continue
            if isa == 'PBXCopyFilesBuildPhase' and ph.get('files') and all(self.o.get(self.o.get(b, {}).get('fileRef'), {}).get('path') in built or self.o.get(b, {}).get('productRef') for b in ph['files']):
                continue  # what is linked in needs no copy
            if isa == 'PBXCopyFilesBuildPhase' and ph.get('files'):
                self.note(f'"{ph.get("name", "Copy Files")}" is not done (embedded extensions and frameworks)')
                continue
            for b in ph.get('files', []):
                b = self.o.get(b, {})
                flt = b.get('platformFilters', []) + ([b['platformFilter']] if 'platformFilter' in b else [])
                if flt and 'ios' not in flt:
                    continue
                if b.get('productRef'):
                    product(self.o.get(b['productRef'], {}).get('productName', '?'))
                    continue
                r = b.get('fileRef')
                f = self.o.get(r, {})
                each = f.get('children', []) if f.get('isa') == 'PBXVariantGroup' else [r]
                for x in each:
                    d, w = self.o.get(x, {}), self.where.get(x)
                    nm = d.get('path') or d.get('name', '?')
                    if isa == 'PBXFrameworksBuildPhase':
                        framework(nm, w)
                    elif isa == 'PBXHeadersBuildPhase':
                        if w and 'Public' in b.get('settings', {}).get('ATTRIBUTES', []):
                            public.append(w)
                    elif w is None:
                        if isa in ('PBXSourcesBuildPhase', 'PBXResourcesBuildPhase'):
                            self.fail(f'{nm}: not a file of the project')
                    elif isa == 'PBXSourcesBuildPhase':
                        source(w, b.get('settings', {}).get('COMPILER_FLAGS', ''))
                    elif isa == 'PBXResourcesBuildPhase':
                        resource(w)
        if target.get('buildRules'):
            self.fail('build rules of its own')
        for d in target.get('dependencies', []):
            t = self.o.get(self.o.get(d, {}).get('target'), {})
            kind = t.get('productType', '')
            if t and any(b['id'] == self.o[d]['target'] for b in built.values()):
                needs.extend(mods.library(next(b for b in built.values() if b['id'] == self.o[d]['target'])))
            elif self.o.get(d, {}).get('productRef'):
                product(self.o.get(self.o[d]['productRef'], {}).get('productName', '?'))
            elif t:
                (self.note if 'extension' in kind or 'watch' in kind else self.fail)(f'the target {t.get("name")} it depends on is not built here')
        for i in target.get('packageProductDependencies', []):
            product(self.o.get(i, {}).get('productName', '?'))
        for i, f in self.o.items():
            if f.get('isa') == 'PBXFileReference' and self.where.get(i) and self.where[i].endswith(('.h', '.hpp', '.hh', '.inc')):
                heads.add(os.path.dirname(self.where[i]))

        # Settings
        flags, cf, cxx, sw = [], [], [], []
        opt = get('GCC_OPTIMIZATION_LEVEL', 's')
        flags.append('-O' + (opt or 's'))
        for k, opt in (('HEADER_SEARCH_PATHS', '-I'), ('USER_HEADER_SEARCH_PATHS', '-iquote'), ('SYSTEM_HEADER_SEARCH_PATHS', '-isystem')):
            for d in many(k):
                d = d.strip('"')
                top = os.path.normpath(os.path.join(self.base, d[:-3] if d.endswith('/**') else d))
                if not os.path.isdir(top):
                    continue
                for x in [top] + ([r for r, ds, fs in os.walk(top) if r != top] if d.endswith('/**') else []):
                    flags += [opt + '$KIT/' + self.rel(x)] if opt != '-isystem' else [opt, '$KIT/' + self.rel(x)]
                    if opt == '-I':
                        sw += ['-Xcc', '-I$KIT/' + self.rel(x)]
        for d in sorted(heads):
            flags.append('-iquote$KIT/' + self.rel(d))
            sw += ['-Xcc', '-iquote$KIT/' + self.rel(d)]
        defs = many('GCC_PREPROCESSOR_DEFINITIONS')
        flags += ['-D' + d for d in defs]
        sw += [x for d in defs for x in ('-Xcc', '-D' + d)]
        if get('GCC_PREFIX_HEADER'):
            flags += ['-include', '$KIT/' + self.rel(get('GCC_PREFIX_HEADER'))]
        flags += [x for x in many('OTHER_CFLAGS') if x not in ('-g', '-fmodules')]
        for k, to, skip in (('GCC_C_LANGUAGE_STANDARD', cf, 'gnu17'), ('CLANG_CXX_LANGUAGE_STANDARD', cxx, 'gnu++17')):
            v = get(k)
            if v and v != 'compiler-default':
                to.append('-std=' + v)
        if 'OTHER_CPLUSPLUSFLAGS' in raw:
            cxx += [x for x in many('OTHER_CPLUSPLUSFLAGS') if x not in many('OTHER_CFLAGS')]
        for k, f in (('GCC_ENABLE_CPP_EXCEPTIONS', '-fno-exceptions'), ('GCC_ENABLE_CPP_RTTI', '-fno-rtti')):
            if get(k) == 'NO':
                cxx.append(f)
        if get('CLANG_ENABLE_OBJC_ARC') != 'YES':
            cf.append('-fno-objc-arc')
            cxx.append('-fno-objc-arc')
        if yes('CLANG_ENABLE_OBJC_WEAK'):
            cf.append('-fobjc-weak')
            cxx.append('-fobjc-weak')
        if yes('CLANG_ENABLE_MODULES'):
            cf += ['-fmodules', '-fmodules-cache-path=$KIT/build/clang-modules']
        needs = list(dict.fromkeys(needs))
        if needs and not is_lib:
            cc, more = mods.uses(needs)
            cc, more = ([x.replace(self.root, '$KIT') for x in a] for a in (cc, more))
            cf += cc
            flags_cxx = [c for c in cc if not c.startswith('-fmodule-map')]
            cxx += flags_cxx
            sw += more
            fw += mods.fw
            libs += mods.libs
            res += mods.res
        if any(f.endswith('.swift') for f in src):
            so = get('SWIFT_OPTIMIZATION_LEVEL') or '-O'
            sw.insert(0, so)
            sw += [x for c in many('SWIFT_ACTIVE_COMPILATION_CONDITIONS') for x in ('-D', c)]
            v = get('SWIFT_VERSION')
            if v:
                sw += ['-swift-version', v.split('.')[0] if v.split('.')[0] in ('4', '5', '6') else '5']
            if get('SWIFT_DEFAULT_ACTOR_ISOLATION') == 'MainActor':
                sw += ['-default-isolation', 'MainActor']
            up = []
            if yes('SWIFT_APPROACHABLE_CONCURRENCY'):
                up += ['DisableOutwardActorInference', 'GlobalActorIsolatedTypesUsability', 'InferIsolatedConformances', 'InferSendableFromCaptures', 'NonisolatedNonsendingByDefault']
            for k in raw:
                if k.startswith('SWIFT_UPCOMING_FEATURE_') and yes(k):
                    up.append(''.join(w.capitalize() for w in k[23:].split('_')))
            six = v.startswith('6')
            for f in dict.fromkeys(up):
                if not (six and f in ('DisableOutwardActorInference', 'GlobalActorIsolatedTypesUsability', 'InferSendableFromCaptures', 'ConciseMagicFile', 'ForwardTrailingClosures', 'ImplicitOpenExistentials', 'DeprecateApplicationMain', 'ImportObjcForwardDeclarations', 'IsolatedDefaultValues', 'GlobalConcurrency', 'RegionBasedIsolation', 'DynamicActorIsolation', 'BareSlashRegexLiterals', 'NonfrozenEnumExhaustivity')):
                    sw += ['-enable-upcoming-feature', f]
            o = many('OTHER_SWIFT_FLAGS')
            while o:
                x = o.pop(0)
                if x == '-Xfrontend' and o:
                    sw.append(o.pop(0))
                elif x in ('-Xcc', '-D', '-enable-experimental-feature', '-enable-upcoming-feature', '-package-name') and o:
                    sw += [x, o.pop(0)]
                elif x.startswith('-D'):
                    sw += ['-D', x[2:]]
                else:
                    self.note(f'the Swift flag {x} is left out')
            if get('SWIFT_OBJC_BRIDGING_HEADER'):
                p['bridging_header'] = self.rel(get('SWIFT_OBJC_BRIDGING_HEADER'))
            mod = re.sub(r'[^A-Za-z0-9]', '_', name)
            if get('PRODUCT_MODULE_NAME') != mod:
                self.note(f'the Swift module is called {mod}')
            for f in src:
                if f.endswith(('.m', '.mm')) and re.search(r'#\s*import\s*["<][^">]*-Swift\.h[">]', text(os.path.join(self.root, f))):
                    self.fail(f'{f} imports the header Swift makes (-Swift.h), which is not made here')
        lf = many('OTHER_LDFLAGS')
        link = []
        while lf:
            x = lf.pop(0)
            if x in ('-framework', '-weak_framework') and lf:
                fw.append(lf.pop(0))
            elif x.startswith('-l'):
                libs.append(x[2:])
            elif x in ('-ObjC', '-all_load', '-dead_strip'):
                link += [x] if x == '-all_load' else []
            elif x.startswith('-Wl,'):
                link += x[4:].split(',')
            else:
                self.note(f'the linker flag {x} is left out')
        for d in many('LIBRARY_SEARCH_PATHS'):
            d = os.path.join(self.base, d.strip('"'))
            if os.path.isdir(d):
                link.append('-L$KIT/' + self.rel(d))
        p['sources'] = list(dict.fromkeys(src))
        if not src:
            self.fail('no source files')
        if any(f.endswith(('.m', '.mm', '.swift')) for f in src):
            fw += ['Foundation', 'UIKit', 'CoreGraphics']
        p['flags'], p['c_flags'], p['cxx_flags'], p['swift_flags'] = flags, cf, cxx, sw if any(f.endswith('.swift') for f in src) else []
        p['frameworks'], p['libraries'], p['link'] = list(dict.fromkeys(fw)), list(dict.fromkeys(libs)), link
        p['resources'] = list(dict.fromkeys(res))
        if icons:
            p['icon'] = icons[0]
        if is_lib:
            p.pop('icon', None)
            return p | {'needs': needs, 'public': public}
        p['info'] = self.info(info_file, get, raw, yes('GENERATE_INFOPLIST_FILE'))
        return {k: v for k, v in p.items() if v not in ([], {}, '')}

    def assets(self, top, res, icons, icon):
        """What can be had of an asset catalog without compiling it: the icon and the pictures."""
        import json

        def load(d):
            try:
                with open(d + '/Contents.json') as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {}
        for r, ds, fs in os.walk(top):
            ds.sort()
            e = os.path.splitext(r)[1]
            n = os.path.basename(r)[:-len(e)] if e else ''
            if e == '.appiconset' and n == icon:
                pics = [f for f in fs if f.lower().endswith('.png')]
                if pics:
                    icons.append(self.rel(os.path.join(r, max(pics, key=lambda f: os.path.getsize(os.path.join(r, f))))))
            elif e == '.imageset':
                got = False
                for i in load(r).get('images', []):
                    f = i.get('filename')
                    if not f or i.get('idiom', 'universal') not in ('universal', 'iphone') or not os.path.exists(os.path.join(r, f)):
                        continue
                    x = os.path.splitext(f)[1].lower()
                    if x not in ('.png', '.jpg', '.jpeg') or i.get('appearances'):
                        self.note(f'the picture {n} is left out or only in part: {f} (dark versions, PDF and SVG need the asset compiler)')
                        continue
                    sc = i.get('scale', '1x')
                    res.append(self.rel(os.path.join(r, f)) + ':' + n + ('' if sc == '1x' else '@' + sc) + x)
                    got = True
            elif e in ('.colorset', '.symbolset', '.dataset', '.spriteatlas', '.cubetextureset') and (e != '.colorset' or load(r).get('colors', [{}])[0].get('color')):
                self.note(f'{os.path.basename(r)} is left out: it needs the asset compiler')

    def info(self, path, get, raw, generate):
        import datetime
        import plistlib
        d = {}
        if path:
            try:
                with open(path, 'rb') as f:
                    d = plistlib.load(f)
            except FileNotFoundError:
                self.fail(f'{self.rel(path)} is not there')
            except Exception as e:
                self.fail(f'{self.rel(path)} cannot be read: {e}')
        for k in list(raw) if generate else []:
            if not k.startswith('INFOPLIST_KEY_'):
                continue
            n, v = k[14:], get(k)
            if n in ('UIApplicationSceneManifest_Generation', 'UIApplicationSupportsIndirectInputEvents_Generation'):
                if n.startswith('UIApplicationScene') and v == 'YES':
                    d.setdefault('UIApplicationSceneManifest', {'UIApplicationSupportsMultipleScenes': True, 'UISceneConfigurations': {}})
                continue
            if n == 'UILaunchScreen_Generation':
                d.setdefault('UILaunchScreen', {})
                continue
            n = re.sub(r'_iPhone$', '~iphone', re.sub(r'_iPad$', '~ipad', n))
            if n.startswith(('UISupportedInterfaceOrientations', 'UIBackgroundModes', 'UIRequiredDeviceCapabilities')):
                v = v.split()
            elif v in ('YES', 'NO'):
                v = v == 'YES'
            d[n] = v

        def clean(v):
            if isinstance(v, str):
                return self.expand(v, raw)
            if isinstance(v, list):
                return [clean(x) for x in v]
            if isinstance(v, dict):
                return {self.expand(k, raw): clean(x) for k, x in v.items()}
            if isinstance(v, (bytes, datetime.datetime)):
                self.fail('Info.plist holds data or a date')
            return v
        d = clean(d)
        if d.pop('UIMainStoryboardFile', None) or 'UISceneStoryboardFile' in repr(d):
            self.fail('its windows come from a storyboard, which cannot be compiled here')
        if d.pop('UILaunchStoryboardName', None):
            d.setdefault('UILaunchScreen', {})
        d.pop('CFBundleIconName', None)
        for k in ('CFBundleExecutable', 'CFBundlePackageType', 'CFBundleInfoDictionaryVersion'):
            d.pop(k, None)
        v = get('CURRENT_PROJECT_VERSION')
        d.setdefault('CFBundleVersion', v or '1')
        fam = [int(x) for x in re.findall(r'\d+', get('TARGETED_DEVICE_FAMILY', '1')) if x in '12']
        d.setdefault('UIDeviceFamily', fam or [1])
        return {k: v for k, v in d.items() if v != ''}


class SwiftManifest:
    """What a Package.swift says, read from its text: plain literals and calls, as nearly all packages have them."""
    TOK = re.compile(r'\s+|//[^\n]*|/\*.*?\*/|"""(.*?)"""|"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*)|(\d[\d._]*)|([.\[\](){},:=+?!<>&|-]|#\w+|@\w+)', re.S)

    def __init__(self, path):
        s = self.text = text(path)
        self.path, self.vars = path, {}
        m = re.match(r'\s*//\s*swift-tools-version\s*:\s*([\d.]+)', s)
        self.tools = m[1] if m else '5.0'
        self.t, i = [], 0
        while i < len(s):
            m = self.TOK.match(s, i)
            if not m:
                i += 1
                self.t.append(('x', s[i - 1]))
                continue
            for k, g in (('s', 1), ('s', 2), ('i', 3), ('n', 4), ('p', 5)):
                if m[g] is not None:
                    self.t.append((k, m[g]))
                    break
            i = m.end()
        if any(k == 'p' and v == '#if' for k, v in self.t):
            raise Fail(f'{path}: a package that decides with #if is not read here', 1)
        for i, (k, v) in enumerate(self.t):
            if v == 'let' and k == 'i' and i + 1 < len(self.t):
                j = i + 2
                while j < len(self.t) and self.t[j] != ('p', '=') and self.t[j][1] not in ('let', 'var'):
                    j += 1
                try:
                    self.vars.setdefault(self.t[i + 1][1], self.expr(j + 1)[0])
                except (IndexError, ValueError):
                    pass
        self.package = next((v for v in self.vars.values() if isinstance(v, tuple) and v[0] == 'Package'), None)
        if not self.package:
            raise Fail(f'{path}: it cannot be read here', 1)
        m = re.search(r'\b(package|\w+)\.(targets|dependencies|products)\s*(\.append|\+=)', s)
        if m:
            raise Fail(f'{path}: a package changed after it is made is not read here', 1)

    def expr(self, i):
        v, i = self.one(i)
        while self.t[i:i + 1] == [('p', '+')]:
            w, i = self.one(i + 1)
            if not isinstance(v, list) or not isinstance(w, list):
                raise ValueError
            v = v + w
        return v, i

    def one(self, i):
        k, v = self.t[i]
        i += 1
        if k == 's':
            return v.replace('\\"', '"').replace('\\\\', '\\'), i
        if k == 'n':
            return v, i
        if (k, v) == ('p', '['):
            a, d = [], {}
            if self.t[i] == ('p', ':'):
                return {}, i + 2
            while self.t[i] != ('p', ']'):
                x, i = self.expr(i)
                if self.t[i] == ('p', ':'):
                    y, i = self.expr(i + 1)
                    d[str(x)] = y
                else:
                    a.append(x)
                if self.t[i] == ('p', ','):
                    i += 1
            return (d or a), i + 1
        name = ''
        if (k, v) == ('p', '.'):
            k, v = self.t[i]
            i += 1
        if k != 'i':
            raise ValueError
        name = v
        while self.t[i:i + 1] == [('p', '.')] and self.t[i + 1][0] == 'i':
            name, i = self.t[i + 1][1], i + 2
        if self.t[i:i + 1] == [('p', '(')]:
            i += 1
            args = []
            while self.t[i] != ('p', ')'):
                lab = ''
                if self.t[i][0] == 'i' and self.t[i + 1] == ('p', ':'):
                    lab, i = self.t[i][1], i + 2
                x, i = self.expr(i)
                while self.t[i] not in (('p', ','), ('p', ')')):  # 1.0..<2.0 and the like
                    i += 1
                args.append((lab, x))
                if self.t[i] == ('p', ','):
                    i += 1
            return (name, args), i + 1
        if name in ('true', 'false'):
            return name == 'true', i
        return self.vars.get(name, (name, [])), i


def xc_arg(call, label, default=None):
    if not isinstance(call, tuple):
        return default
    return next((v for k, v in call[1] if k == label), default)


def xc_for_ios(call):
    c = next((v for k, v in call[1] if isinstance(v, tuple) and v[0] == 'when'), None)
    p = xc_arg(c, 'platforms')
    return p is None or any(isinstance(x, tuple) and x[0] == 'iOS' for x in p)


class XcModules:
    """The packages and library targets an app uses, each built as a module of its own; the app links their objects."""
    def __init__(self, project):
        self.x, self.root = project, project.root
        self.out = os.path.join(project.root, 'build', 'modules')
        self.mods, self.order, self.packages, self.res, self.fw, self.libs = {}, [], {}, [], [], []

    # -- packages --

    def package(self, folder, name=None):
        folder = os.path.normpath(folder)
        if folder not in self.packages:
            f = os.path.join(folder, 'Package.swift')
            if not os.path.exists(f):
                raise Fail(f'{folder} has no Package.swift', 1)
            m = SwiftManifest(f)
            self.packages[folder] = m
            m.folder, m.name = folder, xc_arg(m.package, 'name', os.path.basename(folder))
            m.ids = {m.name.lower(), os.path.basename(folder).lower()} | ({name.lower()} if name else set())
            m.targets = {xc_arg(t, 'name'): t for t in xc_arg(m.package, 'targets', []) if isinstance(t, tuple)}
            m.products = {xc_arg(p, 'name'): p for p in xc_arg(m.package, 'products', []) if isinstance(p, tuple)}
            m.deps = []
            for d in xc_arg(m.package, 'dependencies', []):
                if xc_arg(d, 'path'):
                    m.deps.append(lambda p=xc_arg(d, 'path'): self.package(os.path.join(folder, p)))
                elif xc_arg(d, 'url'):
                    m.deps.append(lambda d=d: self.remote(xc_arg(d, 'url'), self.wanted(d)))
        return self.packages[folder]

    @staticmethod
    def wanted(d):
        """(kind, value) of a dependency's version."""
        for lab, v in d[1]:
            if lab in ('from', 'exact', 'branch', 'revision'):
                return lab, v
            if isinstance(v, tuple) and v[0] in ('upToNextMajor', 'upToNextMinor', 'exact', 'branch', 'revision'):
                return ('from' if v[0] == 'upToNextMajor' else v[0]), xc_arg(v, 'from') or v[1][0][1]
            if lab == '' and isinstance(v, str):
                return 'from', v
        return 'any', ''

    def remote(self, url, want):
        m = re.match(r'(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$', url)
        if not m:
            raise Fail(f'the package {url} is not on GitHub: put it in a folder of the project as a local package', 1)
        owner, repo = m[1], m[2]
        to = os.path.join(self.root, '.henge-packages', repo)
        if os.path.exists(to + '/Package.swift'):
            return self.package(to, repo)  # got before: its folder is removed to get it anew
        import io
        import json
        import shutil
        import urllib.request
        import zipfile

        def get(u):
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'henge'}), timeout=60) as r:
                return r.read()
        kind, v = want
        ref = v if kind in ('branch', 'revision') else None
        api, code = os.environ.get('HENGE_GITHUB_API', 'https://api.github.com'), os.environ.get('HENGE_GITHUB_CODE', 'https://codeload.github.com')
        try:
            ref = self.pinned(owner, repo) or ref
            if not ref:
                num = lambda t: tuple(int(n) for n in (re.findall(r'\d+', t) + ['0', '0', '0'])[:3])
                tags, page = {}, 1
                while page < 6:
                    got = json.loads(get(f'{api}/repos/{owner}/{repo}/tags?per_page=100&page={page}'))
                    tags |= {num(t['name']): t['name'] for t in got if re.fullmatch(r'v?\d+(\.\d+){0,2}', t['name'])}
                    if len(got) < 100:
                        break
                    page += 1
                low = num(v)
                ok = [t for t in tags if kind == 'any' or (t == low if kind == 'exact' else t >= low and t[:2 if kind == 'upToNextMinor' else 1] == low[:2 if kind == 'upToNextMinor' else 1])]
                if not ok:
                    raise Fail(f'the package {repo} has no version for {kind} {v}', 1)
                ref = tags[max(ok)]
            put(f'getting the package {owner}/{repo} {ref}')
            State.out.flush()
            z = zipfile.ZipFile(io.BytesIO(get(f'{code}/{owner}/{repo}/zip/{ref}')))
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            raise Fail(f'the package {owner}/{repo} could not be got: {e}', 1) from None
        part = to + '.part'
        shutil.rmtree(part, ignore_errors=True)
        for n in z.namelist():
            rel = n.split('/', 1)[1] if '/' in n else ''
            if rel and not n.endswith('/'):
                f = os.path.normpath(os.path.join(part, rel))
                if not f.startswith(part + os.sep):
                    raise Fail(f'the package {repo} holds a bad path', 1)
                os.makedirs(os.path.dirname(f), exist_ok=True)
                with open(f, 'wb') as h:
                    h.write(z.read(n))
        os.rename(part, to)
        return self.package(to, repo)

    def pinned(self, owner, repo):
        """The revision the project's Package.resolved names."""
        import glob
        import json
        for f in glob.glob(glob.escape(self.x.path) + '/**/Package.resolved', recursive=True):
            try:
                o = json.loads(text(f))
            except ValueError:
                continue
            for p in o.get('pins') or o.get('object', {}).get('pins', []):
                u = (p.get('location') or p.get('repositoryURL') or '').lower()
                if re.search(rf'[/:]{re.escape(owner.lower())}/{re.escape(repo.lower())}(\.git)?/?$', u):
                    return p.get('state', {}).get('revision')
        return None

    def product(self, name, near=None, package=None):
        """The modules of a package's product, built before the one that asks."""
        todo = [near] if near else []
        todo += [p for p in self.packages.values() if p not in todo]
        for p in todo:
            if package and package.lower() not in p.ids:
                continue
            if name in p.products:
                if p.products[name][0] == 'plugin':
                    raise Fail(f'the package plugin {name} is not run here', 1)
                return [m for t in xc_arg(p.products[name], 'targets', []) for m in self.target(p, t)]
        raise Fail(f'no package here has the product {name}', 1)

    def target(self, p, name):
        """A package's target; returns the names of its module and all it needs."""
        key = (p.folder, name)
        if key in self.mods:
            return self.mods[key]['all']
        t = p.targets.get(name)
        if not t or t[0] not in ('target', 'executableTarget'):
            raise Fail(f'the package target {name} cannot be built here ({t[0] if t else "not found"})', 1)
        if any(isinstance(d, tuple) for d in xc_arg(t, 'plugins', [])):
            self.x.note(f'the build plugins of {name} are not run')
        if any(m['name'] == name for m in self.mods.values()):
            raise Fail(f'two modules are called {name}', 1)
        self.mods[key] = {'name': name, 'all': []}
        need = []
        for d in xc_arg(t, 'dependencies', []):
            if isinstance(d, str):
                d = ('byName', [('name', d)])
            if not xc_for_ios(d):
                continue
            n = xc_arg(d, 'name')
            if d[0] == 'target' or (d[0] == 'byName' and n in p.targets):
                need += self.target(p, n)
            else:
                for get in p.deps:
                    get()
                need += self.product(n, None, xc_arg(d, 'package'))
        need = list(dict.fromkeys(need))
        where = xc_arg(t, 'path')
        if where is None:
            where = next((d + '/' + name for d in ('Sources', 'Source', 'src', 'srcs') if os.path.isdir(os.path.join(p.folder, d, name))), None)
            if where is None and len(p.targets) == 1 and os.path.isdir(os.path.join(p.folder, 'Sources')):
                where = 'Sources'
            if where is None:
                raise Fail(f'the sources of {name} are not there', 1)
        top = os.path.normpath(os.path.join(p.folder, where))
        gone = [os.path.normpath(os.path.join(top, e)) for e in xc_arg(t, 'exclude', [])]
        only = [os.path.normpath(os.path.join(top, e)) for e in xc_arg(t, 'sources', [])]
        under = lambda f, ds: any(f == d or f.startswith(d + os.sep) for d in ds)
        src, res = [], []
        for r in xc_arg(t, 'resources', []):
            f = os.path.normpath(os.path.join(top, r[1][0][1]))
            if r[0] == 'embedInCode' or not os.path.exists(f):
                raise Fail(f'the resource {r[1][0][1]} of {name} cannot be had here', 1)
            res.append((r[0], f))
        for r, ds, fs in os.walk(top):
            ds[:] = sorted(d for d in ds if not d.startswith('.') and not under(os.path.join(r, d), gone) and not d.endswith(('.docc', '.xcassets', '.bundle')))
            for f in sorted(fs):
                f = os.path.join(r, f)
                if under(f, gone) or (only and not under(f, only)) or under(f, [x for k, x in res if k == 'copy']):
                    continue
                if os.path.splitext(f)[1].lower() in ('.swift', '.c', '.m', '.mm', '.cpp', '.cc', '.cxx', '.s'):
                    src.append(f)
        sw = any(f.endswith('.swift') for f in src)
        if sw and not all(f.endswith('.swift') for f in src):
            raise Fail(f'{name} mixes Swift and C sources', 1)
        m = self.new(key, name, src, need)
        m['flags'] += ['-O2', '-DSWIFT_PACKAGE=1']
        major = int(p.tools.split('.')[0])
        m['swift'] += ['-O', '-D', 'SWIFT_PACKAGE', '-package-name', re.sub(r'\W', '_', p.name.lower()), '-swift-version', '6' if major >= 6 else '5']
        inc = os.path.normpath(os.path.join(top, xc_arg(t, 'publicHeadersPath', 'include')))
        if not sw and os.path.isdir(inc):
            self.clang_module(m, inc)
        for group in ('cSettings', 'cxxSettings', 'swiftSettings', 'linkerSettings'):
            for s in xc_arg(t, group, []):
                if not isinstance(s, tuple) or not xc_for_ios(s):
                    continue
                c = next((v for k, v in s[1] if isinstance(v, tuple) and v[0] == 'when'), None)
                if c and isinstance(xc_arg(c, 'configuration'), tuple) and xc_arg(c, 'configuration')[0].lower() != 'release':
                    continue
                a = s[1][0][1] if s[1] else ''
                to = {'cSettings': m['c'], 'cxxSettings': m['cxx'], 'swiftSettings': m['swift']}.get(group)
                if s[0] == 'define':
                    to += ['-D', a] if group == 'swiftSettings' else ['-D' + a + ('=' + xc_arg(s, 'to') if isinstance(xc_arg(s, 'to'), str) else '')]
                elif s[0] == 'headerSearchPath':
                    to.append('-I' + os.path.join(top, a))
                elif s[0] in ('enableUpcomingFeature', 'enableExperimentalFeature'):
                    to += ['-enable-upcoming-feature' if 'Up' in s[0] else '-enable-experimental-feature', a]
                elif s[0] in ('swiftLanguageMode', 'swiftLanguageVersion') and isinstance(a, tuple):
                    to += ['-swift-version', a[0][1:]]
                elif s[0] == 'defaultIsolation':
                    to += ['-default-isolation', 'MainActor'] if isinstance(a, tuple) and a[0] == 'self' else []
                elif s[0] == 'interoperabilityMode':
                    to += ['-cxx-interoperability-mode=default'] if a[0] == 'Cxx' else []
                elif s[0] == 'linkedFramework':
                    self.fw.append(a)
                elif s[0] == 'linkedLibrary':
                    self.libs.append(a)
                elif s[0] == 'strictMemorySafety':
                    pass
                elif s[0] == 'unsafeFlags' and group in ('cSettings', 'cxxSettings') and isinstance(a, list):
                    to += a
                else:
                    self.x.note(f'the setting {s[0]} of {name} is left out')
        for k, old in (('cxxLanguageStandard', m['cxx']), ('cLanguageStandard', m['c'])):
            v = xc_arg(p.package, k)
            if isinstance(v, tuple):
                old.append('-std=' + re.sub(r'^gnucxx|^gnuxx', 'gnu++', re.sub(r'^cxx', 'c++', v[0])))
        if res:
            b = f'{p.name}_{name}.bundle'
            for kind, f in res:
                each = [f]
                if kind == 'process' and os.path.isdir(f) and not f.endswith(('.xcassets', '.bundle', '.lproj')):
                    each = [os.path.join(r, x) for r, ds, fs in os.walk(f) for x in fs if not x.startswith('.')]
                for x in each:
                    if x.endswith(('.xcassets', '.storyboard', '.xib', '.xcdatamodeld', '.metal', '.xcstrings')) or '.xcassets/' in x:
                        self.x.note(f'{os.path.basename(x)} of {name} is left out: it needs a compiler that is not here')
                    elif not x.endswith('.swift'):
                        d = os.path.basename(os.path.dirname(x))
                        self.res.append(self.x.rel(x) + ':' + b + '/' + (d + '/' if d.endswith('.lproj') else '') + os.path.basename(x))
            if sw:
                m['extra'] = ('import Foundation\nextension Foundation.Bundle { nonisolated static let module: Bundle = '
                              f'Bundle(url: Bundle.main.bundleURL.appendingPathComponent("{b}")) ?? Bundle.main }}\n')
        return m['all']

    # -- both --

    def new(self, key, name, src, need):
        mod = re.sub(r'\W', '_', name)
        m = {'name': name, 'mod': mod, 'src': src, 'need': need, 'all': list(dict.fromkeys(need + [mod])), 'flags': [], 'c': [], 'cxx': [], 'swift': [], 'extra': '', 'cc': [], 'map': None}
        self.mods[key] = m
        self.order.append(m)
        return m

    def clang_module(self, m, inc, headers=None):
        """How others import a C module: its folder of headers and a module map, its own or one made here."""
        own = os.path.join(inc, 'module.modulemap')
        m['cc'] = ['-I' + inc]
        if not os.path.exists(own):
            um = next((h for h in (f'{inc}/{m["mod"]}.h', f'{inc}/{m["mod"]}/{m["mod"]}.h') if os.path.exists(h)), None)
            m['map'] = f'module {m["mod"]} {{\n  umbrella {"header " if um else ""}"{um or inc}"\n  export *\n' + ('' if um else '  module * { export * }\n') + '}\n'
            own = os.path.join(self.out, m['mod'] + '.modulemap')
        m['cc'].append('-fmodule-map-file=' + own)

    def library(self, t):
        """A static library or framework target of the project itself."""
        key = ('', t['id'])
        if key in self.mods:
            return self.mods[key]['all']
        self.mods[key] = {'name': t['name'], 'all': []}
        p = self.x.convert(t, self)
        full = lambda a: a.replace('$KIT', self.root)
        src = [os.path.join(self.root, s) for s in p.get('sources', []) if not s.endswith('.metal')]
        sw = any(f.endswith('.swift') for f in src)
        if sw and not all(f.endswith('.swift') for f in src):
            raise Fail(f'the library {t["name"]} mixes Swift and C sources', 1)
        m = self.new(key, p['name'], src, p['needs'])
        for a, b in (('flags', 'flags'), ('c', 'c_flags'), ('cxx', 'cxx_flags'), ('swift', 'swift_flags')):
            m[a] += [full(x) for x in p.get(b, [])]
        self.res += p.get('resources', [])
        self.fw += p.get('frameworks', [])
        self.libs += p.get('libraries', [])
        heads = p['public']
        if heads and not sw:
            inc = os.path.join(self.out, 'include', m['mod'])
            m['copy'] = [(h, os.path.join(inc, m['mod'], os.path.basename(h))) for h in heads]
            m['cc'] = ['-I' + inc]
            um = os.path.join(inc, m['mod'], m['mod'] + '.h')
            has = any(to == um for h, to in m['copy'])
            m['map'] = f'module {m["mod"]} {{\n  umbrella {"header " if has else ""}"{um if has else os.path.join(inc, m["mod"])}"\n  export *\n' + ('' if has else '  module * { export * }\n') + '}\n'
            m['cc'].append('-fmodule-map-file=' + os.path.join(self.out, m['mod'] + '.modulemap'))
        return m['all']

    def uses(self, names):
        """Compiler arguments with which Swift and C sources find modules."""
        cc = [x for m in self.order if m['mod'] in names for x in m['cc']]
        return cc, (['-I', self.out] if names else []) + [x for c in cc for x in ('-Xcc', c)]

    def build(self, min_ios):
        """Builds what changed; returns (objects, a hash of it all)."""
        import concurrent.futures
        import hashlib
        import json
        import shutil
        import subprocess
        tools = xc_tools()
        os.makedirs(self.out, exist_ok=True)
        res = sorted(os.listdir(tools + '/lib/clang'))[-1]
        tgt = 'arm64-apple-ios' + min_ios
        state_file, objs, sums = os.path.join(self.out, 'state.json'), [], {}
        try:
            with open(state_file) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}

        def digest(f):
            try:
                with open(f, 'rb') as h:
                    return hashlib.sha256(h.read()).hexdigest()
            except OSError:
                return ''
        key = lambda *a: hashlib.sha256(json.dumps(a).encode()).hexdigest()

        def run(a):
            return subprocess.run(a, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.root)
        try:
            for m in self.order:
                for a, b in m.get('copy', []):
                    os.makedirs(os.path.dirname(b), exist_ok=True)
                    if digest(a) != digest(b):
                        shutil.copyfile(a, b)
                if m['map']:
                    f = os.path.join(self.out, m['mod'] + '.modulemap')
                    if not os.path.exists(f) or text(f) != m['map']:
                        with open(f, 'w') as h:
                            h.write(m['map'])
                cc, sw = self.uses(m['all'])
                base = [sums.get(n, '') for n in m['need']]
                swift = [f for f in m['src'] if f.endswith('.swift')]
                if swift:
                    if m['extra']:
                        f = os.path.join(self.out, m['mod'] + '-bundle.swift')
                        with open(f, 'w') as h:
                            h.write(m['extra'])
                        swift.append(f)
                    fx, obj = '/henge/toolchain', os.path.join(self.out, m['mod'] + '.o')
                    a = ['-frontend', '-c'] + swift + ['-module-name', m['mod'], '-target', tgt, '-sdk', fx + '/sdk/iPhoneOS.sdk', '-resource-dir', fx + '/lib/swift', '-enable-objc-interop',
                         '-no-color-diagnostics', '-in-process-plugin-server-path', fx + '/lib/swift/host/libSwiftInProcPluginServer.dylib', '-plugin-path', fx + '/lib/swift/host/plugins',
                         '-module-cache-path', fx + '/module-cache', '-parse-as-library', '-suppress-warnings', '-emit-module-path', os.path.join(self.out, m['mod'] + '.swiftmodule')] + sw + m['swift']
                    k = key(a, [digest(f) for f in swift], base)
                    if state.get(m['mod']) != k or not os.path.exists(obj) or not os.path.exists(obj[:-2] + '.swiftmodule'):
                        put(f'building {m["mod"]} ({len(swift)} Swift file(s))')
                        State.out.flush()
                        state.pop(m['mod'], None)
                        r = run([tools + '/bin/swift-frontend'] + a + ['-o', obj])
                        if r.returncode:
                            outb(r.stdout[-8000:])
                            raise Fail(f'the module {m["mod"]} did not compile', 65)
                        state[m['mod']] = k
                    sums[m['mod']] = k
                    objs.append(obj)
                    continue
                todo, total = [], []
                for f in m['src']:
                    e = os.path.splitext(f)[1].lower()
                    obj = os.path.join(self.out, m['mod'], self.x.rel(f).replace('../', 'up/') + '.o')
                    a = [tools + '/bin/clang'] + (['--driver-mode=g++'] if e in ('.cpp', '.cc', '.cxx', '.mm') else []) + ['-target', tgt, '-isysroot', tools + '/sdk/iPhoneOS.sdk', '-resource-dir', tools + '/lib/clang/' + res]
                    a += m['flags'] + (['-fobjc-arc'] if e in ('.m', '.mm') else []) + (m['cxx'] if e in ('.cpp', '.cc', '.cxx', '.mm') else m['c'])
                    if e == '.m':
                        a += ['-fmodules', '-fmodules-cache-path=' + os.path.join(self.root, 'build', 'clang-modules')]
                    a += [c for c in cc if e == '.m' or not c.startswith('-fmodule-map')] + ['-w', '-fintegrated-cc1', '-fno-color-diagnostics', '-MMD', '-MF', obj + '.d', '-c', f, '-o', obj]
                    old = state.get(obj, {})
                    objs.append(obj)
                    if old.get('key') == key(a) and os.path.exists(obj) and all(digest(d) == h for d, h in old['deps'].items()):
                        total.append(old['deps'])
                    else:
                        todo.append((f, obj, a))

                def one(j):
                    os.makedirs(os.path.dirname(j[1]), exist_ok=True)
                    return j, run(j[2])
                if todo:
                    put(f'building {m["mod"]} ({len(todo)} file(s))')
                    State.out.flush()
                with concurrent.futures.ThreadPoolExecutor(3) as pool:
                    for (f, obj, a), r in pool.map(one, todo):
                        state.pop(obj, None)
                        if r.returncode:
                            outb(r.stdout[-8000:])
                            raise Fail(f'{self.x.rel(f)} did not compile', 65)
                        ds = [w.replace('\\ ', ' ') for w in re.findall(r'(?:\\.|\S)+', text(obj + '.d').replace('\\\n', ' ').split(': ', 1)[1])]
                        state[obj] = {'key': key(a), 'deps': {d: digest(d) for d in ds if d.startswith(self.root + os.sep) or not d.startswith(tools)}}
                        total.append(state[obj]['deps'])
                heads = [h for c in m['cc'] if c.startswith('-I') for r, ds, fs in os.walk(c[2:]) for h in (os.path.join(r, f) for f in fs)]
                sums[m['mod']] = key(sorted(str(sorted(d.items())) for d in total), [digest(h) for h in sorted(heads)], m['map'], m['flags'], m['c'], m['cxx'], base)
        finally:
            with open(state_file, 'w') as f:
                json.dump(state, f)
        return objs, key(sums)[:16]


def xc_build(folder):
    """Ask Henge's builder (its tool server) for a build of a folder. Returns (worked, text)."""
    import http.client
    import json
    port, _, secret = os.environ.get('HENGE_BUILD', '').partition(' ')
    if not secret:
        raise Fail('Henge\'s builder is not there: this works in Henge\'s terminal with Claude', 1)
    c = http.client.HTTPConnection('127.0.0.1', int(port))
    try:
        c.request('POST', '/mcp', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'build', 'arguments': {'folder': folder}}}),
                  {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret})
        r = json.loads(c.getresponse().read())
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise Fail(f'Henge\'s builder did not answer: {e}', 1) from None
    finally:
        c.close()
    r = r.get('result', {})
    return not r.get('isError', True), ''.join(x.get('text', '') for x in r.get('content', []))


XC_HELP = '''usage: xcodebuild [-project X.xcodeproj] [-target NAME | -scheme NAME] [-configuration NAME] [build | clean] [SETTING=VALUE...]
       xcodebuild -list | -showBuildSettings | -showsdks | -version
Henge's own, not Apple's. It writes henge-project.json beside the Xcode project and builds that: one iPhone app of Swift,
C, C++ and Objective-C sources, with the static libraries, frameworks and Swift packages it uses (of plain sources: no
plugins, macros or binaries; those on GitHub are got into .henge-packages), as an unsigned IPA. Not here: the simulator,
tests, archives, signing, storyboards, Core Data models, extensions, build scripts. Of an asset catalog it uses the icon and the plain pictures.'''


@command('xcodebuild')
def xcodebuild(name, args):
    import glob
    import hashlib
    import json
    o, over, acts, args = {}, {}, [], list(args)
    takes = ('project', 'target', 'scheme', 'configuration', 'sdk', 'destination', 'workspace', 'arch', 'derivedDataPath', 'jobs', 'resultBundlePath', 'archivePath', 'clonedSourcePackagesDirPath')
    while args:
        a = args.pop(0)
        if a.startswith('-') and a[1:] in takes:
            if not args:
                raise Fail(f'{a} needs a value', 64)
            o[a[1:]] = args.pop(0)
        elif a in ('-list', '-showBuildSettings', '-showsdks', '-version', '-help', '-json', '-quiet', '-verbose', '-alltargets', '-allowProvisioningUpdates', '-skipMacroValidation', '-skipPackagePluginValidation', '-hideShellScriptEnvironment'):
            o[a[1:]] = True
        elif re.match(r'[A-Za-z_]\w*(\[[^\]]*\])*=', a):
            k, _, v = a.partition('=')
            over[k] = v
        elif a in ('build', 'clean'):
            acts.append(a)
        else:
            raise Fail(f'{a} is not supported here. xcodebuild -help lists what is', 64)
    if 'help' in o:
        put(XC_HELP)
        return 0
    if 'json' in o:
        raise Fail('-json is not supported here', 64)
    if 'version' in o:
        put('Xcode ' + xc_sdk()[1] + '\nBuild version Henge (not Apple\'s xcodebuild)')
        return 0
    if 'showsdks' in o:
        v = xc_sdk()[1]
        put(f'iOS SDKs:\n\tiOS {v}\t-sdk iphoneos{v}')
        return 0
    if 'sdk' in o:
        xc_sdk_ok(o['sdk'])
    if 'imulator' in o.get('destination', '') or 'macOS' in o.get('destination', ''):
        raise Fail('there is no simulator or macOS here: use -destination generic/platform=iOS or none', 1)
    if o.get('arch', 'arm64') != 'arm64':
        raise Fail('only arm64 is built here', 1)
    if 'derivedDataPath' in o:
        err('-derivedDataPath is not used: the build is beside the project')
    path = o.get('project')
    if 'workspace' in o:
        refs = re.findall(r'location\s*=\s*"(?:group|container):([^"]+\.xcodeproj)"', text(os.path.join(o['workspace'], 'contents.xcworkspacedata')))
        if any('Pods' in r for r in refs):
            raise Fail('CocoaPods are not built here', 1)
        if len(refs) != 1:
            raise Fail('a workspace of one project is needed: ' + ', '.join(refs), 1)
        path = os.path.join(os.path.dirname(os.path.abspath(o['workspace'])), refs[0])
    if not path:
        found = sorted(glob.glob('*.xcodeproj'))
        if len(found) != 1:
            raise Fail('name the project with -project: ' + (', '.join(found) or 'there is none in this folder'), 66)
        path = found[0]
    path = path.rstrip('/')
    if not os.path.exists(path + '/project.pbxproj'):
        raise Fail(f'{path} is not an Xcode project', 66)
    x = XcProject(path, o.get('configuration'), over)
    if 'list' in o:
        put(f'Information about project "{os.path.splitext(os.path.basename(path))[0]}":\n    Targets:')
        for t in x.targets:
            put('        ' + t['name'])
        put('\n    Build Configurations:')
        for c in x.configs(x.main):
            put('        ' + c)
        return 0
    want = o.get('target')
    if 'scheme' in o and not want:
        want = o['scheme']
        for f in sorted(glob.glob(glob.escape(path) + '/**/' + glob.escape(want) + '.xcscheme', recursive=True)):
            s = text(f)
            m = re.search(r'<LaunchAction.*?BlueprintName\s*=\s*"([^"]+)"', s, re.S) or re.search(r'BlueprintName\s*=\s*"([^"]+)"', s)
            if m:
                import html
                want = html.unescape(m[1])
                break
    t = x.pick(want)
    if 'showBuildSettings' in o:
        s = x.settings(t)
        put(f'Build settings for action build and target {t["name"]}:')
        for k in sorted(s):
            v = x.expand(s[k], s)
            put(f'    {k} = {" ".join(v) if isinstance(v, list) else v}')
        return 0
    if 'clean' in acts:
        import shutil
        for d in ('build/obj', 'build/clang-modules', 'build/modules', f'build/{x.expand("$(PRODUCT_NAME)", x.settings(t))}.app'):
            shutil.rmtree(os.path.join(x.root, d), ignore_errors=True)
        put('** CLEAN SUCCEEDED **')
        if 'build' not in acts:
            return 0
    mods = XcModules(x)
    p = x.convert(t, mods)
    for n in x.notes:
        err('note: ' + n)
    if x.bad:
        for n in x.bad:
            err('error: ' + n)
        err('\n** BUILD FAILED **\nThis project needs more than Henge\'s xcodebuild has. A henge-project.json written by hand (project_help) may do.')
        return 65
    f, mark = os.path.join(x.root, 'henge-project.json'), os.path.join(x.root, '.henge-xcode')
    if mods.order:
        try:
            objs, mark_of = mods.build(p['min_ios'])
        except Fail as e:
            err(e)
            err('\n** BUILD FAILED **')
            return 65
        p['link'] = p.get('link', []) + [x.rel(o) for o in objs]
        p['defines'] = [f'HENGE_MODULES_{mark_of}=1']  # so that the app's own files are compiled again when a module changed
        if 'swift_flags' in p:
            p['swift_flags'] += ['-D', 'HENGE_MODULES_' + mark_of]
    body = json.dumps(p, indent=1, ensure_ascii=False) + '\n'
    if os.path.exists(f):
        with open(f, 'rb') as h:
            now = hashlib.sha256(h.read()).hexdigest()
        if not os.path.exists(mark) or text(mark).strip() != now:
            raise Fail('henge-project.json was written or changed by hand, so it is left as it is: build with the build tool, or move it away first', 1)
    with open(f, 'w') as h:
        h.write(body)
    with open(mark, 'w') as h:
        h.write(hashlib.sha256(body.encode()).hexdigest() + '\n')
    if 'quiet' not in o:
        put(f'=== BUILD TARGET {t["name"]} WITH CONFIGURATION {x.config} ===')
    State.out.flush()
    ok, said = xc_build(x.root)
    put(said.rstrip())
    put('\n** BUILD SUCCEEDED **' if ok else '\n** BUILD FAILED **')
    return 0 if ok else 65


@command('xcrun')
def xcrun(name, args):
    import shutil
    args, find = list(args), False
    while args and args[0].startswith('-'):
        a = args.pop(0)
        a = a if a.startswith('--') or len(a) == 2 else '-' + a
        if a in ('--sdk', '--toolchain'):
            if not args:
                raise Fail(f'{a} needs a value', 64)
            v = args.pop(0)
            if a == '--sdk':
                xc_sdk_ok(v)
        elif a in ('-f', '--find'):
            find = True
        elif a in ('-r', '--run', '-n', '--no-cache', '-k', '--kill-cache', '-v', '--verbose', '-l', '--log'):
            pass
        elif a == '--show-sdk-path':
            put(xc_sdk()[0])
            return 0
        elif a in ('--show-sdk-version', '--show-sdk-platform-version'):
            put(xc_sdk()[1])
            return 0
        elif a == '--version':
            put("xcrun of Henge (not Apple's)")
            return 0
        else:
            raise Fail(f'{a} is not supported here', 64)
    if not args:
        raise Fail('usage: xcrun [--sdk iphoneos] [--find] TOOL [ARGUMENTS], or --show-sdk-path, --show-sdk-version', 64)
    p = os.path.join(xc_tools(), 'bin', args[0])
    if not os.path.exists(p):
        p = None if '/' in args[0] else shutil.which(args[0])
    if find:
        if not p and args[0] not in CMDS:
            raise Fail(f'unable to find utility "{args[0]}", not a developer tool here', 72)
        put(p or os.path.join(xc_tools(), 'bin', args[0]))
        return 0
    if not p and args[0] not in CMDS:
        raise Fail(f'unable to find utility "{args[0]}", not a developer tool here', 72)
    os.environ['SDKROOT'] = xc_sdk()[0]
    return call([p or args[0]] + args[1:])[0]


@command('xcode-select')
def xcode_select(name, args):
    if args in (['-p'], ['--print-path'], ['-print-path']):
        put(xc_tools())
    elif args in (['-v'], ['--version']):
        put("xcode-select of Henge (not Apple's)")
    else:
        raise Fail('only -p works here: the tools are Henge\'s Toolchain', 1)
    return 0


# ---- git: Henge's own, with git's own formats: clone, submodules, and the everyday local commands ----

GIT_HELP = '''usage: git [-C DIR] [-c NAME=VALUE] COMMAND ...     Henge's own, not the real git; its folders are git's own
  clone [--depth N] [-b REF] [--recursive] URL [DIR]   submodule update --init [--recursive]   fetch
  init  add  rm --cached  commit -m  status  log  show  diff  apply  checkout  switch  branch  tag  ls-files  ls-remote
  rev-parse  describe  config  remote  cat-file
Not here: push (use github_save), merge, pull, rebase, stash, reset, worktree, signing, LFS. Public https repositories only
(github_download has the user's sign-in). A clone of a long history is slow: use --depth 1.'''


class Git:
    def __init__(self, d):
        self.top = d
        g = os.path.join(d, '.git')
        if os.path.isfile(g):
            g = os.path.normpath(os.path.join(d, text(g).split(':', 1)[1].strip()))
        self.dir = g
        self.packs = None
        self.cache, self.held = {}, 0

    @staticmethod
    def find(need=True):
        d = os.getcwd()
        while True:
            if os.path.exists(os.path.join(d, '.git')):
                return Git(d)
            if os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
        if need:
            raise Fail('fatal: not a git repository (or any of the parent directories): .git', 128)

    def file(self, *p):
        return os.path.join(self.dir, *p)

    def put(self, name, s):
        f = self.file(name)
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, 'wb') as h:
            h.write(s if isinstance(s, bytes) else s.encode())

    def get(self, name):
        try:
            return text(self.file(name))
        except (OSError, Fail):
            return None

    @staticmethod
    def make(d):
        g = Git(d)
        os.makedirs(os.path.join(d, '.git', 'refs', 'heads'), exist_ok=True)
        os.makedirs(os.path.join(d, '.git', 'objects', 'pack'), exist_ok=True)
        g.dir = os.path.join(d, '.git')
        g.put('HEAD', 'ref: refs/heads/main\n')
        g.put('config', '[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n')
        return g

    # -- settings --

    @staticmethod
    def parse(s):
        out, sec = [], ''
        for ln in (s or '').splitlines():
            ln = ln.strip()
            m = re.match(r'\[([^\s"\]]+)(?:\s+"((?:[^"\\]|\\.)*)")?\]', ln)
            if m:
                sec = m[1].lower() + ('.' + m[2].replace('\\"', '"').replace('\\\\', '\\') if m[2] is not None else '')
            elif ln and ln[0] not in '#;':
                k, e, v = ln.partition('=')
                v = v.strip()
                out.append((sec + '.' + k.strip().lower(), v[1:-1] if len(v) > 1 and v[0] == v[-1] == '"' else v if e else 'true'))
        return out

    OVER = []

    def settings(self):
        home = os.path.join(os.environ.get('HOME', ''), '.gitconfig')
        return dict((self.parse(text(home)) if os.path.exists(home) else []) + self.parse(self.get('config')) + Git.OVER)

    # -- objects --

    def load_packs(self):
        import struct
        self.packs = []
        d = self.file('objects', 'pack')
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.endswith('.idx'):
                with open(os.path.join(d, f), 'rb') as h:
                    b = h.read()
                n = struct.unpack('>I', b[8 + 255 * 4:8 + 256 * 4])[0]
                self.packs.append((b, n, os.path.join(d, f[:-4] + '.pack'), {}))

    def lookup(self, sha):
        import struct
        if self.packs is None:
            self.load_packs()
        raw = bytes.fromhex(sha)
        for b, n, pack, _ in self.packs:
            lo = struct.unpack_from('>I', b, 8 + (raw[0] - 1) * 4)[0] if raw[0] else 0
            hi = struct.unpack_from('>I', b, 8 + raw[0] * 4)[0]
            base = 8 + 1024
            while lo < hi:
                mid = (lo + hi) // 2
                x = b[base + mid * 20:base + mid * 20 + 20]
                if x == raw:
                    o = struct.unpack_from('>I', b, base + n * 24 + mid * 4)[0]
                    if o >> 31:
                        o = struct.unpack_from('>Q', b, base + n * 28 + (o & 0x7fffffff) * 8)[0]
                    return pack, o
                if x < raw:
                    lo = mid + 1
                else:
                    hi = mid
        return None

    def has(self, sha):
        return os.path.exists(self.file('objects', sha[:2], sha[2:])) or self.lookup(sha) is not None

    def read(self, sha):
        import zlib
        f = self.file('objects', sha[:2], sha[2:])
        if os.path.exists(f):
            with open(f, 'rb') as h:
                b = zlib.decompress(h.read())
            head, _, body = b.partition(b'\0')
            return head.split()[0].decode(), body
        at = self.lookup(sha)
        if not at:
            raise Fail(f'fatal: object {sha} is not here', 128)
        with open(at[0], 'rb') as h:
            return self.unpack(h, at[1], at[0])

    KINDS = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}

    def unpack(self, h, at, pack, find=None):
        """An object of a pack, its deltas applied."""
        import zlib
        chain = []
        while True:
            if (pack, at) in self.cache:
                kind, body = self.cache[(pack, at)]
                break
            kind, size, base, body, _ = git_entry(h, at)
            if kind in self.KINDS.values():
                break
            chain.append((at, body))
            if kind == 'ofs':
                at = base
                continue
            o = find(base) if find else None
            if o is not None:
                at = o
                continue
            kind, body = self.read(base)
            break
        for a, d in reversed(chain):
            body = git_delta(body, d)
            self.keep((pack, a), kind, body)
        if not chain:
            self.keep((pack, at), kind, body)
        return kind, body

    def keep(self, k, kind, body):
        if len(body) < 1 << 20:
            if self.held > 48 << 20:
                self.cache.clear()
                self.held = 0
            self.cache[k] = (kind, body)
            self.held += len(body)

    def write(self, kind, body):
        import hashlib
        import zlib
        b = f'{kind} {len(body)}'.encode() + b'\0' + body
        sha = hashlib.sha1(b).hexdigest()
        if not self.has(sha):
            self.put(f'objects/{sha[:2]}/{sha[2:]}', zlib.compress(b))
        return sha

    def add_pack(self, data):
        """Stores a pack as git does, with its index; what it was sent as is checked."""
        import binascii
        import hashlib
        import struct
        if data[:4] != b'PACK' or hashlib.sha1(data[:-20]).digest() != data[-20:]:
            raise Fail('fatal: the pack that came is damaged', 128)
        n = struct.unpack('>I', data[8:12])[0]
        if not n:
            return
        name = data[-20:].hex()
        path = self.file('objects', 'pack', f'pack-{name}.pack')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import io
        h, at, entries = io.BytesIO(data), 12, []
        for _ in range(n):
            kind, size, base, body, end = git_entry(h, at)
            entries.append((at, end))
            at = end
        known, todo = {}, entries
        while todo:
            later = []
            for at, end in todo:
                try:
                    kind, body = self.unpack(h, at, path, lambda sha: known[sha][0] if sha in known else None)
                except Fail:
                    later.append((at, end))
                    continue
                known[hashlib.sha1(f'{kind} {len(body)}'.encode() + b'\0' + body).hexdigest()] = (at, binascii.crc32(data[at:end]))
            if len(later) == len(todo):
                raise Fail('fatal: the pack that came is not complete', 128)
            todo = later
        names = sorted(known)
        fan, i = [], 0
        for b in range(256):
            while i < len(names) and int(names[i][:2], 16) <= b:
                i += 1
            fan.append(i)
        big = []
        out = [b'\xfftOc' + struct.pack('>I', 2), struct.pack('>256I', *fan), bytes.fromhex(''.join(names)), struct.pack(f'>{len(names)}I', *(known[x][1] for x in names))]
        offs = bytearray()
        for x in names:
            o = known[x][0]
            if o >= 1 << 31:
                big.append(o)
                o = 1 << 31 | len(big) - 1
            offs += struct.pack('>I', o)
        out += [bytes(offs), struct.pack(f'>{len(big)}Q', *big), data[-20:]]
        idx = b''.join(out)
        with open(path, 'wb') as f:
            f.write(data)
        with open(path[:-5] + '.idx', 'wb') as f:
            f.write(idx + hashlib.sha1(idx).digest())
        self.packs = None

    # -- names --

    def refs(self):
        out = {}
        for ln in (self.get('packed-refs') or '').splitlines():
            if ln and ln[0] not in '#^':
                sha, r = ln.split(' ', 1)
                out[r] = sha
        top = self.file('refs')
        for r, ds, fs in os.walk(top):
            for f in fs:
                p = os.path.join(r, f)
                s = text(p).strip()
                if re.fullmatch('[0-9a-f]{40}', s):
                    out[os.path.relpath(p, self.dir)] = s
        return out

    def branch(self):
        s = (self.get('HEAD') or '').strip()
        return s[16:] if s.startswith('ref: refs/heads/') else None

    def head(self):
        b = self.branch()
        return self.refs().get('refs/heads/' + b) if b else (self.get('HEAD') or '').strip() or None

    def resolve(self, name, kind=None):
        sha = self.name(name)
        while sha and kind:
            k, body = self.read(sha)
            if k == kind:
                return sha
            if k == 'tag':
                sha = body.split(b'\n')[0].split()[1].decode()
            elif k == 'commit' and kind == 'tree':
                return body.split(b'\n')[0].split()[1].decode()
            else:
                break
        if not sha or kind:
            raise Fail(f"fatal: ambiguous argument '{name}': unknown revision or path not in the working tree.", 128)
        return sha

    def name(self, n):
        m = re.fullmatch(r'(.+?)((?:[~^]\d*|\^\{\w*\})+)', n)
        if m:
            sha = self.name(m[1])
            for op, arg in re.findall(r'([~^])(\{\w*\}|\d*)', m[2]):
                if not sha:
                    return None
                if arg.startswith('{'):
                    sha = self.resolve(sha, arg[1:-1] or 'commit')
                    continue
                sha = self.resolve(sha, 'commit')
                if op == '~':
                    for _ in range(int(arg or 1)):
                        ps = self.commit(sha)['parents']
                        sha = ps[0] if ps else None
                        if not sha:
                            return None
                elif arg != '0':
                    ps = self.commit(sha)['parents']
                    sha = ps[int(arg or 1) - 1] if len(ps) >= int(arg or 1) else None
            return sha
        if ':' in n:
            rev, p = n.split(':', 1)
            t = self.tree(self.resolve(rev or 'HEAD', 'tree'))
            return t[p][1] if p in t else None
        if n in ('HEAD', '@'):
            return self.head()
        if n == 'FETCH_HEAD':
            return ((self.get(n) or '').split() or [None])[0]
        rs = self.refs()
        for p in ('', 'refs/', 'refs/tags/', 'refs/heads/', 'refs/remotes/'):
            if p + n in rs:
                return rs[p + n]
        if re.fullmatch('[0-9a-f]{40}', n):
            return n if self.has(n) else None
        if re.fullmatch('[0-9a-f]{4,39}', n):
            got = set()
            d = self.file('objects', n[:2])
            for f in os.listdir(d) if os.path.isdir(d) else []:
                if (n[:2] + f).startswith(n):
                    got.add(n[:2] + f)
            if self.packs is None:
                self.load_packs()
            for b, k, _, _ in self.packs:
                for i in range(k):
                    x = b[1032 + i * 20:1052 + i * 20].hex()
                    if x.startswith(n):
                        got.add(x)
            if len(got) == 1:
                return got.pop()
        return None

    def commit(self, sha):
        k, body = self.read(sha)
        head, _, msg = body.decode('utf-8', 'replace').partition('\n\n')
        c = {'parents': [], 'msg': msg, 'sha': sha}
        for ln in head.split('\n'):
            a, _, b = ln.partition(' ')
            if a == 'parent':
                c['parents'].append(b)
            elif a in ('tree', 'author', 'committer'):
                c[a] = b
        if sha in (self.get('shallow') or '').split():
            c['parents'] = []
        return c

    def tree(self, sha, pre=''):
        """{path: (mode, sha)} of a tree, all its folders gone through."""
        out, (k, b), i = {}, self.read(sha), 0
        while i < len(b):
            sp = b.index(b' ', i)
            z = b.index(b'\0', sp)
            mode, n, s = b[i:sp].decode(), os.fsdecode(b[sp + 1:z]), b[z + 1:z + 21].hex()
            i = z + 21
            if n.lower() in ('.', '..', '.git') or '/' in n or not n:
                raise Fail(f'fatal: a bad name in a tree: {n!r}', 128)
            if mode == '40000':
                out |= self.tree(s, pre + n + '/')
            else:
                out[pre + n] = (mode, s)
        return out

    def log(self, start):
        """Commits from some on, newest first."""
        import heapq
        seen, q = set(), []

        def add(s):
            if s not in seen:
                seen.add(s)
                c = self.commit(s)
                heapq.heappush(q, (-int(c['committer'].split()[-2]), len(seen), c))
        for s in start:
            add(s)
        while q:
            c = heapq.heappop(q)[2]
            yield c
            for p in c['parents']:
                add(p)

    # -- the index --

    def index(self):
        import struct
        out = {}
        try:
            with open(self.file('index'), 'rb') as h:
                b = h.read()
        except OSError:
            return out
        ver, n = struct.unpack('>II', b[4:12])
        if ver not in (2, 3):
            raise Fail('fatal: this index version is not read here', 128)
        i = 12
        for _ in range(n):
            f = struct.unpack('>10I', b[i:i + 40])
            fl = struct.unpack('>H', b[i + 60:i + 62])[0]
            j = i + 62 + (2 if fl & 0x4000 else 0)
            z = b.index(b'\0', j)
            out[os.fsdecode(b[j:z])] = {'mode': oct(f[6])[2:], 'sha': b[i + 40:i + 60].hex(), 'stat': f}
            i += (z - i + 8) // 8 * 8
        return out

    def save_index(self, ix):
        import hashlib
        import struct
        out = [b'DIRC' + struct.pack('>II', 2, len(ix))]
        for p in sorted(ix, key=os.fsencode):
            e, n = ix[p], os.fsencode(p)
            f = list(e.get('stat') or [0] * 10)
            f[6] = int(e['mode'], 8)
            x = struct.pack('>10I', *(v & 0xffffffff for v in f)) + bytes.fromhex(e['sha']) + struct.pack('>H', min(len(n), 0xfff)) + n
            out.append(x + b'\0' * (8 - len(x) % 8))
        b = b''.join(out)
        self.put('index', b + hashlib.sha1(b).digest())

    @staticmethod
    def stat(p):
        st = os.lstat(p)
        return [int(st.st_ctime), st.st_ctime_ns % 10**9, int(st.st_mtime), st.st_mtime_ns % 10**9, st.st_dev, st.st_ino, 0, st.st_uid, st.st_gid, st.st_size]

    def mode(self, p):
        import stat
        st = os.lstat(p)
        return '120000' if stat.S_ISLNK(st.st_mode) else '100755' if st.st_mode & 0o100 else '100644'

    def body(self, p):
        if os.path.islink(p):
            return os.fsencode(os.readlink(p))
        with open(p, 'rb') as h:
            return h.read()

    def hash(self, b):
        import hashlib
        return hashlib.sha1(b'blob %d\0' % len(b) + b).hexdigest()

    def changed(self, rel, e):
        """How a file differs from its entry: None, 'D' or 'M'."""
        p = os.path.join(self.top, rel)
        if e['mode'] == '160000':
            if not os.path.exists(os.path.join(p, '.git')):
                return None
            return None if Git(p).head() in (None, e['sha']) else 'M'
        if not os.path.lexists(p) or (os.path.isdir(p) and not os.path.islink(p)):
            return 'D'
        s, o = self.stat(p), e.get('stat')
        if self.mode(p) != e['mode'] and not (os.environ.get('HENGE_TOOLS') and self.mode(p) != '120000' and e['mode'] != '120000' and s[9] == (o or s)[9]):
            return 'M'
        if o and s[2:4] == list(o[2:4]) and s[9] == o[9]:
            return None
        return None if self.hash(self.body(p)) == e['sha'] else 'M'

    def ignored(self):
        rules = []

        def load(f, base):
            if os.path.exists(f):
                for ln in text(f).splitlines():
                    ln = ln.rstrip()
                    if not ln or ln[0] == '#':
                        continue
                    neg = ln[0] == '!'
                    ln = ln[1:] if neg else ln
                    only = ln.endswith('/')
                    ln = ln.rstrip('/')
                    anch = '/' in ln
                    ln, r, i = ln.lstrip('/'), '', 0
                    while i < len(ln):
                        if ln[i:i + 3] == '**/':
                            r += '(?:.*/)?'
                            i += 3
                        elif ln[i:i + 2] == '**':
                            r += '.*'
                            i += 2
                        elif ln[i] == '*':
                            r += '[^/]*'
                            i += 1
                        elif ln[i] == '?':
                            r += '[^/]'
                            i += 1
                        elif ln[i] == '[' and ']' in ln[i + 2:]:
                            j = ln.index(']', i + 2)
                            r += '[' + ln[i + 1:j].replace('!', '^', 1) + ']'
                            i = j + 1
                        else:
                            i += ln[i] == '\\' and i + 1 < len(ln)
                            r += re.escape(ln[i])
                            i += 1
                    try:
                        rules.append((re.compile(re.escape(base) + ('' if anch else '(?:.*/)?') + r + r'\Z'), neg, only))
                    except re.error:
                        pass
        load(self.file('info', 'exclude'), '')
        load(os.path.join(self.top, '.gitignore'), '')

        def test(rel, isdir):
            hit = False
            for r, neg, only in rules:
                if (isdir or not only) and r.match(rel):
                    hit = not neg
            return hit
        return load, test

    def untracked(self, ix, start=''):
        load, test = self.ignored()
        dirs = {''}
        for p in ix:
            d = os.path.dirname(p)
            while d not in dirs:
                dirs.add(d)
                d = os.path.dirname(d)
        for r, ds, fs in os.walk(self.top):
            rel = os.path.relpath(r, self.top)
            rel = '' if rel == '.' else rel + '/'
            if rel:
                load(os.path.join(r, '.gitignore'), rel)
            for d in sorted(ds):
                p = rel + d
                if d == '.git' or p in ix or test(p, True):
                    ds.remove(d)
                elif os.path.islink(os.path.join(r, d)):
                    ds.remove(d)
                    yield p
                elif os.path.exists(os.path.join(r, d, '.git')):
                    ds.remove(d)
                    yield p + '/'
            ds.sort()
            for f in sorted(fs):
                p = rel + f
                if p not in ix and not test(p, False):
                    yield p

    # -- the files --

    def checkout(self, tree, force=False, only=None):
        """The working files as in a tree; changed ones stop it unless force."""
        ix, new = self.index(), self.tree(tree)
        if only is not None:
            new = {p: v for p, v in new.items() if only(p)}
        gone = [p for p in ix if p not in new] if only is None else []
        todo = [p for p, v in new.items() if force or p not in ix or (ix[p]['mode'], ix[p]['sha']) != v]
        if not force:
            bad = [p for p in gone + todo if (p in ix and self.changed(p, ix[p]) == 'M') or (p not in ix and new[p][0] != '160000' and os.path.lexists(os.path.join(self.top, p)) and self.hash(self.body(os.path.join(self.top, p))) != new[p][1])]
            if bad:
                raise Fail('error: these files would be overwritten:\n\t' + '\n\t'.join(bad[:20]) + '\nCommit them or move them away first.', 1)
        real = os.path.realpath(self.top)
        for p in gone:
            f = os.path.join(self.top, p)
            if ix[p]['mode'] != '160000' and os.path.lexists(f):
                os.remove(f)
                d = os.path.dirname(f)
                while d != self.top and not os.listdir(d):
                    os.rmdir(d)
                    d = os.path.dirname(d)
            del ix[p]
        for p in todo:
            mode, sha = new[p]
            f = os.path.join(self.top, p)
            d = os.path.dirname(f)
            os.makedirs(d, exist_ok=True)
            if not (os.path.realpath(d) + '/').startswith(real.rstrip('/') + '/'):
                raise Fail(f'fatal: {p} would be outside the repository', 128)
            if mode == '160000':
                os.makedirs(f, exist_ok=True)
                ix[p] = {'mode': mode, 'sha': sha, 'stat': None}
                continue
            b = self.read(sha)[1]
            if os.path.lexists(f):
                os.remove(f)
            if mode == '120000':
                os.symlink(os.fsdecode(b), f)
            else:
                with open(f, 'wb') as h:
                    h.write(b)
                os.chmod(f, 0o755 if mode == '100755' else 0o644)
            ix[p] = {'mode': mode, 'sha': sha, 'stat': self.stat(f)}
        self.save_index(ix)

    def write_tree(self, ix):
        def build(pre, items):
            ents, i = [], 0
            while i < len(items):
                p, e = items[i]
                n, _, rest = p[len(pre):].partition('/')
                if rest:
                    sub = pre + n + '/'
                    j = i
                    while j < len(items) and items[j][0].startswith(sub):
                        j += 1
                    ents.append((os.fsencode(n + '/'), '40000', build(sub, items[i:j])))
                    i = j
                else:
                    ents.append((os.fsencode(n), e['mode'], e['sha']))
                    i += 1
            ents.sort()
            return self.write('tree', b''.join(m.encode() + b' ' + n.rstrip(b'/') + b'\0' + bytes.fromhex(s) for n, m, s in ents))
        return build('', sorted(ix.items()))

    # -- from afar --

    def url(self, u, base=None):
        u = re.sub(r'^git@([^:/]+):', r'https://\1/', u)
        u = re.sub(r'^(?:ssh://git@|git://)', 'https://', u)
        if u.startswith(('./', '../')) and base:
            from urllib.parse import urljoin
            return urljoin(base.rstrip('/') + '/', u)
        return u


def git_entry(h, at):
    """(kind, size, base, data, end) of a pack's entry: kind ofs and ref are deltas."""
    import zlib
    h.seek(at)
    b = h.read(64)
    c, i = b[0], 1
    t, size, sh = c >> 4 & 7, c & 15, 4
    while c & 128:
        c = b[i]
        i += 1
        size |= (c & 127) << sh
        sh += 7
    kind, base = Git.KINDS.get(t), None
    if t == 6:
        c = b[i]
        i += 1
        o = c & 127
        while c & 128:
            c = b[i]
            i += 1
            o = (o + 1) << 7 | c & 127
        kind, base = 'ofs', at - o
    elif t == 7:
        kind, base = 'ref', b[i:i + 20].hex()
        i += 20
    elif kind is None:
        raise Fail('fatal: the pack is damaged', 128)
    h.seek(at + i)
    z, out, used = zlib.decompressobj(), [], 0
    while not z.eof:
        x = h.read(max(4096, min(size, 1 << 20)))
        if not x:
            raise Fail('fatal: the pack is damaged', 128)
        out.append(z.decompress(x))
        used += len(x)
    return kind, size, base, b''.join(out), at + i + used - len(z.unused_data)


def git_delta(base, d):
    i = 0

    def num():
        nonlocal i
        v = sh = 0
        while True:
            c = d[i]
            i += 1
            v |= (c & 127) << sh
            sh += 7
            if not c & 128:
                return v
    num()
    size = num()
    out = []
    while i < len(d):
        c = d[i]
        i += 1
        if c & 128:
            o = n = 0
            for k in range(4):
                if c >> k & 1:
                    o |= d[i] << 8 * k
                    i += 1
            for k in range(3):
                if c >> 4 + k & 1:
                    n |= d[i] << 8 * k
                    i += 1
            n = n or 0x10000
            out.append(base[o:o + n])
        else:
            out.append(d[i:i + c])
            i += c
    r = b''.join(out)
    if len(r) != size:
        raise Fail('fatal: the pack is damaged', 128)
    return r


class GitRemote:
    """A repository over https (git's smart protocol)."""
    def __init__(self, url):
        self.url = url.rstrip('/')
        if not re.match(r'https?://', self.url):
            raise Fail(f'fatal: only https addresses are cloned here: {url}', 128)
        if '@' in self.url.split('/')[2]:
            raise Fail('fatal: an address with a password in it is not used here', 128)
        b = self.ask('/info/refs?service=git-upload-pack').read()
        self.refs, self.caps, self.head = {}, '', None
        for ln in self.lines(b):
            if not ln or ln.startswith(b'#'):
                continue
            ln, _, caps = ln.partition(b'\0')
            if caps:
                self.caps = caps.decode()
            sha, _, r = ln.strip().decode().partition(' ')
            if re.fullmatch('[0-9a-f]{40}', sha) and r != 'capabilities^{}':
                self.refs[r] = sha
        m = re.search(r'symref=HEAD:(\S+)', self.caps)
        self.head = m[1] if m else next((r for r, s in self.refs.items() if r.startswith('refs/heads/') and s == self.refs.get('HEAD')), None)

    def ask(self, path, data=None):
        import urllib.error
        import urllib.request
        q = urllib.request.Request(self.url + path, data=data, headers={'User-Agent': 'git/2.40 (henge)'} | ({'Content-Type': 'application/x-git-upload-pack-request'} if data else {}))
        try:
            return urllib.request.urlopen(q, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise Fail(f'fatal: {self.url} is not there, or is private (github_download has the user\'s sign-in)', 128) from None
            raise Fail(f'fatal: {self.url} answered {e.code}', 128) from None
        except OSError as e:
            raise Fail(f'fatal: unable to access {self.url}: {e}', 128) from None

    @staticmethod
    def lines(b):
        i = 0
        while i + 4 <= len(b):
            n = int(b[i:i + 4], 16)
            if n < 4:
                yield None
                i += 4
            else:
                yield b[i + 4:i + n]
                i += n

    def fetch(self, g, wants, depth=0):
        """Gets what is wanted, and all it needs, into g."""
        wants = [w for w in dict.fromkeys(wants) if depth or not g.has(w)]
        if not wants:
            return
        pk = lambda s: b'%04x' % (len(s) + 4) + s
        caps = [c for c in ('ofs-delta', 'side-band-64k', 'no-progress') if c in self.caps.split()] + ['agent=git/2.40-henge']
        if depth and 'shallow' not in self.caps.split():
            depth = 0
        req = b''.join(pk(f'want {w}{" " + " ".join(caps) if i == 0 else ""}\n'.encode()) for i, w in enumerate(wants))
        old = (g.get('shallow') or '').split()
        req += b''.join(pk(f'shallow {s}\n'.encode()) for s in old)
        if depth:
            req += pk(f'deepen {depth}\n'.encode())
        req += b'0000'
        if not depth and not old:
            req += b''.join(pk(f'have {s}\n'.encode()) for s in list(dict.fromkeys(g.refs().values()))[:200])
        req += pk(b'done\n')
        b = self.ask('/git-upload-pack', req).read()
        i, pack, shallow = 0, [], set(old)
        while i + 4 <= len(b):
            if b[i:i + 4] == b'PACK':
                pack.append(b[i:])
                break
            n = int(b[i:i + 4], 16)
            x = b[i + 4:i + n] if n >= 4 else b''
            i += max(n, 4)
            if x.startswith(b'shallow '):
                shallow.add(x.split()[1].decode())
            elif x.startswith(b'unshallow '):
                shallow.discard(x.split()[1].decode())
            elif x.startswith((b'NAK', b'ACK')) or not x:
                continue
            elif x.startswith(b'ERR'):
                raise Fail('fatal: ' + x[4:].decode('utf-8', 'replace').strip(), 128)
            elif x[0] == 1:
                pack.append(x[1:])
            elif x[0] == 3:
                raise Fail('fatal: ' + x[1:].decode('utf-8', 'replace').strip(), 128)
        data = b''.join(pack)
        if not data:
            raise Fail('fatal: nothing came', 128)
        g.add_pack(data)
        if shallow != set(old):
            g.put('shallow', ''.join(s + '\n' for s in sorted(shallow)))


def git_clone(url, d, depth=0, ref=None, recursive=False, quiet=False, sha=None, nocheckout=False):
    import shutil
    if os.path.exists(d) and (not os.path.isdir(d) or os.listdir(d)):
        raise Fail(f"fatal: destination path '{d}' already exists and is not an empty directory.", 128)
    if not quiet:
        err(f"Cloning into '{os.path.relpath(d)}'...")
        sys.stderr.flush()
    r = GitRemote(url)
    had = os.path.isdir(d)
    g = Git.make(d)
    try:
        g.put('config', g.get('config') + f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
        heads = {k: v for k, v in r.refs.items() if k.startswith('refs/heads/')}
        tags = {k: v for k, v in r.refs.items() if k.startswith('refs/tags/') and not k.endswith('^{}')}
        full = None
        if sha:
            try:
                r.fetch(g, [sha], depth)
            except Fail:
                depth = 0
                r.fetch(g, list(heads.values()))
        else:
            if ref:
                full = next((p + ref for p in ('refs/heads/', 'refs/tags/') if p + ref in r.refs), None)
                if not full:
                    raise Fail(f'fatal: Remote branch {ref} not found in upstream origin', 128)
            else:
                full = r.head
            if not full or full not in r.refs:
                err('warning: You appear to have cloned an empty repository.')
                return g
            r.fetch(g, [r.refs[full]] if depth else list(heads.values()) + list(tags.values()), depth)
        for k, v in (heads | tags).items():
            if g.has(v):
                g.put(k.replace('refs/heads/', 'refs/remotes/origin/'), v + '\n')
        if sha or full.startswith('refs/tags/'):
            at = g.resolve(sha or r.refs[full], 'commit')
            g.put('HEAD', at + '\n')
        else:
            at = r.refs[full]
            b = full[11:]
            g.put(full, at + '\n')
            g.put('HEAD', f'ref: {full}\n')
            g.put('config', g.get('config') + f'[branch "{b}"]\n\tremote = origin\n\tmerge = {full}\n')
        if not nocheckout:
            g.checkout(g.resolve(at, 'tree'), True)
            if recursive:
                git_submodules(g, url, depth, True, quiet)
    except BaseException:
        # Only what this clone made.
        for n in os.listdir(d):
            p = os.path.join(d, n)
            shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
        if not had:
            os.rmdir(d)
        raise
    return g


def git_submodules(g, base, depth=0, deep=False, quiet=False, only=()):
    f = os.path.join(g.top, '.gitmodules')
    if not os.path.exists(f):
        return
    mods = {}
    for k, v in Git.parse(text(f)):
        n, _, key = k[10:].rpartition('.')
        mods.setdefault(n, {})[key] = v
    ix = g.index()
    for n, m in mods.items():
        p, u = os.path.normpath(m.get('path', '')), m.get('url')
        if not u or p not in ix or ix[p]['mode'] != '160000' or (only and p not in only):
            continue
        if p.startswith('..') or os.path.isabs(p) or not (os.path.realpath(os.path.join(g.top, p)) + '/').startswith(os.path.realpath(g.top) + '/'):
            raise Fail(f'fatal: the submodule {n} would be outside the repository', 128)
        d, sha = os.path.join(g.top, p), ix[p]['sha']
        u = g.url(u, base)
        if os.path.exists(os.path.join(d, '.git')):
            s = Git(d)
            if s.head() != sha:
                if not s.has(sha):
                    GitRemote(u).fetch(s, [sha], depth)
                s.checkout(s.resolve(sha, 'tree'))
                s.put('HEAD', sha + '\n')
        else:
            s = git_clone(u, d, depth, sha=sha, quiet=True)
            if not quiet:
                put(f"Submodule path '{p}': checked out '{sha}'")
                State.out.flush()
        if deep:
            git_submodules(s, u, depth, True, quiet)


def git_patch(path, a, b, ma='100644', mb='100644'):
    """The difference of two files as git shows it; None for one that is not there."""
    import difflib
    o = [f'diff --git a/{path} b/{path}\n']
    if a is None:
        o.append(f'new file mode {mb}\n')
    elif b is None:
        o.append(f'deleted file mode {ma}\n')
    elif ma != mb:
        o += [f'old mode {ma}\n', f'new mode {mb}\n']
    if b'\0' in (a or b'')[:8000] or b'\0' in (b or b'')[:8000]:
        if a != b:
            o.append(f'Binary files {"/dev/null" if a is None else "a/" + path} and {"/dev/null" if b is None else "b/" + path} differ\n')
        return os.fsencode(''.join(o))
    x, y = ([ln + '\n' for ln in s.decode('utf-8', 'surrogateescape').split('\n')] for s in (a or b'', b or b''))
    for s, l in ((a or b'', x), (b or b'', y)):
        if s.endswith(b'\n') or not s:
            l.pop()
        else:
            l[-1] = l[-1][:-1]
    for ln in difflib.unified_diff(x, y, '/dev/null' if a is None else 'a/' + path, '/dev/null' if b is None else 'b/' + path):
        if ln.startswith(('--- ', '+++ ')) and not o[-1].startswith(('+', '-', ' ', '\\', '@')) or ln.startswith('+++ ') and o[-1].startswith('--- '):
            o.append(ln.rstrip('\n').rstrip('\t') + '\n')
        else:
            o.append(ln if ln.endswith('\n') else ln + '\n\\ No newline at end of file\n')
    return os.fsencode(''.join(o)) if len(o) > 1 else b''


def git_date(who):
    import datetime
    m = re.match(r'(.*) (\d+) ([+-])(\d\d)(\d\d)$', who)
    z = datetime.timezone((1 if m[3] == '+' else -1) * datetime.timedelta(hours=int(m[4]), minutes=int(m[5])))
    t = datetime.datetime.fromtimestamp(int(m[2]), z)
    return m[1], f'{t:%a %b} {t.day} {t:%H:%M:%S %Y} {m[3]}{m[4]}{m[5]}', t


def git_decide(g, args):
    """Paths, relative to the repository's top, as a test; None: no paths given."""
    if not args:
        return None
    ps = [os.path.relpath(os.path.realpath(a) if os.path.isdir(a) else os.path.join(os.path.realpath(os.path.dirname(a) or '.'), os.path.basename(a)), os.path.realpath(g.top)) for a in args]
    for a, p in zip(args, ps):
        if p.startswith('..'):
            raise Fail(f"fatal: {a}: '{a}' is outside repository", 128)
    return lambda f: any(p == '.' or f == p or f.startswith(p + '/') or git_glob(p, f) for p in ps)


@command('git')
def git(name, args):
    args, over, here = list(args), [], os.getcwd()
    try:
        while args and args[0].startswith('-'):
            a = args.pop(0)
            if a == '-C' and args:
                os.chdir(args.pop(0))
            elif a == '-c' and args:
                k, _, v = args.pop(0).partition('=')
                over.append((k.lower(), v))
            elif a in ('--version', '-v'):
                put('git version 2.40.0.henge')
                return 0
            elif a in ('--help', '-h'):
                put(GIT_HELP)
                return 0
            elif a not in ('--no-pager', '--no-optional-locks', '--literal-pathspecs'):
                raise Fail(f'{a} is not supported here', 129)
        if not args or args[0] == 'help':
            put(GIT_HELP)
            return 0 if args else 1
        if args[0] == 'version':
            put('git version 2.40.0.henge')
            return 0
        f = globals().get('git_c_' + args[0].replace('-', '_'))
        if not f:
            raise Fail(f"'{args[0]}' is not here: Henge's git has the everyday commands (git help). Push with github_save.", 1)
        Git.OVER = over
        return f(args[1:])
    finally:
        Git.OVER = []
        os.chdir(here)


def git_args(args, flags=(), vals=()):
    """(options, the rest, whether -- was there); unknown options are errors."""
    o, rest, args, dd = {}, [], list(args), None
    while args:
        a = args.pop(0)
        if a == '--':
            dd = len(rest)
            rest += args
            break
        if not a.startswith('-') or a == '-':
            rest.append(a)
            continue
        k, e, v = a.partition('=')
        if k in vals:
            if e:
                o[k] = v
            elif args:
                o[k] = args.pop(0)
            else:
                raise Fail(f'{k} needs a value', 129)
        elif a in flags:
            o[a] = True
        elif len(a) > 2 and a[1] != '-' and a[:2] in vals:
            o[a[:2]] = a[2:]
        elif len(a) > 2 and a[1] != '-' and all('-' + c in flags or '-' + c in vals for c in a[1:]):
            for i, c in enumerate(a[1:]):
                if '-' + c in vals:
                    o['-' + c] = a[i + 2:] or (args.pop(0) if args else '')
                    break
                o['-' + c] = True
        else:
            raise Fail(f'the option {a} is not supported here', 129)
    return o, rest, dd


def git_c_clone(args):
    o, r, _ = git_args(args, ('--recursive', '--recurse-submodules', '-q', '--quiet', '--single-branch', '--no-single-branch', '--shallow-submodules', '--no-checkout', '-n', '--no-tags', '--progress', '-v'),
                       ('--depth', '-b', '--branch', '-j', '--jobs', '--filter', '-c', '--config', '-o'))
    if not r or len(r) > 2:
        raise Fail('usage: git clone [--depth N] [-b REF] [--recursive] URL [DIR]', 129)
    if '-o' in o:
        raise Fail('the option -o is not supported here', 129)
    u = Git('.').url(r[0])
    d = os.path.abspath(r[1] if len(r) > 1 else re.sub(r'\.git$', '', u.rstrip('/').rsplit('/', 1)[1]))
    git_clone(u, d, integer(o.get('--depth', '0')), o.get('-b') or o.get('--branch'), '--recursive' in o or '--recurse-submodules' in o, '-q' in o or '--quiet' in o, nocheckout='-n' in o or '--no-checkout' in o)


def git_c_submodule(args):
    o, r, _ = git_args(args, ('--init', '--recursive', '-q', '--quiet', '--progress', '--force', '--recommend-shallow'), ('--depth', '-j', '--jobs'))
    g = Git.find()
    if r[:1] not in (['update'], ['init'], ['sync'], ['status'], []):
        raise Fail(f'git submodule {r[0]} is not here', 1)
    if r[:1] == ['update']:
        git_submodules(g, g.settings().get('remote.origin.url', ''), integer(o.get('--depth', '0')), '--recursive' in o, '-q' in o or '--quiet' in o, [os.path.relpath(os.path.join(os.getcwd(), p), g.top) for p in r[1:]])
    elif r[:1] in ([], ['status']):
        for p, e in sorted(g.index().items()):
            if e['mode'] == '160000':
                s = os.path.join(g.top, p)
                put((' ' if os.path.exists(s + '/.git') and Git(s).head() == e['sha'] else '-' if not os.path.exists(s + '/.git') else '+') + e['sha'] + ' ' + p)


def git_c_fetch(args):
    o, r, _ = git_args(args, ('-q', '--quiet', '--all', '--tags', '-t', '--prune', '-p', '--no-tags', '-f', '--force', '--unshallow', '--progress'), ('--depth',))
    g = Git.find()
    if '--unshallow' in o:
        o['--depth'] = '2147483647'
    u = g.settings().get(f'remote.{r[0] if r else "origin"}.url') or (r[0] if r and '/' in r[0] else None)
    if not u:
        raise Fail('fatal: no such remote', 128)
    rem, depth = GitRemote(g.url(u)), integer(o.get('--depth', '0'))
    if r[1:]:
        want = [next((rem.refs[p + x] for p in ('', 'refs/heads/', 'refs/tags/') if p + x in rem.refs), x if re.fullmatch('[0-9a-f]{40}', x) else None) for x in r[1:]]
        if None in want:
            raise Fail("fatal: couldn't find remote ref " + r[1 + want.index(None)], 128)
        rem.fetch(g, want, depth)
        g.put('FETCH_HEAD', want[0] + '\n')
    else:
        keep = {k: v for k, v in rem.refs.items() if k.startswith('refs/heads/') or (k.startswith('refs/tags/') and not k.endswith('^{}') and (not depth or '--tags' in o))}
        rem.fetch(g, list(keep.values()), depth)
        want = []
    for k, v in rem.refs.items():
        if k.startswith('refs/heads/') and g.has(v) and (not r[1:] or v in want):
            g.put('refs/remotes/origin/' + k[11:], v + '\n')
        elif k.startswith('refs/tags/') and not k.endswith('^{}') and g.has(v):
            g.put(k, v + '\n')


def git_c_ls_remote(args):
    o, r, _ = git_args(args, ('--heads', '-h', '--tags', '-t', '-q', '--refs', '--symref', '--exit-code'))
    u = r[0] if r else 'origin'
    if '/' not in u:
        u = Git.find().settings().get(f'remote.{u}.url', u)
    for k, v in GitRemote(Git('.').url(u)).refs.items():
        if ('--heads' in o and not k.startswith('refs/heads/')) or ('--tags' in o and not k.startswith('refs/tags/')) or (r[1:] and not any(k == p or k.endswith('/' + p) for p in r[1:])):
            continue
        put(v + '\t' + k)


def git_c_init(args):
    o, r, _ = git_args(args, ('-q', '--quiet'), ('-b', '--initial-branch'))
    d = os.path.abspath(r[0] if r else '.')
    os.makedirs(d, exist_ok=True)
    again = os.path.exists(os.path.join(d, '.git'))
    if not again:
        Git.make(d).put('HEAD', f'ref: refs/heads/{o.get("-b") or o.get("--initial-branch") or dict(Git.OVER).get("init.defaultbranch", "main")}\n')
    if '-q' not in o and '--quiet' not in o:
        put(f'{"Reinitialized existing" if again else "Initialized empty"} Git repository in {d}/.git/')


def git_c_add(args):
    o, r, _ = git_args(args, ('-A', '--all', '-u', '--update', '-f', '--force', '-v', '.'))
    g = Git.find()
    ix = g.index()
    if not r and not {'-A', '--all', '-u', '--update'} & set(o):
        raise Fail('Nothing specified, nothing added.', 0)
    if ('-A' in o or '--all' in o or '-u' in o or '--update' in o) and not r:
        ok = lambda p: True
    else:
        ok = git_decide(g, r)
    hit = False
    for p in list(ix):
        if ok(p):
            hit = True
            c = g.changed(p, ix[p])
            if c == 'D':
                del ix[p]
            elif c and ix[p]['mode'] == '160000':
                ix[p]['sha'] = Git(os.path.join(g.top, p)).head()
            elif c:
                f = os.path.join(g.top, p)
                ix[p] = {'mode': g.mode(f), 'sha': g.write('blob', g.body(f)), 'stat': g.stat(f)}
    if '-u' not in o and '--update' not in o:
        new = list(g.untracked(ix))
        if '-f' in o or '--force' in o:
            new += [os.path.relpath(os.path.realpath(a), os.path.realpath(g.top)) for a in r if os.path.isfile(a)]
        for p in new:
            if ok(p) and p not in ix:
                hit = True
                f = os.path.join(g.top, p)
                if p.endswith('/'):
                    h = Git(f.rstrip('/')).head()
                    if h:
                        ix[p.rstrip('/')] = {'mode': '160000', 'sha': h, 'stat': None}
                else:
                    ix[p] = {'mode': g.mode(f), 'sha': g.write('blob', g.body(f)), 'stat': g.stat(f)}
    for a in r:
        if not hit and not os.path.lexists(a) and not glob_has(a):
            raise Fail(f"fatal: pathspec '{a}' did not match any files", 128)
    g.save_index(ix)


def git_glob(p, f):
    import fnmatch
    return fnmatch.fnmatchcase(f, p)


def glob_has(a):
    return any(c in a for c in '*?[')


def git_c_rm(args):
    o, r, _ = git_args(args, ('--cached', '-r', '-q', '-f', '--ignore-unmatch'))
    if '--cached' not in o:
        raise Fail('only git rm --cached is here: remove the file, then git add -A', 1)
    g = Git.find()
    ok, ix = git_decide(g, r) or (lambda p: False), g.index()
    gone = [p for p in ix if ok(p)]
    if not gone and '--ignore-unmatch' not in o:
        raise Fail(f"fatal: pathspec '{' '.join(r)}' did not match any files", 128)
    for p in gone:
        del ix[p]
        put(f"rm '{p}'")
    g.save_index(ix)


def git_c_commit(args):
    import time
    o, msgs, rest = {}, [], []
    args = list(args)
    while args:
        a = args.pop(0)
        if (a == '--message' or re.fullmatch('-[aqn]*m', a)) and args:
            msgs.append(args.pop(0))
            o |= {'-' + c: True for c in a[1:-1]} if a[1] != '-' else {}
        elif a.startswith('--message='):
            msgs.append(a[10:])
        elif a in ('-F', '--file') and args:
            f = args.pop(0)
            msgs.append(data(f).decode('utf-8', 'replace').rstrip('\n'))
        elif a in ('-a', '--all', '-q', '--quiet', '--allow-empty', '--no-verify', '-n', '--no-gpg-sign'):
            o[a] = True
        else:
            raise Fail(f'{a} is not supported here: git commit -m MESSAGE', 129)
    g = Git.find()
    if '-a' in o or '--all' in o:
        git_c_add(['-u'])
    s, e = g.settings(), os.environ.get
    who = [(e(f'GIT_{k}_NAME') or s.get('user.name'), e(f'GIT_{k}_EMAIL') or s.get('user.email')) for k in ('AUTHOR', 'COMMITTER')]
    if not all(n and m for n, m in who):
        raise Fail('fatal: unable to auto-detect who you are: ask the user, then git config user.name and user.email', 128)
    if not msgs:
        raise Fail('a message is needed: git commit -m MESSAGE', 1)
    ix, head = g.index(), g.head()
    tree = g.write_tree(ix)
    if '--allow-empty' not in o and (tree == g.resolve(head, 'tree') if head else not ix):
        put('nothing to commit')
        return 1
    z = time.strftime('%z')
    now = f'{int(time.time())} {z}'
    body = f'tree {tree}\n' + (f'parent {head}\n' if head else '') + f'author {who[0][0]} <{who[0][1]}> {now}\ncommitter {who[1][0]} <{who[1][1]}> {now}\n\n' + '\n\n'.join(msgs).strip('\n') + '\n'
    sha, b = g.write('commit', body.encode()), g.branch()
    g.put('refs/heads/' + b if b else 'HEAD', sha + '\n')
    if '-q' not in o and '--quiet' not in o:
        put(f'[{b or "detached HEAD"} {sha[:7]}] {msgs[0].splitlines()[0]}')


def git_c_status(args):
    o, r, _ = git_args(args, ('-s', '--short', '--porcelain', '-b', '--branch', '-u', '-uno', '-uall', '--ignored', '--untracked-files=no', '--porcelain=v1', '--ignore-submodules'))
    g = Git.find()
    ix, head = g.index(), g.head()
    old = g.tree(g.resolve(head, 'tree')) if head else {}
    ok = git_decide(g, r) or (lambda p: True)
    st = {}
    for p in sorted(set(ix) | set(old)):
        if not ok(p):
            continue
        x = 'A' if p not in old else 'D' if p not in ix else 'M' if old[p] != (ix[p]['mode'], ix[p]['sha']) else ' '
        y = (g.changed(p, ix[p]) or ' ') if p in ix else ' '
        if x + y != '  ':
            st[p] = x + y
    new = [] if '-uno' in o or '--untracked-files=no' in o else [p for p in g.untracked(ix) if ok(p)]
    if '-uall' not in o:
        dirs, short = {d for p in ix for d in (p[:i + 1] for i, c in enumerate(p) if c == '/')}, []
        for p in new:
            q = next((p[:i + 1] for i, c in enumerate(p) if c == '/' and p[:i + 1] not in dirs), p)
            if q not in short:
                short.append(q)
        new = short
    if {'-s', '--short', '--porcelain', '--porcelain=v1'} & set(o):
        if '-b' in o or '--branch' in o:
            put('## ' + (g.branch() or 'HEAD (no branch)'))
        for p, c in st.items():
            put(c + ' ' + p)
        for p in new:
            put('?? ' + p)
        return 0
    put(f'On branch {g.branch()}' if g.branch() else f'HEAD detached at {(head or "")[:7]}')
    word = {'A': 'new file', 'M': 'modified', 'D': 'deleted'}
    for title, i in (('Changes to be committed:', 0), ('Changes not staged for commit:', 1)):
        l = [(c[i], p) for p, c in st.items() if c[i] != ' ']
        if l:
            put('\n' + title)
            for c, p in l:
                put(f'\t{word[c]}:   {p}')
    if new:
        put('\nUntracked files:')
        for p in new:
            put('\t' + p)
    if not st and not new:
        put('nothing to commit, working tree clean')


def git_changes(g, a, b, ok):
    """Patches from one {path: (mode, sha)} to another."""
    for p in sorted(set(a) | set(b)):
        if a.get(p) != b.get(p) and (ok is None or ok(p)):
            f = lambda t: (b'Subproject commit ' + t[p][1].encode() + b'\n' if t[p][0] == '160000' else g.read(t[p][1])[1]) if p in t else None
            yield p, f(a), f(b), a.get(p, ('100644',))[0], b.get(p, ('100644',))[0]


def git_show_changes(changes, o):
    n = 0
    for p, x, y, m1, m2 in changes:
        n += 1
        if '--name-only' in o:
            put(p)
        elif '--name-status' in o:
            put(('A' if x is None else 'D' if y is None else 'M') + '\t' + p)
        elif '--stat' in o:
            d = git_patch(p, x, y, m1, m2).split(b'\n')
            put(f' {p} | +{sum(1 for l in d if l.startswith(b"+") and not l.startswith(b"+++ "))} -{sum(1 for l in d if l.startswith(b"-") and not l.startswith(b"--- "))}')
        elif '--quiet' not in o:
            outb(git_patch(p, x, y, m1, m2))
    return n


def git_split(g, r, dd):
    """Revisions and paths of an argument list."""
    if dd is not None:
        return r[:dd], r[dd:]
    i = 0
    while i < len(r) and (('..' in r[i] and not os.path.exists(r[i])) or (not os.path.lexists(r[i]) and g.name(r[i])) or r[i] == 'HEAD'):
        i += 1
    for a in r[i:]:
        if not os.path.lexists(a) and not glob_has(a):
            raise Fail(f"fatal: ambiguous argument '{a}': unknown revision or path not in the working tree.", 128)
    return r[:i], r[i:]


def git_c_diff(args):
    o, r, dd = git_args(args, ('--cached', '--staged', '--name-only', '--name-status', '--stat', '--quiet', '--exit-code', '--no-color', '--no-ext-diff', '-p', '--binary', '--full-index'))
    g = Git.find()
    revs, paths = git_split(g, r, dd)
    ok, ix = git_decide(g, paths), g.index()
    if len(revs) == 1 and '..' in revs[0]:
        revs = [x or 'HEAD' for x in revs[0].replace('...', '..').split('..')]
    t = lambda x: g.tree(g.resolve(x, 'tree'))
    if len(revs) == 2:
        ch = git_changes(g, t(revs[0]), t(revs[1]), ok)
    elif '--cached' in o or '--staged' in o:
        ch = git_changes(g, t(revs[0] if revs else 'HEAD') if revs or g.head() else {}, {p: (e['mode'], e['sha']) for p, e in ix.items()}, ok)
    else:
        base = t(revs[0]) if revs else {p: (e['mode'], e['sha']) for p, e in ix.items()}

        def work():
            for p in sorted(base):
                if ok and not ok(p):
                    continue
                m, s = base[p]
                same = ix[p] if p in ix and (ix[p]['mode'], ix[p]['sha']) == (m, s) else {'mode': m, 'sha': s}
                c = g.changed(p, same)
                f = os.path.join(g.top, p)
                if not c:
                    continue
                if m == '160000':
                    yield p, f'Subproject commit {s}\n'.encode(), f'Subproject commit {Git(f).head()}\n'.encode(), m, m
                else:
                    yield p, g.read(s)[1], None if c == 'D' else g.body(f), m, m if c == 'D' else g.mode(f)
            for p in sorted(ix) if revs else []:
                if p not in base and (not ok or ok(p)) and os.path.lexists(os.path.join(g.top, p)):
                    f = os.path.join(g.top, p)
                    yield p, None, g.body(f), '100644', g.mode(f)
        ch = work()
    n = git_show_changes(ch, o)
    return 1 if n and ('--quiet' in o or '--exit-code' in o) else 0


def git_format(g, c, fmt):
    an, ad, at = git_date(c['author'])
    cn, cd, ct = git_date(c['committer'])
    sub, _, body = c['msg'].partition('\n')
    e = lambda x: (x.split(' <')[0], x[x.rfind('<') + 1:].rstrip('>'))
    m = {'H': c['sha'], 'h': c['sha'][:7], 'T': c['tree'], 't': c['tree'][:7], 'P': ' '.join(c['parents']), 'p': ' '.join(x[:7] for x in c['parents']), 's': sub, 'b': body.strip('\n'), 'B': c['msg'].rstrip('\n'),
         'an': e(an)[0], 'ae': e(an)[1], 'ad': ad, 'at': str(int(at.timestamp())), 'ai': f'{at:%Y-%m-%d %H:%M:%S %z}', 'aI': at.isoformat(), 'as': f'{at:%Y-%m-%d}', 'cs': f'{ct:%Y-%m-%d}',
         'cn': e(cn)[0], 'ce': e(cn)[1], 'cd': cd, 'ct': str(int(ct.timestamp())), 'ci': f'{ct:%Y-%m-%d %H:%M:%S %z}', 'cI': ct.isoformat(), 'n': '\n', '%': '%', 'd': '', 'D': ''}
    return re.sub(r'%([ac][nedtiIs]|[A-Za-z%])', lambda k: m.get(k[1], k[0]), fmt)


def git_print(g, c, o):
    f = o.get('--format') or o.get('--pretty')
    if f == 'oneline' or ('--oneline' in o and not f):
        put((c['sha'] if '--oneline' not in o else c['sha'][:7]) + ' ' + c['msg'].partition('\n')[0])
    elif f and f not in ('medium', 'short', 'full'):
        put(git_format(g, c, re.sub(r'^t?format:', '', f)))
    else:
        n, d, _ = git_date(c['author'])
        put(f'commit {c["sha"]}\nAuthor: {n}\nDate:   {d}\n')
        put('\n'.join('    ' + l if l else '' for l in c['msg'].rstrip('\n').split('\n')))


def git_c_log(args):
    args = ['-n' + a[1:] if re.fullmatch(r'-\d+', a) else a for a in args]
    o, r, dd = git_args(args, ('--oneline', '--stat', '-p', '--patch', '--name-only', '--name-status', '--no-color', '--no-decorate', '--decorate', '--all', '--no-merges', '--reverse', '--first-parent'), ('-n', '--max-count', '--format', '--pretty', '--skip'))
    g = Git.find()
    revs, paths = git_split(g, r, dd)
    ok = git_decide(g, paths)
    left = integer(o.get('-n') or o.get('--max-count') or '1000000')
    start, hide = [], set()
    for x in revs or ['HEAD']:
        a, dots, b = x.partition('..')
        if dots:
            hide |= {c['sha'] for c in g.log([g.resolve(a or 'HEAD', 'commit')])}
            x = b or 'HEAD'
        elif x.startswith('^'):
            hide |= {c['sha'] for c in g.log([g.resolve(x[1:], 'commit')])}
            continue
        if x == 'HEAD' and not g.head():
            raise Fail(f"fatal: your current branch '{g.branch()}' does not have any commits yet", 128)
        start.append(g.resolve(x, 'commit'))
    if '--all' in o:
        start += [g.resolve(s, 'commit') for s in g.refs().values()]
    skip, got = integer(o.get('--skip', '0')), []
    for c in g.log(start):
        if c['sha'] in hide or ('--no-merges' in o and len(c['parents']) > 1):
            continue
        show = {'-p', '--patch', '--stat', '--name-only', '--name-status'} & set(o)
        ch = None
        if ok or show:
            ch = list(git_changes(g, g.tree(g.commit(c['parents'][0])['tree']) if c['parents'] else {}, g.tree(c['tree']), ok))
            if ok and not ch:
                continue
        if skip:
            skip -= 1
            continue
        if left <= 0:
            break
        left -= 1
        got.append((c, ch if show else None))
        if '--reverse' not in o:
            c, ch = got.pop()
            git_print(g, c, o)
            if ch is not None:
                git_show_changes(ch, o)
            if not ({'--oneline', '--format', '--pretty'} & set(o)):
                put('')
    for c, ch in reversed(got):
        git_print(g, c, o)
        if ch is not None:
            git_show_changes(ch, o)


def git_c_show(args):
    o, r, dd = git_args(args, ('--oneline', '--stat', '--name-only', '--name-status', '--no-color', '-s', '--no-patch', '-p'), ('--format', '--pretty'))
    g = Git.find()
    for x in r or ['HEAD']:
        sha = g.resolve(x)
        k, b = g.read(sha)
        if k == 'blob':
            outb(b)
        elif k == 'tree':
            put(f'tree {x}\n')
            for p in sorted({q.split('/')[0] + ('/' if '/' in q else '') for q in g.tree(sha)}):
                put(p)
        else:
            c = g.commit(g.resolve(sha, 'commit'))
            git_print(g, c, o)
            if '-s' not in o and '--no-patch' not in o:
                put('')
                git_show_changes(git_changes(g, g.tree(g.commit(c['parents'][0])['tree']) if c['parents'] else {}, g.tree(c['tree']), None), o)


def git_c_apply(args):
    o, r, _ = git_args(args, ('--check', '-R', '--reverse', '-v', '--verbose', '--ignore-whitespace', '--3way', '--index', '--whitespace=nowarn', '--whitespace=fix', '--whitespace=warn'), ('-p', '--directory'))
    if '--index' in o or '--3way' in o:
        raise Fail('--index and --3way are not supported here', 129)
    a = ['-p' + o.get('-p', '1'), '-f', '-s'] + (['-R'] if '-R' in o or '--reverse' in o else []) + (['--dry-run'] if '--check' in o else []) + (['-d', o['--directory']] if '--directory' in o else [])
    for f in r or ['-']:
        b = data(f)
        # All of it or none.
        if call(['patch', '--dry-run'] + [v for v in a if v != '--dry-run'], b, True)[0]:
            err(f'error: patch does not apply: {f}')
            return 1
        if '--check' not in o and call(['patch'] + a, b)[0]:
            return 1


def git_c_checkout(args):
    o, r, dd = git_args(args, ('-q', '--quiet', '-f', '--force', '--detach', '--recurse-submodules'), ('-b', '-B'))
    g = Git.find()
    if '-B' in o:
        raise Fail('-B is not supported here', 129)
    force = '-f' in o or '--force' in o
    if '-b' in o:
        if 'refs/heads/' + o['-b'] in g.refs():
            raise Fail(f"fatal: a branch named '{o['-b']}' already exists", 128)
        at = g.resolve(r[0] if r else 'HEAD', 'commit') if r or g.head() else None
        if r and at != g.head():
            g.checkout(g.resolve(at, 'tree'), force)
        if at:
            g.put('refs/heads/' + o['-b'], at + '\n')
        g.put('HEAD', f'ref: refs/heads/{o["-b"]}\n')
        put(f"Switched to a new branch '{o['-b']}'") if '-q' not in o else None
        return 0
    revs, paths = (r[:dd], r[dd:]) if dd is not None else (r[:1], r[1:]) if r and not os.path.lexists(r[0]) else ([], r)
    if dd is None and r and os.path.lexists(r[0]) and (g.name(r[0]) and len(r) == 1 and 'refs/heads/' + r[0] in g.refs()):
        revs, paths = r, []
    if paths:
        ok = git_decide(g, paths)
        if revs:
            g.checkout(g.resolve(revs[0], 'tree'), True, ok)
        else:
            ix = g.index()
            if not any(ok(p) for p in ix):
                raise Fail(f"error: pathspec '{paths[0]}' did not match any file(s) known to git", 1)
            t = g.write_tree(ix)
            g.checkout(t, True, ok)
        return 0
    if not revs:
        raise Fail('a branch, commit or file is needed', 128)
    n = revs[0]
    rs = g.refs()
    if 'refs/heads/' + n not in rs and 'refs/remotes/origin/' + n in rs and 'refs/tags/' + n not in rs:
        g.checkout(g.resolve(rs['refs/remotes/origin/' + n], 'tree'), force)
        g.put('refs/heads/' + n, rs['refs/remotes/origin/' + n] + '\n')
        g.put('HEAD', f'ref: refs/heads/{n}\n')
        said = f"Switched to a new branch '{n}'"
    else:
        at = g.resolve(n, 'commit')
        g.checkout(g.resolve(at, 'tree'), force)
        if 'refs/heads/' + n in rs and '--detach' not in o:
            g.put('HEAD', f'ref: refs/heads/{n}\n')
            said = f"Switched to branch '{n}'"
        else:
            g.put('HEAD', at + '\n')
            said = f'HEAD is now at {at[:7]} {g.commit(at)["msg"].partition(chr(10))[0]}'
    if '-q' not in o and '--quiet' not in o:
        err(said)


def git_c_switch(args):
    return git_c_checkout(['-b' if a in ('-c', '--create') else a for a in args])


def git_c_branch(args):
    o, r, _ = git_args(args, ('--show-current', '-a', '--all', '-r', '--list', '-v', '-q', '--no-color'))
    g = Git.find()
    if '--show-current' in o:
        if g.branch():
            put(g.branch())
        return 0
    if r and '--list' not in o:
        if 'refs/heads/' + r[0] in g.refs():
            raise Fail(f"fatal: a branch named '{r[0]}' already exists", 128)
        g.put('refs/heads/' + r[0], g.resolve(r[1] if r[1:] else 'HEAD', 'commit') + '\n')
        return 0
    if not g.branch():
        put(f'* (HEAD detached at {(g.head() or "")[:7]})')
    for k in sorted(g.refs()):
        if k.startswith('refs/heads/') and '-r' not in o:
            put(('* ' if k[11:] == g.branch() else '  ') + k[11:])
        elif k.startswith('refs/remotes/') and {'-a', '--all', '-r'} & set(o):
            put('  ' + (k[13:] if '-r' in o else k[5:]))


def git_c_tag(args):
    o, r, _ = git_args(args, ('-l', '--list'))
    g = Git.find()
    if r and '-l' not in o and '--list' not in o:
        if 'refs/tags/' + r[0] in g.refs():
            raise Fail(f"fatal: tag '{r[0]}' already exists", 128)
        g.put('refs/tags/' + r[0], g.resolve(r[1] if r[1:] else 'HEAD') + '\n')
        return 0
    for k in sorted(g.refs()):
        if k.startswith('refs/tags/') and (not r or git_glob(r[0], k[10:])):
            put(k[10:])


def git_c_ls_files(args):
    o, r, dd = git_args(args, ('-z', '-c', '--cached', '-s', '--stage', '-o', '--others', '--exclude-standard', '-m', '--modified', '--recurse-submodules', '--error-unmatch', '--full-name'))
    g = Git.find()
    ix, ok = g.index(), git_decide(g, r)
    here = '' if '--full-name' in o else os.path.relpath(os.path.realpath('.'), os.path.realpath(g.top)) + '/'
    here = '' if here == './' else here
    others = '-o' in o or '--others' in o
    l = list(g.untracked(ix)) if others else [p for p in sorted(ix) if not {'-m', '--modified'} & set(o) or g.changed(p, ix[p])]
    if others and ('-c' in o or '--cached' in o):
        l = sorted(l + list(ix))
    hit = 0
    for p in l:
        if p.startswith(here) and (ok is None or ok(p)):
            hit += 1
            out((f'{ix[p]["mode"]} {ix[p]["sha"]} 0\t' if '-s' in o or '--stage' in o else '') + p[len(here):] + ('\0' if '-z' in o else '\n'))
    if '--error-unmatch' in o and not hit:
        raise Fail(f"error: pathspec '{' '.join(r)}' did not match any file(s) known to git", 1)


def git_c_rev_parse(args):
    o, r, _ = git_args(args, ('--show-toplevel', '--git-dir', '--absolute-git-dir', '--is-inside-work-tree', '--abbrev-ref', '--verify', '-q', '--quiet', '--short', '--show-prefix', '--show-cdup', '--is-shallow-repository', '--is-bare-repository', '--git-common-dir'), ())
    g = Git.find()
    if '--show-toplevel' in o:
        put(g.top)
    if '--git-dir' in o or '--absolute-git-dir' in o or '--git-common-dir' in o:
        put('.git' if '--absolute-git-dir' not in o and os.path.realpath('.') == os.path.realpath(g.top) and g.dir.endswith('/.git') else g.dir)
    if '--is-inside-work-tree' in o:
        put('true')
    if '--is-bare-repository' in o:
        put('false')
    if '--is-shallow-repository' in o:
        put('true' if g.get('shallow') else 'false')
    if '--show-prefix' in o or '--show-cdup' in o:
        p = os.path.relpath(os.path.realpath('.'), os.path.realpath(g.top))
        put('' if p == '.' else p + '/' if '--show-prefix' in o else '../' * (p.count('/') + 1))
    for x in r:
        try:
            sha = g.resolve(x)
        except Fail:
            if '-q' in o or '--quiet' in o:
                return 1
            raise
        if '--abbrev-ref' in o:
            put((g.branch() or 'HEAD') if x in ('HEAD', '@') else x)
        else:
            put(sha[:7] if '--short' in o else sha)


def git_c_describe(args):
    o, r, _ = git_args(['--dirty=-dirty' if a == '--dirty' else a for a in args], ('--tags', '--always', '--long', '--all', '--first-parent'), ('--abbrev', '--match', '--dirty', '--exclude'))
    g = Git.find()
    at = g.resolve(r[0] if r else 'HEAD', 'commit')
    tags = {}
    for k, s in g.refs().items():
        if k.startswith('refs/tags/') and ('--match' not in o or git_glob(o['--match'], k[10:])):
            kind = g.read(s)[0]
            if kind == 'tag' or '--tags' in o:
                tags[g.resolve(s, 'commit')] = k[10:]
    n, d = integer(o.get('--abbrev', '7')), ''
    if '--dirty' in o and any(g.changed(p, e) for p, e in g.index().items()):
        d = o['--dirty']
    for i, c in enumerate(g.log([at])):
        if c['sha'] in tags:
            t = tags[c['sha']]
            put((t if (not i and '--long' not in o) or not n else f'{t}-{i}-g{at[:max(n, 4)]}') + d)
            return 0
    if '--always' in o:
        put(at[:max(n, 4)] + d)
        return 0
    raise Fail('fatal: No names found, cannot describe anything.', 128)


def git_c_config(args):
    o, r, _ = git_args(args, ('--global', '--local', '--get', '--list', '-l', '--add', '--replace-all', '--unset', '--get-all', '--bool'), ('-f', '--file', '--type', '--get-regexp'))
    if '--unset' in o or '-f' in o or '--file' in o or '--get-regexp' in o:
        raise Fail('this form of git config is not here', 1)
    if '--list' in o or '-l' in o:
        g = Git.find(False)
        for k, v in (g.settings() if g else dict(Git.OVER)).items():
            put(f'{k}={v}')
        return 0
    if not r:
        raise Fail('usage: git config [--global] NAME [VALUE]', 129)
    sec, _, key = r[0].rpartition('.')
    k = sec.partition('.')[0].lower() + ('.' + sec.partition('.')[2] if '.' in sec else '') + '.' + key.lower()
    g = Git.find(len(r) > 1 and '--global' not in o)
    home = os.path.join(os.environ.get('HOME', ''), '.gitconfig')
    if len(r) == 1:
        s = g.settings() if g else dict((Git.parse(text(home)) if os.path.exists(home) else []) + Git.OVER)
        if k not in s:
            return 1
        put(s[k])
        return 0
    f = home if '--global' in o else g.file('config')
    ls = text(f).splitlines() if os.path.exists(f) else []
    a, _, b = sec.partition('.')
    head = f'[{a} "{b}"]' if b else f'[{a}]'
    i = next((i for i, l in enumerate(ls) if l.strip().lower() == head.lower()), None)
    new = f'\t{key} = {r[1]}'
    if i is None:
        ls += [head, new]
    else:
        j = i + 1
        while j < len(ls) and not ls[j].strip().startswith('['):
            if ls[j].partition('=')[0].strip().lower() == key.lower():
                ls[j] = new
                break
            j += 1
        else:
            ls.insert(i + 1, new)
    with open(f, 'w') as h:
        h.write('\n'.join(ls) + '\n')


def git_c_remote(args):
    o, r, _ = git_args(args, ('-v', '--verbose'))
    g = Git.find()
    rem = {k[7:-4]: v for k, v in g.settings().items() if k.startswith('remote.') and k.endswith('.url')}
    if not r:
        for n, u in rem.items():
            put(f'{n}\t{u} (fetch)\n{n}\t{u} (push)' if o else n)
    elif r[0] == 'get-url' and r[1:]:
        if r[1] not in rem:
            raise Fail(f"error: No such remote '{r[1]}'", 2)
        put(rem[r[1]])
    elif r[0] in ('add', 'set-url') and len(r) == 3:
        if (r[1] in rem) == (r[0] == 'add'):
            raise Fail(f'error: remote {r[1]} ' + ('already exists.' if r[1] in rem else 'is not there'), 3)
        git_c_config([f'remote.{r[1]}.url', r[2]])
    else:
        raise Fail('git remote, -v, get-url, set-url and add are here', 1)


def git_c_cat_file(args):
    o, r, _ = git_args(args, ('-p', '-t', '-s', '-e'))
    g = Git.find()
    if len(r) == 2 and not o:
        o, r = {'-p': True}, [g.resolve(r[1], r[0])]
    sha = g.resolve(r[0])
    k, b = g.read(sha)
    if '-t' in o:
        put(k)
    elif '-s' in o:
        put(str(len(b)))
    elif '-p' in o:
        if k == 'tree':
            i = 0
            while i < len(b):
                sp, z = b.index(b' ', i), b.index(b'\0', i)
                m = b[i:sp].decode().zfill(6)
                put(f'{m} {"tree" if m[0] == "0" else "commit" if m == "160000" else "blob"} {b[z + 1:z + 21].hex()}\t{os.fsdecode(b[sp + 1:z])}')
                i = z + 21
        else:
            outb(b)


# ---- brew: what a script asks of it, answered by what is here ----

BREW = {'cmake': 'cmake', 'ninja': 'ninja', 'python': 'python3', 'python3': 'python3', 'llvm': 'clang', 'gcc': 'clang', 'make': 'make', 'coreutils': 'ls', 'gnu-sed': 'sed', 'gawk': 'awk', 'grep': 'grep',
        'findutils': 'find', 'gnu-tar': 'tar', 'wget': 'wget', 'curl': 'curl', 'jq': 'jq', 'git': 'git', 'xz': 'xz', 'sqlite': 'sqlite3', 'ripgrep': 'rg', 'tree': 'tree', 'unzip': 'unzip', 'zip': 'zip', 'bash': 'bash', 'glslang': 'glslc', 'shaderc': 'glslc'}


@command('brew')
def brew(name, args):
    import shutil
    what = [a for a in args[1:] if not a.startswith('-')]
    if args[:1] in (['update'], ['upgrade'], ['tap'], ['cleanup']):
        return 0
    if args[:1] == ['--prefix']:
        put(xc_tools())
        return 0
    if args[:1] not in (['install'], ['list'], ['bundle']) or not what:
        raise Fail('this is not Homebrew: brew install only says whether a tool is here already', 1)
    bad = []
    for w in what:
        t = BREW.get(re.sub(r'@.*', '', w), w)
        if t in CMDS or shutil.which(t):
            put(f'{w}: here ({t}), Henge\'s')
        else:
            bad.append(w)
    if bad:
        raise Fail('not here, and cannot be installed: ' + ' '.join(bad) + '. A library: get its source (git clone, github_download) and build it with the project.', 1)


# ---- framecheck: flicker in a run of screenshots ----

def pic_read(path):
    """(width, height, rgb bytes) of a PNG, PPM or BMP."""
    import struct
    import zlib
    with open(path, 'rb') as f:
        b = f.read()
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        i, z, pal, lace, depth = 8, [], b'', 1, 0
        while i + 8 <= len(b):
            n, k = struct.unpack('>I4s', b[i:i + 8])
            x = b[i + 8:i + 8 + n]
            i += n + 12
            if k == b'IHDR':
                w, h, depth, kind, _, _, lace = struct.unpack('>IIBBBBB', x)
            elif k == b'PLTE':
                pal = x
            elif k == b'IDAT':
                z.append(x)
        if depth not in (8, 16) or lace:
            raise Fail(f'{path}: this kind of PNG is not read here', 1)
        ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[kind]
        bpp = ch * depth // 8
        d, st = zlib.decompress(b''.join(z)), w * bpp
        prev, rows = bytearray(st), []
        for y in range(h):
            t, r = d[y * (st + 1)], bytearray(d[y * (st + 1) + 1:(y + 1) * (st + 1)])
            if t == 1:
                for c in range(bpp, st):
                    r[c] = (r[c] + r[c - bpp]) & 255
            elif t == 2:
                r = bytearray((p + q) & 255 for p, q in zip(r, prev))
            elif t == 3:
                for c in range(st):
                    r[c] = (r[c] + (((r[c - bpp] if c >= bpp else 0) + prev[c]) >> 1)) & 255
            elif t == 4:
                for c in range(bpp):
                    r[c] = (r[c] + prev[c]) & 255
                for c in range(bpp, st):
                    a, u, ul = r[c - bpp], prev[c], prev[c - bpp]
                    pa, pb, pc = abs(u - ul), abs(a - ul), abs(a + u - 2 * ul)
                    r[c] = (r[c] + (a if pa <= pb and pa <= pc else u if pb <= pc else ul)) & 255
            rows.append(bytes(r))
            prev = r
        raw = b''.join(rows)
        if depth == 16:
            raw = raw[::2]
        out_ = bytearray(w * h * 3)
        for c in range(3):
            if kind == 3:
                out_[c::3] = raw.translate(bytes((pal[c::3] + bytes(256))[:256]))
            else:
                out_[c::3] = raw[c if ch > 2 else 0::ch]
        return w, h, bytes(out_)
    m = re.match(rb'P6\s+(?:#[^\n]*\s+)*(\d+)\s+(\d+)\s+255\s', b)
    if m:
        w, h = int(m[1]), int(m[2])
        return w, h, b[m.end():m.end() + w * h * 3]
    if b[:2] == b'BM':
        at, = struct.unpack('<I', b[10:14])
        w, h, _, bits, comp = struct.unpack('<iiHHI', b[18:34])
        if bits in (24, 32) and comp in (0, 3):
            st, n = (w * bits // 8 + 3) // 4 * 4, bits // 8
            rows = [b[at + y * st:at + y * st + w * n] for y in range(abs(h))]
            raw = b''.join(rows if h < 0 else rows[::-1])
            out_ = bytearray(w * abs(h) * 3)
            for c in range(3):
                out_[c::3] = raw[2 - c::n]
            return w, abs(h), bytes(out_)
    raise Fail(f'{path}: PNG, PPM and BMP pictures are read here', 1)


def pic_write(path, w, h, rgb):
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    raw = b''.join(b'\0' + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


@command('framecheck')
def framecheck(name, args):
    """Finds what is there in one frame only, or gone in one only: a cell unlike both its neighbours in time, which are alike."""
    o, r, _ = git_args(args, ('-h', '--help'), ('-o', '--threshold', '--cell', '--area', '--size'))
    if not r or '-h' in o or '--help' in o:
        put('usage: framecheck [-o worst.png] [--threshold 24] [--cell 8] [--area 0.1] [--size WxH] FRAMES...   (a folder, or PNG, PPM or BMP files in order)\n'
            '--size: the files are raw RGBA of this size, 4 bytes a pixel, one or more pictures in each.\n'
            'Looks at every three frames in a row for parts that change and change back (flicker) and for frames of one colour.\n'
            '--area: the least part of the picture, in percent. -o: the worst place as three pictures side by side. Exit code 1: something was found.\n'
            'Frames must follow one another (none skipped). Things that blink or move fast on purpose are found too: look at the picture.')
        return 0 if args else 2
    raw = re.fullmatch(r'(\d+)x(\d+)', o.get('--size', ''))
    if '--size' in o and (not raw or not int(raw[1]) * int(raw[2])):
        raise Fail('--size is WxH, like 640x480', 2)
    files = []
    for a in r:
        if os.path.isdir(a):
            l = [f for f in os.listdir(a) if f.lower().endswith(('.rgba', '.raw', '.bin') if raw else ('.png', '.ppm', '.bmp'))]
            l.sort(key=lambda f: [(0, int(x), '') if x.isdigit() else (1, 0, x) for x in re.split(r'(\d+)', f)])
            files += [os.path.join(a, f) for f in l]
        else:
            files.append(a)
    if raw:
        w, h = int(raw[1]), int(raw[2])
        each = []
        for f in files:
            n = os.path.getsize(f)
            if not n or n % (w * h * 4):
                raise Fail(f'{f}: {n} bytes are not whole {w}x{h} RGBA pictures', 1)
            each += [f'{f}#{i}' for i in range(n // (w * h * 4))]
        files = each

    def load(f):
        if not raw:
            return pic_read(f)
        p, _, i = f.rpartition('#')
        with open(p, 'rb') as k:
            k.seek(int(i) * w0 * h0 * 4)
            b = k.read(w0 * h0 * 4)
        rgb = bytearray(w0 * h0 * 3)
        for c in range(3):
            rgb[c::3] = b[c::4]
        return w0, h0, bytes(rgb)
    w0, h0 = (int(raw[1]), int(raw[2])) if raw else (0, 0)
    if len(files) < 3:
        raise Fail('three frames or more are needed', 2)
    T, C, area = float(o.get('--threshold', '24')), max(2, integer(o.get('--cell', '8'))), float(o.get('--area', '0.1'))
    hist, found, size, flat = [], [], None, []
    for n, f in enumerate(files):
        w, h, b = load(f)
        if size and size != (w, h):
            raise Fail(f'{f}: another size ({w}x{h}) than the frames before', 1)
        if w < C or h < C:
            raise Fail(f'{f}: too small', 1)
        size = (w, h)
        cw, rows = w // C, []
        for y in range(h // C):
            acc = [0] * (cw * 3)
            for q in range(0, C, 2):
                ln = b[(y * C + q) * w * 3:(y * C + q) * w * 3 + cw * C * 3]
                for s in range(0, C, 2):
                    for c in range(3):
                        acc[c::3] = [u + v for u, v in zip(acc[c::3], ln[s * 3 + c::C * 3])]
            rows += acc
        k = len(range(0, C, 2)) ** 2
        v = [x / k for x in rows]
        if all(max(v[c::3]) - min(v[c::3]) < 2 for c in range(3)):
            flat.append(n)
        hist = (hist + [v])[-3:]
        if len(hist) == 3:
            p, m, q = hist
            hit = sorted({i // 3 for i in range(len(m)) if abs(m[i] - p[i]) > T and abs(m[i] - q[i]) > T and (m[i] > p[i]) == (m[i] > q[i]) and abs(p[i] - q[i]) < T / 2})
            if hit and len(hit) * 300 >= area * len(m):
                xs, ys = [i % cw for i in hit], [i // cw for i in hit]
                found.append((len(hit), n - 1, (min(xs) * C, min(ys) * C, (max(xs) + 1) * C, (max(ys) + 1) * C)))
    put(f'{len(files)} frames of {size[0]}x{size[1]}')
    if flat:
        put(f'{len(flat)} of one colour: ' + ', '.join(os.path.basename(files[i]) for i in flat[:20]))
    total = (size[0] // C) * (size[1] // C)
    if not found:
        put('no flicker found')
        return 1 if flat else 0
    put(f'{len(found)} frame(s) with flicker:')
    for n, i, box in found[:60]:
        put(f'  {os.path.basename(files[i])}: {n * 100 / total:.1f}% of the picture, x {box[0]}-{box[2]}, y {box[1]}-{box[3]}')
    if '-o' in o:
        n, i, (x0, y0, x1, y1) = max(found)
        x0, y0, x1, y1 = max(0, x0 - 16), max(0, y0 - 16), min(size[0], x1 + 16), min(size[1], y1 + 16)
        ps = [load(files[j])[2] for j in (i - 1, i, i + 1)]
        gap = b'\xff\x00\xff' * 4
        img = b''.join(gap.join(p[(y * size[0] + x0) * 3:(y * size[0] + x1) * 3] for p in ps) for y in range(y0, y1))
        pic_write(o['-o'], (x1 - x0) * 3 + 8, y1 - y0, img)
        put(f'{o["-o"]}: {os.path.basename(files[i])} in the middle, the frames before and after beside it')
    return 1


# ---- main ----

@command('help')
def help_(name, args):
    put('usage: python3 devtools.py NAME ARGS...   names:')
    ln = ' '
    for n in NAMES:
        if len(ln) + len(n) > 98:
            put(ln)
            ln = ' '
        ln += ' ' + n
    put(ln)


NAMES = sorted(CMDS)


def main(argv):
    args = list(argv)
    if args[1:] == ['--names']:
        out(''.join(n + '\n' for n in NAMES))
        State.out.flush()
        return 0
    name = os.path.basename(args.pop(0)) if args and os.path.basename(args[0]) in CMDS else None
    if name is None:
        if args:
            name = os.path.basename(args[1]) if len(args) > 1 else 'help'
            if name in ('--help', '-h', '--list'):
                name = 'help'
            args = args[2:]
    if name not in CMDS:
        sys.stderr.write(f'devtools: no command {name}; try help\n')
        return 127
    try:
        try:
            rc = dispatch(name, args)
        finally:
            State.out.flush()
        return rc
    except BrokenPipeError:
        State.out.b.clear()
        return 141
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main(sys.argv))
