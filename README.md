# iPhone Screen Time report

Pulls **per-app Screen Time out of a physical iPhone** and prints a per-day +
total report for a list of apps over a date range — the numbers you would
otherwise copy by hand, one day at a time, out of Settings.

```bash
./run_report.sh 2026-08-26 2026-09-06 claude github "google docs" anydesk tuflota
```

Output lands in `out/` (a `report_*.txt` and the raw `screentime_*.json`), plus
one screenshot per day in `shots/`.

```
iPhone Screen Time · 2026-08-26 .. 2026-09-06  (phone's local calendar)
==============================================================================================
Date                claude        github   google docs       anydesk       tuflota   DAY TOTAL
----------------------------------------------------------------------------------------------
2026-08-26          27 min             –             –             –         1 min      28 min
2026-08-27      1 h 10 min        20 min         2 min             –         1 min  1 h 33 min
...
----------------------------------------------------------------------------------------------
TOTAL          11 h 26 min    1 h 48 min        50 min         9 min    1 h 10 min 15 h 23 min
```

---

## Why scrape the UI at all?

There is no API for this. All four plausible data routes are dead ends:

- **Apple's Screen Time API** (`FamilyControls` / `DeviceActivity`) hands out
  *opaque tokens*, not app identities, and a `DeviceActivityReport` extension is
  sandboxed so it cannot pass its own numbers back to its host app. It exists to
  build parental-control UIs, not to read usage out.
- **`knowledgeC.db` on the Mac** (`~/Library/Application Support/Knowledge/`)
  holds *Mac* app usage. iPhone rows only appear if Screen Time cross-device
  sync is on, which also requires `~/Library/Application Support/Screen Time/`
  to exist.
- **iTunes/Finder backups** exclude the Screen Time store
  (`RMAdminStore-Local.sqlite`, in `com.apple.remotemanagementd`'s container).
  Forensic tooling reaches it only from a full filesystem image.
- **iPhone Mirroring** needs Wi-Fi *and* Bluetooth on the Mac. On a Mac with no
  Wi-Fi hardware it just reports "Wi‑Fi Off" and never connects.

What is left is driving Settings — but via **WebDriverAgent**, reading Apple's
accessibility tree rather than OCR, so every number is an exact string from the
OS (`28 min`, `1 h 5 min`) and every app is identified by **bundle id**, not by
its localised display name.

## Setup

```bash
git clone --depth 1 https://github.com/appium/WebDriverAgent.git ~/WebDriverAgent
cp config.example.sh config.local.sh   # then fill it in
```

`config.local.sh` (gitignored) needs three things:

| variable | where to find it |
| --- | --- |
| `PHONE_HOST` | the phone's mDNS name, e.g. `my-iphone.local` — Settings › General › About › Name, lowercased, spaces as hyphens. Verify with `ping -c1 my-iphone.local`. |
| `DEVICE_UDID` | `xcrun devicectl list devices` (the `udid` field) |
| `DEV_TEAM` | `security find-identity -v -p codesigning` |

Then build WebDriverAgent once:

```bash
cd ~/WebDriverAgent
xcodebuild -project WebDriverAgent.xcodeproj -scheme WebDriverAgentRunner \
  -destination "id=$DEVICE_UDID" -allowProvisioningUpdates \
  DEVELOPMENT_TEAM=$DEV_TEAM build-for-testing
```

A team with a wildcard provisioning profile signs
`com.facebook.WebDriverAgentRunner` without registering a new app id. **Developer
Mode** must be enabled on the phone (Settings › Privacy & Security).

`run_report.sh` starts WDA (`test-without-building`) if it isn't already up, and
stops it on exit.

**Run everything as the logged-in user, never root** — `xcodebuild` and
`devicectl` talk to user-scoped XPC services and misbehave from a root shell.
The script handles this itself via `as_user`.

### How the phone is reached

Over the LAN, not USB: the phone answers at `$PHONE_HOST` and WebDriverAgent
listens on port **8100** there, so no `iproxy`/usbmuxd tunnel is involved. The
script re-resolves the name on every run, so a new DHCP lease is fine.

## Before a run: keep the phone unlocked

A full 12-day scrape takes ~25–35 min (a 7-day week, ~15–20), and **the phone
auto-locking mid-run kills it** — WDA keeps answering, but every screen is
SpringBoard's lock screen, so navigation fails with *"could not reach the Screen
Time activity screen"*. Nothing can recover this unattended: WDA's `/wda/unlock`
only wakes and swipes, it cannot type a passcode.

Set **Settings › Display & Brightness › Auto-Lock → Never** for the run (and put
it back after). To check mid-run:

```bash
SID=$(curl -s http://$PHONE_HOST:8100/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["sessionId"])')
curl -s "http://$PHONE_HOST:8100/session/$SID/wda/locked"   # {"value":true} == dead in the water
```

## Accuracy: rows tear, so every value is voted on

The most important thing in this repo. iOS recycles table rows, and a snapshot
taken while the list is moving can catch a row **mid-update — the icon already
showing the new app while the labels still show the old one**. The readings look
entirely plausible, which is what makes it dangerous. A first pass here silently
reported TikTok's 1 h 02 min as GitHub's, double-counted one app by also
crediting its 1 h 13 min to Gmail's bundle id, and gave one app's minutes to
another. Eleven such rows in twelve days.

Three defences, all in `read_day_apps`:

1. Only rows lying **wholly inside the viewport** are read
   (`VIEWPORT_TOP`/`VIEWPORT_BOTTOM`); the part-scrolled rows at the edges are
   the ones being recycled.
2. Scroll steps are **short enough that consecutive snapshots overlap**, so each
   row is read several times and its `(name, duration)` pair is decided by
   **majority vote** (`_tally`).
3. Afterwards `drop_torn_rows` enforces the invariant that **one bundle id has
   one app name** across the whole run. Any row disagreeing with the consensus
   is a proven tear and is discarded rather than trusted.

`--from-json` re-runs step 3 and the report against saved raw data in seconds,
which is how to check this without re-driving the phone for half an hour.

**Validate a run by scraping twice and diffing the JSONs.** Two independent
passes over one 12-day range agreed on 58 of 60 app-days; the two that differed
were exactly the cells the first pass had discarded as torn. A third pass over a
single day matched all 21 apps on that day exactly.

## Gotchas worth keeping (verified 2026-09-07, iOS 26.5 / Xcode 26.5)

- **`yesterday_arrow` / `tomorrow_arrow` are not real controls.** They appear in
  the accessibility tree but report `isVisible=0` with a rect pinned under the
  navigation bar; tapping there hits the nav bar, or the back button whose box
  overlaps them, and pops the screen instead of changing the day. Swiping the
  chart horizontally is what actually works — right for the previous day.
- **Cell identifiers are stale.** Because rows are recycled, a row's
  accessibility *identifier* can still name the app that previously occupied it.
  Read the app name from the row's `StaticText` children, never from the Cell's
  own name. The `AppIcon(bundleID=…)` image label is the reliable identifier.
- **Off-screen rows report a collapsed rect** pinned to the table's top edge, so
  their coordinates say nothing about which way to scroll. Use `isVisible` and
  scroll in one direction; don't do rect maths to decide direction.
- **Three "Mostrar más" buttons on the page.** The usage list's is the bare
  `Mostrar más`; pickups and notifications get `.0` / `.1` suffixes.
- **Pickups and notifications list the same apps** just below "Más usadas", with
  bare integer counts. A row counts as usage only if it has *both* a bundle id
  and a value that parses as a duration — that filter, not scroll position, is
  what keeps counts out of the totals.
- **Creating a WDA session with a `bundleId` can block for minutes.** The Screen
  Time screen animates a refresh spinner, so the app never goes quiescent.
  `Wda.attach()` reuses the existing session instead, and `Wda.tune()` sets
  `waitForIdleTimeout: 0` so taps stop waiting for idle. This turned an 8-minute
  navigation into 5 seconds.
- **Tapping the status bar** is iOS's scroll-to-top gesture — one tap instead of
  a dozen swipes.
- **Where a scroll drag *starts* decides whether it scrolls at all.** A drag
  from `y=700` moves the page by exactly 0 pt — measured at both 0.3 s and
  0.6 s — while `y=780` moves ~410 pt at either duration. Some days' layouts put
  something at 700 that swallows the pan; the duration is a red herring. This
  failed silently and cost four days of a nine-day run: the pass sat on screen
  one, read the two rows above the fold, decided the list had ended, and
  reported every app below it as unused (13 Sep's GitHub, 34 min, came out as
  "–"). `scroll_step` now escalates through three gestures and **confirms the
  view actually moved** by diffing row positions, and a day counts as read only
  once the pickups header is reached — otherwise it is retried, then flagged.
- **Screenshots can catch notification banners.** `shots/` is gitignored for
  this reason; check any screenshot before sharing it.

## Localisation

Written against a Spanish (Peru) phone: the month table accepts both
`setiembre` and `septiembre`, and section labels are matched in Spanish with
English fallbacks (`Semana`/`Día`, `Mostrar más`/`Show More`). Apps are matched
by bundle id with separators normalised to spaces, so `google docs` matches
`com.google.Docs` even though the phone shows it as **"Documentos"**. A phone in
another language needs the label constants near the top of
`screentime_report.py` extended.

## Files

| file | what |
| --- | --- |
| `run_report.sh` | entry point: starts WDA, scrapes, writes `out/` |
| `screentime_report.py` | navigation + extraction + report |
| `lib/wda.py` | small WebDriverAgent HTTP client |
| `explore.py` | debugging aid: dump the current screen's tree / tap things |
| `config.example.sh` | copy to `config.local.sh` and fill in |

`explore.py` is what to reach for when iOS changes the layout:

```bash
python3 explore.py --base http://my-iphone.local:8100            # dump this screen
python3 explore.py --base http://my-iphone.local:8100 --tap-text "Día"
```
