"""Writes kit/out/build.ninja for Henge's C parts, as the Makefile's ios-lib builds them, in the projects folder on the
phone (henge-project.json's "commands"); with --symbols OUT OBJ...: the table of tl/shim/tools/hk_symbols.sh."""
import glob
import os
import re
import subprocess
import sys


def symbols(out, objs):
    t = subprocess.run(['llvm-nm', '-gUj'] + objs, capture_output=True, text=True, check=True).stdout
    names = sorted({n for n in t.split('\n') if not n.startswith('_hk_') and
                    (re.fullmatch(r'_{1,2}[A-Za-z][A-Za-z0-9_]*', n) or re.fullmatch(r'_OBJC_(META)?CLASS_\$_[A-Za-z_][A-Za-z0-9_]*', n))})
    with open(out, 'w') as f:
        f.write('typedef struct { const char *name; void *address; } HKSymbol;\n')
        for i, n in enumerate(names):
            f.write(f'extern char hk_s{i} __asm("{n}");\n')
        f.write('const HKSymbol hk_symbols[] = {\n' + ''.join(f'    {{"{n}", &hk_s{i}}},\n' for i, n in enumerate(names)) + '    {0, 0}};\n')
        f.write(f'const int hk_nsymbols = {len(names)};\n')


def main():
    if sys.argv[1:2] == ['--symbols']:
        return symbols(sys.argv[2], sys.argv[3:])
    sdk = os.environ['SDKROOT']
    base = f'-target arm64-apple-ios17.0 -isysroot {sdk}'
    # Read from the Makefile, so that a source added there is built here too.
    mk = open('Makefile').read().replace('\\\n', ' ')

    def words(name):
        m = re.search(rf'^{name} = (.*)$', mk, re.M)
        if not m:
            raise SystemExit(f'the Makefile has no {name}')
        out = []
        t = re.sub(r'\$\(wildcard ([^)]*)\)', r'\1', m[1])
        for w in re.sub(r'\$\(filter-out [^,]*,([^)]*)\)', r'\1', t).split():
            out += sorted(glob.glob(w)) if '*' in w else [w]
        return out
    virgl = '-DHV_VIRGL -DHV_VIRGL_ANGLE -DHV_VIRGL_DLOPEN -Ikit/include/virgl -Ikit/include' if os.path.isdir('kit/include/virgl') else ''
    cf = f'-std=c11 -O3 -Icore -Ihost -Idevices -Ikit/include/slirp {virgl}'
    vm = [(s, cf + (' -mgeneral-regs-only' if s in ('core/mmu.c', 'core/window.c') else '')) for s in words('IOS_SRC')]
    vm += [('devices/pvg_metal.m', cf + ' -fobjc-arc'), ('devices/vgpu_metal.m', cf + ' -fobjc-arc'), ('host/enter.S', '')]
    tl = [(s, '-O3 -fblocks' + (' -fobjc-arc' if s.endswith('.m') else '')) for s in words('TL_SRC')]
    shim = [(s, '-O3 -fblocks -fobjc-arc') for s in words('TL_SHIM')]
    o = [f'cc = clang {base} -w', 'rule c', '  command = $cc $f -MMD -MF $out.d -c $in -o $out', '  depfile = $out.d', '  deps = gcc', '  description = $in',
         'rule sym', '  command = python3 kit/selfbuild.py --symbols $out $in', 'rule lib', '  command = libtool -static -no_warning_for_no_symbols -o $out $in']

    def objs(l):
        r = []
        for s, f in l:
            r.append(f'kit/out/{s}.o')
            # -MMD of what the host's clang++ would pick for .cpp is clang's own here.
            o.extend([f'build {r[-1]}: c {s}', f'  f = {f}'])
        return ' '.join(r)
    a, b, c = objs(vm), objs(tl), objs(shim)
    o += [f'build kit/out/hk_symbols.c: sym {c} | kit/selfbuild.py', 'build kit/out/hk_symbols.o: c kit/out/hk_symbols.c', '  f = -O2',
          f'build kit/out/libhengevm.a: lib {a}', f'build kit/out/libhengetl.a: lib {b} {c} kit/out/hk_symbols.o',
          'default kit/out/libhengevm.a kit/out/libhengetl.a', '']
    os.makedirs('kit/out', exist_ok=True)
    with open('kit/out/build.ninja', 'w') as f:
        f.write('\n'.join(o))


main()
