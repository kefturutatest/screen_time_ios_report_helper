#!/usr/bin/env python3
"""Interactive helper: dump the current iPhone screen's AX tree + a screenshot.

Usage:
    python3 explore.py                    # dump current screen
    python3 explore.py --tap X Y          # tap, then dump
    python3 explore.py --tap-text "Foo"   # tap first element whose text contains Foo
    python3 explore.py --swipe x1 y1 x2 y2
    python3 explore.py --open com.apple.Preferences
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import wda  # noqa: E402

SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=wda.DEFAULT_BASE,
                   help="WDA base URL, e.g. http://my-iphone.local:8100 "
                        "(defaults to $WDA_BASE)")
    p.add_argument("--open", dest="open_app")
    p.add_argument("--tap", nargs=2, type=float)
    p.add_argument("--tap-text")
    p.add_argument("--swipe", nargs=4, type=float)
    p.add_argument("--shot", default="explore.png")
    p.add_argument("--wait", type=float, default=1.5)
    p.add_argument("--no-dump", action="store_true")
    args = p.parse_args()

    d = wda.Wda(args.base)
    d.wait_ready(tries=5, delay=1)

    # Reuse an existing session if WDA already has one, else create.
    st = d.status()
    d.session_id = st.get("sessionId")
    if args.open_app or not d.session_id:
        d.open_app(args.open_app or "com.apple.Preferences",
                   relaunch=bool(args.open_app))

    if args.tap_text:
        tree = d.source()
        hits = wda.find_text(tree, args.tap_text)
        hits = [h for h in hits if wda.center(h)]
        if not hits:
            print(f"!! no element matching {args.tap_text!r}", file=sys.stderr)
            sys.exit(2)
        n = hits[0]
        x, y = wda.center(n)
        print(f"tapping {wda.text_of(n)!r} ({n.get('type')}) at {x:.0f},{y:.0f}")
        d.tap(x, y)
        time.sleep(args.wait)
    elif args.tap:
        d.tap(*args.tap)
        time.sleep(args.wait)
    elif args.swipe:
        d.swipe(*args.swipe)
        time.sleep(args.wait)

    os.makedirs(SHOTS, exist_ok=True)
    shot = os.path.join(SHOTS, args.shot)
    d.screenshot(shot)
    print(f"screenshot -> {shot}")
    print(f"window size: {d.window_size()}")

    if not args.no_dump:
        print(wda.dump(d.source()))


if __name__ == "__main__":
    main()
