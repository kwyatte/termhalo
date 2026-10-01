#!/usr/bin/env bash
# termhalo installer.
#
#   curl -fsSL https://raw.githubusercontent.com/kwyatte/termhalo/main/install.sh | bash
#   ./install.sh                    (from a clone)
#
# Puts everything in ~/.termhalo (override with TERMHALO_HOME) and a `termhalo`
# command in ~/.local/bin. Nothing else on the machine is touched. Re-running
# it upgrades in place and keeps the game save.
set -euo pipefail

HOME_DIR="${TERMHALO_HOME:-$HOME/.termhalo}"
BIN_DIR="${TERMHALO_BIN:-$HOME/.local/bin}"
# Where the files are fetched from when this is not run from a clone.
BASE_URL="${TERMHALO_BASE_URL:-https://raw.githubusercontent.com/kwyatte/termhalo/main}"
FILES="termhalo.py capture.py"

say() { printf '\033[1;36mtermhalo\033[0m  %s\n' "$*"; }
die() { printf '\033[1;31mtermhalo\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "macOS only for now (the mouse capture uses AppKit)."

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || die "needs Python 3.10 or newer (brew install python)."

SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/termhalo.py" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
[ -n "$SRC" ] || [ -n "$BASE_URL" ] ||
  die "run this from a clone, or set TERMHALO_BASE_URL to where the files are hosted."

mkdir -p "$HOME_DIR/app" "$BIN_DIR"
say "installing into $HOME_DIR"
for f in $FILES; do
  if [ -n "$SRC" ]; then
    cp "$SRC/$f" "$HOME_DIR/app/$f"
  else
    curl -fsSL "$BASE_URL/$f" -o "$HOME_DIR/app/$f" || die "could not download $f"
  fi
done

if [ ! -x "$HOME_DIR/venv/bin/python" ]; then
  say "creating a private Python environment"
  "$PY" -m venv "$HOME_DIR/venv"
fi
say "installing Python packages (playwright, pillow, numpy, pyobjc)"
"$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$HOME_DIR/venv/bin/python" -m pip install --quiet \
  playwright pillow numpy pyobjc-framework-Cocoa pyobjc-framework-Quartz
say "fetching the headless browser (about 100 MB the first time)"
"$HOME_DIR/venv/bin/python" -m playwright install chromium

cat > "$BIN_DIR/termhalo" <<EOF
#!/bin/sh
# termhalo launcher. A crash restarts the game after 2s (the save is safe in
# $HOME_DIR/profile); quitting from inside the game ends it for real.
H="\${TERMHALO_HOME:-$HOME_DIR}"
mkdir -p "\$H"
while :; do
  TERMHALO_HOME="\$H" "\$H/venv/bin/python" "\$H/app/termhalo.py" "\$@" 2>>"\$H/stderr.log"
  code=\$?
  [ \$code -eq 0 ] && break
  printf '\ntermhalo stopped (exit %s). details: %s/crash.log\nrestarting in 2s, ctrl-c to give up\n' "\$code" "\$H"
  sleep 2 || break
done
EOF
chmod +x "$BIN_DIR/termhalo"

say "done."
case ":$PATH:" in
  *":$BIN_DIR:"*) say "run:  termhalo" ;;
  *) say "add $BIN_DIR to your PATH, then run:  termhalo"
     say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
esac
say "best in Ghostty or kitty (real pixels). Other terminals get colour blocks."
