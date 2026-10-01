#!/usr/bin/env python3
"""termhalo: play a browser game inside the terminal.

Headless Chromium (on the Mac's GPU) runs the page and Chrome's screencast
pushes its frames here. In Ghostty or kitty they are drawn as real pixels
(kitty graphics protocol; frames handed over as temp files when the terminal
allows it, placeholders + passthrough inside tmux). Any other terminal gets
colour blocks. Joining a match captures the mouse like a browser's pointer
lock: cursor hidden, raw movement aims, real key up/down (macOS).

    termhalo                 nothalo.lol
    termhalo <url>           any other web game

Keys      click = click the page      esc = free the mouse / game menu
          l = capture the mouse by hand      v = pixels <-> blocks
          q = quit (Q when running inside tmux)

Settings  ~/.termhalo/brightness   shadow lift, 1.0 = off (default 1.5)
          TERMHALO_VIEW=960x540    page size       TERMHALO_DPR=2   render density
          TERMHALO_SENS_LOCK=1.5   aim speed       TERMHALO_FPS=50  top frame rate
          TERMHALO_NOFILE=1        never use temp-file frames
          TERMHALO_SOFTWARE=1      software rendering
          TERMHALO_PROFILE=1       timing numbers in the footer
The game's save lives in ~/.termhalo/profile and survives restarts.
"""
import base64, collections, fcntl, io, os, re, select, shutil, struct, subprocess, sys, termios, time, tty
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "https://nothalo.lol"
DATA_DIR = os.path.expanduser(os.environ.get("TERMHALO_HOME", "~/.termhalo"))
PROFILE_DIR = os.path.join(DATA_DIR, "profile")     # Chrome user data: the game's save lives here
CRASH_LOG = os.path.join(DATA_DIR, "crash.log")
DEBUG = os.environ.get("TERMHALO_DEBUG")


def dbg(msg):
    if DEBUG:
        with open(os.path.join(DATA_DIR, "debug.log"), "a") as f:
            f.write(f"{time.time():.3f} {msg}\n")
VIEW = tuple(int(v) for v in os.environ.get("TERMHALO_VIEW", "960x540").split("x"))   # the game's browser window
HOLD = 0.14
LOOK = 40          # px of mouse movement per arrow press
FPS = 15

KEYMAP = {"w": "KeyW", "a": "KeyA", "s": "KeyS", "d": "KeyD", " ": "Space",
          "e": "KeyE", "r": "KeyR", "1": "Digit1", "2": "Digit2",
          "3": "Digit3", "4": "Digit4", "\t": "Tab"}
ARROWS = {"\x1b[A": (0, -LOOK), "\x1b[B": (0, LOOK),
          "\x1b[C": (LOOK, 0), "\x1b[D": (-LOOK, 0)}


# Quarter blocks: each cell shows 2x2 pixels (it used to be 1x2 with half
# blocks), and the pattern that best fits the four pixels is chosen per cell, so
# the picture has about twice the detail for the same window. Bit order of the
# masks: 1 = top-left, 2 = top-right, 4 = bottom-left, 8 = bottom-right.
QUAD = " \u2598\u259d\u2580\u2596\u258c\u259e\u259b\u2597\u259a\u2590\u259c\u2584\u2599\u259f\u2588"
PARTS = [(m, np.array([(m >> i) & 1 for i in range(4)], bool)) for m in range(1, 8)]
CELL_RATIO = 1.9            # cell height / cell width; measured when the terminal says


def measure_cell_ratio():
    """A cell's height over its width, from the terminal's pixel size when it
    reports one (a bare Ghostty window does; a tmux pane does not)."""
    global CELL_RATIO
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
        if rows and cols and xp and yp:
            CELL_RATIO = (yp / rows) / (xp / cols)
    except Exception:
        pass
    return CELL_RATIO


def fit_cells(cols, rows):
    """Cells the picture occupies (16:9, letterboxed) inside cols x rows."""
    aspect = 16 / 9 * CELL_RATIO        # a 16:9 picture is this many times wider than tall, in cells
    wc = max(1, min(cols, int(rows * aspect)))
    hc = max(1, min(rows, int(wc / aspect)))
    return wc, hc


GEOM = (0, 0, 1, 1)         # where the picture sits in the terminal: padx, pady, cells wide, cells tall
SGR_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def cell_to_view(col, row, geom=None, view=None):
    """A terminal cell (1-based, as the mouse reports it) as a point in the game's
    viewport, or None if it is outside the picture. Pure."""
    view = view or VIEW
    padx, pady, wc, hc = geom or GEOM
    px = (col - 1 - padx + 0.5) / wc
    py = (row - 1 - pady + 0.5) / hc
    if not (0 <= px < 1 and 0 <= py < 1):
        return None
    return px * view[0], py * view[1]


def quantize(a):
    """Best fg / bg partition of every cell's four pixels. Returns the chosen
    mask and the two colours for each cell, as arrays."""
    h2, w2, _ = a.shape
    hc, wc = h2 // 2, w2 // 2
    cells = np.stack([a[0::2, 0::2], a[0::2, 1::2], a[1::2, 0::2], a[1::2, 1::2]], axis=2)
    best = np.full((hc, wc), np.inf, np.float32)
    mask = np.zeros((hc, wc), np.uint8)
    fg = np.zeros((hc, wc, 3), np.float32)
    bg = np.zeros((hc, wc, 3), np.float32)
    for m, sel in PARTS:
        f = cells[:, :, sel, :].mean(axis=2)
        g = cells[:, :, ~sel, :].mean(axis=2)
        err = (((cells[:, :, sel, :] - f[:, :, None, :]) ** 2).sum((2, 3))
               + ((cells[:, :, ~sel, :] - g[:, :, None, :]) ** 2).sum((2, 3)))
        better = err < best
        best = np.where(better, err, best)
        mask = np.where(better, m, mask)
        fg = np.where(better[:, :, None], f, fg)
        bg = np.where(better[:, :, None], g, bg)
    return mask, fg, bg


def render(png, cols, rows):
    src = Image.open(io.BytesIO(png)).convert("RGB")
    wc, hc = fit_cells(cols, rows)
    a = np.asarray(src.resize((wc * 2, hc * 2), Image.LANCZOS)).astype(np.float32)
    mask, fg, bg = quantize(a)
    # 6 bits a channel: invisible, and long runs of one colour pair mean far
    # fewer escape codes to send through tmux every frame.
    fg = (fg.astype(np.int32) >> 2) << 2
    bg = (bg.astype(np.int32) >> 2) << 2
    padx, pady = (cols - wc) // 2, (rows - hc) // 2
    blank = " " * cols
    out = [blank] * pady
    for y in range(hc):
        line, prev = [" " * padx], None
        for x in range(wc):
            f, g = tuple(fg[y, x]), tuple(bg[y, x])
            if f == g:                                  # flat cell: just paint it
                key, ch = (g, g), " "
            else:
                key, ch = (f, g), QUAD[mask[y, x]]
            if key != prev:
                (r1, g1, b1), (r2, g2, b2) = key
                line.append(f"\x1b[38;2;{r1};{g1};{b1};48;2;{r2};{g2};{b2}m")
                prev = key
            line.append(ch)
        out.append("".join(line) + "\x1b[0m")
    out += [blank] * (rows - len(out))
    return "\x1b[H" + "\r\n".join(out) + "\x1b[0m"


# ---- real pixels: Kitty graphics + Unicode placeholders -----------------------
# The picture is sent to Ghostty as an actual image, but it is not DRAWN by an
# escape sequence at a screen position (that is what flashed and left a stuck
# copy behind: tmux repaints, Ghostty keeps the image). Instead the pane holds
# ordinary text, a grid of one private-use character (U+10EEEE) whose colour
# names the image. Ghostty paints the image behind those characters, so tmux
# sees plain text: it clips, scrolls and redraws it like anything else.
#
# Only the image data goes through tmux's passthrough envelope, once per tmux
# layer between this program and Ghostty. Needs `set -g allow-passthrough on`.
IMG_IDS = (200, 201)            # two images, swapped every frame; >15 so no theme can remap them
IMG_ID = IMG_IDS[0]
CHUNK = 4096
PLACEHOLDER = "\U0010EEEE"
# Row and column numbers are written as combining marks from kitty's published
# table (gen/rowcolumn-diacritics.txt, all 297). EVERY cell carries both, so
# nothing is inferred from the cell to its left: tmux redraws partial lines, and
# a run of bare cells starting mid-row made every row show image row 0 (the
# "stacked triangles").
ROW_MARKS = [0x305, 0x30d, 0x30e, 0x310, 0x312, 0x33d, 0x33e, 0x33f, 0x346, 0x34a, 0x34b,
             0x34c, 0x350, 0x351, 0x352, 0x357, 0x35b, 0x363, 0x364, 0x365, 0x366, 0x367,
             0x368, 0x369, 0x36a, 0x36b, 0x36c, 0x36d, 0x36e, 0x36f, 0x483, 0x484, 0x485,
             0x486, 0x487, 0x592, 0x593, 0x594, 0x595, 0x597, 0x598, 0x599, 0x59c, 0x59d,
             0x59e, 0x59f, 0x5a0, 0x5a1, 0x5a8, 0x5a9, 0x5ab, 0x5ac, 0x5af, 0x5c4, 0x610,
             0x611, 0x612, 0x613, 0x614, 0x615, 0x616, 0x617, 0x657, 0x658, 0x659, 0x65a,
             0x65b, 0x65d, 0x65e, 0x6d6, 0x6d7, 0x6d8, 0x6d9, 0x6da, 0x6db, 0x6dc, 0x6df,
             0x6e0, 0x6e1, 0x6e2, 0x6e4, 0x6e7, 0x6e8, 0x6eb, 0x6ec, 0x730, 0x732, 0x733,
             0x735, 0x736, 0x73a, 0x73d, 0x73f, 0x740, 0x741, 0x743, 0x745, 0x747, 0x749,
             0x74a, 0x7eb, 0x7ec, 0x7ed, 0x7ee, 0x7ef, 0x7f0, 0x7f1, 0x7f3, 0x816, 0x817,
             0x818, 0x819, 0x81b, 0x81c, 0x81d, 0x81e, 0x81f, 0x820, 0x821, 0x822, 0x823,
             0x825, 0x826, 0x827, 0x829, 0x82a, 0x82b, 0x82c, 0x82d, 0x951, 0x953, 0x954,
             0xf82, 0xf83, 0xf86, 0xf87, 0x135d, 0x135e, 0x135f, 0x17dd, 0x193a, 0x1a17,
             0x1a75, 0x1a76, 0x1a77, 0x1a78, 0x1a79, 0x1a7a, 0x1a7b, 0x1a7c, 0x1b6b, 0x1b6d,
             0x1b6e, 0x1b6f, 0x1b70, 0x1b71, 0x1b72, 0x1b73, 0x1cd0, 0x1cd1, 0x1cd2, 0x1cda,
             0x1cdb, 0x1ce0, 0x1dc0, 0x1dc1, 0x1dc3, 0x1dc4, 0x1dc5, 0x1dc6, 0x1dc7, 0x1dc8,
             0x1dc9, 0x1dcb, 0x1dcc, 0x1dd1, 0x1dd2, 0x1dd3, 0x1dd4, 0x1dd5, 0x1dd6, 0x1dd7,
             0x1dd8, 0x1dd9, 0x1dda, 0x1ddb, 0x1ddc, 0x1ddd, 0x1dde, 0x1ddf, 0x1de0, 0x1de1,
             0x1de2, 0x1de3, 0x1de4, 0x1de5, 0x1de6, 0x1dfe, 0x20d0, 0x20d1, 0x20d4, 0x20d5,
             0x20d6, 0x20d7, 0x20db, 0x20dc, 0x20e1, 0x20e7, 0x20e9, 0x20f0, 0x2cef, 0x2cf0,
             0x2cf1, 0x2de0, 0x2de1, 0x2de2, 0x2de3, 0x2de4, 0x2de5, 0x2de6, 0x2de7, 0x2de8,
             0x2de9, 0x2dea, 0x2deb, 0x2dec, 0x2ded, 0x2dee, 0x2def, 0x2df0, 0x2df1, 0x2df2,
             0x2df3, 0x2df4, 0x2df5, 0x2df6, 0x2df7, 0x2df8, 0x2df9, 0x2dfa, 0x2dfb, 0x2dfc,
             0x2dfd, 0x2dfe, 0x2dff, 0xa66f, 0xa67c, 0xa67d, 0xa6f0, 0xa6f1, 0xa8e0, 0xa8e1,
             0xa8e2, 0xa8e3, 0xa8e4, 0xa8e5, 0xa8e6, 0xa8e7, 0xa8e8, 0xa8e9, 0xa8ea, 0xa8eb,
             0xa8ec, 0xa8ed, 0xa8ee, 0xa8ef, 0xa8f0, 0xa8f1, 0xaab0, 0xaab2, 0xaab3, 0xaab7,
             0xaab8, 0xaabe, 0xaabf, 0xaac1, 0xfe20, 0xfe21, 0xfe22, 0xfe23, 0xfe24, 0xfe25,
             0xfe26, 0x10a0f, 0x10a38, 0x1d185, 0x1d186, 0x1d187, 0x1d188, 0x1d189, 0x1d1aa,
             0x1d1ab, 0x1d1ac, 0x1d1ad, 0x1d242, 0x1d243, 0x1d244]
MAX_ROWS = len(ROW_MARKS)


def tmux(*args):
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def tmux_layers():
    """How many tmux layers sit between this pane and Ghostty, judged by the
    client that is SHOWING this pane's window: 0 = not in tmux, 1 = a tmux client
    straight in Ghostty, 2 = a client running inside another tmux pane (the
    workspace). Returns -1 when no client is showing the window at all."""
    if not os.environ.get("TMUX"):
        return 0
    forced = os.environ.get("TERMHALO_TMUX_LAYERS")
    if forced and forced.isdigit():
        return int(forced)
    # A leaked $TMUX (launched FROM a tmux shell into a bare window) is not the
    # same as running IN tmux: trust it only if our own tty is that pane's tty.
    try:
        mytty = os.ttyname(1)
    except OSError:
        mytty = ""
    pane_tty = tmux("display-message", "-p", "-t",
                    os.environ.get("TMUX_PANE", ""), "#{pane_tty}").strip()
    if pane_tty != mytty:
        return 0
    win = tmux("display-message", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{window_id}")
    best = -1
    for line in tmux("list-clients", "-F", "#{client_termname}\t#{window_id}").splitlines():
        term, _, cw = line.partition("\t")
        if cw == win:
            best = max(best, 2 if term.startswith(("tmux", "screen")) else 1)
    return best


def refresh_real_clients():
    """Make tmux fully redraw the real terminal(s). tmux keeps the placeholder
    cells' row/column marks in its own grid but does NOT send them when the
    cells first go out (measured: 44,820 of 47,808 cells left tmux bare), so
    the terminal draws every line as image row 0: the striped picture. A full
    redraw sends the stored cells whole and is the only thing that fixes it."""
    for line in tmux("list-clients", "-F", "#{client_termname}\t#{client_tty}").splitlines():
        term, _, ctty = line.partition("\t")
        if ctty and not term.startswith(("tmux", "screen")):
            tmux("refresh-client", "-t", ctty)


def wrap_passthrough(seq, layers):
    """Hide `seq` from tmux: one DCS envelope per layer, ESC doubled inside."""
    for _ in range(max(0, layers)):
        seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    return seq


def kitty_image(png, cols, rows, layers, img_id=IMG_ID):
    """The image data as a VIRTUAL placement (U=1: nothing is drawn where the
    cursor is; the placeholder cells show it), replacing the previous frame under
    the same id. q=2 silences the terminal's replies, which would otherwise land
    on stdin looking like key presses."""
    b64 = base64.b64encode(png).decode()
    parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)] or [""]
    out = []
    for n, chunk in enumerate(parts):
        more = 1 if n < len(parts) - 1 else 0
        head = (f"a=T,U=1,f=100,i={img_id},q=2,c={cols},r={rows},m={more}" if n == 0
                else f"q=2,m={more}")
        out.append(wrap_passthrough(f"\x1b_G{head};{chunk}\x1b\\", layers))
    return "".join(out)


def kitty_image_file(path, w, h, cols, rows, layers, img_id=IMG_ID):
    """kitty_image, but the terminal reads the frame from a temp file. Through
    tmux this matters most: the per-frame message is ~150 bytes instead of
    ~400 KB pushed through two tmux layers, which was the old flashing and the
    20 fps ceiling when docked."""
    b64 = base64.b64encode(path.encode()).decode()
    return wrap_passthrough(f"\x1b_Ga=T,U=1,f=24,t=t,s={w},v={h},i={img_id},q=2,"
                            f"c={cols},r={rows};{b64}\x1b\\", layers)


def kitty_place(png, cols, rows, x, y, img_id):
    """Direct placement for a terminal with NO tmux in between (the tv window):
    cursor to (x, y), transmit and show the image there at cols x rows cells.
    C=1 keeps the cursor still. Used with two ids alternating: the new frame is
    placed on top, then the old one deleted, so there is never a gap."""
    b64 = base64.b64encode(png).decode()
    parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)] or [""]
    out = [f"\x1b[{y + 1};{x + 1}H"]
    for n, chunk in enumerate(parts):
        more = 1 if n < len(parts) - 1 else 0
        head = (f"a=T,f=100,i={img_id},p=1,q=2,C=1,z={img_id},c={cols},r={rows},m={more}" if n == 0
                else f"q=2,m={more}")
        out.append(f"\x1b_G{head};{chunk}\x1b\\")
    return "".join(out)


def kitty_place_file(path, w, h, cols, rows, x, y, img_id):
    """Direct placement where the terminal READS the frame from a temp file
    (raw RGB) instead of receiving it through the pty. The escape is ~150
    bytes whatever the frame size, so resolution is no longer limited by how
    fast text can be pushed at the terminal. The terminal deletes the file."""
    b64 = base64.b64encode(path.encode()).decode()
    return (f"\x1b[{y + 1};{x + 1}H\x1b_Ga=T,f=24,t=t,s={w},v={h},i={img_id},p=1,q=2,C=1,"
            f"z={img_id},c={cols},r={rows};{b64}\x1b\\")


FRAME_TAG = "tty-graphics-protocol-termhalo"      # the protocol insists on this in the name


def file_transfer_dir(fd):
    """A temp dir the terminal will read frames from, or None. Asks it: a 1x1
    image by temp file as a QUERY (a=q); only a terminal that really read the
    file answers OK. Not tried inside tmux (it would swallow the exchange)."""
    caps = os.path.join(DATA_DIR, "caps")
    if os.environ.get("TERMHALO_NOFILE"):
        return None
    if tmux_layers() != 0:
        # tmux swallows the terminal's answer, so the question cannot be asked
        # here. The same Ghostty answered it from a bare window earlier.
        try:
            d = open(caps).read().strip()
            return d if d and os.path.isdir(d) else None
        except OSError:
            return None
    for d in dict.fromkeys(filter(None, [os.environ.get("TMPDIR", "").rstrip("/"), "/tmp"])):
        path = os.path.join(d, f"{FRAME_TAG}-probe-{os.getpid()}.rgb")
        try:
            with open(path, "wb") as f:
                f.write(b"\0\0\0")
            b64 = base64.b64encode(path.encode()).decode()
            sys.stdout.write(f"\x1b_Gi=31,a=q,f=24,t=t,s=1,v=1;{b64}\x1b\\")
            sys.stdout.flush()
            reply, end = b"", time.time() + 0.6
            while time.time() < end and b"\x1b\\" not in reply:
                if select.select([fd], [], [], 0.05)[0]:
                    reply += os.read(fd, 256)
            if b";OK" in reply:
                try:
                    with open(caps, "w") as f:
                        f.write(d)
                except OSError:
                    pass
                return d
        except Exception:
            pass
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return None


class Watch:
    """Everything that needs a tmux subprocess, kept off the input loop: each
    call costs 10-40 ms, and on the loop that was a visible hitch every second.
    A daemon thread refreshes these; the loop only reads them."""

    def __init__(self):
        import threading
        self.layers = tmux_layers()
        self.cell = None                      # (cell width, cell height) in real pixels
        self._redraw = []
        self._layout, self._last = None, time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def redraw_soon(self, *delays):
        now = time.time()
        self._redraw = [now + d for d in delays]

    def _run(self):
        while True:
            try:
                self.layers = tmux_layers()
                px = picture_pixels(1, 1, exact=True)
                if px:
                    self.cell = px
                if self.layers > 0:
                    # Any change to the real terminal's layout (sidebar dragged,
                    # pane resized, window switched) makes tmux re-send the image
                    # cells bare again, whether or not the game redrew anything.
                    # Redraw once the change has settled, and every 20 s anyway.
                    lay = "".join(l for l in tmux(
                        "list-clients", "-F",
                        "#{client_termname}\t#{window_layout}\t#{client_width}x#{client_height}"
                    ).splitlines() if not l.startswith(("tmux", "screen")))
                    if lay != self._layout:
                        self._layout = lay
                        self.redraw_soon(0.4, 1.5)
                    elif time.time() - self._last > 20 and not self._redraw:
                        self.redraw_soon(0)
                due = [t for t in self._redraw if t <= time.time()]
                if due:
                    self._redraw = [t for t in self._redraw if t not in due]
                    self._last = time.time()
                    refresh_real_clients()
            except Exception:
                pass
            time.sleep(0.25 if self._redraw else 1.0)


class Pacer:
    """Even frame spacing. Frames used to go out the moment they were ready, so the
    picture ran at whatever the scene allowed (60+ when light, 15 when heavy) and
    swung between them. This hands out a tick every 1/fps seconds: when a frame is
    ready at the tick it is shown, and a late tick never causes a catch-up burst."""

    def __init__(self, fps):
        self.dt = 1.0 / max(1.0, fps)
        self.next = 0.0

    def ready(self, now):
        return now >= self.next - 0.002

    def mark(self, now):
        # stay on the grid when frames arrive on time, but never closer than 85% of an
        # interval to the previous one: a late frame must not be followed by a rushed one
        # (that uneven pair is the stutter), and a long stall must not cause a burst
        self.next = max(self.next + self.dt, now + 0.85 * self.dt)


class FrameGovernor:
    """Holds the picture steady by walking a ladder, one rung at a time:

        40 fps full quality  ->  30 fps full quality  ->  30 fps at 85%, 72%, 61%, 52% size

    Chrome's frame rate is what drops on a heavy scene (your history: about 35 frames/s
    then, about 72 normally). Showing a steady 30 beats a jittery 28-40, so the RATE comes
    down first; only if Chrome cannot even make 30 does the render SIZE come down (the one
    thing that brings Chrome's frames back). A rung is left after ~2 s below its rate and
    regained after ~8 s of clear headroom, so a brief stall never changes the picture. A
    smaller size that does not buy at least 10% more frames is undone and not retried for
    30 s: then the limit is somewhere else (the terminal), and blurring the picture would
    only cost quality for nothing."""

    RUNGS = ((60, 1.0), (50, 1.0), (40, 1.0), (30, 1.0), (30, 0.85), (30, 0.72), (30, 0.61), (30, 0.52))

    def __init__(self, top=60, rungs=None):
        r = list(rungs or self.RUNGS)
        self.rungs = [x for x in r if x[0] <= top] or [(top, 1.0)]
        if self.rungs[0][0] != top and top < r[0][0]:
            self.rungs.insert(0, (top, 1.0))
        self.i = 0
        self.low = self.high = 0
        self.pending = None            # (time, made, rung before) of a size step awaiting judgement
        self.hold_until = 0.0
        self.born = None
        self.climb_hold, self.climbed_at = 0.0, -1e9
        self.paced = False

    def reset(self):
        """Back to the top rung. Returns True when that changed anything."""
        changed = self.i != 0
        self.i, self.low, self.high, self.pending = 0, 0, 0, None
        return changed

    @property
    def target(self):
        return self.rungs[self.i][0]

    @property
    def scale(self):
        return self.rungs[self.i][1]

    def update(self, made, now=None):
        """Call about once a second with the frames Chrome produced per second. Returns
        True when the rung changed."""
        now = time.time() if now is None else now
        if made <= 0:
            return False
        if self.born is None:
            self.born = now
        if now - self.born < 12.0:                    # loading the page is not a heavy scene
            return False
        if self.pending and now - self.pending[0] >= 3.0:
            _t, before, prev = self.pending
            self.pending = None
            if made < before * 1.10 and made < self.target * 0.92:   # the smaller picture bought nothing: undo it
                self.i, self.hold_until, self.low, self.high = prev, now + 30.0, 0, 0
                return True
        if made < self.target * 0.92:
            self.low, self.high = self.low + 1, 0
        elif self.i > 0 and (
                (now >= self.climb_hold and made >= self.target * 0.96) if self.paced else
                made > (self.rungs[self.i - 1][0] * 1.04 if self.rungs[self.i - 1][1] == self.scale
                        else self.target * 1.3)):
            self.low, self.high = 0, self.high + 1       # steady here: earn a try at the rung above
        else:
            self.low = self.high = 0
        if self.low >= 2 and self.i < len(self.rungs) - 1:
            nxt = self.rungs[self.i + 1]
            if nxt[1] < self.scale and now < self.hold_until:
                self.low = 0                              # a smaller picture was tried and did not help
                return False
            if nxt[1] < self.scale:
                self.pending = (now, made, self.i)
            if now - self.climbed_at < 15.0:              # the try upward failed: do not retry soon
                self.climb_hold = now + 120.0
            self.i, self.low = self.i + 1, 0
            return True
        if self.high >= (6 if self.paced else 3) and self.i > 0:
            self.i, self.high, self.pending = self.i - 1, 0, None
            self.climbed_at = now
            return True
        return False


def picture_pixels(wc, hc, exact=False):
    """The picture's size in real screen pixels, when the terminal says.
    exact=True returns one cell's size as floats instead."""
    try:
        r, c, xp, yp = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
    except Exception:
        return None
    if not (r and c and xp and yp) and os.environ.get("TMUX"):
        for line in tmux("list-clients", "-F", "#{client_termname}\t#{client_tty}").splitlines():
            term, _, ctty = line.partition("\t")
            if term.startswith(("tmux", "screen")) or not ctty:
                continue                                  # a nested client, not the real terminal
            try:
                with open(ctty) as t:
                    r, c, xp, yp = struct.unpack(
                        "HHHH", fcntl.ioctl(t.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
                if r and c and xp and yp:
                    break
            except Exception:
                pass
    if not (r and c and xp and yp):
        return None
    if exact:
        return xp / c, yp / r
    return xp * wc // c, yp * hc // r


def kitty_delete(layers):
    return "".join(wrap_passthrough(f"\x1b_Ga=d,d=I,i={i},q=2\x1b\\", layers) for i in IMG_IDS)


def placeholder_cells(cols, rows, padx, pady, img_id=IMG_ID):
    """The text that makes Ghostty paint the image: wc x hc placeholder cells in
    the image's colour, the row mark on the first cell of every line. Written once
    per layout; the frames themselves only replace the image behind it."""
    out = [f"\x1b[38;5;{img_id}m"]
    colmarks = [chr(ROW_MARKS[c]) for c in range(cols)]
    for r in range(rows):
        rm = chr(ROW_MARKS[r])
        out.append(f"\x1b[{pady + r + 1};{padx + 1}H"
                   + "".join(PLACEHOLDER + rm + cm for cm in colmarks))
    out.append("\x1b[0m")
    return "".join(out)


def to_png(jpeg):
    """A screencast frame (JPEG) as the PNG the graphics protocol needs."""
    buf = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).convert("RGB").save(buf, "PNG", compress_level=1)
    return buf.getvalue()


class Encoder:
    """Decode + re-encode frames on a worker thread, so the input loop never
    waits on libjpeg/zlib and the camera keeps moving while a frame builds.
    Only the newest frame is kept; stale ones are simply never encoded."""

    def __init__(self):
        import threading
        self._mu = threading.Lock()
        self._ev = threading.Event()
        self._src = None                  # (seq, base64 jpeg) newest from Chrome
        self._out = None                  # (seq, png bytes | (path, w, h)) newest encoded
        self._taken = True                # has the main loop consumed _out
        self.file_dir = None              # set = write raw RGB files for the terminal
        self._lut_at, self._gamma, self._lut_tab = 0.0, None, None
        self._n = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _lut(self):
        """Shadow lift as a lookup table, from ~/.termhalo/brightness (a gamma:
        1.0 = untouched, default 1.5, higher = brighter). The map is lit like
        dusk (a frame averaged 37/255, two thirds of it under 40), so shadows
        are raised without clipping the bright parts. Re-read once a second,
        so it can be tuned while the game runs."""
        now = time.time()
        if now - self._lut_at > 1.0:
            self._lut_at = now
            try:
                g = float(open(os.path.join(DATA_DIR, "brightness")).read().strip())
            except (OSError, ValueError):
                g = 1.5
            g = max(0.5, min(3.0, g))
            if g != self._gamma:
                self._gamma = g
                self._lut_tab = (None if abs(g - 1.0) < 0.01 else
                                 [round(255 * (i / 255) ** (1 / g)) for i in range(256)] * 3)
        return self._lut_tab

    def feed(self, seq, raw):
        with self._mu:
            self._src = (seq, raw)
        self._ev.set()

    def ready(self, after):
        """The newest encoded frame if it is newer than `after`."""
        with self._mu:
            if self._out and self._out[0] != after:
                self._taken = True
                return self._out
        return None

    def _run(self):
        done = -1
        while True:
            self._ev.wait()
            self._ev.clear()
            with self._mu:
                item = self._src
            if not item or item[0] == done:
                continue
            jpeg = base64.b64decode(item[1])
            lut = self._lut()
            if self.file_dir:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")
                if lut:
                    img = img.point(lut)
                self._n += 1
                path = os.path.join(self.file_dir, f"{FRAME_TAG}-{os.getpid()}-{self._n}.rgb")
                with open(path, "wb") as f:
                    f.write(img.tobytes())
                payload = (path, img.width, img.height)
            elif lut:
                buf = io.BytesIO()
                Image.open(io.BytesIO(jpeg)).convert("RGB").point(lut).save(buf, "PNG", compress_level=1)
                payload = buf.getvalue()
            else:
                payload = to_png(jpeg)
            done = item[0]
            with self._mu:
                stale = None if self._taken else self._out
                self._out, self._taken = (done, payload), False
            if stale and isinstance(stale[1], tuple):   # never shown: the terminal will not delete it
                try:
                    os.unlink(stale[1][0])
                except OSError:
                    pass


class Screencast:
    """Frames pushed by Chrome's own compositor (about 25 a second even with
    software rendering) instead of one slow screenshot at a time (about 4)."""

    def __init__(self, ctx, page, view, quality=int(os.environ.get("TERMHALO_QUALITY", 80))):
        self.raw, self.count = None, 0
        self.paced = os.environ.get("TERMHALO_PACED_ACK", "0") == "1"
        self.unacked = None
        self.quality = quality
        self.cdp = ctx.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.start(view)

    def resize(self, view):
        try:
            self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        self.start(view)

    def start(self, view):
        self.view = view
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": self.quality,
                                               "everyNthFrame": int(os.environ.get("TERMHALO_NTH", 1)),
                                               "maxWidth": view[0], "maxHeight": view[1]})

    def _on_frame(self, ev):
        self.raw = ev["data"]                       # decoded only if it is ever shown
        self.count += 1
        if self.paced:
            self.unacked = ev["sessionId"]          # the loop acknowledges it on its clock
            return
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            pass

    def ack(self):
        """Ask Chrome for the next frame. Chrome sends nothing new until the last frame is
        acknowledged, so WHEN this is called sets the frame rate: called on an even clock
        it makes Chrome deliver evenly, and Python never has to receive (and throw away)
        the 80-150 frames a second Chrome would otherwise push in a match."""
        sid, self.unacked = self.unacked, None
        if sid is not None:
            try:
                self.cdp.send("Page.screencastFrameAck", {"sessionId": sid})
            except Exception:
                pass


def read_keys(fd):
    keys = []
    while select.select([fd], [], [], 0)[0]:
        data = os.read(fd, 64).decode(errors="ignore")
        i = 0
        while i < len(data):
            m = SGR_MOUSE.match(data, i) if data[i] == "\x1b" else None
            if m:
                b, x, y, fin = m.groups()
                keys.append(("mouse", int(b), int(x), int(y), fin == "M"))
                i = m.end()
                continue
            if data[i] == "\x1b" and (data[i:i + 3] in ARROWS
                                      or data[i:i + 3] in ("\x1b[I", "\x1b[O")):
                keys.append(data[i:i + 3]); i += 3
            else:
                keys.append(data[i]); i += 1
    return keys


CAPTURE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture.py")
CAPTURE_PYTHON = os.environ.get("TERMHALO_CAPTURE_PYTHON", sys.executable)   # needs pyobjc

# macOS virtual keycodes (what capture.py reports) -> the DOM codes the page gets.
MAC_KEYS = {0: "KeyA", 1: "KeyS", 2: "KeyD", 3: "KeyF", 4: "KeyH", 5: "KeyG", 6: "KeyZ",
            7: "KeyX", 8: "KeyC", 9: "KeyV", 11: "KeyB", 12: "KeyQ", 13: "KeyW", 14: "KeyE",
            15: "KeyR", 16: "KeyY", 17: "KeyT", 18: "Digit1", 19: "Digit2", 20: "Digit3",
            21: "Digit4", 22: "Digit6", 23: "Digit5", 25: "Digit9", 26: "Digit7",
            28: "Digit8", 29: "Digit0", 31: "KeyO", 32: "KeyU", 34: "KeyI", 35: "KeyP",
            37: "KeyL", 38: "KeyJ", 40: "KeyK", 45: "KeyN", 46: "KeyM", 49: "Space",
            48: "Tab", 36: "Enter", 51: "Backspace", 56: "ShiftLeft", 60: "ShiftRight",
            59: "ControlLeft", 62: "ControlRight", 58: "AltLeft", 61: "AltRight",
            123: "ArrowLeft", 124: "ArrowRight", 125: "ArrowDown", 126: "ArrowUp"}
MOUSE_BTN = {0: "left", 1: "right", 2: "middle"}

# Headless Chrome refuses requestPointerLock, so the game never reached its
# mouse-look state and we could not tell a match from a menu. This grants the
# lock on the page's side and records it in window.__termhaloLock: true means
# "in a match, capture the mouse", exactly when a real browser would lock it.
POINTER_LOCK_SHIM = """
(() => {
  let el = null;
  const fire = () => document.dispatchEvent(new Event('pointerlockchange'));
  Element.prototype.requestPointerLock = function () {
    el = this; window.__termhaloLock = true; try { window.__termhaloTell('lock1'); } catch (e) {} setTimeout(fire, 0); return Promise.resolve();
  };
  Document.prototype.exitPointerLock = function () {
    if (el) { el = null; window.__termhaloLock = false; try { window.__termhaloTell('lock0'); } catch (e) {} setTimeout(fire, 0); }
  };
  Object.defineProperty(Document.prototype, 'pointerLockElement',
                        { get: () => el, configurable: true });
})();
"""


# Mouse-look while locked: the same pointermove + mousemove pair a browser
# fires, carrying only movementX/Y, aimed at whatever holds the lock.
LOOK_JS = """([dx, dy]) => {
  const t = document.pointerLockElement || document.body;
  const o = {movementX: dx, movementY: dy, clientX: innerWidth / 2, clientY: innerHeight / 2,
             screenX: innerWidth / 2, screenY: innerHeight / 2,
             bubbles: true, cancelable: true, view: window};
  t.dispatchEvent(new PointerEvent('pointermove', Object.assign({pointerType: 'mouse'}, o)));
  t.dispatchEvent(new MouseEvent('mousemove', o));
}"""


class Capture:
    """Runs ~/.termhalo/capture.py while the player is in a match. Only the
    frontmost app may hide or freeze the macOS cursor, and a terminal program
    never is one, so that helper becomes it: cursor hidden and frozen, raw
    mouse deltas and REAL key down/up events come back on its stdout."""

    def __init__(self):
        self.proc, self.buf = None, b""

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return True
        try:
            self.proc = subprocess.Popen([CAPTURE_PYTHON, CAPTURE_PY], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.set_blocking(self.proc.stdout.fileno(), False)
            self.buf = b""
            return True
        except Exception:
            self.proc = None
            return False

    def stop(self):
        p, self.proc = self.proc, None
        if p:
            for f in (p.stdin.close, p.terminate):
                try:
                    f()
                except Exception:
                    pass

    def events(self):
        """Everything the helper has said since the last call, as word lists."""
        if not self.proc:
            return []
        try:
            while True:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.buf += chunk
        except (BlockingIOError, OSError):
            pass
        *lines, self.buf = self.buf.split(b"\n")
        return [l.decode(errors="ignore").split() for l in lines if l.strip()]


def main():
    global GEOM
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    vw, vh = VIEW
    look = LOOK * vw // 960
    arrows = {"\x1b[A": (0, -look), "\x1b[B": (0, look), "\x1b[C": (look, 0), "\x1b[D": (-look, 0)}
    # Above the 75 Hz screen on purpose: a cap near the frame time made every
    # frame wait for the NEXT loop pass (measured: Chrome 75/s, shown 50/s).
    # 40 a second, evenly paced: steadier than "as fast as it comes" (60+ then 15) and
    # about a third less work for Chrome, the encoder and the terminal to carry.
    fps_cap = float(os.environ.get("TERMHALO_FPS", 50))
    with sync_playwright() as p:
        # The Mac's own GPU (ANGLE on Metal). The old software renderer put the game
        # at under 9 frames a second and used five CPU cores doing it.
        if os.environ.get("TERMHALO_SOFTWARE"):
            gpu_args = ["--use-gl=angle", "--use-angle=swiftshader",
                        "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
        else:
            gpu_args = ["--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist",
                        "--enable-gpu-rasterization"]
            if os.environ.get("TERMHALO_UNLOCK", "0") == "1":
                # The screencast hands over about every second frame Chrome draws (measured:
                # page 74/s -> stream 42/s). Unlocking the frame rate lifts the page to ~108/s
                # and the stream to ~54/s, which is what a steady 50 needs.
                gpu_args += ["--disable-gpu-vsync", "--disable-frame-rate-limit"]
        # A persistent profile: the game's save (localStorage / IndexedDB) lives
        # in ~/.termhalo/profile and survives crashes, quits and restarts. The
        # old throwaway context lost all progress whenever the process died.
        # 2 = the game draws at retina density, like the page in a real browser
        # on this screen; 1 was what made text and HUD soft once stretched.
        dpr = float(os.environ.get("TERMHALO_DPR", 2))
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, viewport={"width": vw, "height": vh},
            device_scale_factor=dpr, args=gpu_args)
        # The page TELLS us when the game takes or drops the mouse. Asking it instead (page.evaluate, several times a second) waited on the game's
        # busy main thread each time: single passes of 300-580 ms, the visible stutter.
        told = {"lock": None}
        def _tell(_src, what):
            if what in ("lock1", "lock0"):
                told["lock"] = what == "lock1"
        ctx.expose_binding("__termhaloTell", _tell)
        ctx.add_init_script(POINTER_LOCK_SHIM)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="load")
        auto = os.environ.get("TERMHALO_AUTOSTART")
        if auto:                          # test hook: go straight into a bot match on that map
            try:
                if auto != "1":
                    page.goto(f"https://nothalo.lol/{auto}", wait_until="load")
                page.wait_for_selector("#menu-bots", timeout=15000)
                page.click("#menu-bots")
            except Exception:
                pass
        # Frames are sent smaller than the browser window: the bytes through tmux
        # (twice) are the limit, and Ghostty scales the image to the pane anyway.
        send_w = int(os.environ.get("TERMHALO_SEND", 720))
        cast = Screencast(ctx, page, (send_w, send_w * vh // vw))
        enc = Encoder()
        page.mouse.move(vw // 2, vh // 2)
        held, mx, my = {}, vw // 2, vh // 2
        pixels = os.environ.get("TERMHALO_PIXELS", "1") != "0"        # v flips it live
        watch = Watch()
        layers = watch.layers
        acc_x = acc_y = 0.0               # look movement not yet sent (fractions carry over)
        foot_at = 0.0
        cur_dsf = dpr                     # render density currently in effect
        drawn = None                      # the placeholder layout currently on screen
        cur = 0                           # which of IMG_IDS the screen is showing
        seen, last_emit = 0, 0.0
        stamps = collections.deque(maxlen=30)
        dragging = False
        last_click = ""
        sens_px = float(os.environ.get("TERMHALO_SENS_LOCK", 1.5))  # look speed in a match
        cap = Capture()                   # the cursor-hiding input helper
        locked = paused = manual = False  # manual = captured with `l`, not by the game
        lock_poll = 0.0
        close_poll = 0.0
        hover = None                      # newest pointer position over the picture
        cap_keys = set()                  # keys the helper says are down
        prof = collections.deque(maxlen=20)
        ticks = collections.deque(maxlen=300)           # seconds of work per loop pass
        gaps = collections.deque(maxlen=200)            # seconds between frames shown
        stats_at, stats_txt, last_shown = 0.0, "", 0.0
        want_known = False
        look_busy = []
        pacer = Pacer(fps_cap / 0.6 if os.environ.get("TERMHALO_PACED_ACK", "0") == "1" else fps_cap)
        ack_next = 0.0
        gov = FrameGovernor(int(fps_cap)) if os.environ.get("TERMHALO_GOVERNOR", "1") != "0" else None
        gov_at = 0.0
        if gov:
            gov.paced = cast.paced
        arrivals = collections.deque(maxlen=40)         # (time, frames Chrome has produced so far)
        measure_cell_ratio()
        # ?1003 = report ALL pointer motion, not only drags: moving the mouse
        # looks around, like pointer lock in the real browser. ?1004 = focus
        # in/out reports, so cmd-tabbing away releases the lock and pauses.
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1004h\x1b[?1006h")
        tty.setcbreak(fd)
        enc.file_dir = file_transfer_dir(fd)          # None = frames go through the pty as PNG
        if enc.file_dir:
            # A killed instance cannot tidy up: drop the frame it left behind.
            import glob
            for stray in glob.glob(os.path.join(enc.file_dir, f"{FRAME_TAG}-[0-9]*")):
                if f"-{os.getpid()}-" not in stray:
                    try:
                        os.unlink(stray)
                    except OSError:
                        pass
        size_at = 0.0
        wait_over = 0.0
        sent = collections.deque()                    # (time, frame file) handed to the terminal
        redraw_at = []                                # when to force tmux's full redraw
        try:
            while True:
                t0 = time.time()
                look_dx = look_dy = 0.0
                n_keys = 0
                for k in read_keys(fd):
                    n_keys += 1
                    if isinstance(k, tuple):                       # a mouse event
                        _, bt, mc, mr, down = k
                        if paused and not bt & 96 and down:
                            # A click on the picture means it has focus again,
                            # whatever tmux did or did not report.
                            paused = False
                            try:
                                cast.cdp.send("Page.setWebLifecycleState", {"state": "active"})
                            except Exception:
                                pass
                        if bt & 64 or paused or locked:             # wheel / helper owns input
                            continue
                        pt = cell_to_view(mc, mr)
                        if not pt:
                            continue
                        if bt & 32 and bt & 3 == 3:
                            # Menus: the pointer hovers where it really is, like a
                            # web page. Applied once per tick below.
                            hover = (int(pt[0]), int(pt[1]))
                            continue
                        mx, my = int(pt[0]), int(pt[1])
                        hover = None
                        page.mouse.move(mx, my)
                        if bt & 32:
                            pass                                    # dragging
                        elif bt & 3 == 0 and down:
                            page.mouse.down(); dragging = True
                            last_click = f" | clicked {mx},{my}"
                        elif not down and dragging:
                            page.mouse.up(); dragging = False
                        continue
                    if k == "\x1b[O":                               # window lost focus
                        if locked:
                            continue                                # only to our own helper
                        for code in list(held):
                            page.keyboard.up(code)
                        held.clear()
                        if layers > 0:
                            continue                                # docked above a chat: the chat has focus by design, so never pause on blur (no viewer = layers<0 already pauses)
                        if not paused:
                            paused = True
                            try:                                    # freeze = the web's hidden-tab pause
                                cast.cdp.send("Page.setWebLifecycleState", {"state": "frozen"})
                            except Exception:
                                pass
                        continue
                    if k == "\x1b[I":                               # focus is back
                        if paused:
                            paused = False
                            try:
                                cast.cdp.send("Page.setWebLifecycleState", {"state": "active"})
                            except Exception:
                                pass
                        continue
                    if k == ("Q" if layers > 0 else "q"):           # docked: shift-q, typing is too close
                        return
                    if paused:
                        continue
                    if k == "l":                                    # capture by hand
                        locked = manual = cap.start()
                        continue
                    if k == "\x1b":                                 # menus: esc goes to the game
                        page.keyboard.press("Escape")
                        continue
                    if k in arrows:
                        dx, dy = arrows[k]
                        mx, my = mx + dx, my + dy
                        page.mouse.move(mx, my)
                        if not vw * 0.1 < mx < vw * 0.9 or not vh * 0.18 < my < vh * 0.82:
                            mx, my = vw // 2, vh // 2
                            page.mouse.move(mx, my)
                    elif k == "v":
                        pixels = not pixels
                        sys.stdout.write(kitty_delete(max(layers, 0)) + "\x1b[2J")
                        drawn = None
                    elif k in ("f", "\n", "\r"):
                        page.mouse.down(); page.mouse.up()
                    elif k == "m":
                        page.mouse.click(mx, my)
                    elif k.lower() in KEYMAP:
                        code = KEYMAP[k.lower()]
                        if code not in held:
                            page.keyboard.down(code)
                        held[code] = t0
                # The game asking for pointer lock = a match started: capture.
                # The game dropping it (its own menu) = give the mouse back.
                T_in = time.time()
                if not paused and (told["lock"] is not None or t0 - lock_poll > 3.0):
                    if told["lock"] is not None:
                        want, told["lock"] = told["lock"], None
                        want_known = want
                    elif locked and not manual:
                        want = want_known               # nothing new from the page
                        lock_poll = t0
                    else:
                        lock_poll = t0                  # rare safety net if a message was missed
                        try:
                            want = want_known = bool(page.evaluate("!!window.__termhaloLock"))
                        except Exception:
                            want = False
                    if want and not locked:
                        locked, manual = cap.start(), False
                    # Deliberately NOT released when the game drops its lock (death,
                    # respawn screen): the capture is held from Play until esc or
                    # cmd-tab, so aiming is never interrupted and the cursor never
                    # reappears mid-session. The game's own pointer stays parked
                    # where Play was clicked, so a click on that overlay is Play.
                if locked:
                    ended = ""
                    for ev in cap.events():
                        try:
                            if ev[0] == "m":
                                look_dx += float(ev[1]) * sens_px
                                look_dy += float(ev[2]) * sens_px
                            elif ev[0] in ("d", "u"):
                                btn = MOUSE_BTN.get(int(ev[1]), "left")
                                (page.mouse.down if ev[0] == "d" else page.mouse.up)(button=btn)
                            elif ev[0] in ("kd", "ku"):
                                code = MAC_KEYS.get(int(ev[1]))
                                if code and ev[0] == "kd":
                                    page.keyboard.down(code); cap_keys.add(code)
                                elif code:
                                    page.keyboard.up(code); cap_keys.discard(code)
                            elif ev[0] in ("esc", "blur"):
                                ended = ev[0]
                        except (ValueError, IndexError):
                            pass
                    if not ended and not cap.running():
                        ended = "gone"                              # the helper died
                    if ended:
                        cap.stop()
                        locked = manual = False
                        for code in list(cap_keys):
                            page.keyboard.up(code)
                        cap_keys.clear()
                        page.mouse.up()
                        try:                                        # the game shows its own menu
                            page.evaluate("document.exitPointerLock()")
                        except Exception:
                            pass
                        lock_poll = t0
                        if ended == "blur" and not paused and layers <= 0:          # cmd-tab: pause, like a hidden tab
                            paused = True
                            try:
                                cast.cdp.send("Page.setWebLifecycleState", {"state": "frozen"})
                            except Exception:
                                pass
                elif hover and not paused:
                    mx, my = hover
                    hover = None
                    page.mouse.move(mx, my)
                if locked and not paused and (look_dx or look_dy or acc_x >= 1 or acc_x <= -1
                                              or acc_y >= 1 or acc_y <= -1):
                    # In a match: hand the game pure movement, the way a locked
                    # pointer reports it. No position is involved, so there is
                    # no edge to hit and nothing to snap back from (moving a
                    # virtual mouse and re-centring it yanked the camera back).
                    acc_x += look_dx
                    acc_y += look_dy
                    # Sent WITHOUT waiting for the page to answer: waiting blocked this loop on
                    # the game's main thread for up to 80 ms per mouse movement, in the middle
                    # of a fight. While two sends are still unanswered the movement is kept and
                    # added to the next one, so a busy page gets one bigger move, not a queue.
                    look_busy = [t for t in look_busy if not t.done()]
                    if len(look_busy) < 2:
                        ix, iy = int(acc_x), int(acc_y)
                        acc_x -= ix
                        acc_y -= iy
                        if ix or iy:
                            try:
                                task = page._loop.create_task(page._impl_obj.evaluate(LOOK_JS, [ix, iy]))
                                task.add_done_callback(lambda f: f.cancelled() or f.exception())
                                look_busy.append(task)
                            except Exception:
                                page.evaluate(LOOK_JS, [ix, iy])
                elif (look_dx or look_dy) and not paused:
                    # One move per tick for however many motion reports arrived;
                    # recenter near the edges like the arrows do, so the virtual
                    # mouse never runs out of room.
                    mx, my = mx + look_dx, my + look_dy
                    page.mouse.move(mx, my)
                    if not vw * 0.1 < mx < vw * 0.9 or not vh * 0.18 < my < vh * 0.82:
                        mx, my = vw // 2, vh // 2
                        page.mouse.move(mx, my)
                for code, ts in list(held.items()):
                    if t0 - ts > HOLD:
                        page.keyboard.up(code); del held[code]

                T_js = time.time()
                sz = os.get_terminal_size()
                cols, rows = sz.columns, sz.lines - 1
                layers = watch.layers                               # who is watching can change
                if not arrivals or arrivals[-1][1] != cast.count:
                    arrivals.append((time.time(), cast.count))
                if cast.paced and cast.unacked is not None and t0 >= ack_next - 0.002:
                    ack_dt = 1.0 / (gov.target if gov else fps_cap)
                    ack_next = max(ack_next + ack_dt, t0 + 0.85 * ack_dt)
                    cast.ack()

                if pixels and cast.raw is not None and layers >= 0:
                    enc.feed(cast.count, cast.raw)      # worker encodes newest only
                if redraw_at:
                    watch.redraw_soon(*[max(0.0, t - t0) for t in redraw_at])
                    redraw_at = []
                if gov and not locked and gov.i:
                    # Menus and the lobby make Chrome hand over ~42 frames a second whatever
                    # the load (it samples a mostly still page slowly). That is not a heavy
                    # scene: only a match is judged, and a match starts from the top rung.
                    if gov.reset():
                        pacer.dt = (0.6 if cast.paced else 1.0) / gov.target
                if gov and locked and t0 - gov_at > 1.0 and len(arrivals) > 5 and arrivals[-1][0] > arrivals[0][0]:
                    gov_at = t0
                    if gov.update((arrivals[-1][1] - arrivals[0][1]) / (arrivals[-1][0] - arrivals[0][0]), t0):
                        pacer.dt = (0.6 if cast.paced else 1.0) / gov.target
                if enc.file_dir and t0 - size_at > 1.0:
                    # Stream exactly as many pixels as the picture covers on
                    # screen (never more than the game renders): no stretching.
                    size_at = t0
                    fw, fh = fit_cells(cols, rows)
                    px = (int(watch.cell[0] * fw), int(watch.cell[1] * fh)) if watch.cell else None
                    if px:
                        scale = gov.scale if gov else 1.0
                        tw = max(320, min(int(vw * dpr), px[0]))
                        tw = max(240, int(tw * scale)) // 2 * 2
                        want = (tw, tw * vh // vw // 2 * 2)
                        if abs(want[0] - cast.view[0]) > cast.view[0] * 0.06:
                            cast.resize(want)
                        # Render only as dense as the picture is shown: drawing
                        # 1920 wide to display 774 cost Chrome a third of its
                        # frame rate. Never below 1x (text goes soft), never
                        # above what was asked for.
                        dsf = max(1.0 if scale >= 1.0 else max(0.5, scale),
                                  min(dpr, round(want[0] / vw * 4 + 0.49) / 4))
                        if dsf != cur_dsf:
                            cur_dsf = dsf
                            try:
                                cast.cdp.send("Emulation.setDeviceMetricsOverride",
                                              {"width": vw, "height": vh,
                                               "deviceScaleFactor": dsf, "mobile": False})
                            except Exception:
                                pass
                T_sz = time.time()
                got = (enc.ready(seen) if pixels and layers >= 0
                       and pacer.ready(t0) else None)
                if (pixels and got and layers >= 0 and pacer.ready(t0)) or \
                        (not pixels and cast.raw is not None and cast.count != seen
                         and layers >= 0 and pacer.ready(t0)):
                    last_emit = t0
                    pacer.mark(t0)
                    wc, hc = fit_cells(cols, rows)
                    if pixels and (hc > MAX_ROWS or wc > MAX_ROWS):  # the mark table's limit
                        hc = min(hc, MAX_ROWS)
                        wc = max(1, min(wc, MAX_ROWS, int(hc * 16 / 9 * CELL_RATIO)))
                    GEOM = ((cols - wc) // 2, (rows - hc) // 2, wc, hc)
                    if pixels:
                        # The character grid is written ONCE per layout and never
                        # touched again; each frame only replaces the image behind it
                        # (same id). Rewriting the grid every frame to swap ids sent
                        # ~50 KB of text a frame through two tmux layers, and tmux's
                        # piecemeal redraws of that were the flashing and the rows that
                        # showed the wrong slice of the picture.
                        seen, png = got
                        if isinstance(png, tuple):
                            # The terminal deletes each file as it reads it. One
                            # still there 2s later was never read (pane hidden,
                            # passthrough dropped): remove it, or they pile up.
                            sent.append((t0, png[0]))
                            while sent and t0 - sent[0][0] > 2:
                                try:
                                    os.unlink(sent.popleft()[1])
                                except OSError:
                                    pass
                        ta = tb = time.time()           # encode happens off-loop now
                        if layers == 0:
                            # No tmux: place the image directly, double buffered.
                            if drawn != (cols, rows, wc, hc):
                                dbg(f"CLEAR was={drawn} now={(cols, rows, wc, hc)}")
                                sys.stdout.write("\x1b[2J" + kitty_delete(0))
                                drawn = (cols, rows, wc, hc)
                            nxt = 1 - cur
                            if isinstance(png, tuple):          # (path, w, h): by file
                                place = kitty_place_file(*png, wc, hc, GEOM[0], GEOM[1], IMG_IDS[nxt])
                            else:
                                place = kitty_place(png, wc, hc, GEOM[0], GEOM[1], IMG_IDS[nxt])
                            data = place + f"\x1b_Ga=d,d=I,i={IMG_IDS[cur]},q=2\x1b\\"
                            dbg(f"F place={IMG_IDS[nxt]} del={IMG_IDS[1 - nxt]} {len(data)}b")
                            cur = nxt
                        else:
                            if drawn != (cols, rows, wc, hc):
                                dbg(f"CLEAR was={drawn} now={(cols, rows, wc, hc)}")
                                sys.stdout.write("\x1b[2J" + placeholder_cells(wc, hc, GEOM[0], GEOM[1], IMG_ID))
                                drawn = (cols, rows, wc, hc)
                                # once it has travelled both tmux layers, and once more for stragglers
                                redraw_at = [t0 + 0.5, t0 + 1.6]
                            if isinstance(png, tuple):
                                data = kitty_image_file(*png, wc, hc, layers, IMG_ID)
                            else:
                                data = kitty_image(png, wc, hc, layers, IMG_ID)
                        tc = time.time()
                        sys.stdout.write(data)
                        sys.stdout.flush()
                        td = time.time()
                        prof.append((tb - ta, tc - tb, td - tc, len(data)))
                    else:
                        seen = cast.count
                        sys.stdout.write(render(base64.b64decode(cast.raw), cols, rows))
                    stamps.append(time.time())
                    if last_shown:
                        gaps.append(stamps[-1] - last_shown)
                    last_shown = stamps[-1]
                T_em = time.time()
                fps = ""
                if len(stamps) > 2 and stamps[-1] > stamps[0] and time.time() - stamps[-1] < 3:
                    fps = f"{(len(stamps) - 1) / (stamps[-1] - stamps[0]):4.1f} fps"
                mode = ("pixels" if pixels else "blocks") + (" (waiting for a viewer)" if layers < 0 else "")
                mode += " | PAUSED" if paused else (" | IN GAME, esc frees the mouse" if locked else " | l = capture mouse")
                if os.environ.get("TERMHALO_PROFILE") and t0 - stats_at > 1.0 and len(ticks) > 20 and len(gaps) > 20:
                    # How even the picture is, not just how fast: pass = work per
                    # loop pass, gap = time between frames shown (median/95th/worst).
                    stats_at = t0
                    q = lambda d, f: sorted(d)[min(len(d) - 1, int(len(d) * f))] * 1000
                    made = ((arrivals[-1][1] - arrivals[0][1]) / (arrivals[-1][0] - arrivals[0][0])
                            if len(arrivals) > 5 and arrivals[-1][0] > arrivals[0][0] else 0)
                    stats_txt = (f" | pass {q(ticks, .5):.0f}/{q(ticks, .95):.0f}/{max(ticks) * 1000:.0f}ms"
                                 f" gap {q(gaps, .5):.0f}/{q(gaps, .95):.0f}/{max(gaps) * 1000:.0f}ms"
                                 f" chrome {made:.0f}/s")
                    try:
                        with open(os.path.join(DATA_DIR, "stats"), "w") as fh:
                            fh.write(f"{fps}{stats_txt} locked={locked}\n")
                        if locked:                        # in a match: keep a history to read later
                            with open(os.path.join(DATA_DIR, "stats.log"), "a") as fh:
                                fh.write(f"{time.strftime('%H:%M:%S')} {fps}{stats_txt}\n")
                    except OSError:
                        pass
                mode += stats_txt
                if gov and (gov.scale < 1.0 or gov.target < fps_cap):
                    mode += f" | holding {gov.target}" + (f" q {int(gov.scale * 100)}%" if gov.scale < 1.0 else "")
                via = f"{cast.view[0]}x{cast.view[1]} by file" if enc.file_dir else f"{cast.view[0]}px by pty"
                foot = (f" {'Q' if layers > 0 else 'q'} quit | {fps} | {via} | v: {mode}{last_click}")
                # Never wider than the window: a wrapped footer scrolls the
                # screen and leaves stacked copies of itself behind.
                if t0 - foot_at > 0.25:
                    foot_at = t0
                    sys.stdout.write(f"\x1b[{rows + 1};1H\x1b[0m\x1b[2K{foot[:max(0, cols - 1)]}")
                sys.stdout.flush()
                T_end = time.time()
                ticks.append(T_end - t0)
                if os.environ.get("TERMHALO_PROFILE") and (T_end - t0 > 0.025 or wait_over > 0.030):
                    # where a slow pass went: input, page calls, sizing, sending the frame, footer, and
                    # how long the idle wait overran (frame events are handled inside it)
                    try:
                        with open(os.path.join(DATA_DIR, "slow.log"), "a") as fh:
                            fh.write(f"{time.strftime('%H:%M:%S')} pass {(T_end - t0) * 1000:.0f}ms"
                                     f" in {(T_in - t0) * 1000:.0f} js {(T_js - T_in) * 1000:.0f}"
                                     f" size {(T_sz - T_js) * 1000:.0f} emit {(T_em - T_sz) * 1000:.0f}"
                                     f" foot {(T_end - T_em) * 1000:.0f} waitover {wait_over * 1000:.0f}"
                                     f" locked={int(locked)} keys={n_keys}\n")
                    except OSError:
                        pass
                # Idle INSIDE playwright, not in sleep(): Chrome holds the next
                # frame until this one is acknowledged, and the acknowledgement
                # only goes out while playwright is running. Sleeping here made
                # frames arrive 20 ms, then 40 ms, then 20 ms apart. 120 passes a
                # second also halves how stale the mouse can be.
                w_ms = max(1.0, (1 / 200 - (time.time() - t0)) * 1000)
                w0 = time.time()
                page.wait_for_timeout(w_ms)
                wait_over = time.time() - w0 - w_ms / 1000
        finally:
            cap.stop()                              # cursor back, always
            if enc.file_dir:                        # frames the terminal never read
                import glob
                for stray in glob.glob(os.path.join(enc.file_dir, f"{FRAME_TAG}-{os.getpid()}-*")):
                    try:
                        os.unlink(stray)
                    except OSError:
                        pass
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            try:
                sys.stdout.write(kitty_delete(max(layers, 0)))
            except Exception:
                pass
            sys.stdout.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l\x1b[?1006l\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()                      # restore the screen even if close() hangs
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    import faulthandler, traceback
    os.makedirs(PROFILE_DIR, exist_ok=True)
    _log = open(CRASH_LOG, "a")
    faulthandler.enable(_log)          # a hard native death (segfault etc.) lands here too
    try:
        main()
    except KeyboardInterrupt:
        pass                           # ctrl-c is a quit, not a crash
    except Exception:
        _log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{traceback.format_exc()}")
        _log.flush()
        traceback.print_exc()
        sys.exit(1)                    # the launcher loop restarts on a non-zero exit
