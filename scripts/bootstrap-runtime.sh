#!/bin/sh
set -eu
source_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ "$(uname -s)" = Darwin ] || { echo 'Android Publisher requires macOS.' >&2; exit 1; }
runtime_root=${ANDROID_PUBLISHER_RUNTIME:-"$HOME/Library/Application Support/AndroidPublisher/runtime"}
arch=$(uname -m)
case "$arch" in arm64|x86_64) ;; *) echo 'Unsupported Mac architecture.' >&2; exit 1;; esac
python_root="$runtime_root/python-3.12.13-$arch"
if [ ! -x "$python_root/python/bin/python3" ]; then
    mkdir -p "$runtime_root"
    chmod 700 "$runtime_root"
    url=$(/usr/bin/plutil -extract "python_macos_${arch}.url" raw -o - "$source_root/assets.json")
    expected=$(/usr/bin/plutil -extract "python_macos_${arch}.checksum" raw -o - "$source_root/assets.json")
    stage=$(mktemp -d "$runtime_root/python-install.XXXXXX")
    trap 'rm -rf "$stage"' EXIT HUP INT TERM
    /usr/bin/curl -fL --retry 2 --connect-timeout 20 --max-time 300 "$url" -o "$stage/python.tar.gz"
    actual=$(/usr/bin/shasum -a 256 "$stage/python.tar.gz" | /usr/bin/cut -d ' ' -f 1)
    [ "$actual" = "$expected" ] || { echo 'Python checksum failed.' >&2; exit 1; }
    mkdir "$stage/unpacked"
    /usr/bin/tar -xzf "$stage/python.tar.gz" -C "$stage/unpacked"
    "$stage/unpacked/python/bin/python3" -c 'import sys,ssl,sqlite3,plistlib,pyexpat; assert sys.version_info[:3] == (3,12,13)'
    if [ ! -d "$python_root" ]; then mv "$stage/unpacked" "$python_root"; fi
    rm -rf "$stage"
    trap - EXIT HUP INT TERM
fi
cd "$source_root"
PYTHONDONTWRITEBYTECODE=1 exec "$python_root/python/bin/python3" -m publisher.runtime install --source "$source_root"
