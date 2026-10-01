---
name: termhalo
description: Use when the user wants to play a browser game (nothalo.lol by default, or any URL) inside their terminal, install or launch termhalo, dock the game beside their work in tmux, or fix termhalo problems such as a striped picture, a dark picture, a visible cursor, or a low frame rate.
---

# termhalo

termhalo runs a web game in headless Chromium and draws it in the terminal:
real pixels in Ghostty or kitty, colour blocks elsewhere. macOS only.

## Install

Check first: `command -v termhalo`. If it is missing, run the installer from
the termhalo repository (`./install.sh` in a clone, or
`curl -fsSL https://raw.githubusercontent.com/kwyatte/termhalo/main/install.sh | bash`). It installs into
`~/.termhalo` and adds `~/.local/bin/termhalo`; it touches nothing else. It
downloads about 100 MB the first time, so tell the user before starting.

## Launch

The game needs a real terminal, so do not run it through a non-interactive
shell. Give the user the command, or start it somewhere they can see:

- A plain window: the user runs `termhalo` (or `termhalo <url>`).
- Inside tmux, as its own window: `tmux new-window -n termhalo termhalo`
- Docked above the current pane:
  `tmux split-window -vb -l 60% termhalo`

Only one copy can run at a time (the browser locks its save folder). If a
start fails, look for a running copy before anything else:
`pgrep -fl termhalo.py`.

## Controls to tell the user

- Click the page as normal. Joining a match captures the mouse: the cursor
  is hidden and mouse movement aims.
- `esc` frees the mouse. Switching apps frees it and pauses the game.
- `l` captures the mouse by hand if a game does not ask for it itself.
- `q` quits. Inside tmux it is `Q`, so stray typing cannot quit.
- `v` switches between pixels and colour blocks.

## Fixing things

| Symptom | Cause and fix |
|---|---|
| Picture is vertical stripes (inside tmux) | tmux sent the image cells without their position marks. `tmux refresh-client` fixes it at once; termhalo also redraws by itself after a layout change and every 20 s. |
| Picture too dark or washed out | `echo 1.8 > ~/.termhalo/brightness` (1.0 = off, default 1.5). Applies within a second. |
| Blocks instead of pixels | The terminal has no kitty graphics. Use Ghostty or kitty. |
| Cursor stays visible in a match | The capture helper could not start. Check that `~/.termhalo/venv/bin/python -c "import AppKit"` works; re-run the installer if not. |
| Aim too slow or fast | Start with `TERMHALO_SENS_LOCK=2 termhalo` (default 1.5). |
| Game keeps restarting | Read `~/.termhalo/crash.log` and `~/.termhalo/stderr.log`. |
| Choppy | Start with `TERMHALO_PROFILE=1 termhalo` and read the footer: `gap` is the time between frames shown (median/95th/worst), `pass` is the work per loop pass and `chrome` is what the browser produces. Every pass over 25 ms is written to `~/.termhalo/slow.log` with where the time went. A low `chrome` number in a match means the game itself is the limit. |

Inside tmux, `allow-passthrough on` must be set (`tmux set -g allow-passthrough on`)
or no picture appears at all.

## Do not

- Do not kill it with `pkill -f termhalo`: that also kills the launcher loop
  and closes the window. Kill the Python process by pid, or have the user quit.
- Do not start a second copy to "test" while one is running.
