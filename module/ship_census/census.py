"""
ShipCensus - Tools-section task that walks the dock and records every ship's
progression state (level, affinity, enhance, limit break stars, skill levels)
into config/ship_census.json, then regenerates the standalone dashboard at
config/ship_census_dashboard.html.

Reader design (capture session 2026-07-25, screenshots/ship_census_capture/):
- The detail page lands on the Info view, which shows name, level, the star
  row (gold = current, dark = remaining -> limit break state, METAs included)
  and all three skill cards ("LEVEL: N" / padlock "Locked" / gray "?") in one
  frame. Only the enhance state needs a sidebar tab visit, and a ship once
  enhance-maxed stays maxed, so delta runs skip that visit.
- Exact affinity (0-200) is NOT on the detail page; the dock's Stats overlay
  (cycles OFF -> stats+Affinity -> armor -> skills -> OFF) prints it on every
  card. A grid pass reads (level, affinity) per card in dock order before the
  detail pass, and records join by position with a level sanity check.
- ship_view_next swipes survive sidebar tab visits and Archive detours
  (verified live), so one swipe sweep covers the whole filtered dock.
- Sidebar tabs shift down one slot on retrofit-capable ships and the top slot
  reads "Research" on METAs - tabs are template-searched, never fixed-position.
"""
import difflib
import json
import os
import re
from datetime import datetime

import cv2
import numpy as np
from PIL import Image

from module.base.button import Button
from module.base.timer import Timer
from module.base.utils import crop, rgb2luma
from module.logger import logger
from module.ocr.ocr import Digit, Ocr
from module.retire.assets import DOCK_EMPTY, DOCK_FIRST_NPC, SHIP_DETAIL_CHECK
from module.retire.dock import CARD_GRIDS, DOCK_SCROLL, Dock
from module.ship_census.assets import *
from module.ship_census.dashboard import generate_dashboard
from module.ship_census.store import CensusStore, missing_fields as store_missing_fields
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock


def _btn(area, name):
    return Button(area=area, color=(), button=area, name=name)


class BrightDigit(Digit):
    """
    Digit OCR for bright text of ANY hue on a dark backdrop. The dock
    overlay tints values by state (blue below 100 affinity, white, pale
    green at caps), so color-similarity extraction fails; plain inverted
    luma feeds the model letters-dark-on-light regardless of hue.
    """

    def pre_process(self, image):
        return cv2.subtract(255, rgb2luma(image))


# ---------------- detail page (Info view) ----------------

# Ship level right of the "Level:" label - same area ExpFeed reads in production
OCR_DETAIL_LEVEL = Digit(_btn((758, 283, 798, 319), 'DETAIL_LEVEL'),
                         letter=(255, 255, 255), threshold=128, name='OCR_CENSUS_LEVEL')
# HP value on the Info view's stats panel - the join key against the grid
# pass (white digits; the green "+N" gear/enhance bonus fails white extraction)
OCR_DETAIL_HP = Digit(_btn((748, 328, 868, 364), 'DETAIL_HP'),
                      letter=(255, 255, 255), threshold=128, name='OCR_CENSUS_HP')
# Ship name chip; x>=210 excludes the hull-type badge (DD/CVL/...). The
# edit-pencil icon trails the name at a varying x and OCRs as junk that
# _clean_name strips. cnocr model: azur_lane leetspeaks stylized names.
OCR_NAME = Ocr(_btn((210, 88, 448, 116), 'SHIP_NAME'), lang='cnocr',
               letter=(255, 255, 255), threshold=128, name='OCR_CENSUS_NAME')
# Star row above the name chip, centered: gold stars = current, dark = missing
STAR_AREA = (240, 50, 430, 95)
# Affinity tier badge ("Oath" dove and friends) floats right of the name chip
TIER_AREA = (535, 98, 695, 180)
# Sidebar top slot region: "Research" here = META ship (they have no
# Enhance/LimitBreak); retrofit ships push Enhance down one slot
SIDEBAR_TOP_AREA = (0, 125, 110, 235)
SIDEBAR_AREA = (0, 125, 110, 600)
# Skill cards: three slots; the "LEVEL: N" box sits bottom-right of each.
# Text is dark slate (or gold at max) on a light box - gray extraction at
# threshold 160 read every unlocked card in calibration.
SKILL_LEVEL_OCRS = [
    Ocr(_btn((768 + 188 * i, 570, 868 + 188 * i, 612), 'SKILL_%s' % (i + 1)),
        lang='azur_lane', letter=(90, 90, 90), threshold=160,
        name='OCR_SKILL_%s' % (i + 1))
    for i in range(3)
]
SKILL_SLOT_AREAS = [(683 + 188 * i, 520, 871 + 188 * i, 620) for i in range(3)]
# Skill name band at the card top (long names render as a scrolling marquee,
# so any frame shows some 12-14 char window of the name)
SKILL_NAME_AREAS = [(765 + 188 * i, 530, 869 + 188 * i, 562) for i in range(3)]
# Skills that can never be leveled: their card reads "LEVEL: 1" forever, and
# nothing on it distinguishes them from a levelable skill sitting at Lv.1, so
# they are identified by name. Two families are known:
#   - All Out Assault barrages (most vanguards, "All Out Assault I/II")
#   - Siren Killer I/II/III, on every PR/DR research ship (Monarch, Ibuki,
#     Plymouth...); it upgrades with the ship's development level, not with
#     skill books, so a maxed ship still shows Lv.1
# Matching is on fragments of the squashed (lowercase alnum) OCR read because
# the model garbles the stylized band; each entry is a set of fragments that
# must all appear. Observed reads: 'outnsault', 'outnsalt', 'butssaut n',
# 'lsirenkiller', 'lsrenkiller', 'lirenkiller', 'sirenkiler'.
FIXED_SKILL_PATTERNS = (
    ('assa',),
    ('sault',),
    ('ssaut',),
    ('out', 'salt'),
    ('out', 'saul'),
    ('out', 'ssal'),
    ('renkil',),
    ('irenki',),
    ('sirenk',),
)


def match_fixed_skill(text):
    """
    Args:
        text (str): Squashed lowercase alnum OCR of a skill name band.

    Returns:
        bool: True if the name belongs to a skill that cannot be leveled.
    """
    return any(all(frag in text for frag in pattern) for pattern in FIXED_SKILL_PATTERNS)

# ---------------- enhance tab ----------------

# Four stat rows (FP/TRP/AVI/RLD) at a 48px pitch. Each active row's right
# end reads "EXP:MAX" (full) or "EXP:cur/next" in bright green; rows the hull
# cannot enhance (cap MAX:0) render the same text dimmed gray. The tiny MAX:N
# labels themselves are below OCR size.
ENH_ROW_PITCH = 48
# Tight text strip for the activity check...
ENH_EXP_AREAS = [(1178, 133 + ENH_ROW_PITCH * i, 1250, 153 + ENH_ROW_PITCH * i) for i in range(4)]
# ...a taller window per row for the TEMPLATE_ENH_MAX match (46x26 px)...
ENH_MAX_AREAS = [(1166, 122 + ENH_ROW_PITCH * i, 1258, 162 + ENH_ROW_PITCH * i) for i in range(4)]
# ...and the EXP bar between them, which is opaque chrome: solid yellow across
# its full width exactly when that stat is maxed.
ENH_BAR_AREAS = [(906, 137 + ENH_ROW_PITCH * i, 1171, 150 + ENH_ROW_PITCH * i) for i in range(4)]
# Row activity is judged on GREEN DOMINANCE, never on brightness: the panel is
# translucent, so absolute luma tracks the ship art behind it. Live bug: on
# Unicorn (Retrofit) - a CVL, so FP and TRP are capped at MAX:0 - her white
# wings pushed both dimmed rows past any luma threshold, the reader looked for
# "EXP:MAX" in rows that have no EXP at all, and a fully enhanced ship reported
# "open". Measured over bright and dark art alike: enhanceable rows carry
# 300-470 green pixels, capped-at-zero rows exactly 0.
ENH_GREEN_MIN = 150
ENH_GREEN_MARGIN = 40
ENH_ROW_ACTIVE_COUNT = 100
# Bar fill: yellow columns / total. Measured 1.000 on every maxed row and
# 0.000 on every unmaxed one; 0.99 keeps a nearly-full EXP bar (39/40 to the
# next point) from passing as maxed.
ENH_BAR_FULL_RATIO = 0.99
# The blue "Fill" button (opaque chrome) marks the Enhance panel - tab
# templates cannot tell selected from unselected, and color counting false-
# positives on blue ship art
ENH_FILL_SEARCH = (950, 588, 1135, 660)

# ---------------- dock grid pass (Stats overlay) ----------------

# A card's overlay draws six evenly spaced white label rows - HP, FP, TRP,
# AVI, RLD, Affinity - with the values right-aligned beside them. Everything
# anchors on the HP label, whose y is SEARCHED, never assumed: the dock does
# not land on row boundaries after a scroll (offsets of 20-50 px are normal),
# and a fixed row geometry then reads the FP or TRP line as "HP" and the
# affinity value off the bottom of the card. Live, that cost 45 of 105 cards
# their affinity on one pass, and the dashboard showed "-" for those ships.
# The six labels have distinct pixel widths (HP 21, FP 18, TRP 30, AVI 26,
# RLD 29, Affinity 57 with an extent of ~71). "Affinity" is the only wide-but-
# not-full-width band on a card, which makes it the landmark; HP then sits
# exactly 127 px above it. Card frames, names and star bars span the full
# strip (extent 85) and are excluded by the extent window. EN client only.
CARD_AFF_LABEL_WIDTH = (48, 68)
CARD_AFF_LABEL_EXTENT = (62, 80)
CARD_HP_LABEL_MAX_WIDTH = 34
CARD_AFF_OFFSET = 127                      # HP label center -> Affinity label center
CARD_AFF_OFFSET_TOLERANCE = 8
CARD_LABEL_REL = (2, 88)                   # label column, cell-relative x
CARD_VALUE_X_REL = (84, 140)               # value column, right-aligned
CARD_GRID_TOP = 60                         # y band the dock draws cards in
CARD_GRID_BOTTOM = 720
CARD_ROW_SEPARATION = 60                   # min y gap between two card rows
CARD_ROW_PITCH = 227                       # px between card rows
# Where to tap a card, relative to (column x, HP label y): the middle of the
# card, clear of the lock icon and the name band
CARD_TAP_REL = (75, 60)
CARD_LABEL_LUMA = 200
CARD_LABEL_WIDTH = 8                       # min bright pixels in a label row
# The Stats cycle button in the dock top bar: amber when an overlay page is
# active (mean ~(167,122,69)), blue when off (~(66,80,119))
# Dock search box: the magnifier toggles it, the field takes adb-typed text
DOCK_SEARCH_BUTTON = _btn((648, 6, 692, 48), 'DOCK_SEARCH')
DOCK_SEARCH_FIELD = _btn((710, 12, 970, 44), 'DOCK_SEARCH_FIELD')
DOCK_SEARCH_FIRST_CARD = _btn((100, 90, 225, 265), 'DOCK_SEARCH_FIRST_CARD')
DOCK_SEARCH_STATE_AREA = (648, 6, 692, 48)   # amber while open, blue when closed
STATS_BUTTON = _btn((905, 10, 970, 44), 'STATS_BUTTON')
STATS_STATE_AREA = (860, 8, 978, 45)
# Card area watched by wait_until_stable before reading a page - the dock
# keeps sliding for a moment after the scroll drag lets go
GRID_STABLE_AREA = _btn((93, 76, 1220, 700), 'GRID_STABLE_AREA')

RARITY_SCOPE = {
    'elite_and_above': ['elite', 'super_rare', 'ultra_rare'],
    'rare_and_above': ['rare', 'elite', 'super_rare', 'ultra_rare'],
    'all': 'all',
}

# Canonical EN ship names + rarities (from AzurLaneData ship_data_statistics).
# OCR of the stylized name chip drifts between runs (the Live2D art behind it
# moves), so raw reads are fuzzy-matched to canonical names to keep store
# keys stable. Unmatched names (data lag, e.g. newest ships) stay raw.
SHIP_NAMES_FILE = os.path.join(os.path.dirname(__file__), 'ship_names_en.json')
# Repair scan output: a JSON report plus one frame per ship that still has a
# gap after being re-read, so the failure can be diagnosed offline
REPAIR_REPORT_FILE = './config/ship_census_repair.json'
REPAIR_FRAME_DIR = './screenshots/ship_census_repair'
# Rough seconds per card swiped vs per row dragged, used to decide between
# swiping to the next target and jumping through the dock to it
REPAIR_SWIPE_COST = 2.5
REPAIR_DRAG_COST = 1.2
REPAIR_JUMP_OVERHEAD = 8.0
# How far to swipe ahead when a jump lands next to the target instead of on it
REPAIR_LOCAL_SEARCH = 3


def _load_ship_names():
    try:
        with open(SHIP_NAMES_FILE, encoding='utf-8') as f:
            base = json.load(f)
    except (OSError, ValueError):
        logger.warning('ship_names_en.json missing or unreadable, '
                       'ship names will not be canonicalized')
        return {}
    out = {}
    for name, info in base.items():
        out[name] = info
        # Synthetic display-name variants the game shows but the data lacks
        out.setdefault(name + ' META', {'rarity': info.get('rarity'), 'research': False})
        out.setdefault(name + ' (Retrofit)', dict(info))
    return out


SHIP_NAMES = _load_ship_names()
_NAME_SQUASH = {re.sub(r'[^a-z0-9]', '', n.lower()): n for n in SHIP_NAMES}
SWEEP_SAFETY_LIMIT = 1500
GRID_PAGE_LIMIT = 200
# Grid sweep stepping: the dock is dragged by whole card rows (see
# grid_drag_rows). Two rows is the largest stroke that stays inside the card
# area at both ends.
GRID_DRAG_X = 640
GRID_DRAG_Y = 620
GRID_DRAG_ROWS_MAX = 2
GRID_REWIND_LIMIT = 3
GRID_STUCK_LIMIT = 2
# Screens in a row that may come back without a single readable card before the
# sweep gives up (see grid_sweep - a drag can open a ship by accident)
GRID_BLANK_LIMIT = 3
# Ship-to-ship swipe boxes, (area, stroke length), tried in order. The stock
# equipment SWIPE_AREA reaches y=527, where the secretary dialogue bubble
# swallows drags (live: a sweep died at ship 2 when both random swipe points
# landed on it). Whatever eats a stroke - the bubble, a touch animation, a
# Live2D skin - covers a fixed part of the page and clears after a while, so
# every retry moves the stroke somewhere else and lengthens it.
SWIPE_BOXES = [
    ((225, 180, 570, 430), 250),
    ((225, 150, 570, 300), 400),
    ((140, 180, 420, 430), 250),
    ((150, 150, 660, 260), 400),
]
# Star totals are rarity base + 3; Elite and above only produce 5 or 6 slot
# rows, so anything else is a misread (dark stars can vanish into dark art)
STAR_TOTAL_VALID = (5, 6)
STAR_SIM = 0.75
TAB_SIM = 0.70
# Calibrated on captures: padlock true>=0.99 / false<=0.49
SKILL_LOCK_SIM = 0.75


class ShipCensus(Dock):
    def run(self):
        mode = self.config.ShipCensus_ScanMode
        store = CensusStore().load()
        logger.hr('Ship census', level=1)
        logger.info('Mode: {}, ships on record: {}'.format(mode, len(store.ships)))

        if mode == 'repair':
            self.census_repair(store)
        elif mode != 'dashboard_only':
            self.census_sweep(store, full=(mode == 'full'))

        path = generate_dashboard(store)
        logger.info('Dashboard written to {}'.format(path))

    # ---------------- repair ----------------

    def census_repair(self, store):
        """
        Re-read only the ships whose record has gaps (the dashboard's "-"), and
        write down what happened to each one.

        Targets cluster: the dock is sorted by level, and most gaps are on
        low-level ships at the tail (dark limit-break stars vanish into dark
        art, low HP values collide between ships and break the affinity join).
        So the walk enters the dock at the first target and swipes forward to
        the last, reading every ship on the way but only recording targets.

        Every target that still has a gap afterwards gets its Info-view frame
        saved next to the report, which is what makes the failure diagnosable
        offline instead of by guesswork.

        Pages:
            in: Any
            out: page_dock, filters reset
        """
        targets = store.incomplete_ships()
        logger.hr('Census repair', level=2)
        if not targets:
            logger.info('No record has gaps, nothing to repair')
            return
        counts = {}
        for fields in targets.values():
            for field in fields:
                counts[field] = counts.get(field, 0) + 1
        logger.info('{} records with gaps: {}'.format(len(targets), counts))

        self.ui_ensure(page_dock)
        self.dock_search_close()
        self.dock_favourite_set(enable=False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(rarity=RARITY_SCOPE[self.config.ShipCensus_RarityScope])
        grid = self.grid_pass()
        affinity_index = self.grid_affinity_index(grid)
        # How many affinity values this grid pass has per HP, before the walk
        # starts consuming them - the difference between "the card was never
        # read", "it was read but the value would not OCR" and "another ship
        # sharing this HP took the value"
        supply = {hp: len(values) for hp, values in affinity_index.items()}
        logger.info('Grid pass: {} cards, {} with affinity'.format(
            len(grid), sum(supply.values())))

        # Which HP values to stop on, and how far the walk has to go
        wanted = {}
        for key, missing in targets.items():
            hp = store.ships[key].get('hp')
            if hp:
                wanted.setdefault(hp, []).append(key)
        grid_hps = set(hp for hp, _ in grid)
        positions = sorted(i for i, (hp, _) in enumerate(grid) if hp in wanted)
        unreachable = [key for key in targets
                       if not store.ships[key].get('hp')
                       or store.ships[key]['hp'] not in grid_hps]
        if not positions:
            logger.warning('No target ship could be found in the dock by HP')
            self.dock_filter_set(wait_loading=False)
            return
        logger.info('{} targets sit between dock cards {} and {}; {} are not findable by HP '
                    '(their stored HP matches no card)'.format(
                        len(targets) - len(unreachable), positions[0], positions[-1],
                        len(unreachable)))

        report = dict(generated=None, targets=len(targets), grid_cards=len(grid),
                      unreachable=unreachable, ships=[])
        done = set()
        visited = 0
        index = -1
        for position in positions:
            if visited >= SWEEP_SAFETY_LIMIT:
                logger.warning('Repair walk safety limit reached')
                break
            # Targets are scattered over the whole dock, so swipe only across
            # short gaps and jump through the dock for long ones: a jump costs
            # about position/14 drags, a swipe about 2.5 s per card.
            gap = position - index
            if index < 0 or gap <= 0 or gap * REPAIR_SWIPE_COST > \
                    position / 14.0 * REPAIR_DRAG_COST + REPAIR_JUMP_OVERHEAD:
                if not self.dock_enter_at(position, grid):
                    logger.warning('Could not enter the dock at card {}, skipping'.format(position))
                    continue
                index = self.resync_index(position, grid)
            else:
                while index < position:
                    advanced = False
                    for attempt in range(len(SWIPE_BOXES)):
                        if self.ship_view_next_safe(attempt):
                            advanced = True
                            break
                        self.device.sleep((1.5, 2.5))
                        self.device.click_record_clear()
                    self.device.click_record_clear()
                    if not advanced:
                        break
                    index += 1
                if index < position:
                    logger.info('Swiping stalled at card {}, jumping instead'.format(index))
                    if not self.dock_enter_at(position, grid):
                        continue
                    index = self.resync_index(position, grid)

            visited += 1
            self.ensure_info_view()
            detail = self.read_ship_detail()
            key = self.repair_match_key(store, targets, done, detail)
            # A jump lands within a card or two - grid positions and dock
            # positions drift apart - so look a little way forward before
            # writing this target off
            for _ in range(REPAIR_LOCAL_SEARCH):
                if key is not None:
                    break
                logger.info('Card {} is {!r} (HP {}), not a pending target - looking '
                            'ahead'.format(index, detail['name'], detail['hp']))
                if not self.ship_view_next_safe():
                    break
                index += 1
                self.ensure_info_view()
                detail = self.read_ship_detail()
                key = self.repair_match_key(store, targets, done, detail)
            if key is None:
                continue
            report['ships'].append(self.repair_ship(store, key, targets[key], detail,
                                                    affinity_index, grid, supply))
            done.add(key)
            if len(done) >= len(targets) - len(unreachable):
                break

        # Targets whose stored HP matches no card are reachable only by name
        for key in unreachable:
            if visited >= SWEEP_SAFETY_LIMIT:
                logger.warning('Repair walk safety limit reached')
                break
            name = store.ships[key].get('name')
            if not name:
                continue
            visited += 1
            if not self.dock_enter_by_name(name, want=name):
                logger.info('{} could not be opened by name either'.format(key))
                continue
            detail = self.read_ship_detail()
            report['ships'].append(self.repair_ship(store, key, targets[key], detail,
                                                    affinity_index, grid, supply))
            done.add(key)
        if unreachable:
            self.dock_search_close()

        store.save()
        self.detail_exit_to_dock()
        self.dock_filter_set(wait_loading=False)
        self.repair_write_report(report, targets, done, unreachable)

    @staticmethod
    def repair_match_key(store, targets, done, detail):
        """
        Which target record the ship now on screen belongs to - matched on name
        and HP, never on position, since the walk passes non-targets too.

        Returns:
            str: Record key, or None if this ship is not a pending target.
        """
        if not detail['name']:
            return None
        pending = [key for key in targets if key not in done]
        exact = [key for key in pending
                 if store.ships[key].get('name') == detail['name']
                 and store.ships[key].get('hp') == detail['hp']]
        if exact:
            return exact[0]
        by_name = [key for key in pending if store.ships[key].get('name') == detail['name']]
        if len(by_name) == 1:
            logger.info('Matched {} by name (stored HP {}, read {})'.format(
                by_name[0], store.ships[by_name[0]].get('hp'), detail['hp']))
            return by_name[0]
        return None

    def repair_ship(self, store, key, missing_before, detail, affinity_index, grid, supply):
        """
        Re-record one target and note what is still missing and why.

        Args:
            supply (dict): HP -> affinity values the grid pass held before the
                walk began, which is what separates a coverage gap from an OCR
                failure from a value another ship with the same HP took.

        Returns:
            dict: One report entry.
        """
        logger.hr('Repairing {} (was missing {})'.format(key, ', '.join(missing_before)), level=2)
        fields = self.ship_fields(detail, key, affinity_index)
        if detail['is_meta'] or detail['is_research']:
            fields['enhance_maxed'] = None
        else:
            fields['enhance_maxed'] = self.read_enhance_tab()
        store.record(key, deep=True, **fields)
        store.save()

        hp = detail['hp']
        cards = sum(1 for card_hp, _ in grid if card_hp == hp)
        entry = dict(
            key=key, name=detail['name'], hp=hp, level=detail['level'],
            missing_before=missing_before,
            missing_after=store_missing_fields(store.ships[key]),
            grid_cards_with_this_hp=cards,
            grid_affinities_for_this_hp=supply.get(hp, 0),
            ships_sharing_this_hp=sum(1 for ship in store.ships.values()
                                      if ship.get('hp') == hp),
            reasons={},
            frame=None,
        )
        if 'affinity' in entry['missing_after']:
            if not hp:
                entry['reasons']['affinity'] = 'HP unreadable, nothing to join on'
            elif not cards:
                entry['reasons']['affinity'] = 'no card with this HP in the grid pass ' \
                                               '(the dock sweep never covered it)'
            elif not supply.get(hp):
                entry['reasons']['affinity'] = 'card found but its affinity value would not OCR'
            else:
                entry['reasons']['affinity'] = 'affinity values existed for this HP but were ' \
                                               'taken by the {} ships that share it'.format(
                                                   entry['ships_sharing_this_hp'])
        for field in entry['missing_after']:
            entry['reasons'].setdefault(field, 'the detail page read it as unavailable again')

        if entry['missing_after']:
            entry['frame'] = self.repair_save_frame(key)
            logger.warning('{} still missing {}: {} (frame: {})'.format(
                key, ', '.join(entry['missing_after']),
                '; '.join('{}: {}'.format(k, v) for k, v in entry['reasons'].items()),
                entry['frame']))
        else:
            logger.info('{} is complete now'.format(key))
        return entry

    def repair_save_frame(self, key):
        """Save the current frame under screenshots/ship_census_repair/."""
        self.ensure_info_view()
        os.makedirs(REPAIR_FRAME_DIR, exist_ok=True)
        name = re.sub(r'[^A-Za-z0-9#_.-]+', '_', key)
        path = os.path.join(REPAIR_FRAME_DIR, '{}.png'.format(name))
        try:
            Image.fromarray(self.device.image).save(path)
        except (OSError, ValueError) as e:
            logger.warning('Could not save repair frame for {}: {}'.format(key, e))
            return None
        return path

    @staticmethod
    def repair_write_report(report, targets, done, unreachable):
        """Write the repair report and log a summary of what is still missing."""
        report['generated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        report['visited'] = len(done)
        still = {}
        for entry in report['ships']:
            for field in entry['missing_after']:
                still[field] = still.get(field, 0) + 1
        report['still_missing'] = still
        report['not_visited'] = [key for key in targets if key not in done]
        try:
            os.makedirs(os.path.dirname(REPAIR_REPORT_FILE), exist_ok=True)
            with open(REPAIR_REPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
        except OSError as e:
            logger.warning('Could not write the repair report: {}'.format(e))
        logger.hr('Census repair result', level=2)
        logger.info('Re-read {} of {} target ships'.format(len(done), len(targets)))
        logger.info('Still missing after the re-read: {}'.format(still or 'nothing'))
        if unreachable:
            logger.info('{} targets could not be found in the dock by HP (their stored HP is '
                        'null or no card matches it): {}'.format(
                            len(unreachable), ', '.join(unreachable[:10])))
        logger.info('Report written to {}'.format(REPAIR_REPORT_FILE))

    # ---------------- sweep ----------------

    def census_sweep(self, store, full=False):
        """
        Grid pass (levels + affinity in dock order), then a detail swipe pass
        joined by position. Resumable: the cursor is saved after every ship.

        Pages:
            in: Any
            out: page_dock, filters reset
        """
        scope = self.config.ShipCensus_RarityScope
        stale_days = int(self.config.ShipCensus_StaleDays)
        skip = store.sweep_begin('full' if full else 'delta', scope)
        store.save()
        if skip:
            logger.info('Resuming sweep, skipping past {} processed ships'.format(skip))

        logger.hr('Census sweep', level=2)
        self.ui_ensure(page_dock)
        self.dock_search_close()
        self.dock_favourite_set(enable=False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(rarity=RARITY_SCOPE[scope])
        self.device.screenshot()

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('Dock empty under census filter, nothing to scan')
            store.sweep_end(complete=True)
            store.save()
            self.dock_filter_set(wait_loading=False)
            return

        grid = self.grid_pass()
        cards = len(grid)
        affinity_index = self.grid_affinity_index(grid)
        logger.info('Grid pass: {} cards, {} with affinity'.format(
            cards, sum(len(v) for v in affinity_index.values())))

        # NPC rentals occupy card 1 during events; dock_enter_first skips them
        self.device.screenshot()
        npc_offset = 1 if self.appear(DOCK_FIRST_NPC, offset=(20, 20)) else 0
        entered = False
        if skip:
            # Resuming: tap straight into card `skip` instead of swiping past
            # every ship already done (on a 550-ship dock that alone is 15 min)
            if self.dock_enter_at(skip + npc_offset, grid):
                skip = self.resync_index(skip + npc_offset, grid) - npc_offset
                entered = True
            else:
                logger.warning('Could not resume at card {}, restarting the sweep from '
                               'the top'.format(skip + npc_offset))
                skip = 0
        if not entered and not self.dock_enter_first():
            logger.info('No enterable ship in dock')
            store.sweep_end(complete=True)
            store.save()
            self.dock_filter_set(wait_loading=False)
            return

        visited = 0
        complete = False
        index = skip + npc_offset
        while 1:
            visited += 1
            last_hp = self.process_ship(store, affinity_index, stale_days=stale_days,
                                        full=full)
            store.save()
            self.device.click_record_clear()
            index += 1
            if visited >= SWEEP_SAFETY_LIMIT:
                logger.warning('Census sweep safety limit reached')
                break
            # A failed swipe can mean end-of-dock OR a swallowed drag: the
            # secretary's dialogue bubble and her touch animations cover part
            # of the page for a while and eat every stroke that lands on them,
            # so each retry uses a different box (SWIPE_BOXES). The grid pass
            # knows how many cards exist, so a premature "end" gets retried.
            advanced = False
            for attempt in range(len(SWIPE_BOXES)):
                if self.ship_view_next_safe(attempt):
                    advanced = True
                    break
                if index >= cards:
                    break
                logger.info('Swipe did not advance (attempt {}), grid expects {} more '
                            'cards'.format(attempt + 1, cards - index))
                self.device.sleep((1.5, 2.5))
                self.device.click_record_clear()
            # Swiping can stay blocked for as long as an animation runs (live:
            # a sweep died 161 ships in with 71 cards still to go, and the same
            # ship swiped fine minutes later). Going back to the dock and
            # tapping the next card is not subject to any of that.
            if not advanced and index < cards:
                logger.info('Swipe sweep stalled at card {}, re-entering from the '
                            'dock'.format(index))
                advanced = self.dock_enter_at(index, grid, after_hp=last_hp)
                if advanced:
                    index = self.resync_index(index, grid)
                    # Landing back on the ship we just read would record it a
                    # second time under a phantom "#2" copy key
                    if last_hp and OCR_DETAIL_HP.ocr(self.device.image) == last_hp:
                        logger.info('Re-entered on the same ship, swiping once more')
                        if not self.ship_view_next_safe():
                            advanced = False
            if not advanced:
                logger.info('Census sweep reached the end of the dock')
                complete = True
                break

        # "Complete" must mean the whole dock was walked, because sweep_end
        # flags every unseen in-scope ship as missing ("gone?" on the
        # dashboard). A sweep that stopped early - a swallowed swipe read as
        # end-of-dock, or a grid pass that itself truncated - would otherwise
        # brand every ship it never reached as retired (live: a run that
        # visited 49 of ~190 ships flagged 96 records).
        covered = complete and bool(cards) and index >= cards
        if complete and not covered:
            logger.warning('Grid pass saw {} cards but the detail pass ended at index {} - '
                           'sweep recorded as incomplete, missing flags untouched'.format(
                               cards, index))
        store.sweep_end(complete=covered)
        store.save()
        self.detail_exit_to_dock()
        self.dock_filter_set(wait_loading=False)
        logger.info('Census sweep processed {} ships this run'.format(visited))

    @staticmethod
    def grid_affinity_index(grid):
        """
        Build {HP: [affinity, ...]} in dock order from the grid pass.

        The detail pass consumes this by HP rather than by position. Positional
        joins survive only while both passes agree on every card: one row read
        twice by the grid pass (a frame caught mid-scroll, an overlap dedupe
        that missed) shifts everything after it and every later ship silently
        loses its affinity - live, 155 of 161 joins were rejected, each one
        exactly one card late. HP is near-unique per ship, and duplicate copies
        are consumed in dock order, so a stale extra entry costs at most the
        one ship that shares its HP.

        Args:
            grid (list[(int, int)]): (hp, affinity) per card, dock order.

        Returns:
            dict[int, list[int]]:
        """
        index = {}
        for hp, affinity in grid:
            if hp and affinity is not None and 0 <= affinity <= 200:
                index.setdefault(hp, []).append(affinity)
        return index

    @staticmethod
    def ship_fields(detail, key, affinity_index):
        """
        Turn one detail-page read into record fields, taking the affinity from
        the grid pass by HP (see grid_affinity_index; the matched entry is
        consumed so a second copy of the same hull gets the next one).

        Returns:
            dict: Fields for CensusStore.record - only what was actually read,
                so a failed affinity join leaves the stored value alone.
        """
        affinity = None
        pending = affinity_index.get(detail['hp']) if detail['hp'] else None
        if pending:
            affinity = pending.pop(0)
        else:
            logger.info('No grid affinity for HP {}, affinity not updated'.format(detail['hp']))

        fields = dict(
            name=detail['name'],
            copy=int(key.rsplit('#', 1)[1]),
            level=detail['level'],
            hp=detail['hp'],
            is_meta=detail['is_meta'],
            is_research=detail['is_research'],
            lb_current=detail['lb_current'],
            lb_max=detail['lb_max'],
            skills=detail['skills'],
            oathed=detail['oath_badge'] or (affinity is not None and affinity > 100),
        )
        if detail['rarity'] is not None:
            fields['rarity'] = detail['rarity']
        if affinity is not None:
            fields['affinity'] = affinity
        return fields

    def process_ship(self, store, affinity_index, stale_days=7, full=False):
        """
        Read the ship currently open on the detail page and upsert its record.

        Args:
            store (CensusStore):
            affinity_index (dict): From grid_affinity_index, consumed in place.

        Returns:
            int: HP of the ship just read - what identifies this card in the
                dock if the sweep has to re-enter (None if unreadable).

        Pages:
            in: SHIP_DETAIL_CHECK (Info view)
            out: SHIP_DETAIL_CHECK (Info view)
        """
        self.ensure_info_view()
        detail = self.read_ship_detail()
        name, level = detail['name'], detail['level']
        if not name:
            logger.warning('Ship name unreadable, ship skipped')
            store.sweep_advance(None)
            return detail['hp']
        key = store.sweep_key(name)
        logger.hr('Ship {} (Lv.{})'.format(key, level), level=2)

        fields = self.ship_fields(detail, key, affinity_index)

        if not full and not store.needs_deep_scan(key, level, stale_days):
            logger.info('Record fresh, enhance tab skipped')
            store.record(key, **fields)
            store.sweep_advance(key)
            return detail['hp']

        # Enhance state needs a tab visit; once maxed it stays maxed. Lab
        # ships (META / PR research) have no Enhance tab at all - their
        # upgrade systems live in the META Lab / Shipyard.
        if detail['is_meta'] or detail['is_research']:
            fields['enhance_maxed'] = None
        else:
            prior = store.ships.get(key)
            if not full and prior and prior.get('enhance_maxed') is True:
                fields['enhance_maxed'] = True
            else:
                fields['enhance_maxed'] = self.read_enhance_tab()

        store.record(key, deep=True, **fields)
        store.sweep_advance(key)
        return detail['hp']

    # ---------------- detail page readers ----------------

    def read_ship_detail(self):
        """
        Read everything the Info view offers from (mostly) one frame.

        Returns:
            dict: name, level, is_meta, lb_current, lb_max, skills, oath_badge
        """
        level = self.read_level()  # settles info bars, leaves a fresh frame
        image = self.device.image

        name, rarity, dict_research = self.canonical_name(self._clean_name(OCR_NAME.ocr(image)))
        hp = OCR_DETAIL_HP.ocr(image)

        # Lab-type ships (META / PR research) have a "Research" top tab and
        # no Enhance/LimitBreak. Primary classification is by name (the PR
        # roster is known; METAs carry the suffix) because bright ship art
        # can sink even luma template matching on the translucent sidebar
        # (live: Plymouth's wedding wings). The template is the fallback for
        # ships the name data does not know yet.
        is_meta = bool(name and name.endswith('META'))
        is_research = dict_research
        if not is_meta and not is_research:
            sidebar_top = crop(image, SIDEBAR_TOP_AREA)
            if TEMPLATE_TAB_RESEARCH.match(sidebar_top, similarity=TAB_SIM) \
                    or TEMPLATE_TAB_RESEARCH.match_luma(sidebar_top, similarity=TAB_SIM):
                is_research = True

        star_img = crop(image, STAR_AREA)
        gold = len(TEMPLATE_STAR_GOLD.match_multi(star_img, similarity=STAR_SIM, name='STAR_GOLD'))
        dark = len(TEMPLATE_STAR_EMPTY.match_multi(star_img, similarity=STAR_SIM, name='STAR_EMPTY'))
        total = gold + dark
        if gold >= 1 and total in STAR_TOTAL_VALID:
            lb_current, lb_max = gold, total
        else:
            logger.info('Star row implausible (gold={}, dark={}), limit break unknown'.format(gold, dark))
            lb_current = lb_max = None

        oath_badge = TEMPLATE_TIER_OATH.match(crop(image, TIER_AREA), similarity=TAB_SIM)

        skills = self.read_skills(image)

        logger.info('Detail: name={!r}, Lv.{}, HP {}, stars {}/{}, meta={}, research={}, '
                    'oath_badge={}, skills={}'.format(
                        name, level, hp, gold, total, is_meta, is_research, oath_badge,
                        ['L' if s['locked'] else s['level'] for s in skills]))
        return dict(name=name, rarity=rarity, level=level, hp=hp, is_meta=is_meta,
                    is_research=is_research, lb_current=lb_current, lb_max=lb_max,
                    skills=skills, oath_badge=oath_badge)

    @staticmethod
    def _clean_name(name):
        """
        cnocr output cleanup: leading chip-edge junk ('>'), the edit-pencil
        icon OCR'd as a short trailing lowercase token ('f', 'ti', '+g'),
        stray symbols. Keeps roman numerals and digit-bearing tail tokens
        ("Laffey II", "U-2501", "Z23").
        """
        if not name:
            return None
        name = re.sub(r'[|/\\_\[\]{}<>~`^"\'+=*!?]+', ' ', str(name))
        name = re.sub(r'\s+', ' ', name).strip(' .-:;,')
        name = re.sub(r'\)[a-z]{1,2}$', ')', name)  # pencil junk fused to ')'
        parts = name.split(' ')
        while len(parts) > 1:
            tail = parts[-1]
            if len(tail) <= 2 and tail.islower() and tail.isalpha():
                parts.pop()
            else:
                break
        name = ' '.join(parts).strip(' .-:;,')
        return name if len(name) >= 2 else None

    @staticmethod
    def canonical_name(cleaned):
        """
        Fuzzy-match a cleaned OCR name against the canonical EN list.

        Returns:
            (str, str, bool): (canonical or raw name, rarity or None,
                research ship according to the name data)
        """
        if not cleaned:
            return None, None, False
        squash = re.sub(r'[^a-z0-9]', '', cleaned.lower())
        if not squash:
            return cleaned, None, False
        hit = _NAME_SQUASH.get(squash)
        if hit is None:
            close = difflib.get_close_matches(squash, _NAME_SQUASH.keys(), n=1, cutoff=0.8)
            if close:
                hit = _NAME_SQUASH[close[0]]
        if hit is None:
            return cleaned, None, False
        info = SHIP_NAMES.get(hit) or {}
        return hit, info.get('rarity'), bool(info.get('research'))

    def read_level(self, skip_first_screenshot=True):
        """Level from the detail header, waiting out info bars (ExpFeed idiom)."""
        timeout = Timer(5, count=10).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.handle_info_bar():
                continue
            level = OCR_DETAIL_LEVEL.ocr(self.device.image)
            if 1 <= level <= 125:
                return level
            if timeout.reached():
                logger.warning('read_level timeout, OCR failed')
                return None

    def read_skills(self, image):
        """
        Classify the three skill slots. A parseable "LEVEL: N" box wins
        (locked cards read "LEVEL: ??" which never parses); then the padlock
        card (calibration: true 0.99+ vs false <=0.49); then the gray "?"
        card. Nothing -> no skill.

        Semantics (user-reported edge cases):
        - "?" cards mean "no skill in this slot (currently)" - single-skill
          ships like Enterprise show them in unused slots - so they record
          NO skill rather than a pending one. Real padlock cards are skills
          gated by limit break and stay pending.
        - Some skills cannot be leveled at all and sit at Lv.1 forever (All
          Out Assault barrages, the PR/DR Siren Killer); the name band
          identifies them and they record max=1. See FIXED_SKILL_PATTERNS.
        """
        skills = []
        for i in range(3):
            text = str(SKILL_LEVEL_OCRS[i].ocr(image)).replace(' ', '')
            m = re.search(r'(\d+)$', text)
            if m and 'LEV' in text.upper().replace('9', 'E'):
                level = int(m.group(1))
                if 1 <= level <= 10:
                    skill_max = 10
                    if level < 10 and self.skill_is_fixed(image, i):
                        skill_max = 1
                    skills.append({'level': level, 'max': skill_max, 'locked': False})
                    continue
            slot_img = crop(image, SKILL_SLOT_AREAS[i])
            if TEMPLATE_SKILL_LOCKED.match(slot_img, similarity=SKILL_LOCK_SIM):
                skills.append({'level': 0, 'max': 10, 'locked': True})
                continue
            # "?" card or empty background: no skill here
        return skills

    def skill_is_fixed(self, image, i):
        """
        OCR the skill name band and look for a skill that cannot be leveled.
        Card chrome varies with rarity (white-on-dark and dark-on-light
        bands both exist), so both extractions are tried. Long names render
        as a scrolling marquee - a single frame can catch an unreadable
        window (missed live on U-81), so up to two fresh frames are retried
        when a device is attached.
        """
        for attempt in range(3):
            for letter, thr in ((255, 255, 255), 128), ((70, 75, 85), 128):
                ocr = Ocr(_btn(SKILL_NAME_AREAS[i], 'SKILL_NAME_%s' % (i + 1)), lang='cnocr',
                          letter=letter, threshold=thr, name='OCR_SKILL_NAME')
                ocr.SHOW_LOG = False
                text = re.sub(r'[^a-z0-9]', '', str(ocr.ocr(image)).lower())
                if match_fixed_skill(text):
                    logger.info('Skill %s cannot be leveled (read %r)' % (i + 1, text))
                    return True
            if attempt >= 2 or not hasattr(self, 'device'):
                break
            self.device.sleep((0.5, 0.7))
            self.device.screenshot()
            image = self.device.image
        return False

    # ---------------- enhance tab ----------------

    def read_enhance_tab(self):
        """
        Visit the Enhance tab and judge whether every enhanceable stat is full.

        Returns:
            bool: True/False, or None if the tab could not be read.

        Pages:
            in: SHIP_DETAIL_CHECK (Info view)
            out: SHIP_DETAIL_CHECK (Info view)
        """
        if not self.goto_sidebar_tab(TEMPLATE_TAB_ENHANCE, 'Enhance'):
            # Blind clicks may have left a wrong tab open - restore Info so
            # the next ship's read starts from the right view
            self.goto_sidebar_tab(TEMPLATE_TAB_INFO, 'Info', verify_info=True)
            return None

        result = None
        timeout = Timer(6, count=6).start()
        while 1:
            self.device.screenshot()
            active = [self.enh_row_active(self.device.image, i) for i in range(4)]
            if any(active):
                maxed = [self.enh_row_maxed(self.device.image, i)
                         for i in range(4) if active[i]]
                result = all(maxed)
                logger.info('Enhance rows active={} maxed_rows={} -> maxed={}'.format(
                    active, maxed, result))
                break
            if timeout.reached():
                logger.warning('Enhance panel not readable, enhance unknown')
                break

        self.goto_sidebar_tab(TEMPLATE_TAB_INFO, 'Info', verify_info=True)
        return result

    @staticmethod
    def enh_row_active(image, i):
        """
        A row the hull can enhance shows green "EXP:..." text; capped-at-zero
        rows render it dimmed gray. Judged on green dominance because the
        panel is translucent - see ENH_GREEN_MIN.
        """
        px = crop(image, ENH_EXP_AREAS[i]).astype(int)
        r, g, b = px[:, :, 0], px[:, :, 1], px[:, :, 2]
        green = (g > ENH_GREEN_MIN) & (g > r + ENH_GREEN_MARGIN) & (g > b + ENH_GREEN_MARGIN)
        return int(np.sum(green)) >= ENH_ROW_ACTIVE_COUNT

    @staticmethod
    def enh_row_bar_full(image, i):
        """
        The row's EXP bar is yellow end to end, i.e. that stat is maxed.
        Column means first, so the bar's own dark border pixels and the MAX:N
        label's descenders cannot break the run.
        """
        col = crop(image, ENH_BAR_AREAS[i]).astype(int).mean(axis=0)
        r, g, b = col[:, 0], col[:, 1], col[:, 2]
        yellow = (r > 150) & (g > 130) & (r - b > 60)
        return float(np.mean(yellow)) >= ENH_BAR_FULL_RATIO

    @classmethod
    def enh_row_maxed(cls, image, i):
        """
        Two independent signals, either one is enough: the filled bar (opaque
        chrome, so background-independent) and the "EXP:MAX" template. The
        template alone cannot carry the decision - over bright art it sinks to
        0.685 on a genuinely maxed row (live: Unicorn's RLD), which is under
        any threshold that still rejects unmaxed rows (they peak at 0.47).
        """
        if cls.enh_row_bar_full(image, i):
            return True
        return bool(TEMPLATE_ENH_MAX.match(crop(image, ENH_MAX_AREAS[i]), similarity=TAB_SIM))

    def is_in_enhance(self):
        """The Fill button (opaque chrome) is unique to the Enhance panel."""
        return TEMPLATE_ENH_FILL.match(crop(self.device.image, ENH_FILL_SEARCH),
                                       similarity=TAB_SIM)

    def is_in_info(self):
        """
        On the Info view the "Level:" readout parses AND the Enhance panel is
        absent - a bare level-parse can false-positive on enhance-bar pixels
        (live: one ship got read entirely on the wrong tab that way).
        """
        return not self.is_in_enhance() \
            and 1 <= OCR_DETAIL_LEVEL.ocr(self.device.image) <= 125

    def ensure_info_view(self):
        """Heal a sticky non-Info tab left over from the previous ship."""
        self.device.screenshot()
        if not self.is_in_info():
            logger.info('Not on the Info view, returning to it')
            self.goto_sidebar_tab(TEMPLATE_TAB_INFO, 'Info', verify_info=True)

    def goto_sidebar_tab(self, template, tab_name, verify_info=False):
        """
        Reach a sidebar tab. The tabs sit on a semi-transparent panel that
        ship art tints, so luma matching backs up the plain match. No blind
        clicking: an unexpected sidebar means an unexpected ship type (live:
        blind clicks on a research ship navigated into the Shipyard and off
        the detail page entirely). Arrival is judged only by opaque elements
        of the destination view.
        """
        timeout = Timer(9, count=8).start()
        clicked = Timer(1.5)
        while 1:
            self.device.screenshot()
            if verify_info:
                if self.is_in_info():
                    return True
            elif self.is_in_enhance():
                return True
            if timeout.reached():
                logger.warning('goto_sidebar_tab({}) timeout'.format(tab_name))
                return False
            if clicked.reached():
                sim, btn = template.match_result(crop(self.device.image, SIDEBAR_AREA))
                if sim < TAB_SIM:
                    sim, btn = template.match_luma_result(crop(self.device.image, SIDEBAR_AREA))
                if sim >= TAB_SIM:
                    btn = btn.move((SIDEBAR_AREA[0], SIDEBAR_AREA[1]))
                    self.device.click(btn)
                    clicked.reset()

    # ---------------- grid pass ----------------

    def grid_pass(self):
        """
        With the dock's Stats overlay on its Affinity page, walk the dock and
        read (HP, affinity) per card.

        Returns:
            list[(int, int)]: (hp, affinity) per card, dock order.

        Pages:
            in: page_dock (filters applied)
            out: page_dock (at top, overlay off)
        """
        logger.hr('Grid pass', level=2)
        # The game restores the dock's last scroll position across visits, so
        # the sweep must start from the top or everything above the remembered
        # position is silently skipped (observed live)
        if DOCK_SCROLL.appear(main=self):
            DOCK_SCROLL.set_top(main=self)
            self.device.click_record_clear()
            self.device.sleep((0.6, 1.0))
        if not self.overlay_set(True):
            logger.warning('Could not reach the Affinity overlay page, '
                           'affinity will be missing this sweep')
            return []

        entries = self.grid_sweep()

        self.overlay_set(False)
        if DOCK_SCROLL.appear(main=self):
            DOCK_SCROLL.set_top(main=self)
            self.device.click_record_clear()
        self.device.sleep((0.6, 1.0))
        return entries

    def grid_sweep(self):
        """
        Walk the dock from top to bottom, reading every card exactly once.

        The dock is moved by dragging the cards themselves, one card row
        (CARD_ROW_PITCH px) at a time - measured live, a drag lands within a
        few px, because ALAS's drag holds at the end and kills the fling that
        makes a plain swipe useless. The scrollbar cannot do this job: ALAS
        measures its thumb 46-76 px long for one and the same list, and its
        smallest registrable drag is worth about six card rows on a full dock,
        so a "two row" step really moved seven and 60% of the dock was never
        read. Nothing here reads at_bottom() either - one bad thumb reading
        makes it true, which once ended a sweep 27% in.

        Each step advances one row less than the screen just read, so
        consecutive screens always share a row; that overlap is what drops
        repeats. The sweep ends when the screen stops changing.

        Returns:
            list[(int, int)]: (hp, affinity) per card, dock order.
        """
        entries = []
        prev_flat = []
        rewinds = 0
        stuck = 0
        blank = 0
        for page in range(GRID_PAGE_LIMIT):
            rows, _ = self.read_grid_page_best(page)
            flat = [pair for row in rows for pair in row]

            if not flat:
                # No cards at all. A drag whose list cannot move gets taken as a
                # tap on the card underneath, which opens that ship - and with
                # no cards to read the sweep used to just keep dragging on the
                # ship's page (live: minutes of it, no log to say so). Heal.
                blank += 1
                if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                    logger.warning('A drag opened a ship instead of scrolling, backing out '
                                   'to the dock')
                    self.detail_exit_to_dock()
                    if not self.overlay_is_on():
                        self.overlay_set(True)
                    continue
                if blank > GRID_BLANK_LIMIT:
                    logger.warning('Grid sweep read no cards {} screens running, stopping '
                                   'after {} cards'.format(blank, len(entries)))
                    break
                logger.info('Grid screen {} held no readable card, retrying'.format(page))
                self.device.sleep((1.0, 1.4))
                continue
            blank = 0

            if prev_flat and [hp for hp, _ in flat] == [hp for hp, _ in prev_flat]:
                stuck += 1
                if stuck >= GRID_STUCK_LIMIT:
                    logger.info('Grid sweep done: {} cards over {} screens'.format(
                        len(entries), page + 1))
                    break
                self.grid_drag_rows(GRID_DRAG_ROWS_MAX)
                continue
            stuck = 0

            overlap = self.overlap_length(prev_flat, flat)
            if prev_flat and flat and not overlap:
                if rewinds < GRID_REWIND_LIMIT:
                    rewinds += 1
                    logger.info('Grid screen {} shares no card with the previous one, '
                                'rewinding ({}/{})'.format(page, rewinds, GRID_REWIND_LIMIT))
                    self.grid_drag_rows(-1)
                    continue
                logger.warning('Grid screen {} still shares no card with the previous one, '
                               'some ships may be missing affinity'.format(page))
            rewinds = 0

            entries.extend(flat[overlap:])
            prev_flat = flat
            self.grid_drag_rows(min(max(len(rows) - 1, 1), GRID_DRAG_ROWS_MAX))
        return entries

    def grid_wait_stable(self):
        """
        Screenshot once the dock has stopped moving. Reading a frame mid-scroll
        gives half-drawn rows: bogus HP anchors, unreadable values (live: 58 of
        231 grid cards had no HP) and phantom rows that inflate the count.
        """
        self.wait_until_stable(GRID_STABLE_AREA, timer=Timer(0.3, count=1),
                               timeout=Timer(3, count=6), skip_first_screenshot=False)

    def read_grid_page_best(self, page=0, attempts=3):
        """
        Read the visible page, re-reading while values come back unreadable and
        keeping the best attempt. Attempts are never merged - a read that finds
        one card row fewer would merge rows of different ships together. An
        identical repeat ends it early: whatever is unreadable in this frame
        (a value clipped by the screen edge, art washing out a digit) will stay
        unreadable, and every retry costs a second.

        Returns:
            (list[list], bool): rows of (hp, affinity) pairs in dock order, and
                whether a gap was hit (i.e. this looks like the last page).
        """
        best = ([], False)
        best_score = -1
        previous = None
        for attempt in range(attempts):
            self.grid_wait_stable()
            rows, ended = self.grid_page_rows(self.read_grid_page(self.device.image))
            cards = sum(len(row) for row in rows)
            unreadable = sum(1 for row in rows for hp, aff in row
                             if hp is None or aff is None)
            if cards - unreadable > best_score:
                best, best_score = (rows, ended), cards - unreadable
            if not unreadable:
                break
            if rows == previous:
                logger.info('Grid page {}: {} cards, {} unreadable and unchanged on a '
                            're-read, taking it'.format(page, cards, unreadable))
                break
            previous = rows
            logger.info('Grid page {}: {} cards, {} unreadable, ended={} - re-reading '
                        '({}/{})'.format(page, cards, unreadable, ended, attempt + 1, attempts))
            self.device.sleep((0.6, 0.9))
        return best

    def read_grid_page(self, image):
        """
        Locate every fully visible card on screen and read it. Rows are found,
        not assumed - see CARD_LABEL_PITCH.

        Returns:
            list[list]: one list of 7 entries per card row, in dock order; each
                entry is (hp, affinity) or None where that column holds no card.
        """
        found = []
        for col in range(7):
            for hp_y, aff_y in self.card_anchors(image, col):
                found.append((hp_y, col, aff_y))
        found.sort()

        rows = []
        row_tops = []
        for hp_y, col, aff_y in found:
            if not rows or hp_y - row_tops[-1] > CARD_ROW_SEPARATION:
                rows.append([None] * 7)
                row_tops.append(hp_y)
            rows[-1][col] = self.read_card(image, col, hp_y, aff_y)
        return rows

    @staticmethod
    def grid_page_rows(rows):
        """
        Truncate a page at its first gap: cards fill the grid left to right,
        top to bottom, so the first empty cell is the end of the dock list.

        Returns:
            (list[list], bool): rows of pairs, and whether a gap was hit.
        """
        out = []
        ended = False
        for row in rows:
            keep = []
            for pair in row:
                if pair is None:
                    ended = True
                    break
                keep.append(pair)
            if keep:
                out.append(keep)
            if ended:
                break
        return out, ended

    def appear_then_back_from_ship(self):
        """
        True (having backed out) if a ship's detail page is open when the dock
        was expected. Dock drags open ships by accident: when the list cannot
        move, the gesture registers as a tap on the card under the stroke.
        """
        self.device.screenshot()
        if not self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
            return False
        logger.warning('A dock drag opened a ship, backing out')
        self.detail_exit_to_dock()
        return True

    def grid_drag_rows(self, rows):
        """
        Move the dock by `rows` card rows (negative scrolls back up) by
        dragging the cards. device.drag holds at the end of the stroke, so the
        list stops where it is put instead of flinging on - measured live at
        one row per CARD_ROW_PITCH px.
        """
        distance = int(CARD_ROW_PITCH * rows)
        start = np.array([GRID_DRAG_X, GRID_DRAG_Y if rows > 0
                          else GRID_DRAG_Y - GRID_DRAG_ROWS_MAX * CARD_ROW_PITCH])
        self.device.drag(start, start - np.array([0, distance]),
                         point_random=(-5, -5, 5, 5))
        # Dozens of consecutive drags are legitimate here; without this the
        # 12-same-button safety raises GameTooManyClickError
        self.device.click_record_clear()
        self.device.sleep((0.9, 1.3))

    @staticmethod
    def overlap_length(prev, flat):
        """
        How many cards at the head of `flat` repeat the tail of `prev`, matched
        on HP alone. Longest match wins; 0 means the screens do not overlap.
        """
        for k in range(min(len(prev), len(flat)), 0, -1):
            if [hp for hp, _ in prev[-k:]] == [hp for hp, _ in flat[:k]]:
                return k
        return 0

    @staticmethod
    def card_x(col):
        """Left edge of dock column `col`, mirroring CARD_GRIDS geometry."""
        return int(93 + (164 + 2 / 3) * col)

    @classmethod
    def card_label_bands(cls, image, col):
        """
        The white overlay label bands down one dock column.

        Returns:
            list[(int, int, int)]: (y center, bright pixel columns, extent) per
                band, top down.
        """
        lx1, lx2 = CARD_LABEL_REL
        ox = cls.card_x(col)
        mask = rgb2luma(crop(image, (ox + lx1, CARD_GRID_TOP, ox + lx2, CARD_GRID_BOTTOM))) \
            > CARD_LABEL_LUMA
        hit = mask.sum(axis=1) >= CARD_LABEL_WIDTH
        bands = []
        start = None
        for i, on in enumerate(list(hit) + [False]):
            if on and start is None:
                start = i
            elif not on and start is not None:
                columns = mask[start:i].any(axis=0)
                lit = np.where(columns)[0]
                bands.append((CARD_GRID_TOP + (start + i - 1) // 2, int(columns.sum()),
                              int(lit[-1] - lit[0]) if len(lit) else 0))
                start = None
        return bands

    @classmethod
    def card_anchors(cls, image, col):
        """
        Locate every fully visible card in a column, top down.

        The Affinity label is the landmark (see CARD_AFF_LABEL_WIDTH) and the
        HP label sits 127 px above it. A card cut off by the top or bottom edge
        is missing one of the two and gets skipped - its affinity would be
        unreadable anyway, and the page overlap catches it on the next screen.

        Returns:
            list[(int, int)]: (HP row y, Affinity row y) per card, top down.
        """
        bands = cls.card_label_bands(image, col)
        anchors = []
        for y, width, extent in bands:
            if not (CARD_AFF_LABEL_WIDTH[0] <= width <= CARD_AFF_LABEL_WIDTH[1]
                    and CARD_AFF_LABEL_EXTENT[0] <= extent <= CARD_AFF_LABEL_EXTENT[1]):
                continue
            hp = min((b for b in bands if b[1] <= CARD_HP_LABEL_MAX_WIDTH
                      and abs(y - CARD_AFF_OFFSET - b[0]) <= CARD_AFF_OFFSET_TOLERANCE),
                     key=lambda b: abs(y - CARD_AFF_OFFSET - b[0]), default=None)
            if hp is not None:
                anchors.append((hp[0], y))
        return anchors

    def _read_card_value(self, image, ox, yc, name):
        vx1, vx2 = CARD_VALUE_X_REL
        ocr = BrightDigit(_btn((ox + vx1, yc - 13, ox + vx2, yc + 13), name),
                          letter=(255, 255, 255), threshold=128, name='OCR_' + name)
        ocr.SHOW_LOG = False
        return ocr.ocr(image)

    def read_card(self, image, col, hp_y, aff_y):
        """
        (hp, affinity) of the card in column `col`, read beside its own HP and
        Affinity label rows. Either value is None when its OCR came back
        implausible.
        """
        ox = self.card_x(col)
        hp = self._read_card_value(image, ox, hp_y, 'CARD_HP')
        affinity = self._read_card_value(image, ox, aff_y, 'CARD_AFF')
        if not 0 < hp <= 99999:
            hp = None
        if not 0 <= affinity <= 200:
            affinity = None
        return hp, affinity

    def overlay_page_is_affinity(self, image):
        """OCR card 1's bottom label: 'Affinity' names the overlay page we need."""
        anchors = self.card_anchors(image, 0)
        if not anchors:
            return False
        ox = self.card_x(0)
        yc = anchors[0][1]
        ocr = Ocr(_btn((ox + 2, yc - 14, ox + 88, yc + 14), 'OVERLAY_LABEL'), lang='cnocr',
                  letter=(255, 255, 255), threshold=128, name='OCR_OVERLAY_LABEL')
        ocr.SHOW_LOG = False
        text = str(ocr.ocr(image))
        return 'affin' in text.lower()

    def overlay_is_on(self):
        """The Stats button is amber when an overlay page is active, blue when off."""
        mean = np.array(crop(self.device.image, STATS_STATE_AREA)).reshape(-1, 3).mean(axis=0)
        return mean[0] > 120 and mean[0] > mean[2]

    def overlay_set(self, enable):
        """
        Cycle the dock's Stats button (OFF -> stats+Affinity -> armor ->
        skills -> OFF) until the Affinity page (enable=True) or OFF
        (enable=False) is reached.
        """
        for attempt in range(8):
            self.device.screenshot()
            on = self.overlay_is_on()
            if not enable:
                if not on:
                    return True
            elif on and self.overlay_page_is_affinity(self.device.image):
                return True
            self.device.click(STATS_BUTTON)
            self.device.sleep((1.0, 1.4))
            self.device.click_record_clear()
        logger.warning('overlay_set({}) gave up after 8 taps'.format(enable))
        return False

    # ---------------- navigation ----------------

    def ship_view_next_safe(self, attempt=0):
        """
        ship_view_next with a dialogue-safe swipe box swapped in for the
        duration of the call (Equipment._ship_view_swipe reads the module
        globals at call time; tasks run single-threaded so this is safe).

        Args:
            attempt (int): Retry number - each one uses a different box and
                stroke length from SWIPE_BOXES, because whatever swallowed the
                last stroke (dialogue bubble, touch animation) covers a fixed
                part of the page.
        """
        from module.equipment import equipment as equipment_mod
        area, distance = SWIPE_BOXES[attempt % len(SWIPE_BOXES)]
        saved_area = equipment_mod.SWIPE_AREA
        saved_distance = equipment_mod.SWIPE_DISTANCE
        equipment_mod.SWIPE_AREA = _btn(area, 'CENSUS_SWIPE_AREA')
        equipment_mod.SWIPE_DISTANCE = distance
        try:
            return self.ship_view_next(check_button=SHIP_DETAIL_CHECK)
        finally:
            equipment_mod.SWIPE_AREA = saved_area
            equipment_mod.SWIPE_DISTANCE = saved_distance

    def dock_enter_at(self, index, grid, after_hp=None):
        """
        Open dock card `index` directly, from wherever the UI currently is.

        Used to resume an interrupted sweep and to get past a page where
        swiping is blocked. The dock is scrolled by whole card rows with the
        Stats overlay on, so the wanted card can be recognised before it is
        tapped - the scroll only lands within a row or so, and tapping the
        wrong card shifts the rest of the sweep.

        Recognition prefers `after_hp`, the ship the sweep last read: the card
        after it is exactly where the sweep wants to go, and that does not
        depend on the grid pass and the dock agreeing on positions (they drift
        apart - the grid can read a card twice when two screens fail to share
        one). The grid's own HP for `index` is the next best hint, and plain
        geometry the last resort.

        Args:
            index (int): 0-based position in dock order.
            grid (list): Grid pass entries, for the HP hints.
            after_hp (int): HP of the ship to continue after, if known.

        Returns:
            bool: True if the ship's detail page is open.

        Pages:
            in: Any
            out: SHIP_DETAIL_CHECK, or page_dock if it failed
        """
        logger.info('Entering dock card {}'.format(index))
        self.ui_ensure(page_dock)
        if DOCK_SCROLL.appear(main=self):
            DOCK_SCROLL.set_top(main=self)
            self.device.click_record_clear()
            self.device.sleep((0.6, 1.0))
        if not self.overlay_set(True):
            logger.warning('Could not reach the Affinity overlay, cannot locate card {}'
                           .format(index))
            return False

        # Stop a row short so the target lands mid-screen: a row of drags
        # accumulates a few px of error, and a card at the very top edge is
        # half cut off, unreadable, and therefore unfindable
        remaining = max(index // 7 - 1, 0)
        while remaining > 0:
            step = min(remaining, GRID_DRAG_ROWS_MAX)
            self.grid_drag_rows(step)
            remaining -= step
            # A drag the list cannot follow is taken as a tap and opens the card
            # underneath; carrying on would drag on that ship's page
            if self.appear_then_back_from_ship():
                if not self.overlay_is_on():
                    self.overlay_set(True)
        self.grid_wait_stable()
        target_hp = grid[index][0] if 0 <= index < len(grid) else None
        point = self.dock_card_point(self.device.image, index % 7, target_hp, after_hp)
        self.overlay_set(False)
        if point is None:
            logger.warning('Card {} not found in the dock'.format(index))
            return False

        button = _btn((point[0] - 20, point[1] - 20, point[0] + 20, point[1] + 20),
                      'DOCK_CARD_%s' % index)
        timeout = Timer(12, count=12).start()
        clicked = Timer(2.5)
        while 1:
            self.device.screenshot()
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                self.device.sleep((0.8, 1.2))
                return True
            if timeout.reached():
                logger.warning('Card {} did not open'.format(index))
                return False
            if self.handle_info_bar():
                continue
            if clicked.reached():
                self.device.click(button)
                self.device.click_record_clear()
                clicked.reset()

    def dock_enter_by_name(self, name, want=None, tries=4):
        """
        Open a ship through the dock's search box.

        This is the way in for records whose stored HP matches no card - a
        misread HP leaves the name as the only handle. The search is a prefix
        match over every owned ship, so "Bristol" also offers Bristol META:
        the result set is walked with swipes until the name matches `want`.
        The first tap on a card only dismisses the suggestion dropdown and the
        IME, hence the retry loop.

        Args:
            name (str): What to type (stylised suffixes are stripped).
            want (str): Canonical name to stop on; defaults to `name`.
            tries (int): How many ships of the result set to check.

        Returns:
            bool: True with that ship's detail page open.

        Pages:
            in: Any
            out: SHIP_DETAIL_CHECK, or page_dock if it failed
        """
        want = want or name
        query = re.sub(r'\(.*?\)', ' ', name)
        query = re.sub(r'[^A-Za-z0-9 ]+', ' ', query)
        query = ' '.join(query.split()[:2]).strip()
        if not query:
            return False
        logger.info('Searching the dock for {!r} (want {!r})'.format(query, want))
        self.ui_ensure(page_dock)
        self.device.click(DOCK_SEARCH_BUTTON)
        self.device.sleep((1.2, 1.5))
        self.device.click(DOCK_SEARCH_FIELD)
        self.device.sleep((1.2, 1.5))
        self.device.adb_shell(['input', 'text', query.replace(' ', '%s')])
        self.device.sleep((1.0, 1.4))
        self.device.adb_shell(['input', 'keyevent', '66'])
        self.device.sleep((1.5, 2.0))

        timeout = Timer(20, count=20).start()
        clicked = Timer(2.5)
        while 1:
            self.device.screenshot()
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                break
            if timeout.reached():
                logger.warning('Search for {!r} opened nothing'.format(query))
                self.dock_search_close()
                return False
            if self.handle_info_bar():
                continue
            if clicked.reached():
                self.device.click(DOCK_SEARCH_FIRST_CARD)
                self.device.click_record_clear()
                clicked.reset()

        for _ in range(tries):
            self.device.sleep((0.8, 1.2))
            self.ensure_info_view()
            got = self.canonical_name(self._clean_name(OCR_NAME.ocr(self.device.image)))[0]
            if got == want:
                return True
            logger.info('Search result is {!r}, looking further'.format(got))
            if not self.ship_view_next_safe():
                break
        logger.info('{!r} was not among the search results'.format(want))
        return False

    def dock_search_is_open(self):
        """The magnifier is amber while the search box is open, blue when not."""
        mean = np.array(crop(self.device.image, DOCK_SEARCH_STATE_AREA)).reshape(-1, 3).mean(axis=0)
        return mean[0] > 120 and mean[0] > mean[2]

    def dock_search_close(self, skip_first_screenshot=False):
        """
        Close the dock's search box if it is open.

        Leaving it open filters the dock down to one ship AND hides the Favorite
        and Stats buttons, so the next pass cannot set its filters and clicks
        itself into a GameTooManyClickError (live, after a name lookup left it
        open). Every mode therefore clears it before touching the dock.
        """
        self.ui_ensure(page_dock)
        for _ in range(3):
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if not self.dock_search_is_open():
                return True
            logger.info('Dock search box is open, closing it')
            self.device.click(DOCK_SEARCH_BUTTON)
            self.device.sleep((1.0, 1.4))
            self.device.click_record_clear()
        logger.warning('Could not close the dock search box')
        return False

    def dock_card_point(self, image, col, target_hp=None, after_hp=None):
        """
        Where to tap for the card the sweep wants next: the card following
        `after_hp` if that ship is on screen, else the card whose HP is
        `target_hp`, else the second visible card of column `col`.

        Returns:
            (int, int): Tap point, or None if nothing usable is on screen.
        """
        found = []
        for c in range(7):
            for hp_y, aff_y in self.card_anchors(image, c):
                found.append((hp_y, c, aff_y))
        found.sort()
        # Dock order: cluster into rows the same way read_grid_page does, never
        # by dividing y by the row pitch - rows sit at an arbitrary offset, so
        # a row at y=145 buckets as row 1 and the order comes out scrambled
        rows = []
        tops = []
        for hp_y, c, aff_y in found:
            if not rows or hp_y - tops[-1] > CARD_ROW_SEPARATION:
                rows.append([])
                tops.append(hp_y)
            rows[-1].append((c, hp_y, aff_y))
        cards = [(c, hp_y, self.read_card(image, c, hp_y, aff_y)[0])
                 for row in rows for c, hp_y, aff_y in sorted(row)]
        if not cards:
            return None

        def point(card):
            return self.card_x(card[0]) + CARD_TAP_REL[0], card[1] + CARD_TAP_REL[1]

        if after_hp:
            previous = [i for i, x in enumerate(cards) if x[2] == after_hp]
            if previous:
                # HP is not unique - duplicate copies share one, and so do
                # different un-levelled ships (Le Triomphant and Le Malin both
                # sit at 326) - so pick the match nearest where the scroll was
                # aimed: the card just before the target, which was placed on
                # the second visible row of column `col`.
                expected = max(self.dock_card_slot(cards, col, 1) - 1, 0)
                i = min(previous, key=lambda i: abs(i - expected))
                if i + 1 < len(cards):
                    logger.info('Card located as the one after HP {}'.format(after_hp))
                    return point(cards[i + 1])
        if target_hp:
            match = [x for x in cards if x[2] == target_hp]
            if match:
                pick = min(match, key=lambda x: (x[0] != col, x[1]))
                logger.info('Card located by HP {} at column {}'.format(target_hp, pick[0]))
                return point(pick)
            logger.info('HP {} not on screen, falling back to column {}'.format(target_hp, col))
        column = [x for x in cards if x[0] == col]
        if not column:
            return None
        # Second visible row where possible - the target was scrolled to sit
        # mid-screen, so the topmost row is the one before it
        return point(sorted(column, key=lambda x: x[1])[1 if len(column) > 1 else 0])

    @staticmethod
    def dock_card_slot(cards, col, row):
        """
        Position within `cards` (dock order) of the card at screen row `row`,
        column `col` - or the nearest thing on screen.
        """
        tops = sorted(set(hp_y for _, hp_y, _ in cards))
        if not tops:
            return 0
        want_y = tops[min(row, len(tops) - 1)]
        return min(range(len(cards)),
                   key=lambda i: (abs(cards[i][1] - want_y) > CARD_ROW_SEPARATION,
                                  abs(cards[i][0] - col)))

    def resync_index(self, index, grid, window=12):
        """
        Work out which dock card actually opened, by matching the ship's HP
        against the grid pass around the expected position. The scroll lands
        within a row of the target, and being one row out would otherwise skip
        or repeat seven ships for the rest of the sweep.

        Returns:
            int: Corrected dock index.
        """
        self.ensure_info_view()
        hp = OCR_DETAIL_HP.ocr(self.device.image)
        if not hp:
            return index
        low, high = max(index - window, 0), min(index + window + 1, len(grid))
        matches = [i for i in range(low, high) if grid[i][0] == hp]
        if not matches:
            logger.info('Re-entered on HP {}, which the grid does not have near card '
                        '{}'.format(hp, index))
            return index
        best = min(matches, key=lambda i: abs(i - index))
        if best != index:
            logger.info('Re-entered at card {} rather than {}'.format(best, index))
        return best

    def detail_exit_to_dock(self, skip_first_screenshot=True):
        """Back out from ship detail to page_dock (MetaLab's exit idiom)."""
        timeout = Timer(10, count=10).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.ui_page_appear(page_dock):
                return
            if timeout.reached():
                logger.warning('detail_exit_to_dock timeout, using ui_ensure')
                self.ui_ensure(page_dock)
                return
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=3):
                self.device.click(BACK_ARROW)
                continue
