# ShipCensus — dock progression census + completion dashboard

Custom feature of this ALAS fork (branch `custom`). Scans the dock, records every
ship's progression into `config/ship_census.json`, and generates a standalone
dashboard at `config/ship_census_dashboard.html` (both git-ignored). Purpose:
help the owner decide which ships to max next; designed so a future auto-max
task can consume the same store.

**Status: working in production.** Readers offline-validated and live-verified
(145/145 checks at handoff, 89/89 on the enhance + fixed-skill fixes). The grid
pass covers 162 of 163 known ships (99.4%) and the affinity join lands on 20/20
ships in a live smoke. **The Elite+ dock is ~550 ships**, so a full sweep is
~45 min (grid pass ~4, detail pass the rest) — earlier runs only ever reached
the first 80-160 before dying.

## Owner's requirements (decided via Q&A)

- Completion = max level + affinity ≥ 100 + enhance max + fully limit broken
  + **all skills Lv.10**. Oath / cognition awakening are displayed, not required.
- Level cap counts as 120 by default; the dashboard has a global 120/125 toggle
  plus per-ship "125" pins (saved in browser localStorage).
- Scan scope: Elite and above (GUI-configurable).
- Scanning is resumable + delta; dashboard is standalone HTML (never pywebio).

## How to run

GUI: **Tool → Ship Census** (ScanMode: delta / full / repair / dashboard_only). Task class:
`ShipCensus` in [census.py](census.py), dispatched via `alas.py:ship_census()`.
Logs: `log/YYYY-MM-DD_alas.txt`. Safe to stop anytime — the sweep cursor is saved
per ship and resumes. Delta runs re-read skills/level/stars/affinity for every
ship each sweep (cheap, all on one screen); only the enhance-tab visit is skipped
once a ship reads maxed (irreversible) or fresh within `StaleDays`.

## Repair mode — diagnosing the dashboard's "-"

`ScanMode: repair` re-reads **only** the records with gaps and writes down what
happened to each one, instead of re-walking 550 ships to find out.

- Targets come from `store.incomplete_ships()` (`missing_fields()` in store.py:
  level / hp / affinity / limit break / skills, plus enhance for non-lab ships;
  `rarity` is excluded — it comes from the name dictionary, not the screen).
- The run starts by pruning **phantom records** against its grid pass
  (`store.prune_phantom_copies`) — see the sweep section below for where they
  come from. They are targets no walk could ever satisfy.
- Targets are scattered over the whole dock (live: cards 22-643 of 644), so the
  walk swipes across short gaps and jumps through the dock (`dock_enter_at`) for
  long ones, comparing costs (~2.5 s per card swiped vs ~1.2 s per row dragged).
  Ships are matched to records by name+HP, never by position; a jump that lands
  next to its target (grid positions drift from dock positions) swipes a few
  cards ahead before giving up on it.
- Ships are matched to records by name+HP first, then by **HP alone when it
  singles a record out**, then by a fuzzy name. A record's stored name came from
  the same OCR that failed on it, so matching strictly on it locks out exactly
  the records the repair exists for (live: the walk opened Prinz Moritz, read
  'Prinz Morit' off the chip and walked past — 21 ships lost that way).
- Whatever the walk did not reach is then looked up by **name**:
  `dock_enter_by_name` types into the dock's search box and walks the result set
  until the name matches. This is the only way in for a record whose stored HP
  matches no card, and the second chance for one the walk passed over. Verified
  including `Illustrious μ` (typed as "Illustrious", μ stripped) and the
  two-word `Hammann II` (spaces sent as `%s` to `input text`).
- **The search field must be emptied before typing** (caret-to-end + 40
  backspaces). The game keeps the previous query and closing the box does not
  clear it. While every lookup succeeded this went unnoticed — the caller left
  through the ship's page, so the next `ui_ensure(page_dock)` came via
  page_main, reloading the dock and wiping the field as a side effect. Live, the
  first lookup that *failed* ended on the dock instead and left "S ur" behind:
  all 53 searches after it typed onto the end of it and matched nothing,
  Tirpitz and Kiev included.
- Output: `config/ship_census_repair.json` — per ship, what was missing before
  and after, plus **why** it is still missing, and one frame per still-incomplete
  ship under `screenshots/ship_census_repair/`.
- The affinity "why" is the useful part: for each gap it separates *no card with
  this HP in the grid pass* (coverage), *card found but the value would not OCR*,
  and *values existed but the ships sharing this HP took them* (the join's weak
  spot — HP is not unique).

## Architecture

Two passes per sweep, **joined by HP** (`grid_affinity_index`: HP → affinities
in dock order, consumed as ships are visited). A positional join drifts the
moment either pass mis-steps by one card and every ship after it silently loses
affinity — live, 155 of 161 joins were rejected, each exactly one card late:

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
- **Card rows are never at a fixed y.** The dock does not stop on a row
  boundary, so offsets of 20-50 px are normal and fixed geometry reads the FP or
  TRP line as "HP" and affinity off the bottom of the card (live: 45 of 105
  cards lost). Find the rows instead: in each column strip, the **"Affinity"
  label is the landmark** — the only band 48-68 px wide with an extent of 62-80
  (HP 21/24, FP 18/20, TRP 30/35, AVI 26/28, RLD 29/34; frames, names and star
  bars span the full 85). HP is the narrow band 127 px above it. A card cut off
  by a screen edge simply fails to pair and is skipped.
- Values are state-tinted (blue/white/pale-green) → `BrightDigit` (inverted-luma
  OCR), never color-extract. Read each value beside *its own* label row.
- Card presence = an HP/Affinity label pair. Lv.-badge white-pixel counting
  reads ZERO on plain (non-oath) card frames — don't use it.
- **Never page the dock with `DOCK_SCROLL`.** ALAS measures the thumb 46-76 px
  long for one and the same list, so `3 * total / length` (rows in the list) is
  off by up to 3×; the smallest drag the scrollbar registers is worth ~6 card
  rows on a full dock (a "2 row" step really moved 7, and 60% of the dock went
  unread); `at_bottom()` goes true on a single bad reading and ended a sweep 27%
  in. Drag the **cards** instead: `device.drag()` holds at the end of the stroke,
  kills the fling a plain swipe gets, and lands one row per 227 px. Step
  `rows_read - 1` rows so screens always share a row, dedupe on that overlap
  (flat HP sequence — rows can be a card short mid-row), and end the sweep when
  the screen stops changing. ~550 Elite+ cards ≈ 75 screens ≈ 4 min.
- `set_top` before reading: the game **remembers dock scroll across visits**.
- `device.click_record_clear()` after every drag and resume-skip swipe —
  12 consecutive same-button actions raise GameTooManyClickError.
- Screens are read only once `wait_until_stable` says the dock has settled, and
  re-read (up to 3×) while any card's values come back unreadable.
- **A dock drag can open a ship.** When the list cannot move (bottom of the dock,
  or a frame where it will not follow), the stroke registers as a tap on the card
  underneath. A screen with no readable card therefore means "check whether we
  are still on the dock", not "scroll further": the sweep backs out of the ship
  page (`appear_then_back_from_ship`), restores the overlay and carries on, and
  gives up after `GRID_BLANK_LIMIT` blank screens. Live, without this the sweep
  dragged on a ship's page for minutes and logged nothing but `Drag ...` lines.

**Detail page**
- The detail page lands on the Info view. Ship-to-ship swipes survive sidebar
  tab visits and Archive detours, but the stock `SWIPE_AREA` overlaps the
  secretary dialogue bubble which eats drags → `SWIPE_BOXES` (swapped into the
  equipment module globals per call) holds four boxes/stroke lengths and each
  retry uses the next one, since whatever swallows a stroke covers a fixed part
  of the page and clears on its own after a while.
- **A blocked swipe is not the end of the dock.** When every box fails, the
  sweep goes back to the dock and taps the next card (`dock_enter_at`) instead
  of concluding it is done — live, a sweep died 161 ships in with 71 cards to
  go, and the same ship swiped fine minutes later. The same path resumes an
  interrupted sweep (tapping card N beats swiping past N ships: 15 min on a
  550-ship dock).
- **…but the grid's card count cannot decide when it *is* the end.** The grid
  over-counts: it re-reads a row whenever two screens fail to share one, and
  reported 551 cards on one run and 538 an hour later for a dock of ~499. At
  the real end of the dock the swipe stops working while `index < cards` is
  still true, so the sweep re-entered and re-walked the last ~20 cards four
  times over, filing each of them again under the next free copy number — **46
  phantom `Name#2/#3/#4` records**, all permanently incomplete, all impossible
  for the repair scan to satisfy (no second card to open), and a quarter of its
  target list. `sweep_at_dock_end` decides it without the count: the grid's
  *last* entry is the last dock card whatever the total is off by, and a second
  stall on the same ship means the walk is going round the tail. Finishing on
  that last card now counts as full coverage even when `index < cards`.
- Tapping a specific card: scroll with the Stats overlay on so cards can be
  identified by HP, aim a row short (the target lands mid-screen, and a card at
  the screen edge is half cut and unfindable), then anchor on the **HP of the
  ship just read** and tap the card after it. **HP is not unique** — duplicate
  copies share one, and so do different un-levelled ships (Le Triomphant and
  Le Malin both sit at 326) — so rank matches by how close they are to where
  the scroll was aimed, and confirm afterwards with `resync_index` (which
  re-derives the dock index from the HP that actually opened). Grid-pass
  positions drift from true dock positions, so they are a hint, never the key.
- Star row (240,50)-(430,95): gold templates = current LB, dark = remaining;
  total clamps to {5,6}. Works for META activation stars too.
- **Never trust the dark-star count for the row length.** Dark stars are hollow
  outlines with nothing behind them, so ship art swallows them: Gromky's third
  disappears into her white hat, Hunter META's three into black rubble, and all
  53 un-limit-broken SR ships in the store read a 5-slot row where the game
  draws 6 — a *silent wrong value*, worse than the 27 gaps the same fault left.
  Gold sits on bright chrome and counts reliably. A dark star is only ever lost,
  never invented, so `star_row_to_limit_break` takes the row length as the
  largest of what the frame shows, what this ship read on an earlier pass
  (`learn_star_totals`, rebuilt from the store each run) and what its rarity
  implies (`star_total_floor`: Elite 5, SR/UR 6). Two exceptions in that floor:
  a **Bulin's** row is 5 gold and no dark at all despite the name data calling
  it super rare, and a **META** follows its own progression, so a META of a
  Normal hull (Königsberg META) still shows 5 slots.
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
- **Bulins** (`NO_ENHANCE_HINT`) are the third no-Enhance family: their sidebar
  is Gear + Info only, no Enhance and no LimitBreak. They carry `no_enhance` and
  `missing_fields()` stops reporting their null enhance state — live, 7
  Prototype Bulin MKII records sat in the repair list for good, each costing a
  tab search that could never succeed.
- Sidebar tab search relaxes its threshold towards `TAB_SIM_FLOOR` over
  successive attempts, on a longer timeout. Dark art held both the plain and
  luma match under `TAB_SIM` for the whole 9 s on Otto von Alvensleben (black
  outfit) and Albacore μ (starfield), which is how their enhance state stayed
  null. Only the *search* relaxes — arrival is still the opaque Fill button, so
  a loose match costs a click, not a wrong reading; and Live2D drift means a
  later frame often matches where the first did not.
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
  against every capture, positives and negatives. 145/145 at handoff; 33/33 on
  the repair fixes (star rows, match key, missing-field rules, phantom prune).
- The frames under `screenshots/ship_census_repair/` are the other capture set —
  one per ship the repair scan could not finish, which is what made all of this
  diagnosable offline. Re-running a reader across both directories and diffing
  old verdict vs new is the cheapest way to check a reader change does not lose
  reads it used to get (the star-row change: 39 verdicts improved, 0 lost).
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
- The dock re-entry lands on the intended card most of the time but not always:
  when the anchor ship's HP is shared by a neighbour (a duplicate copy, or an
  un-levelled ship that collides) it can land on the twin or a card or two off.
  The sweep detects landing on the same ship and swipes once more, so it always
  moves forward, but a ship either side of the stall can be re-read or skipped.
  OCR'ing the card's name band would settle it.
- **Why 110 of the first repair run's 186 targets were never re-read**
  (2026-07-26, all four causes now fixed — see the sections above):
  46 were phantom records the sweep had invented at the end of the dock;
  53 real ships were lost to the poisoned dock search field, in one unbroken
  run of failures from the first unmatchable name onwards;
  21 the walk passed but did not recognise, because the match key insisted on
  the record's own (garbled) stored name.
  Of the 76 it *did* re-read, 27 kept null limit breaks (lost dark stars),
  7 were Bulins whose Enhance tab does not exist, and 1 (Albacore μ) lost its
  sidebar tab search to dark art.
- Affinity gaps whose stored HP matches no grid card are a knock-on of the
  above: the ship is never opened, so its misread HP is never corrected and the
  join has nothing to hook onto. They should follow the ships being reachable
  again; re-check after the next repair run.
- The grid pass itself still over-counts (551 vs 538 for the same dock). Nothing
  depends on the total any more, but `overlap_length` failing is also why some
  cards never make it into the affinity index.
- Grid pass speed: screens that straddle a row boundary read 2 rows instead of
  3, so the sweep steps 1 row instead of 2. Nudging the dock into alignment once
  (the anchor y says by how much) would cut the grid pass roughly in half.
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
  fixed-level skills, store `READER_VERSION`
- `a83e1341f` short sweeps no longer flag ships `missing`; grid-page re-read
- `ce288dcd7` affinity join keyed on HP; grid rows found by their labels; dock
  paged by card drags instead of the scrollbar
- `78083e4ef` blocked swipes no longer read as end-of-dock: rotating swipe
  boxes, dock re-entry (`dock_enter_at`), sweeps resume by tapping card N rewrite
- `4e72e39ac` repair scan mode
- `45da79db1` a dock drag that opened a ship no longer wrecks the grid sweep
- _(this change)_ the four reasons repair mode left 110 of 186 targets
  unvisited: phantom records at the end of the dock (`sweep_at_dock_end`,
  `prune_phantom_copies`), the un-cleared dock search field, the name-only match
  key, plus star rows that no longer need their dark stars and Bulins classified
  as having no Enhance tab
