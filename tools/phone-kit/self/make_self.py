"""make_self.py HENGE.ipa OUT: Henge as a project of its own terminal (OUT/Henge-source): this repository's files, the libraries
its build downloads or builds (kit/lib, kit/include), and what of the app the phone cannot build, taken from HENGE.ipa (the
icons' Assets.car, the frameworks, the JIT helper). Run where the app was built."""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import zipfile
ipa, top = sys.argv[1], sys.argv[2] + '/Henge-source'
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.abspath(here + '/../../..')
os.makedirs(top)
for n in subprocess.run(['git', '-C', root, 'ls-files', '-z'], capture_output=True, text=True, check=True).stdout.split('\0'):
    if n and os.path.isfile(root + '/' + n) and not os.path.islink(root + '/' + n):
        os.makedirs(os.path.dirname(top + '/' + n), exist_ok=True)
        shutil.copy(root + '/' + n, top + '/' + n)
built = os.environ.get('HENGE_BUILT', root)  # the tree that holds ios/deps and build/ios
husk = os.environ.get('IOS_DEPS', built + '/ios/deps')
k = top + '/kit'
os.makedirs(k + '/lib')
shutil.copy(here + '/selfbuild.py', k)
for n in os.listdir(built + '/ios/deps/lib'):
    shutil.copy(built + '/ios/deps/lib/' + n, k + '/lib')
shutil.copy(built + '/build/ios/libhengerestore.a', k + '/lib')
shutil.copytree(husk + '/include/slirp', k + '/include/slirp')
for n in ('virgl', 'epoxy', 'EGL', 'KHR', 'GLES2', 'GLES3'):
    if os.path.isdir(f'{built}/ios/deps-gpu/include/{n}'):
        shutil.copytree(f'{built}/ios/deps-gpu/include/{n}', f'{k}/include/{n}')
res = []
with zipfile.ZipFile(ipa) as z:
    for n in z.namelist():
        p = n.split('/', 2)
        if len(p) < 3 or not p[2] or n.endswith('/'):
            continue
        f = p[2]
        if f == 'Info.plist':
            info = plistlib.loads(z.read(n))
        if f in ('Henge', 'Info.plist', 'PkgInfo') or f.startswith('_CodeSignature'):
            continue
        os.makedirs(os.path.dirname(f'{k}/app/{f}'), exist_ok=True)
        with open(f'{k}/app/{f}', 'wb') as o:
            o.write(z.read(n))
        if f.split('/')[0] not in [r.split(':')[1] for r in res]:
            res.append(f'kit/app/{f.split("/")[0]}:{f.split("/")[0]}')
info['HengeBuildCommit'] = 'on the phone'
info.pop('HengeBuildDate', None)
native = sorted({os.path.relpath(d, top) for d, _, fs in os.walk(top + '/ios/Henge') if any(f.endswith('.h') for f in fs)})
p = {'name': 'Henge', 'bundle_id': info['CFBundleIdentifier'], 'version': info['CFBundleShortVersionString'], 'min_ios': '17.0',
     'commands': ['python3 kit/selfbuild.py', 'ninja -f kit/out/build.ninja'],
     'sources': ['ios/Henge/**/*.swift', 'ios/Henge/**/*.c', 'ios/Henge/**/*.m', 'ios/Henge/**/*.S', 'ios/HengeJITHelper/HengeJITMessages.swift'],
     'include': ['core', 'host', 'devices'] + native, 'flags': ['-O2', '-w'], 'c_flags': ['-std=gnu11'], 'bridging_header': 'ios/Henge/HengeBridge.h',
     'frameworks': ['Metal', 'IOSurface', 'AudioToolbox', 'AVFAudio', 'CoreFoundation', 'QuartzCore', 'CoreText', 'ImageIO', 'CoreVideo', 'UniformTypeIdentifiers', 'UIKit', 'SwiftUI'],
     'libraries': ['kit/out/libhengevm.a', 'kit/out/libhengetl.a', 'kit/lib/libhengerestore.a', 'kit/lib/libslirp.a', 'kit/lib/libglib-2.0.a', 'kit/lib/libintl.a',
                   'kit/lib/libpcre2-8.a', 'kit/lib/libhusk_rppairing.a', 'z', 'iconv', 'resolv', 'compression', 'bz2'],
     'resources': res, 'info': info}
with open(top + '/henge-project.json', 'w') as f:
    json.dump(p, f, indent=1)
shutil.copy(here + '/CLAUDE.md', top + '/CLAUDE.md')
print(top, len(res), info['CFBundleVersion'])
