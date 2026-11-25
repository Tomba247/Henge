#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests tools/phone-kit/devtools.py on small inputs: against the Mac's own tools where they agree, else against known output."""
import http.server
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools', 'phone-kit', 'devtools.py')
TOOL = os.path.abspath(TOOL)
SYS = {'expr': '/bin/expr', 'date': '/bin/date', 'realpath': '/bin/realpath', 'md5': '/sbin/md5', 'md5sum': '/sbin/md5sum'}
passed, failed, T0 = 0, [], time.time()
ENV = {**os.environ, 'LC_ALL': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'TZ': 'UTC'}
for k in ('MAKEFLAGS', 'MAKELEVEL', 'TMPDIR_'):
    ENV.pop(k, None)
top = os.path.realpath(tempfile.mkdtemp())


def run(cmd, inp=b'', cwd=None):
    if isinstance(inp, str):
        inp = inp.encode()
    p = subprocess.run(cmd, input=inp, capture_output=True, cwd=cwd, env=ENV, timeout=30)
    return p.returncode, p.stdout, p.stderr


def ours(args, inp=b'', cwd=None):
    return run([sys.executable, TOOL] + list(args), inp, cwd)


def check(what, ok, detail=''):
    global passed
    if ok:
        passed += 1
    else:
        failed.append(f'{what}\n    {detail}')


def eq(args, want, inp=b'', rc=0, cwd=None):
    """Our output must be `want`."""
    c, o, e = ours(args, inp, cwd or top)
    if isinstance(want, str):
        want = want.encode()
    check(args, (o == want or want is None) and c == rc, f'rc {c} want {rc}\n    got  {o!r}\n    want {want!r}\n    err {e.decode()[-600:]}')
    return o


def same(args, inp=b'', cwd=None, loose=False):
    """Our output must be that of the Mac's tool."""
    t = SYS.get(args[0], '/usr/bin/' + args[0])
    a, b = ours(args, inp, cwd or top), run([t] + list(args[1:]), inp, cwd or top)
    x, y = a[1], b[1]
    if loose:
        x, y = (b'\n'.join(b' '.join(z.split()) for z in w.split(b'\n')) for w in (x, y))
    check(args, x == y and a[0] == b[0], f'rc {a[0]} mac {b[0]}\n    got {x!r}\n    mac {y!r}\n    err {a[2].decode()[-600:]} | {b[2].decode()[-200:]}')


def w(path, s, mode=None):
    path = os.path.join(top, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(s if isinstance(s, bytes) else s.encode())
    if mode:
        os.chmod(path, mode)
    return path


def r(path):
    with open(os.path.join(top, path), 'rb') as f:
        return f.read()


TXT = 'alpha one\nBeta two\ngamma [three]\n\nalpha four a+b\nfoo.bar foo-bar foobar\nx=1; y=22;\ntab\there\n'
NUMS = ''.join(f'{i}\n' for i in range(1, 13))
CSV = 'name,age,city\nbob,30,paris\nann,25,rome\ncat,41,oslo\nann,7,rome\n'


def t_grep():
    w('t.txt', TXT)
    w('d/a.c', 'int main\nfoo bar\nFoo\n')
    w('d/b.h', 'foo\n')
    w('d/sub/c.c', 'a\nb\nfoo\nc\nd\n')
    w('bin', b'ab\0foo\n\xff\xfe\n')
    w('pats', 'alpha\nfoo\n')
    for a in (['alpha'], ['-n', 'a'], ['-v', 'a'], ['-c', 'a'], ['-i', 'BETA'], ['-o', 'fo[a-z]*'], ['-w', 'foo'], ['-x', 'Beta two'],
              ['-E', '(alpha|Beta) (one|two)'], ['-E', 'a{2}|o{2}'], ['-F', 'a+b'], ['[three]'], ['a\\+b'], ['-E', '^$'], ['-e', 'one', '-e', 'two'],
              ['-f', 'pats'], ['-A1', 'Beta'], ['-B1', 'four'], ['-C1', '-n', 'gamma'], ['-m1', 'alpha'], ['-q', 'zzz'], ['-l', 'a'],
              ['-H', 'one'], ['-hn', 'one'], ['[[:digit:]]\\{2\\}'], ['-E', '\\bfoo\\b'], ['^\\(a\\).*\\1'], ['nomatch'],
              ['-n', '-A1', '-E', 'alpha|tab'], ['-E', '[^]a]=|[]]'], ['-b', 'foo'], ['-on', 'o'], ['-E', '\\.bar|=2+'], ['-c', '^$']):
        same(['grep'] + a + ['t.txt'])
    same(['grep', '-n', 'a', 't.txt', 'd/a.c'])
    same(['egrep', 'a|b', 't.txt'])
    same(['fgrep', '.', 't.txt'])
    same(['grep', 'x'], 'ax\nb\nxx')
    same(['grep', '-c', 'foo', 'bin'])
    same(['grep', '-a', 'foo', 'bin'])
    same(['grep', '-s', 'foo', 'nofile'])
    eq(['grep', '-rn', 'foo', 'd'], 'd/a.c:2:foo bar\nd/b.h:1:foo\nd/sub/c.c:3:foo\n')
    eq(['grep', '-rl', 'foo', '.', '--include=*.c', '--exclude-dir=sub'], './d/a.c\n')
    eq(['grep', '-rlZ', 'foo', 'd', '--exclude', '*.c'], 'd/b.h\0')
    eq(['grep', '-oP', r'foo\Kb\w+', 't.txt'], 'bar\n')
    eq(['grep', '-P', r'\d{2}(?=;)', 't.txt'], 'x=1; y=22;\n')
    eq(['grep', '--color=always', 'two', 't.txt'], 'Beta \x1b[01;31m\x1b[Ktwo\x1b[m\x1b[K\n')
    eq(['grep', '-rn', '-C1', 'foo', 'd/sub', 'd/b.h'], 'd/sub/c.c-2-b\nd/sub/c.c:3:foo\nd/sub/c.c-4-c\n--\nd/b.h:1:foo\n')
    eq(['grep', 'foo', 'bin'], 'Binary file bin matches\n')
    eq(['grep', '-I', 'foo', 'bin'], '', rc=1)
    eq(['grep', '--bogus', 'x'], '', rc=2)
    eq(['grep', '-E', '(', 't.txt'], '', rc=2)
    w('d/.gitignore', 'ig/\n*.log\n')
    w('d/ig/x.c', 'foo\n')
    w('d/y.log', 'foo\n')
    eq(['rg', 'foo', 'd'], 'd/a.c:foo bar\nd/b.h:foo\nd/sub/c.c:foo\n')
    eq(['rg', '-l', '-g', '*.h', 'foo', 'd'], 'd/b.h\n')
    eq(['rg', '-n', '-t', 'c', '-S', 'foo', 'd/sub'], 'd/sub/c.c:3:foo\n')
    eq(['rg', '--files', 'd'], 'd/a.c\nd/b.h\nd/sub/c.c\n')


def t_find():
    for p in ('f/a.c', 'f/B.txt', 'f/x/y.c', 'f/x/z/deep.h', 'f/.hid'):
        w(p, 'x' * 10)
    w('f/empty', '')
    w('f/big', b'a' * 3000)
    os.utime(os.path.join(top, 'f/B.txt'), (0, 0))
    os.symlink('a.c', os.path.join(top, 'f/link'))
    for a in (['f', '-type', 'f'], ['f', '-name', '*.c'], ['f', '-iname', 'b*'], ['f', '-maxdepth', '1'], ['f', '-mindepth', '2', '-type', 'd'],
              ['f', '-path', '*x/*'], ['f', '-size', '+1k'], ['f', '-size', '-5c'], ['f', '-empty'], ['f', '-mtime', '+1'], ['f', '-mtime', '-1'],
              ['f', '(', '-name', '*.c', '-o', '-name', '*.h', ')', '-print'], ['f', '!', '-name', '*.c', '-type', 'f'],
              ['f', '-name', 'x', '-prune', '-o', '-type', 'f', '-print'], ['f', '-not', '-type', 'f'], ['f', '-type', 'l'],
              ['f', '-newer', 'f/B.txt', '-type', 'f'], ['f/nope']):
        a1, b1 = ours(['find'] + a, b'', top), run(['/usr/bin/find'] + a, b'', top)
        check(['find'] + a, sorted(a1[1].split(b'\n')) == sorted(b1[1].split(b'\n')) and a1[0] == b1[0], f'{a1} {b1}')
    eq(['find', 'f', '-name', '*.c', '-print0'], 'f/a.c\0f/x/y.c\0')
    eq(['find', 'f', '-name', '*.c', '-exec', 'grep', '-c', 'x', '{}', '+'], 'f/a.c:1\nf/x/y.c:1\n')
    eq(['find', 'f', '-name', '*.c', '-exec', 'echo', 'got {}', ';'], 'got f/a.c\ngot f/x/y.c\n')
    eq(['find', 'f', '-name', '*.h', '-printf', '%f %s %P\\n'], 'deep.h 10 x/z/deep.h\n')
    eq(['find', 'f', '-bogus'], '', rc=2)
    eq(['find', 'f/x', '-delete'], '')
    check('find -delete', not os.path.exists(os.path.join(top, 'f/x')))


def t_sed():
    w('t.txt', TXT)
    w('n.txt', NUMS)
    w('ins', 'inserted\n')
    for a in (['s/a/X/'], ['s/a/X/g'], ['s/a/X/2'], ['-n', '/alpha/p'], ['2d'], ['$d'], ['2,4d'], ['/Beta/,/foo/d'], ['-n', '$='], ['1!d'],
              ['-e', 's/a/b/', '-e', 's/b/c/g'], ['s|/|_|g'], ['-E', 's/([a-z]+) ([a-z]+)/\\2 \\1/'], ['s/\\([a-z]*\\) \\(.*\\)/\\2-\\1/'],
              ['s/foo/[&]/g'], ['/^$/d'], ['y/abc/xyz/'], ['2q'], ['-n', 'x;p'], ['G'], ['-n', 'H;${x;s/\\n/,/g;p;}'], ['$!N;s/\\n/ /'],
              ['-n', '2{p;p;}'], ['/alpha/{s/a/Q/;n;s/a/Z/;}'], ['=', ], ['-n', 'l'], ['s/x*/-/g'], ['2r ins'], ['s/[^,]*$/<&>/'], ['s/[/]/x/'],
              ['-E', 's/o+$|t/T/g'], ['2,/o/s/$/!/'], ['1,3!s/a/b/'], ['/gamma/!d'], ['s/.*/"&"/'], ['5,2p'],
              ['1i\\\nhello'], ['$a\\\nbye'], ['2c\\\nchanged'], [':a\ns/o/0/\nta'], ['-n', '3,5p']):
        same(['sed'] + a + ['t.txt'])
        same(['sed'] + a + ['n.txt'])
    same(['sed', 's/a/b/'], 'xa\nya')
    same(['sed', '-n', '$p', 't.txt', 'n.txt'])
    eq(['sed', ':a;N;$!ba;s/\\n/,/g'], '1,2,3\n', '1\n2\n3\n')
    eq(['sed', '0~3d', 'n.txt'], '1\n2\n4\n5\n7\n8\n10\n11\n')
    eq(['sed', '-n', '/5/,+2p;/^11/,$p', 'n.txt'], '5\n6\n7\n11\n12\n')
    eq(['sed', '-E', 's/(a)(l)/\\U\\1\\E\\2/;s/one/a\\tb/I'], 'Alpha a\tb\n', 'alpha ONE\n')
    eq(['sed', '1i\\  top'], '  top\na\n', 'a\n')
    eq(['sed', '-e', '1i top', '-e', '$a end'], 'top\na\nend\n', 'a\n')
    eq(['sed', '-s', '-n', '$p', 'n.txt', 'n.txt'], '12\n12\n')
    for a, bak in ((['-i', 's/a/b/'], None), (['-i', '', 's/a/b/'], None), (['-i.bak', 's/a/b/'], 'i.txt.bak'), (['-i', '.old', 's/a/b/'], 'i.txt.old'),
                   (['-i', '-e', 's/a/b/'], None), (['-ni', 's/a/b/p'], None), (['-Ei', 's/(a)/b/'], None)):
        w('i.txt', 'a\na')
        eq(['sed'] + a + ['i.txt'], '')
        check(['sed'] + a, r('i.txt') == b'b\nb' and (not bak or r(bak) == b'a\na'), repr(r('i.txt')))
    eq(['sed', 'k'], '', rc=2)
    eq(['sed', 's/\\xff/y/'], b'\xff\xfea\n', b'\xff\xfea\n')


def t_awk():
    w('c.csv', CSV)
    w('t.txt', TXT)
    w('n.txt', NUMS)
    progs = ['{print $1}', '{print $NF, NF}', 'NR==2', '/a/', 'NR%2{print NR": "$0}', 'END{print NR}', '{s+=$1} END{print s, s/NR}',
             'BEGIN{FS=","}{print $2}', '{$2="X"; print}', '{NF=1; print; $3="z"; print}', '/Beta/,/foo/', '!seen[$1]++', '{a[$1]++} END{n=0; for(k in a)n+=a[k]; print n}',
             'BEGIN{printf "%d %5.2f|%-5s|%05d %x %c %e %%\\n", 42.9, 3.14159, "ab", 42, 255, 65, 12345.678}',
             'BEGIN{x="abc"; print length(x), substr(x,2), substr(x,1,2), index(x,"c"), toupper(x)}',
             'BEGIN{s="foo bar foo"; n=gsub(/foo/,"[&]",s); print n, s; sub("bar","\\\\&",s); print s}',
             'BEGIN{n=split("a:b:c",a,":"); for(i=1;i<=n;i++)print i,a[i]; delete a[2]; print (2 in a), (1 in a)}',
             'function f(x,y){y=x*2; return y} BEGIN{print f(3), f("4")}', 'function r(n){return n<2?1:n*r(n-1)} BEGIN{print r(10)}',
             'function fill(a,n, i){for(i=0;i<n;i++)a[i]=i*i} BEGIN{fill(q,4); print q[3], length(q)}',
             'BEGIN{i=5; while(i-->0){if(i==3)continue; if(i==1)break; print i}; do{print "d",i}while(i++<2)}',
             'BEGIN{print (1==1.0), ("a"<"b"), ("10"<"9"), (10<9), 2^10, -3%2, 1e6, 0.1+0.2, 100000000000, 1/3}',
             'BEGIN{print match("foobar",/o+/), RSTART, RLENGTH; print match("x",/y/), RSTART, RLENGTH}',
             'BEGIN{print length()}', 'BEGIN{a["x",1]=5; for(k in a){split(k,p,SUBSEP); print p[1],p[2]}; if(("x",1) in a)print "yes"}',
             'BEGIN{OFS="-"; $0="a b c"; $1=$1; print; print NF}', 'BEGIN{print substr("hello",2,3) "x" 1+2 "y"}', '$1>5', '$1=="alpha"{next}{print}',
             'BEGIN{x=1; x+=2; x*=3; x-=1; x/=2; x^=2; x%=7; print x, x++, ++x, x--, -x, !x}', 'BEGIN { print 1,2 > "/dev/stdout" }',
             'BEGIN{while((getline l < "n.txt")>0)n+=l; print n; close("n.txt"); getline l < "n.txt"; print l}', 'NR==1{getline; print} NR==3{getline x; print x, NR}',
             'BEGIN{exit 3} END{print "end"}', 'BEGIN{print toupper(substr("abc",1,1)) tolower("XYZ")}', '{print length($0) | "sort -rn"}',
             'BEGIN{print int(-3.7), sqrt(16), exp(0), log(1), atan2(0,-1), sin(0), cos(0)}', 'BEGIN { if (!("a" in x)) print "no"; print length(x) }',
             'BEGIN{"echo hi there" | getline v; print v}', '{ print > "/dev/stderr" }', 'BEGIN{print "a" > "o1"; print "b" > "o1"; close("o1"); while((getline z<"o1")>0)print "r",z}',
             'BEGIN { print -2^2, 2^3^2, 1 - 1 - 1, 2 * 3 + 4, "x" > "/dev/stdout" }', '/^[[:alpha:]]+ / && !/one/ || /=/ {print "m", $0}', 'BEGIN{$3="c"; print NF, $0}',
             'BEGIN{a="10"; b=9; print (a>b), (a""> b"")}', '$2 ~ /^t/ {print $2} $1 !~ "a" {print "na", $1}', 'BEGIN{print substr("x",5) "|" 1 " " -1}',
             '{ n[length($0)] = $0 } END { print n[0] "|" n[8] }', 'BEGIN{printf "%s %s\\n", "a", "b"; printf("%3d|%-3d|%+d\\n", 1, 2, 3)}',
             'BEGIN{RS=""}{print NR": "$1; n++}END{print n}', 'BEGIN{CONVFMT="%.2g"; a=3.14159; b=a ""; print b; OFMT="%.3f"; print a}', 'END{print $0, NF}',
             'BEGIN{print ARGC, ARGV[1]}', '{ x = x $1 ; } ; END { print x }', 'BEGIN{print 010+0, "3abc"+1, ".5."+0}', 'BEGIN { getline; print "got " $0 }']
    for p in progs:
        same(['awk', p, 't.txt'])
        if '$' in p:
            same(['awk', p, 'n.txt'])
    same(['awk', '-F,', 'NR>1{t[$3]+=$2} END{print t["rome"], t["oslo"]}', 'c.csv'])
    same(['awk', '-F', '[,e]+', '{print $2}', 'c.csv'])
    same(['awk', '-F', '\t', '{print $2}', 't.txt'])
    same(['awk', '-F|', '{print $2}'], 'a|b|c\n')
    same(['awk', '-v', 'x=5\\ty', 'BEGIN{print x}'])
    same(['awk', '{print v, $1}', 'v=1', 'n.txt', 'v=2', 'n.txt'])
    same(['awk', 'FNR==NR{a[$1]=1;next} $1 in a', 'n.txt', 'n.txt'])
    same(['awk', '{print FILENAME, FNR}', 't.txt', 'n.txt'])
    w('p.awk', '# comment\nBEGIN { x = 1 }\n{ if ($1 > 3)\n    print "big"\n  else\n    print "small"\n}\nEND {\n  for (i = 0; i < 3; i++) {\n    if (i == 1)\n      continue\n    print i\n  }\n}\n')
    same(['awk', '-f', 'p.awk', 'n.txt'])
    eq(['awk', 'BEGIN{print gensub(/(a)(b)/,"\\\\2\\\\1","g","abab"); print strtonum("0x10")}'], 'baba\n16\n')
    eq(['awk', 'BEGIN{print ENVIRON["TZ"]; system("echo sys"); print substr("日本語",2,1)}'], 'UTC\nsys\n本\n')
    eq(['awk', 'BEGIN{print 1/0}'], '', rc=2)
    eq(['awk', '{'], '', rc=2)
    eq(['awk', '-f', 'nope'], '', rc=2)


def t_diff():
    rnd = random.Random(7)
    w('o1.txt', TXT)
    w('o2.txt', TXT.replace('Beta two', 'BETA 2').replace('tab\there\n', '') + 'more\nlast')
    for a in (['-u'], [], ['-q'], ['-i', '-u'], ['-w'], ['-b'], ['-U1']):
        x, y = ours(['diff'] + a + ['o1.txt', 'o2.txt'], b'', top), run(['/usr/bin/diff'] + a + ['o1.txt', 'o2.txt'], b'', top)
        f = lambda s: [z for z in s.split(b'\n') if not z.startswith((b'---', b'+++'))]
        check(['diff'] + a, f(x[1]) == f(y[1]) and x[0] == y[0], f'{x}\n    {y}')
    eq(['diff', 'o1.txt', 'o1.txt'], '')
    eq(['diff', '-u', '--label', 'a', '-L', 'b', 'o1.txt', 'o2.txt'], None, rc=1)
    w('w1', 'a  b\n\nc\n')
    w('w2', 'a b\nc\n')
    eq(['diff', '-bB', 'w1', 'w2'], '')
    eq(['diff', '-w', 'w1', 'w2'], '2d1\n< \n', rc=1)
    for p, s in (('da/x', '1\n'), ('da/only', 'o\n'), ('da/s/y', 'y\n'), ('db/x', '2\n'), ('db/s/y', 'y\n'), ('db/new/n', 'n\n')):
        w(p, s)
    eq(['diff', '-r', 'da', 'db'], 'Only in db: new\nOnly in da: only\ndiff -r da/x db/x\n1c1\n< 1\n---\n> 2\n', rc=1)
    eq(['diff', '-rq', 'da', 'db'], 'Only in db: new\nOnly in da: only\nFiles da/x and db/x differ\n', rc=1)
    d = eq(['diff', '-ruN', 'da', 'db'], None, rc=1)
    shutil.copytree(os.path.join(top, 'da'), os.path.join(top, 'dc'))
    eq(['patch', '-p1'], None, d, cwd=os.path.join(top, 'dc'))
    eq(['diff', '-r', 'db', 'dc'], '')
    eq(['patch', '-R', '-p1', '-s'], '', d, cwd=os.path.join(top, 'dc'))
    eq(['diff', '-r', 'da', 'dc'], '')
    for n in range(12):     # random edits: our diff with Apple's patch, Apple's diff with our patch
        a = [f'line {rnd.randrange(8)}' for _ in range(rnd.randrange(60))]
        b = list(a)
        for _ in range(rnd.randrange(1, 8)):
            k = rnd.randrange(len(b) + 1)
            if rnd.random() < .5:
                b[k:k] = [f'new {rnd.randrange(5)}'] * rnd.randrange(1, 4)
            else:
                del b[k:k + rnd.randrange(1, 4)]
        e1, e2 = rnd.choice(['\n', '']), rnd.choice(['\n', ''])
        w('r1', '\n'.join(a) + e1 if a else '')
        w('r2', '\n'.join(b) + e2 if b else '')
        style = ['-u'] if n % 3 else []
        d1, d2 = ours(['diff'] + style + ['r1', 'r2'], b'', top)[1], run(['/usr/bin/diff'] + style + ['r1', 'r2'], b'', top)[1]
        for tool, d in ((['/usr/bin/patch'], d1), ([sys.executable, TOOL, 'patch'], d2), ([sys.executable, TOOL, 'patch'], d1)):
            shutil.copy(os.path.join(top, 'r1'), os.path.join(top, 'r3'))
            if d:
                z = run(tool + ['-s', 'r3'], d, top)
                got = r('r3') if os.path.exists(os.path.join(top, 'r3')) else b''
                check(f'patch round {n} {tool[-1]}', got == r('r2') and z[0] == 0, f'{z}\n{d.decode()}\n{r("r1")!r}')


def t_patch():
    w('p.txt', NUMS)
    d = '--- a/p.txt\n+++ b/p.txt\n@@ -4,3 +4,4 @@\n 4\n-5\n+five\n+5b\n 6\n'
    eq(['patch', '-p1', '--dry-run'], 'checking file p.txt\n', d)
    check('dry run', r('p.txt') == NUMS.encode())
    w('p.txt', 'zero\n' + NUMS)
    eq(['patch', '-p1'], 'patching file p.txt\nHunk #1 succeeded at 5 (offset 1 line).\n', d)
    eq(['patch', '-p1', '-N'], None, d, rc=1)
    eq(['patch', '-R', '-p1', '-i', w('p.diff', d)], None)
    check('reverse', r('p.txt') == b'zero\n' + NUMS.encode(), r('p.txt'))
    w('p.txt', NUMS.replace('6\n', 'six\n', 1))
    o = eq(['patch', '-p1'], None, d)
    check('fuzz', b'fuzz' in o and b'five' in r('p.txt'), o)
    w('p.txt', 'nothing\n')
    eq(['patch', '-p1'], None, d, rc=1)
    check('reject', os.path.exists(os.path.join(top, 'p.txt.rej')))
    eq(['patch', '-p1'], None, 'diff --git a/nd/n.txt b/nd/n.txt\nnew file mode 100755\n--- /dev/null\n+++ b/nd/n.txt\n@@ -0,0 +1 @@\n+hi\n')
    check('new file', r('nd/n.txt') == b'hi\n' and os.stat(os.path.join(top, 'nd/n.txt')).st_mode & 0o100)
    eq(['patch', '-p1'], None, 'diff --git a/nd/n.txt b/nd/n.txt\ndeleted file mode 100755\n--- a/nd/n.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-hi\n')
    check('delete', not os.path.exists(os.path.join(top, 'nd/n.txt')))
    eq(['patch', '-p0'], '', '--- ../evil\n+++ ../evil\n@@ -0,0 +1 @@\n+x\n', rc=2)
    check('no escape', not os.path.exists(os.path.join(top, '..', 'evil')))
    w('c1', b'a\nb\n')
    w('c2', b'a\nc\nd\n')
    same(['cmp', 'c1', 'c2'])
    same(['cmp', '-s', 'c1', 'c1'])
    same(['cmp', '-l', 'c1', 'c2'], loose=True)


def t_text():
    w('s.txt', 'b 10\na 9\nc 2\na 10\nB 1\n')
    w('h.txt', '1K\n2M\n500\n1G\n')
    w('v.txt', 'v1.10\nv1.9\nv1.2.3\n')
    w('cs.csv', CSV)
    w('n.txt', NUMS)
    w('a.s', 'a\nb\nc\n')
    w('b.s', 'b\nc\nd\n')
    ENV['LC_ALL'] = 'C'
    for a in ([], ['-r'], ['-k2n'], ['-k2,2nr'], ['-u', '-k1,1'], ['-f'], ['-k1,1', '-k2,2n'], ['-n', '-k2'], ['-s', '-k1,1'], ['-c'], ['-b', '-k2.1,2.1']):
        same(['sort'] + a + ['s.txt'])
    same(['sort', '-t,', '-k2,2n', 'cs.csv'])
    same(['sort', '-h', 'h.txt'])
    same(['sort', '-V', 'v.txt'])
    same(['sort', '-rn', 'n.txt'])
    same(['sort', '-z'], 'b\0a\0')
    eq(['sort', '-o', 's.out', 's.txt'], '')
    check('sort -o', r('s.out').startswith(b'B 1'))
    ENV['LC_ALL'] = 'en_US.UTF-8'
    U = 'a\na\nb\nB\nc\nc\nc\n'
    for a in ([], ['-c'], ['-d'], ['-u'], ['-i'], ['-f1']):
        same(['uniq'] + a, U)
    for a in (['-d,', '-f1'], ['-d,', '-f2-'], ['-d,', '-f1,3'], ['-c1-3'], ['-c', '2'], ['-b-4'], ['-d:', '-f2'], ['-s', '-d:', '-f2']):
        same(['cut'] + a + ['cs.csv'])
    for a in (['a-z', 'A-Z'], ['-d', 'aeiou'], ['-s', 'l'], ['-d', '[:space:]'], ['[:lower:]', '[:upper:]'], ['-cd', 'a-z'], ['\\n', ' '], ['abc', 'x'],
              ['-s', '[:space:]', ' '], ['-c', 'a-z\\n', '_'], ['-ds', 'a', 'l']):
        same(['tr'] + a, 'hello  all, abc Z\nsecond\n')
    eq(['tee', 'tee1', 'tee2'], 'x\n', 'x\n')
    check('tee', r('tee1') == b'x\n' == r('tee2'))
    for a in (['echo'], ['-n2', 'echo'], ['-I{}', 'echo', '<{}>'], ['-0', 'echo'], ['-L1', 'echo', 'l'], ['-n', '1', '-P4', 'echo'], []):
        same(['xargs'] + a, 'a b\nc "d e"\n' if a[:1] != ['-0'] else 'a b\0c\0')
    eq(['xargs', 'grep', '-l', 'a'], 'a.s\n', 'a.s b.s')
    eq(['xargs', 'no-such-cmd'], '', 'x', rc=127)
    for a in ([], ['-12'], ['-23'], ['-3']):
        same(['comm'] + a + ['a.s', 'b.s'])
    for a in (['a.s', 'b.s'], ['-d,', 'a.s', 'n.txt'], ['-s', 'a.s'], ['-sd,', 'a.s', 'b.s']):
        same(['paste'] + a)
    same(['paste', '-', '-'], '1\n2\n3\n')
    for a in ([], ['-ba'], ['-ba', '-w3', '-s:', '-v5', '-i2']):
        same(['nl'] + a, 'x\n\ny\n', loose=True)
    eq(['tac', 'a.s'], 'c\nb\na\n')
    same(['rev', 'a.s', 's.txt'])
    for a in (['-w5'], ['-s', '-w10']):
        same(['fold'] + a, 'the quick brown fox jumps\n')
    for a in (['3'], ['2', '4'], ['1', '2', '7'], ['-w', '8', '10'], ['5', '-2', '1'], ['-f', '%03g', '2']):
        same(['seq'] + a)
    eq(['seq', '-s,', '3'], '1,2,3\n')
    eq(['seq', '0', '.5', '1'], '0.0\n0.5\n1.0\n')
    for a in (['1', '+', '2'], ['7', '/', '2'], ['7', '%', '3'], ['3', '*', '(', '1', '+', '1', ')'], ['abc', ':', 'a\\(.\\)'], ['abc', ':', '.*'],
              ['2', '>', '10'], ['a', '=', 'b'], ['0', '|', '5'], ['', '&', '1']):
        same(['expr'] + a)
    d = bytes(range(256)) + b'x' * 70
    w('bin', d)
    for a in (['bin'], ['-p', 'bin'], ['-l', '20', '-s', '3', 'bin'], ['-g1', '-c8', 'bin'], ['-i', 'bin'], ['-u', 'bin'], ['-b', '-l', '9', 'bin']):
        same(['xxd'] + a)
    eq(['xxd', '-r'], d, ours(['xxd', 'bin'], b'', top)[1])
    eq(['xxd', '-r', '-p'], d, ours(['xxd', '-p', 'bin'], b'', top)[1])
    for a in (['-c', 'bin'], ['-Ad', '-tx1', 'bin'], ['-x', 'bin'], ['-A', 'd', '-t', 'u1', 'bin'], ['bin'], ['-tx4', '-N8', 'bin'], ['-b', 'bin']):
        same(['od'] + a, loose=True)
    same(['base64'], d)
    eq(['base64', '-w', '0'], 'aGk=', 'hi')
    eq(['base64', '-d'], 'hello', 'aGVs\nbG8=\n')


def t_files():
    w('e/x.txt', 'hello\n')
    w('e/sub/y', b'a' * 5000)
    w('a.s', 'a\nb\nc\n')
    os.symlink('x.txt', os.path.join(top, 'e/ln'))
    for a in (['md5', 'a.s'], ['md5', '-q', 'a.s'], ['md5sum', 'a.s'], ['shasum', 'a.s'], ['shasum', '-a', '256', 'a.s', 'e/x.txt']):
        same(a)
    s = ours(['sha256sum', 'a.s', 'e/x.txt'], b'', top)[1]
    eq(['sha256sum', '-c'], 'a.s: OK\ne/x.txt: OK\n', s)
    eq(['shasum', '-c', '-'], 'a.s: FAILED\n', b'0' * 40 + b'  a.s\n', rc=1)
    same(['date', '-u', '-r', '86400', '+%Y-%m-%d %H:%M:%S %j %a %s'])
    eq(['date', '-u', '-d', '2024-02-29T12:00:00Z', '+%F %T %Z'], '2024-02-29 12:00:00 UTC\n')
    eq(['date', '-u', '-d', '@0', '-Iseconds'], '1970-01-01T00:00:00+00:00\n')
    same(['du', '-sk', 'e'])
    same(['du', '-ak', 'e/sub'])
    eq(['df', '-h', '.'], None)
    for f in ('%z', '%N', '%Lp', '%Sp', '%m', '%HT', '%Su', '%Sm'):
        same(['stat', '-f', f, 'e/x.txt'])
    eq(['stat', '-c', '%s %n %a %F', 'e/x.txt', 'e'], None)
    eq(['stat', '-c', '%s %F', 'e/x.txt', 'e/ln'], '6 regular file\n5 symbolic link\n')
    eq(['file', 'e/x.txt', 'e', 'e/ln'], 'e/x.txt: ASCII text\ne: directory\ne/ln: symbolic link to x.txt\n')
    same(['readlink', 'e/ln'])
    same(['realpath', 'e/ln'])
    eq(['readlink', '-f', 'e/ln'], top + '/e/x.txt\n')
    p = ours(['mktemp', '-d', os.path.join(top, 'tXXXXXX')])[1].decode().strip()
    check('mktemp', os.path.isdir(p))
    eq(['tree', 'e'], 'e\n├── ln -> x.txt\n├── sub\n│   └── y\n└── x.txt\n\n1 directory, 3 files\n')
    for z, un, suf in (('gzip', 'gunzip', '.gz'), ('bzip2', 'bunzip2', '.bz2'), ('xz', 'unxz', '.xz')):
        w('z.txt', TXT * 50)
        eq([z, 'z.txt'], '')
        check(z, not os.path.exists(os.path.join(top, 'z.txt')))
        eq([z, '-dc', 'z.txt' + suf], TXT * 50)
        eq([un, '-k', 'z.txt' + suf], '')
        check(un, r('z.txt') == (TXT * 50).encode())
        eq([z, '-dc'], 'q', ours([z, '-c'], b'q')[1])
    eq(['zcat', 'z.txt.gz'], TXT * 50)
    eq(['gzip', '-d'], TXT, run(['/usr/bin/gzip', '-c'], TXT)[1])
    eq(['gzip', '-d'], '', b'garbage', rc=1)


def t_archives():
    import io
    import tarfile
    import zipfile
    w('src/a.txt', 'A\n')
    w('src/run.sh', '#!/bin/sh\n', 0o755)
    w('src/deep/b.txt', 'B\n')
    w('src/deep/skip.o', 'o\n')
    os.symlink('a.txt', os.path.join(top, 'src/l'))
    for fl, n in (('c', 't.tar'), ('cz', 't.tgz'), ('cj', 't.tbz'), ('cJ', 't.txz')):
        eq(['tar', fl + 'f', n, '--exclude', '*.o', 'src'], '')
        eq(['tar', '-tf', n], 'src/\nsrc/a.txt\nsrc/deep/\nsrc/deep/b.txt\nsrc/l\nsrc/run.sh\n')
        o = 'x-' + n
        os.makedirs(os.path.join(top, o))
        check('mac reads ' + n, run(['/usr/bin/tar', 'xf', n, '-C', o], b'', top)[0] == 0 and r(o + '/src/deep/b.txt') == b'B\n')
        shutil.rmtree(os.path.join(top, o))
        os.makedirs(os.path.join(top, o))
        eq(['tar', '-x' + fl[1:] + 'f', n, '-C', o, '--strip-components=1'], '')
        check('tar x ' + n, r(o + '/deep/b.txt') == b'B\n' and os.stat(os.path.join(top, o, 'run.sh')).st_mode & 0o100
              and os.readlink(os.path.join(top, o, 'l')) == 'a.txt')
    run(['/usr/bin/tar', 'czf', 'mac.tgz', '-C', 'src', '.'], b'', top)
    os.makedirs(os.path.join(top, 'fm'))
    eq(['tar', 'xzf', 'mac.tgz', '-C', 'fm', './deep/b.txt'], '')
    check('tar member', r('fm/deep/b.txt') == b'B\n' and not os.path.exists(os.path.join(top, 'fm/a.txt')))
    eq(['tar', '-xOf', 't.tar', 'src/a.txt'], 'A\n')
    b = ours(['tar', 'cf', '-', '-C', 'src', 'a.txt', '-C', 'deep', 'b.txt'], b'', top)[1]
    eq(['tar', 'tf', '-'], 'a.txt\nb.txt\n', b)
    with tarfile.open(os.path.join(top, 'evil.tar'), 'w') as t:
        for n, typ, link in (('../evil1', None, ''), ('/tmp/../' + top[1:] + '/evil2', None, ''), ('out', tarfile.SYMTYPE, '..'), ('out/evil3', None, ''),
                             ('good', None, '')):
            i = tarfile.TarInfo(n)
            if typ:
                i.type, i.linkname = typ, link
            i.size = 0 if typ else 2
            t.addfile(i, None if typ else io.BytesIO(b'x\n'))
    os.makedirs(os.path.join(top, 'jail'))
    eq(['tar', 'xf', 'evil.tar', '-C', 'jail'], '', rc=2)
    with zipfile.ZipFile(os.path.join(top, 'evil.zip'), 'w') as z:
        z.writestr('../evil4', 'x')
        i = zipfile.ZipInfo('zl')
        i.create_system, i.external_attr = 3, 0o120777 << 16
        z.writestr(i, '..')
        z.writestr('zl/evil5', 'x')
        z.writestr('good2', 'x')
    eq(['unzip', '-q', 'evil.zip', '-d', 'jail'], '', rc=2)
    check('nothing escaped', not any(os.path.exists(os.path.join(top, f'evil{n}')) for n in range(1, 6)) and os.path.exists(os.path.join(top, 'jail/good'))
          and os.path.exists(os.path.join(top, 'jail/good2')))
    eq(['zip', '-qr', 'z.zip', 'src', '-x', '*.o'], '')
    check('mac unzip', run(['/usr/bin/unzip', '-tq', 'z.zip'], b'', top)[0] == 0)
    eq(['unzip', '-q', 'z.zip', '-d', 'uz'], '')
    check('unzip', r('uz/src/deep/b.txt') == b'B\n' and not os.path.exists(os.path.join(top, 'uz/src/deep/skip.o'))
          and os.stat(os.path.join(top, 'uz/src/run.sh')).st_mode & 0o100)
    eq(['unzip', '-p', 'z.zip', 'src/a.txt'], 'A\n')
    o = eq(['unzip', '-l', 'z.zip'], None)
    check('unzip -l', b'src/deep/b.txt' in o)
    eq(['unzip', '-qo', 'z.zip', '-d', 'uz'], '')
    run(['/usr/bin/zip', '-q', 'm.zip', 'src/a.txt'], b'', top)
    eq(['unzip', '-qj', 'm.zip', '-d', 'um'], '')
    check('unzip -j', r('um/a.txt') == b'A\n')


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def go(self):
        n = int(self.headers.get('Content-Length') or 0)
        b = self.rfile.read(n)
        p = self.path
        if p.startswith('/redir'):
            self.send_response(302)
            self.send_header('Location', '/echo')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        out = f'{self.command} {p} {b.decode()} {self.headers.get("X-A", "")} {self.headers.get("Content-Type", "")} {self.headers.get("Authorization", "")}\n'.encode()
        if p.startswith('/file'):
            out = b'data\0\xff\n'
        self.send_response(404 if p.startswith('/missing') else 200)
        self.send_header('Content-Length', str(len(out)))
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(out)
    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = go


def t_curl():
    s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    u = f'http://127.0.0.1:{s.server_port}'
    eq(['curl', '-s', u + '/a?b=1'], 'GET /a?b=1    \n')
    eq(['curl', '-s', '-d', 'x=1', '-d', 'y=2', u + '/p'], 'POST /p x=1&y=2  application/x-www-form-urlencoded \n')
    eq(['curl', '-sX', 'PUT', '-H', 'X-A: v', '--json', '{"a":1}', u + '/p'], 'PUT /p {"a":1} v application/json \n')
    eq(['curl', '-s', '-u', 'me:pw', u + '/p'], 'GET /p    Basic bWU6cHc=\n')
    eq(['curl', '-s', '-w', '%{http_code}\\n', '-o', '/dev/null', u + '/redir'], '302\n')
    eq(['curl', '-sSfL', u + '/redir'], 'GET /echo    \n')
    eq(['curl', '-sL', '-d', 'q', u + '/redir'], 'GET /echo    \n')
    eq(['curl', '-fsS', u + '/missing'], '', rc=22)
    eq(['curl', '-s', u + '/missing'], 'GET /missing    \n')
    eq(['curl', '-sG', '--data-urlencode', 'q=a b&c', u + '/g'], 'GET /g?q=a%20b%26c    \n')
    o = eq(['curl', '-sI', u + '/h'], None)
    check('curl -I', o.startswith(b'HTTP/1') and b'Content-Type: text/plain' in o, o)
    o = eq(['curl', '-si', u + '/h'], None)
    check('curl -i', o.endswith(b'\r\n\r\nGET /h    \n'), o)
    eq(['curl', '-sO', u + '/file.bin'], '')
    check('curl -O', r('file.bin') == b'data\0\xff\n')
    eq(['curl', '-s', '-o', 'cd/x', '--create-dirs', u + '/file.bin'], '')
    eq(['curl', '-s', 'http://127.0.0.1:1/'], '', rc=7)
    eq(['curl', '--nope', u], '', rc=2)
    eq(['wget', '-q', '-O', '-', u + '/w'], 'GET /w    \n')
    eq(['wget', '-q', u + '/file2'], '')
    check('wget', r('file2') == b'data\0\xff\n')
    eq(['wget', '-q', u + '/missing'], '', rc=8)
    s.shutdown()


def t_sql_jq():
    q = "create table t(a int, b text); insert into t values(1,'x'),(2,'y,z'),(3,null); select * from t;"
    same(['sqlite3', ':memory:', q])
    same(['sqlite3', '-csv', ':memory:', q])
    eq(['sqlite3', '-header', '-csv', ':memory:', q], 'a,b\r\n1,x\r\n2,"y,z"\r\n3,\r\n')
    same(['sqlite3', '-json', ':memory:', q])
    eq(['sqlite3', 'q.db', q], '1|x\n2|y,z\n3|\n')
    same(['sqlite3', 'q.db', '.tables'], loose=True)
    same(['sqlite3', 'q.db', '.schema'])
    same(['sqlite3', 'q.db'], '.headers on\nselect a,\n b from t where a<3;\nselect count(*) n from t;\n')
    eq(['sqlite3', 'q.db', 'select * from nope'], '', rc=1)
    J = '{"a":1,"b":[1,2,{"c":"x"}],"s":"he\\"y","n":null,"f":1.5,"o":{"k2":2,"k1":1}}'
    L = '[{"n":"bob","v":3},{"n":"ann","v":1},{"n":"bob","v":2}]'
    for p in ('.', '.a', '.b[2].c', '.b[]', '.b[1:]', '.["a"]', '.zz', '.a,.f', 'keys', 'keys_unsorted', 'length', '.o|to_entries', '.o|with_entries(.value+=1)',
              '[.b[]|type]', '.b|map(numbers)', '.o.k1+.a', '.s|length', '.n//"d"', 'has("a")', 'if .a==1 then "y" else "n" end', '{a,x:.f}', '{(.s):.a}',
              '"v=\\(.a) \\(.b)"', '.a as $x|.o.k2+$x', '[range(3)]', 'reduce .b[0:2][] as $i (0;.+$i)', '.b[0]=9', '.o|=keys', 'del(.b)', 'del(.b[0,1])', '[..|numbers]', 'paths',
              '.s|test("he")', '.s|split("e")', '[.a,.f]|add', '.b|tojson', '"[1,2]"|fromjson', '.a|tostring', '"12"|tonumber', 'try error("x") catch .', '.a.b?', '[.b[]|tostring]|join("-")',
              '.a>0 and .f<2', '(.n|not) or false', '[1,[2,[3]]]|flatten', '.o|map_values(.*10)', '"a,b"|split(",")', '[3,1,2]|sort|reverse', 'first(.b[])', '[limit(2;.b[])]',
              '"abc"|ascii_upcase|.[1:]', '"x1y22"|[scan("[0-9]+")]', '"foo"|gsub("o";"0")', '"foo bar"|capture("(?<a>\\\\w+) (?<b>\\\\w+)")', '[.b[0],.s]|@csv', '.s|@sh', '"a b"|@uri', '"hi"|@base64',
              '10/4,7%3,2*3,5-7', 'def f(x): x*2; f(.a)', '.o|to_entries|from_entries', '[1,2]|contains([1])', '.b|index(2)', 'path(..)', '.["b"][0]', '.o."k1"', '-.a', '.b|last', 'getpath(["o","k1"])',
              '[.[]?]|length', '.a == 1.0', 'input_line_number', '1 as $x | 2 as $y | [$x,$y]', '[.b[]|select(type=="number" and .>1)]', 'any(.b[]; .==1)', '{"a":{"b":1}} * {"a":{"c":2}}', 'ltrimstr("x")'):
        same(['jq', '-c', p], J)
    for p in ('sort_by(.v)', 'group_by(.n)', 'map(.n)|unique', 'unique_by(.n)', 'max_by(.v)', 'min_by(.v).n', 'map(select(.n=="bob"))', '[.[]|{(.n):.v}]|add', '.[0]|keys', 'map(.v)|add/length',
              'to_entries|map(.key)', '.[]|"\\(.n)\\t\\(.v)"', 'map([.n,.v])|.[]|@tsv', 'INDEX(.n)|keys', '.[1:]|length', 'foreach .[] as $x (0;.+$x.v)', 'all(.v>0)'):
        same(['jq', '-c', p], L)
    for a in (['.'], ['-r', '.s'], ['-S', '.'], ['--tab', '.o'], ['-e', '.zz'], ['-e', '.a'], ['-n', '1+1'], ['-j', '.a,.s'], ['--arg', 'x', 'q', '$x'], ['--argjson', 'x', '{"z":1}', '$x.z+.a'],
              ['--indent', '1', '.o'], ['-s', 'length'], ['-R', '.'], ['.a.b'], ['.['], ['-Rs', '.']):
        same(['jq'] + a, J + '\n')
    same(['jq', '-c', '.'], '1 "a" [2]\n{"x":null}')
    same(['jq', '-n', '[inputs]|add'], '1 2 3')
    eq(['jq', '.'], '', '{bad', rc=2)


MK1 = '''
CC := echo cc
SRC = a.c b.c
OBJ := $(SRC:.c=.o)
X ?= dflt
X += more
define two
@echo one $(1)
@echo two $(1)
endef
ifeq ($(X),dflt more)
Y = yes
else ifdef Z
Y = z
else
Y = no
endif
ifneq ($(OBJ),)
ifndef NOPE
W = w
endif
endif
all: prog extra | dir
\t@echo all $(X) $(Y) $(W) $@ [$^] [$|]
prog: $(OBJ)
\t$(CC) -o $@ $^ $<
\t@touch $@
%.o: %.c h.h
\t@echo compile $< to $@ stem $* $(@D) $(<F)
\t@touch $@
dir:
\tmkdir -p dir
extra: ; @echo $(call two_,q) inline
two_ = [$(1)$(1)]
.PHONY: all extra clean
clean:
\t-rm -f $(OBJ) prog nofile/x
\t$(call two,c)
\t-@false
\t@echo done
funcs:
\t@echo $(subst a,b,banana) $(patsubst %.c,%.o,x.c y.h) $(filter %.c,a.c b.h) $(filter-out %.c,a.c b.h) $(sort b a b) $(word 2,x y z) $(words x y)
\t@echo $(firstword q w) $(lastword q w) $(dir a/b c) $(notdir a/b c) $(basename a/b.c x) $(suffix a.c b.d e) $(addprefix p/,a b) $(addsuffix .x,a b) $(join a b,1 2)
\t@echo $(foreach i,1 2 3,<$(i)>) $(if x,t,f) $(if ,t,f) $(or ,b) $(and a,b) $(strip  a  b ) $(shell echo sh; echo two) $(wildcard *.c) $(findstring a,abc) $(wordlist 2,3,a b c d)
\t@echo $(origin X) $(flavor SRC) $(value SRC) $$HOME_ $(words $(OBJ)) $(notdir $(abspath a.c))
multi1 multi2: ; @echo $@
static: s1.q s2.q
s1.q s2.q: %.q: %.c2 ; @echo static $@ from $< $*
%.c2: ; @echo making $@
tgt: V = specific
tgt: sub ; @echo $@ $(V)
sub: ; @echo $@ $(V)
fail: ; exit 3
cont: ; @echo a \\
\tb
export EXP = exported
envt: ; @echo $$EXP
-include nothere.mk
include inc.mk
dq: ; @echo "dollar $$(( 1 + 2 ))" '#hash' # comment
.SUFFIXES: .in .out
.in.out:
\t@echo suffix $< $@
EMPTY =
dbl:: ; @echo d1
dbl:: ; @echo d2
long = a \\
  b   \\
  c
longt: ; @echo "$(long)"
'''


def t_make():
    def setup(d):
        shutil.rmtree(os.path.join(top, d), ignore_errors=True)
        for n, s in (('Makefile', MK1), ('a.c', ''), ('b.c', ''), ('h.h', ''), ('inc.mk', 'INC = included\nincl: ; @echo $(INC) $(MAKEFILE_LIST)\n'), ('f.in', ''),
                     ('rec/Makefile', 'all: ; @echo rec $(V) $(MAKELEVEL)\n'), ('R.mk', 'all:\n\t@$(MAKE) --no-print-directory -C rec V=7\n\t@cd rec && $(MAKE) --no-print-directory V=8\n')):
            w(d + '/' + n, s)
    setup('m1')
    setup('m2')
    norm = lambda b: b.replace(b'`', b"'").replace(b'/m2', b'/m1')

    def mk(*a, err=False):
        x, y = ours(('make',) + a, b'', os.path.join(top, 'm1')), run(('/usr/bin/make',) + a, b'', os.path.join(top, 'm2'))
        check(('make',) + a, norm(x[1]) == norm(y[1]) and x[0] == y[0], f'rc {x[0]} {y[0]}\n    got {x[1].decode()}\n    mac {y[1].decode()}\n    err {x[2].decode()} | {y[2].decode()}')
    mk('-n')
    mk()
    mk()
    mk('prog')
    for d in ('m1', 'm2'):
        for f, k in (('b.c', 5), ('a.o', -200), ('b.o', -200), ('prog', -100), ('a.c', -300), ('h.h', -300)):
            os.utime(os.path.join(top, d, f), (time.time() + k, time.time() + k))
    mk('-n', 'prog')
    mk()
    for a in (['funcs'], ['multi1', 'multi2'], ['static'], ['tgt'], ['fail'], ['-k', 'fail', 'sub'], ['cont'], ['envt'], ['dq'], ['f.out'], ['incl'], ['dbl'], ['longt'], ['clean'],
              ['-s', 'extra'], ['X=cmd', 'extra', 'funcs'], ['Z=1', 'X=q', 'all'], ['-B', 'prog'], ['nothing'], ['-j4', 'sub'], ['-f', 'R.mk'], ['CC=:', '-n', '-B', 'a.o'], ['-i', 'fail', 'sub'],
              ['EMPTY=1', '-e', 'sub'], ['-q', 'prog'], ['dir']):
        mk(*a)
    w('m3/Makefile', 'OBJ=x.o y.o\nprog: $(OBJ)\n\tcat $^ > $@\nCC=cp\nCFLAGS=\n%.o: %.c\n\tcp $< $@\n\t@echo "$@: $< z.h" > $*.d\n-include $(OBJ:.o=.d)\n')
    for f in ('x.c', 'y.c', 'z.h'):
        w('m3/' + f, f + '\n')
    c = os.path.join(top, 'm3')
    eq(['make', '-j', '8'], 'cp x.c x.o\ncp y.c y.o\ncat x.o y.o > prog\n', cwd=c)
    eq(['make', '-C', 'm3', '-s'], '')
    os.utime(os.path.join(c, 'z.h'), (time.time() + 9, time.time() + 9))
    eq(['make', '-n'], 'cp x.c x.o\necho "x.o: x.c z.h" > x.d\ncp y.c y.o\necho "y.o: y.c z.h" > y.d\ncat x.o y.o > prog\n', cwd=c)
    w('m4/Makefile', 'hello: hello.o util.o\nhello.o util.o: u.h\n')
    w('m4/u.h', 'int u(void);\n')
    w('m4/hello.c', '#include <stdio.h>\n#include "u.h"\nint main(void){printf("hi %d\\n",u());}\n')
    w('m4/util.c', '#include "u.h"\nint u(void){return 5;}\n')
    eq(['make', 'CC=cc'], 'cc    -c -o hello.o hello.c\ncc    -c -o util.o util.c\ncc   hello.o util.o   -o hello\n', cwd=os.path.join(top, 'm4'))
    check('built', run([os.path.join(top, 'm4/hello')])[1] == b'hi 5\n')
    w('m5/Makefile', 'gen.mk: ; echo "V=gen" > $@\ninclude gen.mk\nall: ; @echo $(V)\n.DEFAULT_GOAL := all\n$(foreach t,p q,$(eval $(t): ; @echo rule-$(t)))\n')
    o = eq(['make'], None, cwd=os.path.join(top, 'm5'))
    check('generated include', o.endswith(b'gen\n'), o)
    eq(['make', 'q'], 'rule-q\n', cwd=os.path.join(top, 'm5'))
    eq(['make', '-f', 'nofile'], '', rc=2)


def t_misc():
    o = eq(['--names'], None).decode().split()
    bad = set('echo cat cp mv rm mkdir rmdir ls ln touch pwd head tail wc chmod printf basename dirname sleep uname env test'.split())
    check('names', not bad & set(o) and {'seq', 'tr', 'grep', 'make'} <= set(o), o)
    eq(['help'], None)
    eq(['nosuch'], '', rc=127)
    w('big', 'line\n' * 200000)
    p = subprocess.Popen([sys.executable, TOOL, 'grep', 'line', os.path.join(top, 'big')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdout.read(10)
    p.stdout.close()
    e = p.stderr.read()
    check('broken pipe', p.wait() == 141 and e == b'', e)


def main():
    global top
    base = top
    for t in (t_grep, t_find, t_sed, t_awk, t_diff, t_patch, t_text, t_files, t_archives, t_curl, t_sql_jq, t_make, t_misc):
        top = os.path.join(base, t.__name__)
        os.makedirs(top)
        try:
            t()
        except Exception as e:
            import traceback
            failed.append(f'{t.__name__} stopped: {e!r}\n{traceback.format_exc()}')
    shutil.rmtree(base, ignore_errors=True)
    for f in failed:
        print('FAIL', f)
    print(f'devtools: {passed} passed, {len(failed)} failed, {time.time() - T0:.0f} s')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
