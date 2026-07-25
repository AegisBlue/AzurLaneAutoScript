# ShipCensus — dock progression census + completion dashboard

Custom feature of this ALAS fork (branch `custom`). Scans the dock, records every
ship's progression into `config/ship_census.json`, and generates a standalone
dashboard at `config/ship_census_dashboard.html` (both git-ignored). Purpose:
help the owner decide which ships to max next; designed so a future auto-max
task can consume the same store.

**Status: working in production.** First full run recorded 142 Elite+ ships in
~6 min; all readers offline-validated and live-verified (145/145 checks at
handoff, 89/89 on the enhance + fixed-skill fixes).

## Owner's requirements (decided via Q&A)

- Completion = max level + affinity ≥ 100 + enhance max + fully limit broken
  + **all skills Lv.10**. Oath / cognition awakening are displayed, not required.
- Level cap counts as 120 by default; the dashboard has a global 120/125 toggle
  plus per-ship "125" pins (saved in browser localStorage).
- Scan scope: Elite and above (GUI-configurable).
- Scanning is resumable + delta; dashboard is standalone HTML (never pywebio).

## How to run

GUI: **Tool → Ship Census** (ScanMode: delta / full / dashboard_only). Task class:
`ShipCensus` in [census.py](census.py), dispatched via `alas.py:ship_census()`.
Logs: `log/YYYY-MM-DD_alas.txt`. Safe to stop anytime — the sweep cursor is saved
per ship and resumes. Delta runs re-read skills/level/stars/affinity for every
ship each sweep (cheap, all on one screen); only the enhance-tab visit is skipped
once a ship reads maxed (irreversible) or fresh within `StaleDays`.

## Architecture

Two passes per sweep, joined by position with an **HP equality check** (HP is
near-unique per ship; a failed join only nulls that ship's affinity):

1. **Grid pass** — with the dock's Stats overlay on its Affinity page, read
   `(HP, affinity)` per card in dock order across all pages.
2. **Detail pass** — ExpFeed-style swipe iteration over ship detail pages. One
   Info-view frame yields name, level, HP, star row (limit break), skills; the
   Enhance tab is visited only when needed.

Files: [census.py](census.py) (task + readers), [store.py](store.py) (pure-stdlib
JSON store: `Name#n` dupe keys, sweep cursor, delta rules, missing-flag on
completed sweeps, `READER_VERSION` — **bump it whenever a reader changes what it
can see**; records scanned by an older reader are re-deep-scanned on the next
delta run instead of sitting on a stale wrong value until `StaleDays` expires), [dashboard.py](dashboard.py) + [dashboard_template.html](dashboard_template.html)
(self-contained page; template renders demo data when opened raw),
[ship_names_en.json](ship_names_en.json) (926 canonical EN names + rarity + the
41-ship PR/DR research roster, distilled from AzurLaneTools/AzurLaneData),
[assets.py](assets.py) (generated — never hand-edit; PNGs in `assets/{en,cn}/ship_census/`).

## Hard-won facts (violate these and it breaks)

**Dock / grid pass**
- The Stats button (905,10)-(970,44) cycles OFF → stats+Affinity → armor →
  skills → OFF. Amber mean ≈ (167,122,69) when on, blue ≈ (66,80,119) off. The
  page is identified by OCR of card 1's "Affinity" label, dynamically located.
- Overlay stat rows shift ~13 px between oath-framed and plain cards at constant
  pitch → anchor everything on the HP label band (top-most white band in cell-rel
  window (2,15,88,58)); affinity row = anchor + 126. Values are state-tinted
  (blue/white/pale-green) → `BrightDigit` (inverted-luma OCR), never color-extract.
- Card presence = HP-anchor found. Lv.-badge white-pixel counting reads ZERO on
  plain (non-oath) card frames — don't use it.
- The new dock shows **3 rows (7×3)**; `CARD_GRIDS` only models 2. `card_origin()`
  computes the third row; row 3's overlay rows still fit above y=720.
- `DOCK_SCROLL.next_page()` drags 0.8 viewports and the dock snaps to full
  3-row pages → rows get **skipped** between screens. Scroll row-exactly instead:
  `rows_total = 3 * Scroll.total / Scroll.length`, step 2 rows via `set()` with
  tight random_range, keep the row-fingerprint overlap dedupe as insurance.
- `set_top` before reading: the game **remembers dock scroll across visits**.
- `device.click_record_clear()` after every scroll and resume-skip swipe —
  12 consecutive same-button actions raise GameTooManyClickError.

**Detail page**
- The detail page lands on the Info view. Ship-to-ship swipes survive sidebar
  tab visits and Archive detours, but the stock `SWIPE_AREA` overlaps the
  secretary dialogue bubble which eats drags → `CENSUS_SWIPE_AREA` (225,180,570,430)
  is swapped into the equipment module global per call. A premature "end of dock"
  is retried up to 3× while the grid count says more cards exist.
- Star row (240,50)-(430,95): gold templates = current LB, dark = remaining;
  total clamps to {5,6} (Elite max 5, SR/UR/META 6; dark stars vanish on dark art
  → implausible totals record null). Works for META activation stars too.
- **Skill semantics**: "LEVEL: N" boxes are dark-on-light → gray-letter OCR
  (letter (90,90,90), thr 160). Locked cards read "LEVEL: ??" so classify
  digits-first, then padlock template (true ≥0.99 / false ≤0.49). Gray **"?"
  cards mean "no skill in this slot"** (Enterprise-style single-skill ships) —
  record nothing.
- **Skills that never level** sit at "LEVEL: 1" forever and the card looks
  exactly like a levelable Lv.1 skill → identified by name only, via the name
  band (marquee: retry up to 3 fresh frames, two extractions each) fuzzy-matched
  against `FIXED_SKILL_PATTERNS`, then recorded with max=1. Two families:
  **All Out Assault** barrages and **Siren Killer I/II/III** on every PR/DR
  research ship (it tracks development level, not skill books). OCR garbles the
  stylized band, so match short fragments: 'assa'/'sault'/'ssaut'/'outnsalt'
  and 'renkil'/'irenki' ('lsirenkiller', 'lsrenkiller', 'lirenkiller',
  'sirenkiler' all read live). META ships' Lv.1 skills ARE levelable — don't
  generalize "Lv.1 on a maxed ship" into a rule.
- Names drift between runs (Live2D art moves behind the chip) → always
  canonicalize via `ship_names_en.json` (squash + difflib 0.8). Unmatched names
  stay raw (data lags newest ships, e.g. Elbe META, U-2501 — harmless).
- **Lab-type ships** (sidebar = Research/Gear/Info, no Enhance/LimitBreak):
  METAs (name suffix " META") upgrade via META Lab; **PR research ships** (the
  41-name roster; e.g. Plymouth) via the Shipyard. Classify by NAME first —
  bright art defeats even luma template matching (Plymouth's wedding wings).
  Both record `enhance_maxed: null` (dashboard shows n/a + META/PR badge).
  **Never blind-click sidebar slots** — on a PR ship that navigates into the
  Shipyard and off the detail page entirely.
- Enhance tab: entry click via template+luma search (tabs shift one slot down on
  retrofit ships); **arrival = the opaque Fill button template** (tab highlight
  sims overlap; color counting false-positives on blue art). Return-to-Info
  verified by level-parse AND not-Fill-button (bare level-parse false-positives
  on enhance-bar pixels).
- Enhance rows (the panel is translucent — every judgement must ignore the art
  behind it; the tiny MAX:N labels are below OCR size):
  - *Enhanceable?* **Green dominance** of the "EXP:..." text, never brightness.
    Measured: enhanceable rows 300–470 green px, MAX:0 rows exactly 0. The old
    luma>200 test read Unicorn (Retrofit)'s capped FP/TRP rows as active (her
    white wings shine through) and reported a fully enhanced ship as "open" —
    same for every bright-art CVL, e.g. Independence.
  - *Maxed?* **Bar fill** (opaque chrome: 100% yellow columns when maxed, 0%
    otherwise) OR `TEMPLATE_ENH_MAX`. Neither alone is enough — over bright art
    the EXP:MAX template drops to 0.685 on a maxed row while unmaxed rows reach
    0.47, and a bar at 39/40 EXP is nearly full, hence the 0.99 ratio.
- General rule (inherited from MetaLab): template-match only opaque chrome;
  anything over art needs luma fallback plus a non-template arrival check.

**Wiring gotchas**
- Tool-section tasks skip SCHEDULER_PRIORITY but must be added to the
  `get_available_func()` whitelist in `module/submodule/utils.py`, or the Run
  button logs "No function matched" and exits. The whitelist is read in the
  spawned child process — no Alas.exe restart needed after editing.
- Full recipe for GUI tasks: task.yaml, argument.yaml, alas.py method,
  `config_updater` regen, i18n ×4, and (for scheduled tasks) SCHEDULER_PRIORITY.

## Testing workflow

- Offline: captures in `screenshots/ship_census_capture/` (git-ignored, 40 PNGs
  covering every ship type/state). Validation script pattern (scratchpad,
  `validate_census.py`): boot with `module.config.server.server='en'`,
  instantiate readers via `ShipCensus.__new__` (no device), assert every reader
  against every capture, positives and negatives. 145/145 at handoff.
- Live smoke: instantiate `ShipCensus(config='alas', task='ShipCensus')` in a
  script, monkeypatch `GRID_PAGE_LIMIT` / `SWEEP_SAFETY_LIMIT` small, point
  `CensusStore` at a scratch file. Set `PYTHONIOENCODING=utf-8` (cp1252 console
  crashes on μ ship names). Never run while an ALAS scheduler drives the game.
- **Capturing one named ship** (how the Unicorn/Monarch edge cases were pinned
  down): `ui_ensure(page_dock)` → click the magnifier (670,27) → click the field
  (840,28) → `device.adb_shell(['input','text',name])` → keyevent 66 → click the
  first result card (163,177) in a retry loop until `SHIP_DETAIL_CHECK` (the
  first tap only dismisses the suggestion dropdown / IME). Beware: the search is
  a prefix match on ALL owned ships, so "Bristol" opens Bristol META.

## Open items / future work

- **Auto-max task** (the owner's stated end goal): consume the store to pick
  targets — ExpFeed packs for leveling, the LimitBreak task, Academy for skills;
  keep the store schema versioned (`SCHEMA_VERSION` in store.py).
- Enhance read came back null for 3 non-lab ships in run 1 — likely transient
  timeouts; delta runs retry them. Investigate if it persists.
- The 2026-07-25 delta run ended at ship 81 of the 189 the grid pass counted
  ("Census sweep reached the end of the dock" after 3 failed swipe retries) —
  the swipe sweep can still die early; worth a look before trusting a run's
  `missing` flags.
- The dock's Stats overlay also has a Skills page (rows truncate above 2 skills
  — detail page stays authoritative) — possible future grid-only shortcut.
- `ship_names_en.json` refresh: rebuild from AzurLaneTools/AzurLaneData when new
  ships release (curl needs a browser User-Agent for the wiki; github raw works).
- A crashed run once queued ALAS's stock `Restart` recovery task in alas.json —
  harmless one-time game relaunch when the scheduler next runs.

## Commit trail

- `76045c279` Phase A: scaffold, store, dashboard, GUI wiring
- `8ba264d20` Phase B: live readers, assets, name dictionary (capture session)
- `abd2c2797` Tool-page whitelist fix
- `1e6980c37` click-record fix for scroll paging
- `0eea0436b` AOA skills, "?" slots, grid-pass coverage
- `ffa558f0b` enhance false-negatives (translucent panel), Siren Killer /
  fixed-level skills, store `READER_VERSION` rewrite
