#!/usr/bin/env python
"""Pointer + keyboard capture for termhalo.

A terminal program is never the frontmost app, and macOS only lets the
frontmost app hide or freeze the cursor. This helper IS an app (no dock icon,
one invisible full-screen window): while it runs the cursor is hidden and
frozen, and every mouse move / button / key is printed on stdout for
termhalo.py to feed to the game:

    ready            the capture is live
    m DX DY          raw mouse movement (pixels)
    d N / u N        mouse button N down / up (0 left, 1 right, 2 middle)
    kd CODE / ku CODE  key down / up (macOS virtual keycode), real key-ups
    esc              escape pressed: capture ends
    blur             another app came to the front (cmd-tab): capture ends

It quits on esc, on blur, or when its stdin closes (the game went away). The
cursor comes back by itself when this process ends, however it ends."""
import sys, threading
import AppKit, Quartz
from AppKit import (NSApplication, NSApp, NSWindow, NSColor, NSScreen, NSCursor,
                    NSBackingStoreBuffered, NSEvent)

MODS = {56: 1 << 17, 60: 1 << 17,      # shift L/R
        59: 1 << 18, 62: 1 << 18,      # control
        58: 1 << 19, 61: 1 << 19}      # option


def out(s):
    try:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
    except Exception:
        NSApp.terminate_(None)


class Win(NSWindow):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def sendEvent_(self, ev):
        t = ev.type()
        if t in (AppKit.NSEventTypeMouseMoved, AppKit.NSEventTypeLeftMouseDragged,
                 AppKit.NSEventTypeRightMouseDragged, AppKit.NSEventTypeOtherMouseDragged):
            dx, dy = ev.deltaX(), ev.deltaY()
            if dx or dy:
                out(f"m {dx:.1f} {dy:.1f}")
        elif t == AppKit.NSEventTypeLeftMouseDown:
            out("d 0")
        elif t == AppKit.NSEventTypeLeftMouseUp:
            out("u 0")
        elif t == AppKit.NSEventTypeRightMouseDown:
            out("d 1")
        elif t == AppKit.NSEventTypeRightMouseUp:
            out("u 1")
        elif t == AppKit.NSEventTypeOtherMouseDown:
            out("d 2")
        elif t == AppKit.NSEventTypeOtherMouseUp:
            out("u 2")
        elif t == AppKit.NSEventTypeKeyDown:
            if ev.keyCode() == 53:
                out("esc")
                NSApp.terminate_(None)
            elif not ev.isARepeat():
                out(f"kd {ev.keyCode()}")
        elif t == AppKit.NSEventTypeKeyUp:
            out(f"ku {ev.keyCode()}")
        elif t == AppKit.NSEventTypeFlagsChanged:
            bit = MODS.get(ev.keyCode())
            if bit:
                out(("kd " if ev.modifierFlags() & bit else "ku ") + str(ev.keyCode()))


class Delegate(AppKit.NSObject):
    def applicationDidFinishLaunching_(self, note):
        frame = NSScreen.mainScreen().frame()
        w = Win.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, 0, NSBackingStoreBuffered, False)
        w.setOpaque_(False)
        # Not fully clear: a fully transparent window lets clicks fall through.
        w.setBackgroundColor_(NSColor.blackColor().colorWithAlphaComponent_(0.01))
        w.setLevel_(AppKit.NSStatusWindowLevel)
        w.setAcceptsMouseMovedEvents_(True)
        w.setIgnoresMouseEvents_(False)
        w.setHasShadow_(False)
        self.w = w
        NSApp.activateIgnoringOtherApps_(True)
        w.makeKeyAndOrderFront_(None)
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)   # freeze it in place
        NSCursor.hide()
        self.armed = False
        # activation is not instant; blur only counts once we have been active
        self.performSelector_withObject_afterDelay_("arm:", None, 0.5)
        out("ready")

    def arm_(self, _):
        self.armed = True
        out("active 1" if NSApp.isActive() else "active 0")

    def applicationDidResignActive_(self, note):
        if getattr(self, "armed", False):
            out("blur")
            NSApp.terminate_(None)

    def applicationWillTerminate_(self, note):
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        NSCursor.unhide()


def watch_stdin():
    try:
        sys.stdin.read()                 # returns when the game closes our stdin
    except Exception:
        pass
    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: NSApp.terminate_(None))


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    d = Delegate.alloc().init()
    app.setDelegate_(d)
    threading.Thread(target=watch_stdin, daemon=True).start()
    app.run()
