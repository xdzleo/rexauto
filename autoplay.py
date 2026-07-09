"""autoplay.py -- launch a recompiled game and keep pressing menu-advance keys
(Enter=START, A/Space=A-button on the MnK driver) so title/menu screens advance
without a human at the PC. Takes periodic screenshots for later review.

Usage:
  python autoplay.py <exe> <game_data_root> [--seconds N] [--shots DIR]

Keys are sent to the game window only when it is the foreground window (never
types into other apps). Screenshots land in --shots every ~20s.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time

user32 = ctypes.windll.user32

VK_RETURN = 0x0D
VK_SPACE = 0x20
VK_A = 0x41
KEYEVENTF_KEYUP = 0x0002


def press(vk):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def foreground_pid():
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def screenshot(path):
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
        "$bmp.Save('%s');$g.Dispose();$bmp.Dispose()" % path.replace("'", "''")
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("game_root")
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--shots", default=None)
    args = ap.parse_args()

    p = subprocess.Popen([args.exe, "--game_data_root=%s" % args.game_root],
                         cwd=os.path.dirname(args.exe),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    print("pid=%d" % p.pid, flush=True)
    t0 = time.time()
    shot_i = 0
    last_shot = 0.0
    # first 20s: let it boot untouched (intro logos); then press keys every ~3s
    while time.time() - t0 < args.seconds:
        if p.poll() is not None:
            print("EXITED rc=%s at t=%.0fs" % (p.returncode, time.time() - t0), flush=True)
            return
        t = time.time() - t0
        if t > 20 and foreground_pid() == p.pid:
            # alternate START (Enter) and A (Space + literal A) presses
            press(VK_RETURN)
            time.sleep(0.8)
            press(VK_SPACE)
            time.sleep(0.8)
            press(VK_A)
        if args.shots and time.time() - last_shot > 20:
            last_shot = time.time()
            shot_i += 1
            screenshot(os.path.join(args.shots, "shot_%02d.png" % shot_i))
        time.sleep(1.5)
    print("ALIVE at %ds -- terminating" % args.seconds, flush=True)
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()


if __name__ == "__main__":
    main()
