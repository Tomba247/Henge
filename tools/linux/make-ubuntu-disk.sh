#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Makes an Ubuntu machine for Henge's Linux machine (docs/linux-machine.md): a
# folder holding the kernel (Image), the root disk (disk.img, or packed as
# disk.img.hvsparse) and machine.json. Everything comes from Ubuntu as it is:
# the arm64 cloud root file system and the stock generic kernel, its modules
# indexed (depmod) since nothing installs them as packages here.
#
#   sudo tools/linux/make-ubuntu-disk.sh OUTDIR [--size 16G] [--desktop] [--pack] [--release noble]
#
# --desktop installs Xfce with LightDM signing in on its own. That runs apt
# inside the image, so it needs a host that runs arm64 programs: an arm64
# machine (GitHub's ubuntu-24.04-arm runners) or qemu-user registered with
# binfmt_misc. --pack writes disk.img.hvsparse (holes left out) for moving it
# to the phone. Needs: curl, xz, zstd, mkfs.ext4 (e2fsprogs), depmod (kmod),
# dpkg-deb, and a C compiler for --pack. Run as root (file ownership in the
# image).
set -euo pipefail

OUT=""; SIZE=16G; DESKTOP=0; PACK=0; RELEASE=noble
# The release's own kernel series (noble: 6.8, what the Linux machine is tried with), not the
# newer hardware-enablement kernels the same archive also has. KSERIES=7.0.0 picks those.
KSERIES=${KSERIES:-6.8.0}
ARCHIVE=${ARCHIVE:-http://ports.ubuntu.com/ubuntu-ports}
CLOUD=${CLOUD:-https://cloud-images.ubuntu.com}
while [ $# -gt 0 ]; do
    case "$1" in
        --size) SIZE=$2; shift ;;
        --desktop) DESKTOP=1 ;;
        --pack) PACK=1 ;;
        --release) RELEASE=$2; shift ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) OUT=$1 ;;
    esac
    shift
done
[ -n "$OUT" ] || { echo "usage: $0 OUTDIR [--size 16G] [--desktop] [--pack] [--release noble]" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "$0: run as root (the image's files keep their owners)" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/henge-ubuntu.XXXXXX")
ROOT="$WORK/root"
cleanup() {
    for m in dev/pts dev proc sys run; do umount -l "$ROOT/$m" 2>/dev/null || true; done
    rm -rf "$WORK"
}
trap cleanup EXIT

say() { echo "== $*"; }

say "root file system: Ubuntu $RELEASE cloud image (arm64)"
curl -fsSL "$CLOUD/$RELEASE/current/$RELEASE-server-cloudimg-arm64-root.tar.xz" -o "$WORK/root.tar.xz"
mkdir -p "$ROOT"
tar -C "$ROOT" --numeric-owner -xJf "$WORK/root.tar.xz"
rm "$WORK/root.tar.xz"

say "kernel: the newest $KSERIES generic kernel in $RELEASE-updates"
curl -fsSL "$ARCHIVE/dists/$RELEASE-updates/main/binary-arm64/Packages.xz" | xz -d > "$WORK/Packages"
KVER=$(sed -n 's/^Package: linux-image-unsigned-\([0-9.]*-[0-9]*-generic\)$/\1/p' "$WORK/Packages" | grep "^$KSERIES-" | sort -V | tail -1)
[ -n "$KVER" ] || { echo "no generic kernel found in $RELEASE-updates" >&2; exit 1; }
echo "   $KVER"
mkdir -p "$WORK/k"
for pkg in linux-image-unsigned-$KVER linux-modules-$KVER linux-modules-extra-$KVER; do
    file=$(awk -v p="$pkg" '$1 == "Package:" { hit = ($2 == p) } hit && $1 == "Filename:" { print $2; exit }' "$WORK/Packages")
    [ -n "$file" ] || { echo "$pkg not found" >&2; exit 1; }
    curl -fsSL "$ARCHIVE/$file" -o "$WORK/pkg.deb"
    dpkg-deb -x "$WORK/pkg.deb" "$WORK/k"
    rm "$WORK/pkg.deb"
done
# Henge loads an uncompressed arm64 Image; Ubuntu's vmlinuz is gzip.
zcat "$WORK/k/boot/vmlinuz-$KVER" > "$OUT/Image"
mkdir -p "$ROOT/usr/lib/modules"
rm -rf "$ROOT/usr/lib/modules/$KVER"
cp -a "$WORK/k/lib/modules/$KVER" "$ROOT/usr/lib/modules/$KVER"
cp "$WORK/k/boot/config-$KVER" "$ROOT/boot/" 2>/dev/null || true
depmod -b "$ROOT/usr" "$KVER"
rm -rf "$WORK/k"

say "settings"
# No cloud to ask for user data; the machine is configured here instead.
touch "$ROOT/etc/cloud/cloud-init.disabled"
echo "/dev/vda / ext4 discard,commit=30,errors=remount-ro 0 1" > "$ROOT/etc/fstab"
echo henge-ubuntu > "$ROOT/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 henge-ubuntu\n' > "$ROOT/etc/hosts"
# The network: virtio-net on Henge's user-mode network, by DHCP.
cat > "$ROOT/etc/netplan/01-henge.yaml" <<'YAML'
network:
  version: 2
  ethernets:
    henge:
      match:
        driver: virtio_net
      dhcp4: true
YAML
chmod 600 "$ROOT/etc/netplan/01-henge.yaml"
ln -sf /dev/null "$ROOT/etc/systemd/system/systemd-networkd-wait-online.service"
# The user: henge, no password, an administrator (sudo).
useradd -R "$ROOT" -m -s /bin/bash -G adm,sudo,audio,video,plugdev,input henge
passwd -R "$ROOT" -d henge >/dev/null
echo 'henge ALL=(ALL) NOPASSWD:ALL' > "$ROOT/etc/sudoers.d/henge"
chmod 440 "$ROOT/etc/sudoers.d/henge"
# The serial console (and the screen's text console) sign in by themselves.
for unit in serial-getty@ttyAMA0 getty@tty1; do
    mkdir -p "$ROOT/etc/systemd/system/$unit.service.d"
    printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin henge --noclear %%I $TERM\n' \
        > "$ROOT/etc/systemd/system/$unit.service.d/autologin.conf"
done

# A self-test that only runs when the kernel's command line has henge.autotest (the
# Linux machine workflow, .github/workflows/linux-machine.yml): reports to the serial
# console what came up, then powers off.
cat > "$ROOT/etc/systemd/system/henge-autotest.service" <<'UNIT'
[Unit]
Description=Henge Linux machine self-test
ConditionKernelCommandLine=henge.autotest
After=multi-user.target

[Service]
Type=oneshot
StandardOutput=tty
TTYPath=/dev/ttyAMA0
ExecStart=/bin/sh -c 'echo; echo "=== henge-autotest: Ubuntu is up ==="; . /etc/os-release; echo "$PRETTY_NAME, kernel $(uname -r), PID 1 is $(cat /proc/1/comm)"; echo "state: $(systemctl is-system-running)"; echo "failed units:"; systemctl --failed --no-legend --no-pager; echo "modules:"; lsmod | grep -E "^(xhci_pci|usbhid|hid_magicmouse|virtio_input|virtio_snd|virtio_net)" ; ip -brief address; snap version 2>&1 | head -2; echo "=== henge-autotest: end ==="'
ExecStartPost=/bin/systemctl poweroff

[Install]
WantedBy=multi-user.target
UNIT
ln -sf /etc/systemd/system/henge-autotest.service "$ROOT/etc/systemd/system/multi-user.target.wants/henge-autotest.service"

if [ "$DESKTOP" = 1 ]; then
    if ! chroot "$ROOT" /bin/true 2>/dev/null; then
        echo "--desktop: this host cannot run the image's arm64 programs (use an arm64 machine, or register qemu-user with binfmt_misc)" >&2
        exit 1
    fi
    say "desktop: Xfce with LightDM signing in as henge"
    for m in dev dev/pts proc sys run; do mkdir -p "$ROOT/$m"; mount --bind "/$m" "$ROOT/$m"; done
    mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.image" 2>/dev/null || true
    cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get update
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        xubuntu-core lightdm lightdm-gtk-greeter xserver-xorg-video-fbdev xserver-xorg-input-libinput \
        pulseaudio pavucontrol fonts-dejavu-core
    chroot "$ROOT" apt-get clean
    mkdir -p "$ROOT/etc/lightdm/lightdm.conf.d"
    printf '[Seat:*]\nautologin-user=henge\nautologin-session=xfce\n' > "$ROOT/etc/lightdm/lightdm.conf.d/50-henge.conf"
    chroot "$ROOT" systemctl set-default graphical.target
    rm -f "$ROOT/etc/resolv.conf"
    mv "$ROOT/etc/resolv.conf.image" "$ROOT/etc/resolv.conf" 2>/dev/null || true
    for m in dev/pts dev proc sys run; do umount -l "$ROOT/$m"; done
fi

say "disk: $SIZE ext4"
rm -f "$OUT/disk.img"
truncate -s "$SIZE" "$OUT/disk.img"
mkfs.ext4 -q -F -L henge-root -d "$ROOT" "$OUT/disk.img"

NAME="Ubuntu ${RELEASE^}"
[ "$DESKTOP" = 1 ] && NAME="$NAME (Xfce)"
cat > "$OUT/machine.json" <<JSON
{"kind": "linux", "name": "$NAME", "ramMB": 4096, "kernel": "Image", "kernelVersion": "$KVER",
 "cmdline": "console=tty0 console=ttyAMA0 root=/dev/vda rw rootwait"}
JSON

if [ "$PACK" = 1 ]; then
    say "packing the disk (holes left out)"
    cc -O2 -std=gnu11 -I"$HERE/host" "$HERE/tools/hvsparse_main.c" "$HERE/host/hvsparse.c" -o "$WORK/hvsparse"
    "$WORK/hvsparse" pack "$OUT/disk.img" "$OUT/disk.img.hvsparse"
    rm "$OUT/disk.img"
fi
say "done: $OUT ($(du -sh "$OUT" | cut -f1))"
