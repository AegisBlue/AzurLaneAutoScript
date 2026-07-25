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

import cv2
import numpy as np

from module.base.button import Button
from module.base.timer import Timer
from module.base.utils import crop, rgb2luma
from module.logger import logger
from module.ocr.ocr import Digit, Ocr
from module.retire.assets import DOCK_EMPTY, DOCK_FIRST_NPC, SHIP_DETAIL_CHECK
from module.retire.dock import CARD_GRIDS, DOCK_SCROLL, Dock
from module.ship_census.assets import *
from module.ship_census.dashboard import generate_dashboard
from module.ship_census.store import CensusStore
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

# The overlay's six stat rows shift up to ~13px with the card frame style but
# keep a constant pitch, so everything anchors on the HP label (the top-most
# white label band): value windows sit right of the labels, capped at the
# card edge so the neighbor card's frame stays out.
CARD_HP_ANCHOR_REL = (2, 15, 88, 58)       # search window for the HP label
CARD_VALUE_X_REL = (84, 140)               # value column, right-aligned
CARD_AFF_OFFSET = 126                      # HP label center -> Affinity row center
# The Stats cycle button in the dock top bar: amber when an overlay page is
# active (mean ~(167,122,69)), blue when off (~(66,80,119))
STATS_BUTTON = _btn((905, 10, 970, 44), 'STATS_BUTTON')
STATS_STATE_AREA = (860, 8, 978, 45)

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
GRID_PAGE_LIMIT = 60
# Ship-to-ship swipe box: the stock equipment SWIPE_AREA reaches y=527, where
# the secretary dialogue bubble swallows drags (live: a sweep died at ship 2
# when both random swipe points landed on it)
CENSUS_SWIPE_AREA = Button(area=(225, 180, 570, 430), color=(),
                           button=(225, 180, 570, 430), name='CENSUS_SWIPE_AREA')
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

        if mode != 'dashboard_only':
            self.census_sweep(store, full=(mode == 'full'))

        path = generate_dashboard(store)
        logger.info('Dashboard written to {}'.format(path))

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
        logger.info('Grid pass: {} cards'.format(len(grid)))

        # NPC rentals occupy card 1 during events; dock_enter_first skips them
        self.device.screenshot()
        npc_offset = 1 if self.appear(DOCK_FIRST_NPC, offset=(20, 20)) else 0
        if not self.dock_enter_first():
            logger.info('No enterable ship in dock')
            store.sweep_end(complete=True)
            store.save()
            self.dock_filter_set(wait_loading=False)
            return

        for _ in range(skip):
            self.device.click_record_clear()
            if not self.ship_view_next_safe():
                logger.info('Dock ended during resume skip, sweep was already complete')
                store.sweep_end(complete=True)
                store.save()
                self.detail_exit_to_dock()
                self.dock_filter_set(wait_loading=False)
                return

        visited = 0
        complete = False
        index = skip + npc_offset
        while 1:
            visited += 1
            self.process_ship(store, grid, index, stale_days=stale_days, full=full)
            store.save()
            self.device.click_record_clear()
            index += 1
            if visited >= SWEEP_SAFETY_LIMIT:
                logger.warning('Census sweep safety limit reached')
                break
            # A failed swipe can mean end-of-dock OR a swallowed drag (Live2D
            # skins eat them occasionally); the grid pass knows how many
            # cards exist, so a premature "end" gets retried
            advanced = False
            for attempt in range(3):
                if self.ship_view_next_safe():
                    advanced = True
                    break
                if index >= len(grid):
                    break
                logger.info('Swipe did not advance (attempt {}), grid expects {} more '
                            'cards'.format(attempt + 1, len(grid) - index))
                self.device.sleep((1.5, 2.5))
                self.device.click_record_clear()
            if not advanced:
                logger.info('Census sweep reached the end of the dock')
                complete = True
                break

        if complete and grid and index != len(grid):
            logger.warning('Grid pass saw {} cards but detail pass ended at index {} - '
                           'affinity joins were level-checked per ship'.format(len(grid), index))
        store.sweep_end(complete=complete)
        store.save()
        self.detail_exit_to_dock()
        self.dock_filter_set(wait_loading=False)
        logger.info('Census sweep processed {} ships this run'.format(visited))

    def process_ship(self, store, grid, index, stale_days=7, full=False):
        """
        Read the ship currently open on the detail page and upsert its record.

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
            return
        key = store.sweep_key(name)
        logger.hr('Ship {} (Lv.{})'.format(key, level), level=2)

        # Join affinity from the grid pass; trust it only if the HP values
        # agree (HP is near-unique per ship, so a misaligned join is rejected)
        affinity = None
        if 0 <= index < len(grid):
            g_hp, g_aff = grid[index]
            if g_hp and g_hp == detail['hp'] and g_aff is not None and 0 <= g_aff <= 200:
                affinity = g_aff
            else:
                logger.info('Grid join rejected at index {} (grid HP {} vs detail HP {})'.format(
                    index, g_hp, detail['hp']))

        oathed = detail['oath_badge'] or (affinity is not None and affinity > 100)

        fields = dict(
            name=name,
            copy=int(key.rsplit('#', 1)[1]),
            level=level,
            hp=detail['hp'],
            is_meta=detail['is_meta'],
            is_research=detail['is_research'],
            lb_current=detail['lb_current'],
            lb_max=detail['lb_max'],
            skills=detail['skills'],
            oathed=oathed,
        )
        if detail['rarity'] is not None:
            fields['rarity'] = detail['rarity']
        if affinity is not None:
            fields['affinity'] = affinity

        if not full and not store.needs_deep_scan(key, level, stale_days):
            logger.info('Record fresh, enhance tab skipped')
            store.record(key, **fields)
            store.sweep_advance(key)
            return

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
        With the dock's Stats overlay on its Affinity page, walk the pages and
        read (level, affinity) per card in dock order. next_page drags 80% of
        a viewport, so consecutive screens overlap; overlapping row
        fingerprints are dropped before appending.

        Returns:
            list[(int, int)]: (level, affinity) per card, dock order.

        Pages:
            in: page_dock (filters applied, at top)
            out: page_dock (at top, overlay off)
        """
        logger.hr('Grid pass', level=2)
        # The game restores the dock's last scroll position across visits;
        # both passes must start from the top or everything above the
        # remembered position is silently skipped (observed live)
        if DOCK_SCROLL.appear(main=self):
            DOCK_SCROLL.set_top(main=self)
            self.device.click_record_clear()
            self.device.sleep((0.6, 1.0))
        if not self.overlay_set(True):
            logger.warning('Could not reach the Affinity overlay page, '
                           'affinity will be missing this sweep')
            return []

        entries = []
        prev_rows = []
        for page in range(GRID_PAGE_LIMIT):
            self.device.screenshot()
            rows = []
            ended = False
            for r in range(3):
                row = []
                for c in range(7):
                    pair = self.read_card_hp_affinity(self.device.image, c, r)
                    if pair is None:
                        ended = True
                        break
                    row.append(pair)
                if row:
                    rows.append(row)
                if ended:
                    break

            # Drop the overlap with the previous screen (row-fingerprinted)
            start = 0
            for k in range(min(len(prev_rows), len(rows)), 0, -1):
                if prev_rows[-k:] == rows[:k]:
                    start = k
                    break
            for row in rows[start:]:
                entries.extend(row)
            prev_rows = rows

            if ended:
                break
            if not DOCK_SCROLL.appear(main=self) or DOCK_SCROLL.at_bottom(main=self):
                break
            # Advance exactly 2 of the 3 visible rows so consecutive screens
            # always overlap (next_page's 0.8-viewport drag skips ~2 unread
            # rows per page - live: that nulled affinity for 132/142 ships).
            # The scroll thumb length encodes visible/total content.
            pos = DOCK_SCROLL.cal_position(main=self)
            rows_total = 3.0 * DOCK_SCROLL.total / max(DOCK_SCROLL.length, 1)
            step = 2.0 / max(rows_total - 3.0, 1.0)
            DOCK_SCROLL.set(min(pos + step, 1.0), main=self,
                            random_range=(-0.005, 0.005), distance_check=False)
            # Dozens of consecutive scroll swipes are legitimate here; without
            # this the 12-same-button safety raises GameTooManyClickError
            self.device.click_record_clear()
            self.device.sleep((1.0, 1.4))

        self.overlay_set(False)
        if DOCK_SCROLL.appear(main=self):
            DOCK_SCROLL.set_top(main=self)
            self.device.click_record_clear()
        self.device.sleep((0.6, 1.0))
        return entries

    @staticmethod
    def card_origin(col, row):
        """
        Cell origin for the dock's visible 7x3 grid. CARD_GRIDS only models
        the top 2 rows; the new dock UI shows 3 (row 3's overlay rows still
        fit above y=720). Geometry mirrors CARD_GRIDS origin/delta.
        """
        return int(93 + (164 + 2 / 3) * col), int(76 + 227 * row)

    @classmethod
    def card_hp_anchor_y(cls, image, col, row):
        """
        y-center of the HP label - the top-most white label band in the
        card's upper stat area. All other overlay rows sit at fixed offsets
        below it.
        """
        ox, oy = cls.card_origin(col, row)
        lx1, ly1, lx2, ly2 = CARD_HP_ANCHOR_REL
        label_img = crop(image, (ox + lx1, oy + ly1, ox + lx2, oy + ly2))
        mask = rgb2luma(label_img) > 200
        row_counts = mask.sum(axis=1)
        bands = np.where(row_counts >= 8)[0]
        if not len(bands):
            return None
        top = bands[0]
        bottom = top
        while bottom + 1 in bands:
            bottom += 1
        return oy + ly1 + (top + bottom) // 2

    def _read_card_value(self, image, ox, yc, name):
        vx1, vx2 = CARD_VALUE_X_REL
        ocr = BrightDigit(_btn((ox + vx1, yc - 13, ox + vx2, yc + 13), name),
                          letter=(255, 255, 255), threshold=128, name='OCR_' + name)
        ocr.SHOW_LOG = False
        return ocr.ocr(image)

    def read_card_hp_affinity(self, image, col, row):
        """
        (hp, affinity) from the overlay stat rows, both anchored on the HP
        label. Affinity clamps to 0-200; None marks an unreadable field.
        Returns None (no tuple) when the cell holds no card - the overlay
        labels only render on cards, which doubles as the presence check
        (the Lv. badge is too dim on plain card frames to count pixels on).
        """
        yc = self.card_hp_anchor_y(image, col, row)
        if yc is None:
            return None
        ox = self.card_origin(col, row)[0]
        hp = self._read_card_value(image, ox, yc, 'CARD_HP')
        affinity = self._read_card_value(image, ox, yc + CARD_AFF_OFFSET, 'CARD_AFF')
        if not 0 < hp <= 99999:
            hp = None
        if not 0 <= affinity <= 200:
            affinity = None
        return hp, affinity

    def overlay_page_is_affinity(self, image):
        """OCR card 1's bottom label: 'Affinity' names the overlay page we need."""
        yc = self.card_hp_anchor_y(image, 0, 0)
        if yc is None:
            return False
        ox = self.card_origin(0, 0)[0]
        yc += CARD_AFF_OFFSET
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

    def ship_view_next_safe(self):
        """
        ship_view_next with the dialogue-safe swipe box swapped in for the
        duration of the call (Equipment._ship_view_swipe reads the module
        global at call time; tasks run single-threaded so this is safe).
        """
        from module.equipment import equipment as equipment_mod
        saved = equipment_mod.SWIPE_AREA
        equipment_mod.SWIPE_AREA = CENSUS_SWIPE_AREA
        try:
            return self.ship_view_next(check_button=SHIP_DETAIL_CHECK)
        finally:
            equipment_mod.SWIPE_AREA = saved

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
