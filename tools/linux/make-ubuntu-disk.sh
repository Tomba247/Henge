#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Makes an Ubuntu machine for Henge's Linux machine (docs/linux-machine.md): a
# folder holding the kernel (Image), the root disk (disk.img, or packed as
# disk.img.hvsparse) and machine.json. Everything comes from Ubuntu as it is:
# the arm64 cloud root file system and the stock generic kernel, its modules
# indexed (depmod) since nothing installs them as packages here.
#
#   sudo tools/linux/make-ubuntu-disk.sh OUTDIR [--size 60G] [--desktop | --gnome] [--pack] [--release noble] [--kernel-dir DIR]
#
# --kernel-dir takes the kernel tools/linux/build-kernel.sh built (16 KB pages,
# what HengeVM runs fastest: see henge-kernel.config) instead of Ubuntu's own
# 4 KB generic kernel.
#
# --desktop installs Xfce with LightDM signing in on its own; --gnome installs
# Ubuntu's own desktop (GNOME Shell with Ubuntu's dock and theme) with GDM signing
# in on its own. Either runs apt
# inside the image, so it needs a host that runs arm64 programs: an arm64
# machine (GitHub's ubuntu-24.04-arm runners) or qemu-user registered with
# binfmt_misc. --pack writes disk.img.hvsparse (holes left out) for moving it
# to the phone. Needs: curl, xz, zstd, mkfs.ext4 (e2fsprogs), depmod (kmod),
# dpkg-deb, and a C compiler for --pack. Run as root (file ownership in the
# image).
set -euo pipefail

OUT=""; SIZE=60G; DESKTOP=""; PACK=0; RELEASE=noble; KDIR=""
# The release's own kernel series (noble: 6.8, what the Linux machine is tried with), not the
# newer hardware-enablement kernels the same archive also has. KSERIES=7.0.0 picks those.
KSERIES=${KSERIES:-6.8.0}
ARCHIVE=${ARCHIVE:-http://ports.ubuntu.com/ubuntu-ports}
CLOUD=${CLOUD:-https://cloud-images.ubuntu.com}
while [ $# -gt 0 ]; do
    case "$1" in
        --size) SIZE=$2; shift ;;
        --desktop) DESKTOP=xfce ;;
        --gnome) DESKTOP=gnome ;;
        --pack) PACK=1 ;;
        --release) RELEASE=$2; shift ;;
        --kernel-dir) KDIR=$(cd "$2" && pwd); shift ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) OUT=$1 ;;
    esac
    shift
done
[ -n "$OUT" ] || { echo "usage: $0 OUTDIR [--size 60G] [--desktop | --gnome] [--pack] [--release noble] [--kernel-dir DIR]" >&2; exit 2; }
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

if [ -n "$KDIR" ]; then
    KVER=$(cat "$KDIR/release")
    say "kernel: $KVER from $KDIR"
    cp "$KDIR/Image" "$OUT/Image"
    mkdir -p "$ROOT/usr/lib/modules"
    rm -rf "$ROOT/usr/lib/modules/$KVER"
    cp -a "$KDIR/lib/modules/$KVER" "$ROOT/usr/lib/modules/$KVER"
    cp "$KDIR/config" "$ROOT/boot/config-$KVER" 2>/dev/null || true
    depmod -b "$ROOT/usr" "$KVER"
else
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
fi

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
cat > "$ROOT/usr/local/bin/henge-autotest" <<'TEST'
#!/bin/sh
# The Linux machine's self-test (tools/linux/make-ubuntu-disk.sh): what came up, on the serial console.
echo
echo "=== henge-autotest: Ubuntu is up ==="
. /etc/os-release
echo "$PRETTY_NAME, kernel $(uname -r), PID 1 is $(cat /proc/1/comm)"
dm=$(basename "$(readlink -f /etc/systemd/system/display-manager.service 2>/dev/null)" .service)
case "$dm" in
    lightdm) desktop=xfce shell=xfce4-panel wait=120 ;;
    gdm|gdm3) desktop=gnome shell=gnome-shell wait=300 ;;
    *) desktop="" ;;
esac
if [ -n "$desktop" ]; then
    i=0
    while [ $i -lt $wait ] && ! pgrep -u henge -x "$shell" >/dev/null; do sleep 1; i=$((i + 1)); done
    sleep 20
    if pgrep -u henge -x "$shell" >/dev/null; then echo "desktop: $desktop ($shell) up after about $i s"
    else echo "desktop: no $shell after $i s"; fi
    echo "=== henge-autotest: screenshot ==="
    sleep 2
    ps -eo comm= | grep -xE "Xorg|Xwayland|lightdm|gdm-.*|gnome-shell|gnome-session-b.*|xfce4-session|xfce4-panel|xfwm4|xfdesktop|Thunar|nautilus|pulseaudio|pipewire|wireplumber|plank" | sort -u | tr "\n" " "
    echo
    echo "display: $(for d in /sys/class/drm/card[0-9]; do basename "$(readlink -f $d/device/driver)"; done | tr "\n" " ")"
    u=$(id -u henge)
    if [ "$desktop" = xfce ]; then
        grep -hE "LoadModule: .(modesetting|fbdev).|glamor|Output .* connected|using drmmode|Not using" /var/log/Xorg.0.log 2>/dev/null | cut -d"]" -f2- | sed "s/^/xorg:/" | head -8
        g=$(DISPLAY=:0 XAUTHORITY=/var/run/lightdm/root/:0 timeout 90 glxinfo -B 2>&1)
    else
        for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
            echo "session $s: $(loginctl show-session "$s" -p Name -p Type -p Desktop -p Class -p State 2>/dev/null | tr "\n" " ")"
        done
        journalctl -b -o cat --no-pager _COMM=gnome-shell 2>/dev/null | grep -iE "device|render|gpu|egl|kms|software|llvmpipe|virgl|error|fail" | head -15 | sed "s/^/shell: /"
        xa=$(ls /run/user/$u/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
        [ -n "$xa" ] || xa=/run/user/$u/gdm/Xauthority
        g=$(runuser -u henge -- env DISPLAY=:0 XAUTHORITY="$xa" XDG_RUNTIME_DIR=/run/user/$u timeout 90 glxinfo -B 2>&1)
    fi
    echo "gl: $(echo "$g" | grep -E "OpenGL renderer|OpenGL version|direct rendering" | tr -s " " | tr "\n" ";")"
    echo "$g" | grep -q "OpenGL renderer" || echo "glxinfo said: $(echo "$g" | tail -6 | tr "\n" ";")"
fi
echo "state: $(systemctl is-system-running)"
echo "failed units:"
systemctl --failed --no-legend --no-pager
echo "firefox: $(command -v firefox || echo none)"
echo "input:"
grep -h "^N:" /proc/bus/input/devices
ip -brief address
snap version 2>&1 | head -2
echo "=== henge-autotest: end ==="
TEST
chmod 755 "$ROOT/usr/local/bin/henge-autotest"
cat > "$ROOT/etc/systemd/system/henge-autotest.service" <<'UNIT'
[Unit]
Description=Henge Linux machine self-test
ConditionKernelCommandLine=henge.autotest
After=multi-user.target

[Service]
Type=oneshot
StandardOutput=tty
TTYPath=/dev/ttyAMA0
ExecStart=/usr/local/bin/henge-autotest
ExecStartPost=/bin/systemctl poweroff

[Install]
WantedBy=multi-user.target
UNIT
ln -sf /etc/systemd/system/henge-autotest.service "$ROOT/etc/systemd/system/multi-user.target.wants/henge-autotest.service"

if [ -n "$DESKTOP" ]; then
    if ! chroot "$ROOT" /bin/true 2>/dev/null; then
        echo "--$DESKTOP: this host cannot run the image's arm64 programs (use an arm64 machine, or register qemu-user with binfmt_misc)" >&2
        exit 1
    fi
    for m in dev dev/pts proc sys run; do mkdir -p "$ROOT/$m"; mount --bind "/$m" "$ROOT/$m"; done
    mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.image" 2>/dev/null || true
    cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get update
fi

if [ "$DESKTOP" = xfce ]; then
    say "desktop: Xfce with LightDM signing in as henge"
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        xubuntu-core lightdm lightdm-gtk-greeter xserver-xorg-video-fbdev xserver-xorg-input-libinput \
        pulseaudio pavucontrol fonts-dejavu-core \
        yaru-theme-gtk yaru-theme-icon yaru-theme-sound fonts-ubuntu ubuntu-wallpapers plank \
        xfce4-terminal mousepad ristretto xfce4-screenshooter \
        libgl1-mesa-dri libegl-mesa0 mesa-utils
    # Ubuntu's look on Xfce: Yaru theme and icons, Ubuntu font and wallpaper, a dock (plank) at the
    # bottom. Set per user on first sign-in (xfconf), so Xubuntu's defaults stay underneath.
    cat > "$ROOT/usr/local/bin/henge-look" <<'LOOK'
#!/bin/sh
# Ubuntu's look for Xfce, once per user (tools/linux/make-ubuntu-disk.sh).
mark="$HOME/.config/henge-look-1"
if [ ! -e "$mark" ]; then
    sleep 2
    x() { xfconf-query -c "$1" -p "$2" --create -t "$3" -s "$4"; }
    x xsettings /Net/ThemeName string Yaru-dark
    x xsettings /Net/IconThemeName string Yaru
    x xsettings /Gtk/FontName string "Ubuntu 11"
    x xsettings /Gtk/MonospaceFontName string "Ubuntu Mono 13"
    x xfwm4 /general/theme string Default-xhdpi
    x xfwm4 /general/title_font string "Ubuntu Bold 11"
    x xfwm4 /general/button_layout string "O|HMC"
    x xfce4-panel /panels/panel-1/background-style uint 1
    x xfce4-panel /panels/panel-1/background-rgba string "rgba(0,0,0,0.85)" 2>/dev/null || true
    wall=$(ls /usr/share/backgrounds/*Numbat*.png /usr/share/backgrounds/warty-final-ubuntu.png 2>/dev/null | head -1)
    if [ -n "$wall" ]; then
        for p in $(xfconf-query -c xfce4-desktop -l | grep '/last-image$'); do xfconf-query -c xfce4-desktop -p "$p" -s "$wall"; done
        x xfce4-desktop /backdrop/screen0/monitorVirtual-1/workspace0/last-image string "$wall"
    fi
    x xfce4-desktop /desktop-icons/style int 0
    mkdir -p "$HOME/.config" && touch "$mark"
fi
pgrep -u "$(id -u)" -x plank >/dev/null || exec plank
LOOK
    chmod 755 "$ROOT/usr/local/bin/henge-look"
    printf '[Desktop Entry]\nType=Application\nName=Henge look\nExec=/usr/local/bin/henge-look\nOnlyShowIn=XFCE;\n' \
        > "$ROOT/etc/xdg/autostart/henge-look.desktop"
    # Plank's launchers: files, browser, terminal.
    mkdir -p "$ROOT/etc/skel/.config/plank/dock1/launchers"
    for app in thunar firefox xfce4-terminal mousepad; do
        printf '[PlankDockItemPreferences]\nLauncher=file:///usr/share/applications/%s.desktop\n' "$app" \
            > "$ROOT/etc/skel/.config/plank/dock1/launchers/$app.dockitem"
    done
    cp -a "$ROOT/etc/skel/.config/plank" "$ROOT/home/henge/.config/" 2>/dev/null || { mkdir -p "$ROOT/home/henge/.config"; cp -a "$ROOT/etc/skel/.config/plank" "$ROOT/home/henge/.config/"; }
    chroot "$ROOT" chown -R henge:henge /home/henge/.config
    mkdir -p "$ROOT/etc/lightdm/lightdm.conf.d"
    printf '[Seat:*]\nautologin-user=henge\nautologin-session=xfce\n' > "$ROOT/etc/lightdm/lightdm.conf.d/50-henge.conf"
fi

if [ "$DESKTOP" = gnome ]; then
    say "desktop: GNOME (Ubuntu's desktop) with GDM signing in as henge"
    # Ubuntu's own desktop, without what it only recommends (printing, the snap store, ...): GNOME Shell with
    # Ubuntu's session, dock and Yaru theme, and the usual apps.
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ubuntu-desktop-minimal gdm3 ubuntu-session gnome-shell gnome-session \
        gnome-shell-extension-ubuntu-dock gnome-shell-extension-appindicator \
        yaru-theme-gnome-shell yaru-theme-gtk yaru-theme-icon yaru-theme-sound fonts-ubuntu ubuntu-wallpapers ubuntu-wallpapers-noble \
        nautilus gnome-terminal gnome-text-editor eog gnome-calculator gnome-system-monitor file-roller gnome-control-center gnome-tweaks \
        xwayland pipewire-pulse wireplumber dconf-cli \
        libgl1-mesa-dri libegl-mesa0 mesa-utils
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get purge -y gnome-initial-setup 2>/dev/null || true
    # GDM signs henge in on its own, into Ubuntu's session (GNOME on Wayland; GDM falls back to Xorg itself).
    mkdir -p "$ROOT/etc/gdm3" "$ROOT/var/lib/AccountsService/users"
    printf '[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=henge\n\n[security]\n\n[xdmcp]\n\n[chooser]\n\n[debug]\n' \
        > "$ROOT/etc/gdm3/custom.conf"
    printf '[User]\nSession=ubuntu\nSystemAccount=false\n' > "$ROOT/var/lib/AccountsService/users/henge"
    chmod 600 "$ROOT/var/lib/AccountsService/users/henge"
    # No first-run setup or welcome tour; no screen lock or blanking (the phone has its own); Firefox (Mozilla's
    # deb) in the dock; no animations while the guest draws with the CPU (Settings > Accessibility turns them on).
    mkdir -p "$ROOT/home/henge/.config" "$ROOT/etc/skel/.config"
    echo yes > "$ROOT/home/henge/.config/gnome-initial-setup-done"
    echo yes > "$ROOT/etc/skel/.config/gnome-initial-setup-done"
    cat > "$ROOT/usr/share/glib-2.0/schemas/99_henge.gschema.override" <<'GS'
[org.gnome.desktop.session]
idle-delay=0

[org.gnome.desktop.screensaver]
lock-enabled=false
idle-activation-enabled=false

[org.gnome.desktop.lockdown]
disable-lock-screen=true

[org.gnome.settings-daemon.plugins.power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false

[org.gnome.desktop.interface]
enable-animations=false

[org.gnome.desktop.interface:ubuntu]
enable-animations=false

[org.gnome.shell]
welcome-dialog-last-shown-version='999'
favorite-apps=['firefox.desktop', 'org.gnome.Nautilus.desktop', 'org.gnome.Terminal.desktop', 'org.gnome.TextEditor.desktop', 'org.gnome.Settings.desktop']

[org.gnome.shell:ubuntu]
favorite-apps=['firefox.desktop', 'org.gnome.Nautilus.desktop', 'org.gnome.Terminal.desktop', 'org.gnome.TextEditor.desktop', 'org.gnome.Settings.desktop']
GS
    chroot "$ROOT" glib-compile-schemas /usr/share/glib-2.0/schemas
fi

if [ -n "$DESKTOP" ]; then
    # Firefox as a deb from Mozilla's own repository: Ubuntu's firefox package only installs the snap,
    # which cannot be installed into an image being built.
    mkdir -p "$ROOT/etc/apt/keyrings"
    curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o "$ROOT/etc/apt/keyrings/packages.mozilla.org.asc"
    echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > "$ROOT/etc/apt/sources.list.d/mozilla.list"
    printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > "$ROOT/etc/apt/preferences.d/mozilla"
    chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get update
    if ! chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y firefox; then
        echo "   Mozilla's firefox deb not available: the snap is installed at first boot instead"
        rm -f "$ROOT/etc/apt/sources.list.d/mozilla.list" "$ROOT/etc/apt/preferences.d/mozilla"
        cat > "$ROOT/etc/systemd/system/henge-firefox.service" <<'UNIT'
[Unit]
Description=Install Firefox (snap) once the network is up
After=snapd.seeded.service network-online.target
ConditionPathExists=!/var/lib/henge-firefox-done

[Service]
Type=oneshot
ExecStart=/usr/bin/snap install firefox
ExecStartPost=/usr/bin/touch /var/lib/henge-firefox-done

[Install]
WantedBy=multi-user.target
UNIT
        ln -sf /etc/systemd/system/henge-firefox.service "$ROOT/etc/systemd/system/multi-user.target.wants/henge-firefox.service"
    fi
    chroot "$ROOT" update-alternatives --set x-www-browser /usr/bin/firefox 2>/dev/null || true
    chroot "$ROOT" chown -R henge:henge /home/henge
    chroot "$ROOT" apt-get clean
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
[ "$DESKTOP" = xfce ] && NAME="$NAME (Xfce)"
[ "$DESKTOP" = gnome ] && NAME="$NAME (GNOME)"
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
