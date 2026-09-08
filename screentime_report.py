#!/usr/bin/env python3
"""Scrape per-app Screen Time from a physical iPhone via WebDriverAgent.

Navigates: Ajustes -> Tiempo en pantalla -> Ver toda actividad en apps y sitios
-> "Día" tab, then walks backwards a day at a time by swiping the chart,
reading the "Más usadas" section for each day.

Dates are whatever the phone shows, i.e. the device's own local calendar,
which is what the Screen Time UI reports.

Usage:
    python3 screentime_report.py --base http://<phone>:8100 \
        --from 2026-08-26 --to 2026-09-06 \
        --apps claude github "google docs" anydesk tuflota
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import wda  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "shots")
OUT = os.path.join(HERE, "out")

# Spanish month names as the phone renders them (Peru uses "setiembre").
MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    # English fallback, in case the phone language changes
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Sections that follow "Más usadas" — they also list apps, but with pickup /
# notification *counts*, which must never be mistaken for durations.
NEXT_SECTIONS = ("Resumen de consultas", "Notificaciones", "Pickups", "Notifications")


# ---------------------------------------------------------------- helpers

def norm(s):
    """Casefold + strip accents + collapse spaces, for fuzzy app matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("‎", "").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


DUR_RE = re.compile(
    r"(?:(\d+)\s*(?:h|hr|hora?s?)\b)?\s*(?:(\d+)\s*(?:min|m)\b)?\s*(?:(\d+)\s*s\b)?",
    re.I,
)


def parse_duration(text):
    """'1 h 5 min' / '28 min' / '45 s' -> minutes (float). None if not a duration."""
    if not text:
        return None
    t = text.replace(" ", " ").replace("\xa0", " ").strip()
    if not re.search(r"\d", t):
        return None
    # "menos de 1 min" / "less than 1 min"
    if re.search(r"menos de|less than", t, re.I):
        return 0.5
    m = DUR_RE.fullmatch(t.strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 60 + mi + s / 60.0


def fmt_minutes(total):
    """Minutes (float) -> '1 h 05 min' style, matching how the phone reads."""
    total = int(round(total))
    h, m = divmod(total, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def parse_screen_date(label, today):
    """'Hoy, 7 de setiembre' / 'sábado, 30 de agosto' -> date."""
    if not label:
        return None
    low = norm(label)
    if low.startswith("hoy"):
        return today
    if low.startswith("ayer"):
        return today - dt.timedelta(days=1)
    m = re.search(r"(\d{1,2})\s+de\s+([a-z]+)", low)
    if not m:
        m2 = re.search(r"([a-z]+)\s+(\d{1,2})", low)  # English "September 6"
        if not m2:
            return None
        day, month = int(m2.group(2)), MONTHS.get(m2.group(1))
    else:
        day, month = int(m.group(1)), MONTHS.get(m.group(2))
    if not month:
        return None
    # Screen Time only goes backwards, so a month ahead of today means last year.
    year = today.year
    cand = dt.date(year, month, day)
    if cand > today:
        cand = dt.date(year - 1, month, day)
    return cand


def bundle_of(cell):
    for n, _ in wda.walk(cell):
        lbl = wda.text_of(n)
        m = re.search(r"bundleID=([\w.\-]+)", lbl)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- driver

class ScreenTime:
    def __init__(self, d, verbose=True):
        self.d = d
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    def tree(self):
        return self.d.source()

    def nodes(self, tree=None):
        return [n for n, _ in wda.walk(tree if tree is not None else self.tree())]

    def tap_node(self, node, wait=1.5, fx=0.5, fy=0.5):
        """Tap inside `node`'s rect at fractional position (fx, fy)."""
        r = node.get("rect") or {}
        x = r.get("x", 0) + r.get("width", 0) * fx
        y = r.get("y", 0) + r.get("height", 0) * fy
        self.d.tap(x, y)
        time.sleep(wait)

    @staticmethod
    def visible(node):
        return node.get("isVisible") in ("1", 1, True)

    def scroll_to_top(self, times=8):
        """Tapping the status bar is iOS's own scroll-to-top gesture — one tap
        instead of a dozen swipes. Fall back to swiping if it doesn't take."""
        self.d.tap(195, 8)
        time.sleep(1.2)
        for _ in range(times):
            if self.chart_y() is not None:
                return
            self.d.swipe(195, 250, 195, 780, 0.25)
            time.sleep(0.4)

    # ---- navigation ----------------------------------------------
    def goto_activity_day_view(self, today=None, steps=25):
        """Ajustes -> Tiempo en pantalla -> Ver toda actividad -> tab 'Día'.

        Driven by what is actually on screen rather than a fixed tap sequence:
        Settings restores whatever pane it was last left on (a relaunch does
        not reliably return to the root), so a blind "pop back N times, then
        scroll for the Screen Time row" walk gets stranded whenever the phone
        is already deeper — or already exactly where we want to be.
        """
        today = today or dt.date.today()
        self.log("· attaching to Settings")
        self.d.attach("com.apple.Preferences")
        time.sleep(2)

        for _ in range(steps):
            tree = self.tree()
            nodes = [n for n, _ in wda.walk(tree)]
            texts = {wda.text_of(n) for n in nodes}

            # Already on the activity screen?
            if "Semana" in texts and "Día" in texts:
                if self.current_date(today, tree)[0] is not None:
                    self.log("· on 'actividad en apps y sitios', tab Día")
                    return
                self.log("· switching to the 'Día' tab")
                day = [n for n in nodes
                       if wda.text_of(n) in ("Día", "Day") and n.get("type") == "Button"]
                if day:
                    self.tap_node(day[0], wait=3)
                    continue

            summary = [n for n in nodes if wda.text_of(n) == "SCREEN_TIME_SUMMARY"
                       and n.get("type") == "Cell" and self.visible(n)]
            if summary:
                self.log("· opening 'Ver toda actividad en apps y sitios'")
                self.tap_node(summary[0], wait=4)
                continue

            row = [n for n in nodes
                   if wda.text_of(n) == "com.apple.settings.screenTime"
                   and self.visible(n)]
            if row:
                self.log("· opening Tiempo en pantalla")
                self.tap_node(row[0], wait=3)
                continue

            # Somewhere else in Settings: climb out, then hunt for the row.
            if "com.apple.settings.airplaneMode" in texts:
                self.d.swipe(195, 700, 195, 300, 0.25)   # on the root list
                time.sleep(0.8)
                continue
            backs = [n for n in nodes if wda.text_of(n) == "BackButton"
                     and n.get("type") == "Button"]
            if backs:
                self.tap_node(backs[0], wait=1.2)
                continue
            self.d.swipe(195, 700, 195, 300, 0.25)
            time.sleep(0.8)

        raise RuntimeError("could not reach the Screen Time activity screen")

    # The header above the chart, e.g. 'Hoy, 7 de setiembre' /
    # 'Ayer, 6 de setiembre' / 'sábado, 30 de agosto'. The same string also
    # appears with the day's total appended ('…, ocho horas y …'); this
    # pattern deliberately matches only the bare form.
    DATE_LABEL_RE = re.compile(r"^[^,]+,\s*\d{1,2}\s+de\s+\w+$", re.I | re.U)
    DATE_LABEL_EN_RE = re.compile(r"^[^,]+,\s*\w+\s+\d{1,2}$", re.I | re.U)

    def current_date(self, today, tree=None):
        """Read the date shown above the chart."""
        for n in self.nodes(tree):
            t = wda.text_of(n)
            if not t:
                continue
            if self.DATE_LABEL_RE.match(t) or self.DATE_LABEL_EN_RE.match(t):
                dd = parse_screen_date(t, today)
                if dd:
                    return dd, t
        return None, None

    def chart_y(self, tree=None):
        """A y inside the Screen Time chart card, or None if it isn't on screen."""
        for n in self.nodes(tree):
            if str(n.get("name") or "").startswith("SpecifierIdentifierHistoricalScreenTime"):
                r = n.get("rect") or {}
                if r.get("height", 0) > 200 and self.visible(n):
                    return r["y"] + r["height"] * 0.62
        return None

    def step_day(self, ref, expected, back=True, tries=6):
        """Move the view one day, confirming the header agrees.

        Swiping the chart horizontally is what actually works — right for the
        previous day, left for the next. The `yesterday_arrow` /
        `tomorrow_arrow` buttons in the accessibility tree are not rendered
        controls: they report `isVisible=0` with a rect pinned under the
        navigation bar, so tapping there hits the nav bar (or the back button,
        whose box overlaps) instead of changing the day.
        """
        for _ in range(tries):
            self.scroll_to_top()
            y = self.chart_y() or 470.0
            x1, x2 = (90, 330) if back else (330, 90)
            self.d.swipe(x1, y, x2, y, 0.35)
            time.sleep(2.2)
            shown, raw = self.current_date(ref)
            if shown == expected:
                return shown, raw
        shown, raw = self.current_date(ref)
        raise RuntimeError(
            f"could not step to {expected}; phone shows {shown} ({raw!r})")

    def goto_date(self, target, ref, cur=None, max_steps=80):
        """Walk the day view to `target`, from wherever it currently sits."""
        if cur is None:
            cur, _ = self.current_date(ref)
        if cur is None:
            raise RuntimeError("cannot read the date label on the activity screen")
        for _ in range(max_steps):
            if cur == target:
                return cur
            back = cur > target
            nxt = cur - dt.timedelta(days=1) if back else cur + dt.timedelta(days=1)
            cur, _ = self.step_day(ref, nxt, back=back)
        raise RuntimeError(f"could not reach {target}; stopped at {cur}")

    # ---- extraction ----------------------------------------------
    def read_day_apps(self, max_steps=22):
        """-> {bundle_id: {'name':..,'minutes':..}} for the currently shown day.

        One downward pass from the top of the screen. At each step we absorb
        whatever rows are on screen, tap "Mostrar más" when it shows up, and
        stop once the pickups / notifications header comes into view.

        Only rows that carry BOTH an app icon (bundle id) and a value that
        parses as a duration count as usage. The "Resumen de consultas"
        (pickups) and "Notificaciones" sections list the same apps with bare
        integer counts, which the duration parser rejects — so they can never
        contaminate the numbers.

        Note we work off `isVisible` rather than rect maths: rows scrolled out
        of the viewport report a collapsed rect pinned to the table's top edge,
        so their coordinates say nothing about which way to scroll.
        """
        self.scroll_to_top()
        obs = {}
        expanded = 0
        stale = 0
        for _ in range(max_steps):
            tree = self.tree()
            nodes = [n for n, _ in wda.walk(tree)]
            before = len(obs)

            # Only rows sitting *wholly* inside the viewport are trustworthy.
            # A recycled cell part-way on screen can be read mid-update, with
            # its icon already showing the new app while its labels still show
            # the old one (that is how TikTok's 62 min once arrived carrying
            # GitHub's bundle id). Each row is voted on across the overlapping
            # snapshots below, and only the majority reading is kept.
            for n in nodes:
                if n.get("type") == "Cell" and self.visible(n):
                    r = n.get("rect") or {}
                    if r.get("y", -1) >= self.VIEWPORT_TOP and \
                            r.get("y", 0) + r.get("height", 0) <= self.VIEWPORT_BOTTOM:
                        self._absorb_cell(n, obs)

            # "Mostrar más" (bare, no .0/.1 suffix) belongs to "Más usadas";
            # the pickups and notifications lists get suffixed identifiers.
            # It has to be on screen for the tap to land.
            more = [n for n in nodes
                    if n.get("type") == "Cell" and self.visible(n)
                    and wda.text_of(n) in ("Mostrar más", "Mostrar Más", "Show More")]
            if more and expanded < 5:
                expanded += 1
                self.tap_node(more[0], wait=2.0)
                stale = 0
                continue

            stale = stale + 1 if len(obs) == before else 0
            passed = any(wda.text_of(n) in NEXT_SECTIONS and self.visible(n)
                         for n in nodes)
            if passed or (stale >= 4 and obs):
                break

            # Short steps so consecutive snapshots overlap: every row gets read
            # at least twice, which is what makes the vote meaningful.
            self.d.swipe(195, 700, 195, 430, 0.3)
            time.sleep(1.4)
        return self._tally(obs)

    @staticmethod
    def _tally(obs):
        """Resolve each row to its majority (name, duration) reading."""
        out = {}
        for bid, counter in obs.items():
            (name, mins), votes = max(counter.items(), key=lambda kv: (kv[1], kv[0][1]))
            out[bid] = {
                "name": name,
                "minutes": mins,
                "votes": votes,
                "total_reads": sum(counter.values()),
                "disputed": len(counter) > 1,
            }
        return out

    VIEWPORT_TOP = 105
    VIEWPORT_BOTTOM = 830

    @staticmethod
    def _absorb_cell(cell, obs):
        bid = bundle_of(cell)
        if not bid:
            return

        # Only look at the row's own labels. The Cell's *identifier* is not
        # trustworthy: UITableView recycles cells, so a row can still carry the
        # identifier of the app that previously occupied it (that is how
        # "Calendario de Google" ended up labelled with Interbank's bundle id).
        # The StaticText children are re-set on every reuse, so they are.
        labels = [wda.text_of(c) for c, _ in wda.walk(cell)
                  if c is not cell and c.get("type") == "StaticText"]
        labels = [t for t in labels if t and "bundleID=" not in t]

        dur = None
        name = None
        for t in labels:
            v = parse_duration(t)
            if v is not None:
                if dur is None:
                    dur = v
            elif name is None and t != "chevron":
                name = t.replace("‎", "").strip()
        if dur is None:
            return  # pickup / notification rows carry bare counts, not durations

        key = (name or bid, round(dur, 2))
        obs.setdefault(bid, {})
        obs[bid][key] = obs[bid].get(key, 0) + 1


# ---------------------------------------------------------------- main

def match_app(query, bundle, name):
    """Match a user's app name against a row's display name or bundle id.

    Bundle separators become spaces so that e.g. "google docs" matches
    com.google.Docs — useful because the phone shows Google Docs under its
    localised name ("Documentos").
    """
    q = norm(query)
    bundle_words = norm(re.sub(r"[.\-_]+", " ", bundle or ""))
    return q in norm(name) or q in bundle_words


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=wda.DEFAULT_BASE)
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    p.add_argument("--apps", nargs="+", required=True)
    p.add_argument("--today", help="override the phone's 'today' (YYYY-MM-DD)")
    p.add_argument("--out", default=os.path.join(OUT, "screentime.json"))
    p.add_argument("--from-json", dest="from_json",
                   help="re-print a report from saved raw data, without touching the phone")
    args = p.parse_args()

    if args.from_json:
        saved = json.load(open(args.from_json))["days"]
        drop_torn_rows(saved)
        print_report(saved, args.apps,
                     dt.date.fromisoformat(args.date_from),
                     dt.date.fromisoformat(args.date_to))
        return

    d_from = dt.date.fromisoformat(args.date_from)
    d_to = dt.date.fromisoformat(args.date_to)
    if d_from > d_to:
        sys.exit("--from must be <= --to")

    os.makedirs(SHOTS, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    d = wda.Wda(args.base)
    d.wait_ready(tries=10, delay=2)
    st = ScreenTime(d)

    # Reference "today" only anchors the parsing of 'Hoy'/'Ayer' labels; the
    # view itself may be sitting on any day when we arrive.
    ref = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    st.goto_activity_day_view(ref)

    cur, raw = st.current_date(ref)
    st.log(f"· phone shows {raw!r} -> {cur}")
    if cur != d_to:
        st.log(f"· moving to {d_to} …")
        cur = st.goto_date(d_to, ref, cur)

    days = {}
    while True:
        st.log(f"· reading {cur} …")
        apps = st.read_day_apps()
        days[cur.isoformat()] = apps
        d.screenshot(os.path.join(SHOTS, f"day_{cur.isoformat()}.png"))
        st.log(f"    {len(apps)} apps")
        if cur <= d_from:
            break
        cur, _ = st.step_day(ref, cur - dt.timedelta(days=1), back=True)

    with open(args.out, "w") as f:
        json.dump({"days": days, "generated": dt.datetime.now().isoformat()}, f,
                  indent=2, ensure_ascii=False)
    st.log(f"\nraw data -> {args.out}")

    drop_torn_rows(days, log=st.log)
    print_report(days, args.apps, d_from, d_to)


def drop_torn_rows(days, log=print):
    """Discard rows whose app name disagrees with the run-wide consensus.

    A bundle id maps to exactly one app, so if com.google.Gmail reads as
    "Gmail" on eleven days and "Claude" on one, that one is a torn read of a
    recycled cell and its minutes belong to neither app. Dropping is the safe
    call: keeping it would silently credit one app with another's time.
    """
    names = {}
    for rows in days.values():
        for bid, info in rows.items():
            names.setdefault(bid, {})
            names[bid][info["name"]] = names[bid].get(info["name"], 0) + 1

    consensus = {bid: max(c.items(), key=lambda kv: kv[1])[0] for bid, c in names.items()}
    dropped = []
    for day in sorted(days):
        for bid in list(days[day]):
            info = days[day][bid]
            if info["name"] != consensus[bid]:
                dropped.append((day, bid, info["name"], info["minutes"],
                                consensus[bid]))
                del days[day][bid]
    if dropped:
        log(f"\nDiscarded {len(dropped)} torn row(s) "
            f"(cell recycling mixed one app's icon with another's labels):")
        for day, bid, name, mins, want in dropped:
            log(f"  {day}  {name!r} ({fmt_minutes(mins)}) carried {bid}, "
                f"which is {want!r} everywhere else")
    return dropped


def print_report(days, apps, d_from, d_to):
    matched = {a: {} for a in apps}
    totals = {a: 0.0 for a in apps}
    per_day = {}

    for day in sorted(days):
        row = {}
        for a in apps:
            mins = 0.0
            for bid, info in days[day].items():
                if match_app(a, bid, info["name"]):
                    mins += info["minutes"]
                    matched[a][bid] = info["name"]
            row[a] = mins
            totals[a] += mins
        per_day[day] = row

    w = max(11, *(len(a) for a in apps))
    print()
    print(f"iPhone Screen Time · {d_from} .. {d_to}  (phone's local calendar, America/Lima)")
    print("=" * (12 + (w + 3) * len(apps) + 12))
    header = "Date        " + "".join(f"{a[:w]:>{w + 3}}" for a in apps) + f"{'DAY TOTAL':>12}"
    print(header)
    print("-" * len(header))
    for day in sorted(days):
        cells = "".join(f"{(fmt_minutes(per_day[day][a]) if per_day[day][a] else '–'):>{w + 3}}"
                        for a in apps)
        dtot = sum(per_day[day].values())
        print(f"{day}  {cells}{fmt_minutes(dtot) if dtot else '–':>12}")
    print("-" * len(header))
    grand = sum(totals.values())
    print("TOTAL       " + "".join(f"{fmt_minutes(totals[a]):>{w + 3}}" for a in apps)
          + f"{fmt_minutes(grand):>12}")
    print()
    print(f"Grand total across the {len(apps)} apps, {len(days)} days: "
          f"{fmt_minutes(grand)}  ({grand / 60:.2f} h)")
    print()
    print("Matched on the phone as:")
    for a in apps:
        if matched[a]:
            for bid, nm in matched[a].items():
                print(f"  {a:<12} -> {nm}  [{bid}]")
        else:
            print(f"  {a:<12} -> (never appeared in this range)")

    shaky = [(day, info) for day in sorted(days) for bid, info in days[day].items()
             if info.get("disputed") and any(match_app(a, bid, info["name"]) for a in apps)]
    if shaky:
        print("\nRows read inconsistently across snapshots (majority value used):")
        for day, info in shaky:
            print(f"  {day}  {info['name']}  {fmt_minutes(info['minutes'])}"
                  f"  ({info['votes']}/{info['total_reads']} reads agreed)")


if __name__ == "__main__":
    main()
