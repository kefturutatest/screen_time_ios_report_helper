"""Minimal WebDriverAgent HTTP client for driving a physical iPhone.

WDA runs on the device (port 8100) after `xcodebuild test-without-building`.
Reached over the LAN rather than usbmuxd, so the Mac needs no USB tether —
just the phone on the same network. Set WDA_BASE or pass `base` explicitly.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("WDA_BASE", "http://localhost:8100")


class WdaError(RuntimeError):
    pass


class Wda:
    def __init__(self, base=DEFAULT_BASE, timeout=60):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session_id = None

    # ---- transport -------------------------------------------------
    def _req(self, method, path, body=None, timeout=None):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        except urllib.error.URLError as e:
            raise WdaError(f"{method} {path}: {e}") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise WdaError(f"{method} {path}: non-JSON response {raw[:200]!r}")

    def _sreq(self, method, path, body=None, timeout=None):
        if not self.session_id:
            raise WdaError("no session; call open_app() first")
        return self._req(method, f"/session/{self.session_id}{path}", body, timeout)

    # ---- lifecycle -------------------------------------------------
    def status(self):
        return self._req("GET", "/status", timeout=10)

    def wait_ready(self, tries=60, delay=2):
        last = None
        for _ in range(tries):
            try:
                s = self.status()
                if s.get("value", {}).get("state") == "success" or s.get("sessionId") is not None:
                    return s
            except WdaError as e:
                last = e
            time.sleep(delay)
        raise WdaError(f"WDA never became ready: {last}")

    def attach(self, bundle_id="com.apple.Preferences"):
        """Reuse WDA's current session, creating one only if there is none.

        Creating a session with a `bundleId` makes WDA launch the app and wait
        for it to settle, which on the Screen Time screen (with its live
        refresh spinner) can block for many minutes. Attaching to the session
        that is already open costs nothing, so only pay that price once.
        """
        sid = None
        try:
            sid = self.status().get("sessionId")
        except WdaError:
            pass
        if sid:
            self.session_id = sid
            self.tune()
            try:
                self.activate(bundle_id)
            except WdaError:
                pass
            return sid
        return self.open_app(bundle_id)

    def activate(self, bundle_id):
        """Bring an already-running app to the foreground (no relaunch)."""
        return self._sreq("POST", "/wda/apps/activate", {"bundleId": bundle_id},
                          timeout=60)

    def open_app(self, bundle_id="com.apple.Preferences", relaunch=True):
        """Start a session attached to `bundle_id`. Cold-launches it."""
        caps = {
            "capabilities": {
                "alwaysMatch": {
                    "bundleId": bundle_id,
                    "shouldWaitForQuiescence": False,
                    "arguments": [],
                    "environment": {},
                }
            }
        }
        if relaunch:
            caps["capabilities"]["alwaysMatch"]["forceAppLaunch"] = True
        r = self._req("POST", "/session", caps, timeout=120)
        sid = r.get("sessionId") or r.get("value", {}).get("sessionId")
        if not sid:
            raise WdaError(f"could not open session: {r}")
        self.session_id = sid
        self.tune()
        return sid

    def tune(self):
        """Stop XCUITest waiting for the app to go quiescent before each action.

        The Screen Time screen keeps a refresh spinner animating, so it never
        reports idle and every tap/swipe otherwise blocks on 'Wait for
        com.apple.Preferences to idle' until the timeout expires.
        """
        try:
            self._sreq("POST", "/appium/settings", {"settings": {
                "waitForIdleTimeout": 0,
                "animationCoolOffTimeout": 0,
                "shouldWaitForQuiescence": False,
            }}, timeout=30)
        except WdaError:
            pass

    def close(self):
        if self.session_id:
            try:
                self._req("DELETE", f"/session/{self.session_id}", timeout=15)
            except WdaError:
                pass
            self.session_id = None

    # ---- reading ---------------------------------------------------
    def source(self):
        """Full accessibility tree as nested dicts."""
        r = self._sreq("GET", "/source?format=json", timeout=120)
        return r.get("value")

    def window_size(self):
        r = self._sreq("GET", "/window/size", timeout=20)
        return r["value"]

    def screenshot(self, path):
        r = self._sreq("GET", "/screenshot", timeout=60)
        raw = base64.b64decode(r["value"])
        with open(path, "wb") as f:
            f.write(raw)
        return path

    # ---- input -----------------------------------------------------
    def tap(self, x, y):
        # Newer WDA exposes /wda/tap; older /wda/tap/0. Try both.
        try:
            return self._sreq("POST", "/wda/tap", {"x": float(x), "y": float(y)})
        except WdaError:
            return self._sreq("POST", "/wda/tap/0", {"x": float(x), "y": float(y)})

    def swipe(self, x1, y1, x2, y2, duration=0.35):
        return self._sreq(
            "POST",
            "/wda/dragfromtoforduration",
            {
                "fromX": float(x1),
                "fromY": float(y1),
                "toX": float(x2),
                "toY": float(y2),
                "duration": float(duration),
            },
            timeout=60,
        )


# ---- tree helpers --------------------------------------------------

def walk(node, depth=0):
    """Yield (node, depth) for every node in a WDA source tree."""
    if node is None:
        return
    yield node, depth
    for child in node.get("children") or []:
        yield from walk(child, depth + 1)


def center(node):
    r = node.get("rect") or {}
    if not r:
        return None
    return (r.get("x", 0) + r.get("width", 0) / 2.0,
            r.get("y", 0) + r.get("height", 0) / 2.0)


def text_of(node):
    """Best human-readable text for a node."""
    for k in ("name", "label", "value"):
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def find(tree, predicate):
    """All nodes matching predicate(node) -> bool."""
    return [n for n, _ in walk(tree) if predicate(n)]


def find_text(tree, needle, exact=False, types=None):
    """Nodes whose text matches `needle` (case-insensitive)."""
    needle_l = needle.lower()

    def pred(n):
        if types and n.get("type") not in types:
            return False
        t = text_of(n).lower()
        if not t:
            return False
        return t == needle_l if exact else needle_l in t

    return find(tree, pred)


def dump(tree, only_visible=True):
    """Readable indented dump of the tree, for exploration."""
    lines = []
    for n, d in walk(tree):
        if only_visible and n.get("isVisible") in ("0", 0, False):
            continue
        t = text_of(n)
        r = n.get("rect") or {}
        rect = f"({r.get('x')},{r.get('y')} {r.get('width')}x{r.get('height')})" if r else ""
        lines.append(f"{'  ' * d}{n.get('type', '?')} {rect} {t!r}".rstrip())
    return "\n".join(lines)
