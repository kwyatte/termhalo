# termhalo

Play a browser game inside your terminal. Real pixels, mouse aim, a steady
40 to 50 frames a second.

```
curl -fsSL https://raw.githubusercontent.com/kwyatte/termhalo/main/install.sh | bash
termhalo
```

`termhalo` opens [nothalo.lol](https://nothalo.lol). `termhalo <url>` opens any
other web game. The game itself is not part of this project: termhalo loads
the site in a headless browser and shows it in the terminal.

## What you need

| | |
|---|---|
| macOS | Required. The mouse capture uses AppKit. |
| Python 3.10+ | Required. The installer builds its own environment from it. |
| Ghostty or kitty | For real pixels. Any other terminal shows colour blocks. |
| tmux | Optional. Only if you want the game docked beside other panes. |

The installer puts everything in `~/.termhalo` and one command in
`~/.local/bin`. To remove it: `rm -rf ~/.termhalo ~/.local/bin/termhalo`.

From a clone, `./install.sh` does the same thing with the local files.

## Playing

- Click the page like a normal browser window.
- Joining a match captures the mouse: the cursor disappears and moving the
  mouse aims, the same as pointer lock in a browser.
- `esc` gives the mouse back. Switching to another app gives it back and
  pauses the game.
- `q` quits (`Q` inside tmux). `v` switches pixels and colour blocks.
- Your progress is kept in `~/.termhalo/profile`.

## Inside tmux

```
tmux set -g allow-passthrough on
tmux split-window -vb -l 60% termhalo
```

Docked in tmux the game keeps running while you type in another pane.

## Settings

`~/.termhalo/brightness` holds one number, the shadow lift (1.0 = off,
default 1.5). Change it while the game runs. The rest are environment
variables:

| Variable | Default | What it does |
|---|---|---|
| `TERMHALO_FPS` | 50 | Top frame rate. The game steps down to 40 or 30 by itself when the browser cannot keep up, and back up when it can. |
| `TERMHALO_GOVERNOR` | 1 | `0` turns that stepping off. |
| `TERMHALO_VIEW` | 960x540 | Page size. |
| `TERMHALO_DPR` | 2 | Render density. |
| `TERMHALO_QUALITY` | 80 | JPEG quality of the browser's frames. |
| `TERMHALO_SENS_LOCK` | 1.5 | Aim speed in a match. |
| `TERMHALO_PROFILE` | off | `1` shows timing numbers in the footer and writes `stats.log` and `slow.log`. |
| `TERMHALO_NOFILE` | off | `1` never hands frames over as temp files. |
| `TERMHALO_SOFTWARE` | off | `1` forces software rendering. |

## How it works

Chromium renders the game on the GPU and streams frames over the DevTools
protocol. Each frame is written to a temp file and the terminal is told to
read it (kitty graphics protocol), so frame size is not limited by how fast
text can be pushed at a terminal. A small helper app takes the foreground
during a match, because macOS only lets the frontmost app hide and freeze
the cursor; it reports raw mouse movement and real key presses back.

Three things keep the picture even:

- **A frame governor.** It watches how many frames the browser produces in a
  match and holds the highest rate it can sustain (50, 40 or 30), shrinking
  the picture only if 30 is out of reach.
- **The page tells, the loop never asks.** Pointer lock changes are pushed
  from the page. Polling the page meant waiting on the game's busy main
  thread, which showed up as stalls of several hundred milliseconds.
- **Mouse movement is sent without waiting for an answer.** If the page falls
  behind, movements are added together instead of queueing.

## Building on it

Everything is in two files: `termhalo.py` (the browser, the frame pipeline,
input) and `capture.py` (the macOS mouse and keyboard helper). Good places
to start:

- Linux or Windows support: replace `capture.py` and the `Capture` class.
- Other terminals: sixel or iTerm2 images next to the kitty protocol path.
- Per-game key maps for `termhalo <url>`.

With `TERMHALO_PROFILE=1`, `~/.termhalo/slow.log` records every loop pass
over 25 ms and where the time went, which is the fastest way to find a
stutter.

## Claude Code skill

`skill/SKILL.md` teaches Claude Code to install, launch, dock and repair
termhalo. Copy the `skill` folder to `~/.claude/skills/termhalo`.

## License

MIT. termhalo is not affiliated with nothalo.lol, Halo, or Microsoft.
