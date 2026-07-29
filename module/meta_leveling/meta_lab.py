import re

import numpy as np

import module.config.server as server
from module.base.button import Button, ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.logger import logger
from module.meta_leveling.assets import *
from module.ocr.ocr import Digit, DigitCounter, Ocr
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY, SHIP_DETAIL_CHECK
from module.retire.dock import CARD_GRIDS, DOCK_SCROLL, Dock
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock


def _area_button(area, name):
    return Button(area=area, color=(), button=area, name=name)


# --- ship detail page ---
# Ship level right of "Level:", same crop as ExpFeed/Awaken
OCR_DETAIL_LEVEL = Digit(_area_button((758, 283, 798, 319), 'DETAIL_LEVEL'),
                         letter=(255, 255, 255), threshold=128, name='OCR_DETAIL_LEVEL')
# The three skill cards at the bottom right of the Info tab
DETAIL_SKILL_CARDS = [
    _area_button((690, 520, 875, 612), 'DETAIL_SKILL_1'),
    _area_button((875, 520, 1060, 612), 'DETAIL_SKILL_2'),
    _area_button((1060, 520, 1245, 612), 'DETAIL_SKILL_3'),
]
DETAIL_SKILL_LEVELS = [
    _area_button((762, 573, 872, 597), 'DETAIL_SKILL_LEVEL_1'),
    _area_button((947, 573, 1057, 597), 'DETAIL_SKILL_LEVEL_2'),
    _area_button((1132, 573, 1242, 597), 'DETAIL_SKILL_LEVEL_3'),
]

# --- tactics screen ---
# The three skill cards at the bottom of Tactical Training
TACTICS_SKILL_CARDS = [
    _area_button((712, 560, 880, 640), 'TACTICS_SKILL_1'),
    _area_button((897, 560, 1065, 640), 'TACTICS_SKILL_2'),
    _area_button((1082, 560, 1250, 640), 'TACTICS_SKILL_3'),
]
# White triangle marker under the SELECTED skill card
TACTICS_SELECT_MARKERS = [
    _area_button((787, 628, 815, 650), 'TACTICS_MARKER_1'),
    _area_button((959, 628, 987, 650), 'TACTICS_MARKER_2'),
    _area_button((1131, 628, 1159, 650), 'TACTICS_MARKER_3'),
]

# --- quick train dialog ---
# "current[+added]/total" line: the white DigitCounter reads current/total,
# the green [+added] part is dropped by the letter filter (validated offline)
OCR_QT_PROGRESS = DigitCounter(_area_button((790, 158, 970, 184), 'QT_PROGRESS'),
                               letter=(255, 255, 255), threshold=128,
                               name='OCR_QT_PROGRESS')
OCR_QT_T1_OWNED = Digit(_area_button((366, 313, 407, 335), 'QT_T1_OWNED'),
                        letter=(255, 255, 255), threshold=128, name='OCR_QT_T1_OWNED')
OCR_QT_T2_OWNED = Digit(_area_button((366, 417, 407, 439), 'QT_T2_OWNED'),
                        letter=(255, 255, 255), threshold=128, name='OCR_QT_T2_OWNED')
# Single minus of the T1 row, click position only (not cut as an asset)
QT_T1_MINUS = _area_button((555, 288, 602, 334), 'QT_T1_MINUS')

# --- rigging fortification screen ---
# The details panel (bonus + "Materials Needed: owned/needed") docks UNDER
# the selected category; the four category columns start at x 91/358/625/892
# (stride 267). Grey digits on the light bar. NOT a DigitCounter: that class
# clamps current to total, and owned is usually far larger than needed.
def _fortify_mats_ocr(index):
    left = 248 + 267 * index
    return Ocr(_area_button((left, 582, left + 74, 608), f'FORTIFY_MATS_{index + 1}'),
               lang='azur_lane', letter=(100, 100, 105), threshold=96,
               alphabet='0123456789/', name=f'OCR_FORTIFY_MATS_{index + 1}')


OCR_FORTIFY_MATS = [_fortify_mats_ocr(i) for i in range(4)]
# Category click points across the top of the fortification screen
FORTIFY_CATEGORIES = [
    _area_button((190, 320, 250, 370), 'FORTIFY_CAT_1'),
    _area_button((455, 320, 515, 370), 'FORTIFY_CAT_2'),
    _area_button((745, 320, 805, 370), 'FORTIFY_CAT_3'),
    _area_button((995, 320, 1055, 370), 'FORTIFY_CAT_4'),
]

# --- somatic activation screen ---
# "Level Requirement: 70/10". The current level is GREEN when the
# requirement is met (Bristol 70/10) and RED when unmet (Fusou 1/10) ->
# the color alone decides; the "/required" number is informational only.
OCR_ACT_REQ_GREEN = Ocr(_area_button((995, 522, 1075, 554), 'ACT_REQ_GREEN'),
                        lang='azur_lane', letter=(126, 210, 90), threshold=128,
                        alphabet='0123456789', name='OCR_ACT_REQ_GREEN')
OCR_ACT_REQ_RED = Ocr(_area_button((995, 522, 1075, 554), 'ACT_REQ_RED'),
                      lang='azur_lane', letter=(230, 60, 50), threshold=128,
                      alphabet='0123456789', name='OCR_ACT_REQ_RED')
OCR_ACT_REQ_WHITE = Ocr(_area_button((995, 522, 1075, 554), 'ACT_REQ_WHITE'),
                        lang='azur_lane', letter=(255, 255, 255), threshold=128,
                        alphabet='0123456789/', name='OCR_ACT_REQ_WHITE')

# Neutral click point to dismiss full-screen celebrations (somatic
# activation star-up etc.) - a tap anywhere closes them
LAB_DISMISS = _area_button((550, 660, 730, 700), 'LAB_DISMISS')

# Badge search regions on the lab hub (top-left corner of the hub boxes)
HUB_ACTIVATION_BADGE = (195, 195, 265, 265)
HUB_FORTIFY_BADGE = (1010, 175, 1085, 240)
# State tag under TACTICAL RESEARCH
HUB_RESEARCH_TAG = (1170, 395, 1270, 425)

# --- dock sweep geometry ---
# The dock draws 3 rows of 7 cards, but upstream's shared CARD_GRIDS covers
# only the top 2 (grid_shape=(7, 2) in module/retire/dock.py). Reading 14
# cards while paging the list by a full viewport skipped the whole third row
# every page: the 2026-07-28 03:12 run visited 17 ships out of ~42 METAs in
# the dock. These grids mirror CARD_GRIDS' measurements - taken from its own
# attributes so upstream tweaks carry over - and add the missing row. Row 3's
# buttons run past the bottom of a 720px frame; only their centre is ever
# clicked, and the level badge the presence check reads sits at y 535-557,
# well inside it.
LAB_CARD_GRIDS = ButtonGrid(
    origin=tuple(CARD_GRIDS.origin), delta=tuple(CARD_GRIDS.delta),
    button_shape=tuple(CARD_GRIDS.button_shape),
    grid_shape=(CARD_GRIDS.grid_shape[0], 3), name='LAB_CARD')
if server.server != 'jp':
    LAB_CARD_LEVEL_GRIDS = LAB_CARD_GRIDS.crop(area=(77, 5, 138, 27), name='LAB_LEVEL')
else:
    LAB_CARD_LEVEL_GRIDS = LAB_CARD_GRIDS.crop(area=(74, 5, 136, 27), name='LAB_LEVEL')

# Paging by dragging whole card rows, mirroring ShipCensus' grid sweep:
# device.drag holds at the end of the stroke, so the list stops where it is
# put instead of flinging on, at one row per LAB_CARD_ROW_PITCH px.
# DOCK_SCROLL.next_page() is NOT usable here - it moves 0.8 of a viewport
# (2.4 rows), which cannot line up with the 3 rows this sweep reads.
LAB_CARD_ROW_PITCH = 227
LAB_DRAG_X = 640
LAB_DRAG_Y = 620
# Longest stroke that still starts and ends inside the card area
LAB_DRAG_ROWS_MAX = 2
# The dock does NOT snap to row boundaries after a drag - it stops wherever the
# stroke left it, tens of pixels out. Everything below exists to measure that.
#
# Rows are found by the dark gaps BETWEEN them: the cards are bright to their
# edges and the background between two rows is a near-black band running the
# full width of the list. Ship artwork defeats every anchor inside a card (the
# white Lv badge included - see the run this fixed), but nothing draws in the
# gaps.
LAB_GAP_X = (95, 1228)          # full card width, clear of the dock chrome
LAB_GAP_DARK = 60               # mean brightness of a between-rows band
LAB_GAP_MERGE = 6               # px; a bright sliver this thin does not end a gap
# Where the FIRST gap below the top row falls, for any offset the drag can leave
# (+-half a pitch around its nominal 280).
LAB_GAP_SEARCH = (170, 410)
LAB_CARD_TOP = 76               # LAB_CARD_GRIDS.origin[1]
# The list is a window, not the whole screen: above LAB_CARD_TOP sit the dock
# tabs and the filter button, below LAB_CARD_VIEW_BOTTOM the bottom bar. A
# scrolled row overhangs both, and clicking the overhang hits that chrome.
LAB_CARD_VIEW_BOTTOM = 640
# Rows read (and paged) per screen. The dock draws three, but the third one's
# name band - the only presence test that survives ship artwork, see
# dock_card_present - falls off the bottom of the frame as soon as the list is
# scrolled even slightly. Reading two and advancing two costs one extra drag per
# page and makes every card's presence readable.
LAB_ROWS_PER_PAGE = 2
# The name band: an opaque chip with white text, at a fixed offset inside every
# card. Measured over three captured pages, a card reads 310-1005 white pixels
# here and an empty cell reads exactly 0 - including the near-black Hunter META
# card, which a brightness test calls empty.
LAB_NAME_BAND = (5, 168, 133, 195)
LAB_NAME_WHITE = 60
LAB_CARD_HEIGHT = 204           # LAB_CARD_GRIDS.button_shape[1]
# The gap's START is the anchor, not its end. A card's bottom edge is a crisp
# boundary; its TOP edge is a dark border of uneven thickness, which put the
# first version of this 6-16px out - enough for a 22px-tall badge crop to miss.
# Measured against three captured pages, the gap starts within 2px of the card
# bottom every time.
LAB_GAP_ANCHOR = LAB_CARD_TOP + LAB_CARD_HEIGHT
# Sweep bounds. The roster is ~42 METAs; both are runaway brakes, not budgets.
LAB_SWEEP_SHIP_CAP = 80
LAB_SWEEP_PAGE_CAP = 8

# Confirms allowed on the learn dialog before it is taken as unaffordable. A
# working Confirm needs exactly one.
LEARN_CONFIRM_TRIES = 3

SKILL_MAX_LEVEL = 10
# EXP one T1 META Universal Skill Book gives. Used until the task has
# observed one confirmed batch and calibrated the real value from the
# progress counter delta.
T1_BOOK_EXP_DEFAULT = 100
# Books per Quick Train confirm. Live runs showed batches up to 380 books
# apply fine while a 600-book batch confirmed without spending anything -
# large levels simply take several rounds.
QT_BATCH_CAP = 300
# Safety caps per ship per run
FORTIFY_CLICK_CAP = 60
QT_ROUND_CAP = 15


class MetaLab(Dock):
    """
    Daily task: iterate every META ship in the dock and run the META Lab
    upkeep on each — keep a skill researching (activating the next one when
    the current maxes), pump Quick Train books into the researching skill,
    fortify rigging while materials last, and perform somatic activation
    when its level and META Crystal requirements are met.

    Leveling the ships themselves is MetaLeveling's (campaign farming) job.
    """
    books_exhausted = False
    # Set the first time a learn dialog refuses to confirm. Learning any skill
    # costs the same 5 red T3 books, so once one ship cannot afford it none can,
    # and every further attempt is four wasted clicks on a dead dialog.
    learn_blocked = False

    # ------------------------------------------------------------------ ui

    def is_in_lab(self):
        return self.appear(META_LAB_CHECK, offset=(20, 20))

    def is_in_hub(self):
        # Hub = lab title + the SOMATIC ACTIVATION box (subscreens lack it)
        return self.is_in_lab() and self.appear(HUB_ACTIVATION, offset=(20, 20))

    def lab_enter(self, skip_first_screenshot=True):
        """
        From a META ship's detail page, open her META Lab hub via the
        Research sidebar tab.

        Returns:
            bool: True if in hub, False if the tab never appeared (not a
                META/research ship).

        Pages:
            in: SHIP_DETAIL_CHECK
            out: META_LAB_CHECK (hub)
        """
        logger.info('Lab enter')
        timeout = Timer(10, count=10).start()
        self.interval_clear(SHIP_DETAIL_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_in_hub():
                return True
            if timeout.reached():
                logger.warning('lab_enter timeout')
                return False
            # Click the Research tab position blindly: the tab text sits on
            # ship art, so template matching is unreliable across ships.
            # Arrival is verified by META_LAB_CHECK; non-research ships just
            # time out and get skipped.
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=3):
                self.device.click(DETAIL_RESEARCH_TAB)
                continue
            if self.handle_info_bar():
                continue
            if self.handle_game_tips():
                continue

    def handle_lab_info_dialog(self):
        """
        Confirm "Information"-style dialogs the lab throws (fortification
        milestone rewards etc.). The Confirm position varies per dialog, so
        it is searched with a wide offset.

        Returns:
            bool: If handled.
        """
        if self.appear(LEARN_CHECK, offset=(20, 20)):
            if self.appear_then_click(LEARN_CONFIRM, offset=(200, 60), interval=3):
                return True
        return False

    def lab_exit(self, skip_first_screenshot=True):
        """
        Back out of the lab (hub or subscreen) to the ship detail page.

        Pages:
            in: META_LAB_CHECK
            out: SHIP_DETAIL_CHECK
        """
        logger.info('Lab exit')
        timeout = Timer(15, count=15).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)) and not self.is_in_lab():
                return True
            if timeout.reached():
                logger.warning('lab_exit timeout')
                return False
            if self.handle_lab_info_dialog():
                timeout.reset()
                continue
            if self.is_in_lab():
                self.device.click(BACK_ARROW)
                self.device.sleep((1.0, 1.4))
                continue
            if self.handle_popup_cancel('LAB_EXIT'):
                continue
            # Neither lab nor detail: possibly a full-screen celebration
            self.device.click(LAB_DISMISS)
            self.device.sleep((1.0, 1.4))

    def hub_goto(self, button, check, skip_first_screenshot=True):
        """
        From the hub, enter a subscreen with a reliable check element.

        Args:
            button (Button): hub box to click (HUB_RESEARCH etc.)
            check (Button): appears on the destination screen
        """
        timeout = Timer(10, count=10).start()
        self.interval_clear(button)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(check, offset=(20, 20)):
                return True
            if timeout.reached():
                logger.warning(f'hub_goto {button} timeout')
                return False
            if self.is_in_hub() and self.appear(button, offset=(20, 20), interval=3):
                self.device.click(button)
                continue

    def hub_goto_subscreen(self, button, skip_first_screenshot=True):
        """
        From the hub, enter a subscreen whose own header CANNOT be template
        matched (the fortification and activation headers sit on art-tinted
        panels that vary per ship). Success = still in the lab but no
        longer on the hub; the pass afterwards guards itself with opaque
        elements (buttons, OCR reads).

        Args:
            button (Button): hub box to click (HUB_FORTIFY / HUB_ACTIVATION)
        """
        timeout = Timer(10, count=10).start()
        self.interval_clear(button)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_in_lab() and not self.appear(HUB_ACTIVATION, offset=(20, 20)):
                return True
            if timeout.reached():
                logger.warning(f'hub_goto_subscreen {button} timeout')
                return False
            if self.appear(button, offset=(20, 20), interval=3):
                self.device.click(button)
                continue

    def subscreen_back_to_hub(self, skip_first_screenshot=True):
        timeout = Timer(10, count=10).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.is_in_hub():
                return True
            if timeout.reached():
                logger.warning('subscreen_back_to_hub timeout')
                return False
            if self.handle_lab_info_dialog():
                timeout.reset()
                continue
            if self.is_in_lab():
                self.device.click(BACK_ARROW)
                self.device.sleep((1.0, 1.4))
                continue
            # Possibly a full-screen celebration overlay
            self.device.click(LAB_DISMISS)
            self.device.sleep((1.0, 1.4))

    # -------------------------------------------------------------- reading

    def get_detail_level(self, skip_first_screenshot=True):
        """
        Returns:
            int: 1-125, 0 on OCR failure.

        Pages:
            in: SHIP_DETAIL_CHECK
        """
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
                logger.warning('get_detail_level timeout, OCR failed')
                return 0

    def _skill_level_from_text(self, text):
        """
        Parse a "LEVEL: N" strip. Returns int level, or None if unreadable.
        """
        text = text.upper().replace(' ', '')
        if 'MA' in text:  # MAX
            return SKILL_MAX_LEVEL
        match = re.search(r'(\d+)', text)
        if match:
            level = int(match.group(1))
            if 1 <= level <= SKILL_MAX_LEVEL:
                return level
        return None

    def read_detail_skills(self):
        """
        Read the three skill cards on the ship detail Info tab.

        Returns:
            list[str|int]: per card 'locked', 'empty', an int level, or
                'unknown'.

        Pages:
            in: SHIP_DETAIL_CHECK
        """
        results = []
        image = self.device.image
        level_ocr = Ocr([b for b in DETAIL_SKILL_LEVELS], lang='cnocr',
                        letter=(255, 255, 255), threshold=128,
                        alphabet='0123456789LEVEL:? ', name='DETAIL_SKILL_LEVEL')
        texts = level_ocr.ocr(image)
        if not isinstance(texts, list):
            texts = [texts]
        for card, text in zip(DETAIL_SKILL_CARDS, texts):
            crop = self.image_crop(card, copy=False)
            if TEMPLATE_SKILL_LOCKED.match(crop, similarity=0.70):
                results.append('locked')
                continue
            level = self._skill_level_from_text(str(text))
            if level is not None:
                results.append(level)
            elif not str(text).strip():
                results.append('empty')
            else:
                results.append('unknown')
        logger.info(f'Detail skills: {results}')
        return results

    def hub_badge(self, region):
        """
        Args:
            region (tuple): search area on the hub screenshot.

        Returns:
            str: 'alert', 'done' or 'none'
        """
        crop = self.image_crop(_area_button(region, 'BADGE_REGION'), copy=False)
        if TEMPLATE_BADGE_ALERT.match(crop, similarity=0.75):
            return 'alert'
        if TEMPLATE_BADGE_DONE.match(crop, similarity=0.75):
            return 'done'
        return 'none'

    def hub_research_tag(self):
        """
        Returns:
            str: 'available', 'ongoing' or 'none'
        """
        crop = self.image_crop(_area_button(HUB_RESEARCH_TAG, 'TAG_REGION'), copy=False)
        if TEMPLATE_TAG_ONGOING.match(crop, similarity=0.75):
            return 'ongoing'
        if TEMPLATE_TAG_AVAILABLE.match(crop, similarity=0.75):
            return 'available'
        return 'none'

    def read_tactics_states(self):
        """
        Read the three skill cards on the Tactical Training screen via the
        opaque state bands only. Card level digits are NOT read: the card
        chips take the ship art's tint and their thin font defeats OCR.

        Returns:
            list[str]: per card 'trainable', 'researching' or 'other'
                ('other' = learned, maxed or empty slot; the main panel
                disambiguates after selecting the card).

        Pages:
            in: TACTICS_CHECK
        """
        results = []
        for card in TACTICS_SKILL_CARDS:
            crop = self.image_crop(card, copy=False)
            if TEMPLATE_SKILL_TRAINABLE.match(crop, similarity=0.70):
                results.append('trainable')
            elif TEMPLATE_SKILL_RESEARCHING.match(crop, similarity=0.70):
                results.append('researching')
            else:
                results.append('other')
        logger.info(f'Tactics card states: {results}')
        return results

    def skill_card_selected(self, index):
        """
        Returns:
            bool: The white triangle marker sits under card `index`.

        Pages:
            in: TACTICS_CHECK
        """
        return self.image_color_count(TACTICS_SELECT_MARKERS[index],
                                      color=(255, 255, 255), threshold=221, count=25)

    def skill_maxed_notice(self):
        """
        Returns:
            bool: The main panel shows "Current skill is already max level."

        Pages:
            in: TACTICS_CHECK
        """
        crop = self.image_crop(_area_button((710, 300, 1250, 480), 'MAXED_REGION'),
                               copy=False)
        return bool(TEMPLATE_SKILL_MAXED.match(crop, similarity=0.75))

    # -------------------------------------------------------------- tactics

    @property
    def t1_reserve(self):
        return int(self.config.MetaLab_T1BookReserve)

    def process_skill_card(self, index):
        """
        Select the skill card at `index` and drive it as far as possible:
        learn it if trainable (Information dialog, 5 red T3 books), then
        Quick Train it to max while books last. All decisions come from
        opaque, high-contrast UI: the learn dialog, the "already max level"
        notice, and the Quick Train button (present only for a selected
        skill that can be fed).

        Returns:
            str: 'maxed'         skill at max level
                 'in_progress'   skill active but not maxed (no/low books,
                                 or Quick Train disabled)
                 'out_of_books'  book reserve reached mid-feed
                 'empty'         slot has no (selectable) skill
                 'failed'        UI did not behave

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        logger.hr(f'Skill card {index + 1}', level=2)
        card = TACTICS_SKILL_CARDS[index]
        timeout = Timer(20, count=20).start()
        learn_clicks = 0
        self.interval_clear(TACTICS_CHECK)
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.info(f'Skill card {index + 1}: no reaction, treating as empty')
                return 'empty'

            # Learn / resume dialog
            if self.appear(LEARN_CHECK, offset=(20, 20)):
                if not self.config.MetaLab_ActivateSkills:
                    logger.info('Skill activation disabled, cancelling dialog')
                    self.ui_click(LEARN_CANCEL, check_button=TACTICS_CHECK,
                                  offset=(20, 20), skip_first_screenshot=True)
                    return 'in_progress'
                if self.learn_blocked:
                    logger.info(f'Skill card {index + 1}: no books left to learn with, '
                                'cancelling the dialog unopened')
                    self.ui_click(LEARN_CANCEL, check_button=TACTICS_CHECK,
                                  offset=(20, 20), skip_first_screenshot=True)
                    self.device.click_record_clear()
                    return 'cannot_learn'
                if learn_clicks >= LEARN_CONFIRM_TRIES:
                    # The dialog is still up after several confirms, so Confirm
                    # is doing nothing - it is greyed out. In practice that
                    # means the 5 red T3 books it wants are not there (live:
                    # "You don't have enough materials", 1/5, and the old code
                    # clicked it twelve times until ALAS called the game stuck
                    # and killed the run with 14 METAs still unvisited).
                    logger.warning(f'Skill card {index + 1}: the learn dialog will not '
                                   'confirm, almost always the 5 red T3 books it wants - '
                                   'no more learning this run')
                    self.learn_blocked = True
                    self.ui_click(LEARN_CANCEL, check_button=TACTICS_CHECK,
                                  offset=(20, 20), skip_first_screenshot=True)
                    # These are legitimate repeats across a roster of ships; the
                    # click safety counts them as one runaway button otherwise
                    self.device.click_record_clear()
                    return 'cannot_learn'
                if self.appear_then_click(LEARN_CONFIRM, offset=(20, 20), interval=3):
                    learn_clicks += 1
                timeout.reset()
                continue
            if self.handle_popup_confirm('SKILL_CARD'):
                timeout.reset()
                continue

            if not self.appear(TACTICS_CHECK, offset=(20, 20)):
                continue

            # The main panel only describes the SELECTED card - never judge
            # this card from a panel that belongs to another one
            if not self.skill_card_selected(index):
                if self.appear(TACTICS_CHECK, offset=(20, 20), interval=3):
                    self.device.click(card)
                continue

            # Selected skill already maxed
            if self.skill_maxed_notice():
                logger.info(f'Skill card {index + 1}: max level')
                return 'maxed'

            # Selected skill is idle: start researching it first
            if self.appear(BEGIN_RESEARCH, offset=(20, 20), interval=3):
                if not self.config.MetaLab_ActivateSkills:
                    logger.info('Skill idle but activation disabled')
                    return 'in_progress'
                self.device.click(BEGIN_RESEARCH)
                timeout.reset()
                continue

            # Selected skill can be fed
            if self.appear(QUICK_TRAIN, offset=(20, 20)):
                if not self.config.MetaLab_UseQuickTrain or self.books_exhausted:
                    return 'in_progress'
                result = self.quick_train_once()
                if result == 'fed':
                    timeout.reset()
                    continue
                if result == 'no_need':
                    # Level advanced; loop re-reads the panel (the maxed
                    # notice appears once the skill tops out)
                    timeout.reset()
                    self.device.sleep((0.5, 0.8))
                    continue
                if result == 'no_books':
                    return 'out_of_books'
                # 'failed': often the batch DID apply but a skill-max
                # celebration hid the result - re-read the panel so the
                # maxed notice can settle it; the card timeout bounds this
                self.device.sleep((0.8, 1.2))
                continue

            # Selected but no actionable panel content yet - wait for it
            continue

    # Learned T1 book EXP value; 0 = not calibrated yet this session
    t1_book_exp = 0

    def quick_train_once(self, skip_first_screenshot=True):
        """
        Open the Quick Train dialog and feed one batch of T1 books into the
        selected skill, respecting the book reserve, then close the dialog.

        The dialog STAYS OPEN after Confirm and the selected-count digits
        misread (thin font), so the flow is: click a deterministic number
        of +10/+1, Confirm, then verify by the owned-count delta - the only
        reliably readable number. The per-book EXP is calibrated from the
        first confirmed batch (owned delta vs progress delta).

        Returns:
            str: 'fed'         a batch was confirmed (owned count dropped)
                 'no_books'    T1 books at/below the reserve
                 'no_need'     nothing to feed (skill likely maxed)
                 'failed'      dialog did not behave, aborted

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        logger.hr('Quick train', level=2)
        # open the dialog
        timeout = Timer(10, count=10).start()
        self.interval_clear(QUICK_TRAIN)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.appear(QT_CHECK, offset=(20, 20)):
                break
            if timeout.reached():
                logger.warning('Quick Train dialog did not open')
                return 'failed'
            if self.appear(TACTICS_CHECK, offset=(20, 20), interval=3) \
                    and self.appear(QUICK_TRAIN, offset=(20, 20)):
                self.device.click(QUICK_TRAIN)
                continue

        # Let the dialog settle before reading or clicking
        self.device.sleep((0.8, 1.2))
        self.device.screenshot()
        owned = OCR_QT_T1_OWNED.ocr(self.device.image)
        current, _, total = OCR_QT_PROGRESS.ocr(self.device.image)
        allowance = owned - self.t1_reserve
        logger.info(f'Quick Train: T1 owned={owned}, reserve={self.t1_reserve}, '
                    f'progress {current}/{total}')
        if owned <= 0:
            logger.warning('Owned count unreadable, aborting dialog')
            self.qt_close()
            return 'failed'
        if allowance <= 0:
            self.qt_close()
            self.books_exhausted = True
            return 'no_books'
        if total <= 0 or current >= total:
            self.qt_close()
            return 'no_need'

        per_book = self.t1_book_exp if self.t1_book_exp else T1_BOOK_EXP_DEFAULT
        need_books = -(-(total - current) // per_book)  # ceil
        target = min(need_books, allowance, QT_BATCH_CAP)
        if not self.t1_book_exp:
            # Calibration batch: small, so a wrong default cannot overshoot
            target = min(target, 10)
        logger.info(f'Book EXP={per_book}{"" if self.t1_book_exp else " (default)"}, '
                    f'books needed={need_books}, target={target}')

        # Build up the selection with +10 / +1 clicks, no OCR verification
        remain = target
        clicks = 0
        while remain > 0 and clicks < 200:
            if remain >= 10:
                self.device.click(QT_T1_PLUS10)
                remain -= 10
            else:
                self.device.click(QT_T1_PLUS)
                remain -= 1
            clicks += 1
            if clicks % 8 == 0:
                self.device.click_record_clear()
            self.device.sleep((0.15, 0.25))
        self.device.click_record_clear()
        self.device.sleep((0.4, 0.7))

        logger.info(f'Quick Train confirm, spending up to {target} T1 books')
        self.device.click(QT_CONFIRM)
        self.device.sleep((1.2, 1.8))

        # Verify via owned delta; the dialog stays open after Confirm.
        # Maxing a skill throws a celebration overlay on top of the dialog
        # (the owned count then reads 0) - dismiss anything in the way.
        result = 'failed'
        confirm_timer = Timer(15, count=15).start()
        while 1:
            self.device.screenshot()
            if self.handle_popup_confirm('QUICK_TRAIN'):
                continue
            if self.handle_lab_info_dialog():
                continue
            if self.appear(QT_CHECK, offset=(20, 20)):
                owned_now = OCR_QT_T1_OWNED.ocr(self.device.image)
                if owned_now > 0:
                    spent = owned - owned_now
                    if 0 < spent <= target:
                        current_now, _, total_now = OCR_QT_PROGRESS.ocr(self.device.image)
                        logger.info(f'Quick Train spent {spent} books, progress '
                                    f'{current_now}/{total_now}')
                        if not self.t1_book_exp and total_now == total \
                                and current_now > current:
                            per = (current_now - current) / spent
                            if per >= 1 and abs(per - round(per)) < 0.01:
                                self.t1_book_exp = int(round(per))
                                logger.info(f'Calibrated T1 book EXP: {self.t1_book_exp}')
                        result = 'fed'
                        break
                # owned unreadable or unchanged yet: wait it out
            elif self.appear(TACTICS_CHECK, offset=(20, 20)):
                # Dialog closed on its own (possible on skill level max)
                logger.info('Quick Train dialog closed after confirm')
                return 'fed'
            else:
                # Neither dialog nor tactics screen: celebration overlay
                self.device.click(LAB_DISMISS)
                self.device.sleep((0.8, 1.2))
            if confirm_timer.reached():
                logger.warning('Quick Train confirm result not verified')
                break

        self.qt_close()
        return result

    def qt_close(self, skip_first_screenshot=True):
        timeout = Timer(10, count=10).start()
        self.interval_clear(QT_CLOSE)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if not self.appear(QT_CHECK, offset=(20, 20)):
                return
            if timeout.reached():
                logger.warning('qt_close timeout')
                return
            if self.appear_then_click(QT_CLOSE, offset=(20, 20), interval=3):
                continue

    def tactics_pass(self):
        """
        On the Tactical Training screen: make sure a skill is researching,
        then pump books into it until it maxes or books run dry; chain to
        the next trainable skill.

        Returns:
            bool: True if every skill of this ship is maxed.

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        self.device.screenshot()
        states = self.read_tactics_states()

        # Work the researching card first, and STOP at the first card that
        # is not finished: a ship has a single research slot, and both
        # learning a trainable skill and Begin Research on an idle one
        # STEAL it - touching further cards would switch the research away
        # from the unfinished skill (observed as run-to-run ping-ponging).
        order = [i for i, s in enumerate(states) if s == 'researching'] \
            + [i for i, s in enumerate(states) if s != 'researching']

        for index in order:
            result = self.process_skill_card(index)
            logger.info(f'Skill card {index + 1}: {result}')
            if result in ('maxed', 'empty', 'cannot_learn'):
                # A skill that could not be learned started nothing, so the
                # ship's one research slot is still free - the next card may be
                # an idle LEARNED skill, which costs nothing to start. Stopping
                # here would leave her banking no skill EXP at all.
                continue
            # 'in_progress', 'out_of_books' or 'failed': this card is the
            # active project until it maxes - leave the other cards alone
            return False
        logger.info('All skills maxed (or empty)')
        return True

    # --------------------------------------------------------- skill reading

    def inspect_skill_card(self, index):
        """
        Read-only counterpart of process_skill_card: select the card and
        judge it from the main panel, without learning, starting research
        or spending a single book. Selecting a card is the only click this
        makes - the panel describes the SELECTED card only, so there is no
        way around it.

        Returns:
            str: 'maxed'        the "already max level" notice is up
                 'researching'  the ship's research slot sits on this skill
                                (Quick Train offered)
                 'idle'         learned but not researching (Begin Research)
                 'trainable'    not learned yet (the learn dialog opened and
                                was cancelled)
                 'empty'        the slot has no (selectable) skill

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        card = TACTICS_SKILL_CARDS[index]
        timeout = Timer(12, count=12).start()
        self.interval_clear(TACTICS_CHECK)
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.info(f'Skill card {index + 1}: no reaction, treating as empty')
                return 'empty'

            # Selecting a trainable card opens the learn dialog. This pass
            # must not spend the 5 red T3 books, so cancel it out - a card
            # that offers to be learned is by definition not maxed.
            if self.appear(LEARN_CHECK, offset=(20, 20)):
                self.ui_click(LEARN_CANCEL, check_button=TACTICS_CHECK,
                              offset=(20, 20), skip_first_screenshot=True)
                return 'trainable'

            if not self.appear(TACTICS_CHECK, offset=(20, 20)):
                continue
            if not self.skill_card_selected(index):
                if self.appear(TACTICS_CHECK, offset=(20, 20), interval=3):
                    self.device.click(card)
                continue

            if self.skill_maxed_notice():
                return 'maxed'
            # Begin Research = learned but the research slot is elsewhere;
            # Quick Train = this is the skill the ship is researching
            if self.appear(BEGIN_RESEARCH, offset=(20, 20)):
                return 'idle'
            if self.appear(QUICK_TRAIN, offset=(20, 20)):
                return 'researching'

    def start_skill_research(self, index, allow_learn=True):
        """
        Put the ship's single research slot on skill card `index`: start
        research on a learned-but-idle skill (free), or learn a not-yet
        learned one (5 red T3 skill books) when `allow_learn`. Feeds no
        books - Quick Train is MetaLab's job.

        Returns:
            bool: True if the skill is researching afterwards.

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        logger.info(f'Starting research on skill card {index + 1}')
        card = TACTICS_SKILL_CARDS[index]
        timeout = Timer(20, count=20).start()
        self.interval_clear(TACTICS_CHECK)
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning(f'Skill card {index + 1}: research did not start')
                return False

            if self.appear(LEARN_CHECK, offset=(20, 20)):
                if not allow_learn:
                    logger.info('Skill not learned yet and learning is disabled')
                    self.ui_click(LEARN_CANCEL, check_button=TACTICS_CHECK,
                                  offset=(20, 20), skip_first_screenshot=True)
                    return False
                # Only a click buys more time - a dialog whose Confirm never
                # matches must run into the timeout, not loop forever
                if self.appear_then_click(LEARN_CONFIRM, offset=(20, 20), interval=3):
                    timeout.reset()
                continue
            if self.handle_popup_confirm('START_RESEARCH'):
                timeout.reset()
                continue

            if not self.appear(TACTICS_CHECK, offset=(20, 20)):
                continue
            if not self.skill_card_selected(index):
                if self.appear(TACTICS_CHECK, offset=(20, 20), interval=3):
                    self.device.click(card)
                continue

            if self.skill_maxed_notice():
                logger.info(f'Skill card {index + 1}: already max level')
                return False
            if self.appear(BEGIN_RESEARCH, offset=(20, 20), interval=3):
                self.device.click(BEGIN_RESEARCH)
                timeout.reset()
                continue
            # Quick Train only shows for the skill being researched
            if self.appear(QUICK_TRAIN, offset=(20, 20)):
                logger.info(f'Skill card {index + 1}: researching')
                return True

    def tactics_all_maxed(self, start_research=False, allow_learn=False):
        """
        Audit of the three skill cards on the Tactical Training screen.
        Nothing is fed here.

        Args:
            start_research (bool): When the ship's single research slot is
                idle (its previous skill maxed out, or no skill was ever
                started), put it on the next unfinished skill. Without this
                the audit is purely read-only and a ship whose researching
                skill maxed just stops making progress.
            allow_learn (bool): Also learn a not-yet-learned skill to start
                it, spending 5 red T3 skill books. Idle learned skills are
                always preferred - they are free.

        Returns:
            str: 'maxed'    every slot is maxed (empty slots ignored)
                 'unmaxed'  at least one skill can still be trained
                 'unknown'  the cards did not read out conclusively

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        self.device.screenshot()
        states = self.read_tactics_states()
        if 'researching' in states:
            # The one research slot is busy, there is nothing to start
            logger.info('Skills not maxed: a card is researching')
            return 'unmaxed'
        if not start_research and 'trainable' in states:
            logger.info('Skills not maxed: a card is trainable')
            return 'unmaxed'

        results = []
        for index in range(len(TACTICS_SKILL_CARDS)):
            result = self.inspect_skill_card(index)
            logger.info(f'Skill card {index + 1}: {result}')
            if result == 'researching':
                # Research slot busy: the ship progresses on her own
                return 'unmaxed'
            if result in ('idle', 'trainable') and not start_research:
                return 'unmaxed'
            results.append(result)
        if 'maxed' not in results and 'idle' not in results \
                and 'trainable' not in results:
            # Every card read 'empty': the screen never rendered what it
            # was supposed to - do not call that a maxed ship
            logger.warning(f'Skill states inconclusive: {results}')
            return 'unknown'
        if all(state in ('maxed', 'empty') for state in results):
            logger.info(f'All skills maxed: {results}')
            return 'maxed'

        # Nothing is researching but something can be: the slot is idle
        if start_research:
            wanted = ['idle', 'trainable'] if allow_learn else ['idle']
            for state in wanted:
                if state not in results:
                    continue
                index = results.index(state)
                logger.info(f'No skill is researching, starting the {state} '
                            f'skill on card {index + 1}')
                self.start_skill_research(index, allow_learn=allow_learn)
                break
            else:
                logger.info(f'No skill is researching and none can be started: '
                            f'{results}')
        return 'unmaxed'

    def check_skills_maxed(self, start_research=False, allow_learn=False):
        """
        Skill audit of the ship currently on the detail page: open her META
        Lab, look at the skill cards, back out again. Used by MetaLeveling
        to hold a level-finished ship in the fleet until her skills are
        done - and, with `start_research`, to keep her research slot on an
        unfinished skill meanwhile (see tactics_all_maxed).

        Returns:
            str: 'maxed', 'unmaxed' or 'unknown' (lab or tactics screen did
                not open, e.g. a non-META ship)

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        logger.hr('Check skills', level=2)
        if not self.lab_enter():
            logger.warning('META Lab did not open, skill state unknown')
            return 'unknown'

        if self.hub_goto(HUB_RESEARCH, TACTICS_CHECK):
            result = self.tactics_all_maxed(start_research=start_research,
                                            allow_learn=allow_learn)
            self.subscreen_back_to_hub()
        else:
            logger.warning('Tactical Research did not open, skill state unknown')
            result = 'unknown'

        self.lab_exit()
        return result

    # -------------------------------------------------------------- fortify

    def read_fortify_mats(self, index, timeout=3):
        """
        Read "Materials Needed: owned/needed" of category `index` (the
        panel docks under the selected category). Retries with fresh
        screenshots: the panel animates after a fortify click. A capped or
        locked category has no materials line at all -> returns zeros.

        Returns:
            tuple: (owned, needed), zeros when unreadable.

        Pages:
            in: FORTIFY_CHECK
        """
        timer = Timer(timeout, count=5).start()
        while 1:
            raw = str(OCR_FORTIFY_MATS[index].ocr(self.device.image)).replace(' ', '')
            match = re.search(r'(\d+)/(\d+)', raw)
            if match:
                return int(match.group(1)), int(match.group(2))
            if timer.reached():
                return 0, 0
            self.device.sleep((0.4, 0.6))
            self.device.screenshot()

    def fortify_pass(self):
        """
        On the Rigging Fortification screen: for each category, click
        Fortify while materials suffice and the count keeps dropping.
        Categories whose materials line is absent (capped or locked by hull
        type) are skipped.

        Pages:
            in: FORTIFY_CHECK
            out: unchanged
        """
        total_clicks = 0
        for index, category in enumerate(FORTIFY_CATEGORIES):
            self.device.click(category)
            self.device.sleep((1.0, 1.4))
            self.device.screenshot()
            while total_clicks < FORTIFY_CLICK_CAP:
                owned, need = self.read_fortify_mats(index)
                if need <= 0 or owned < need:
                    logger.info(f'Fortify {category.name}: stop (owned={owned}, '
                                f'need={need}) - capped, locked or insufficient')
                    break
                if not self.appear(FORTIFY_BUTTON, offset=(20, 20)):
                    logger.info(f'Fortify {category.name}: no Fortify button, capped')
                    break
                self.device.click(FORTIFY_BUTTON)
                total_clicks += 1
                if total_clicks % 8 == 0:
                    self.device.click_record_clear()
                self.device.sleep((1.0, 1.5))
                self.device.screenshot()
                if self.handle_popup_confirm('FORTIFY'):
                    self.device.screenshot()
                # Milestone reward dialog ("Fortification Rate reached X%")
                if self.handle_lab_info_dialog():
                    self.device.sleep((0.8, 1.2))
                    self.device.screenshot()
                new_owned, _ = self.read_fortify_mats(index)
                if new_owned >= owned:
                    logger.info(f'Fortify {category.name}: no material change, '
                                'category maxed or locked')
                    break
        self.device.click_record_clear()
        logger.info(f'Fortify pass done, {total_clicks} clicks')

    # ----------------------------------------------------------- activation

    def save_debug_screenshot(self, name):
        """
        Dump the current screenshot for offline diagnosis of screens the
        task does not understand yet. Folder is git-ignored.
        """
        import os
        import time
        from PIL import Image
        folder = './screenshots/meta_lab_debug'
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f'{name}_{int(time.time())}.png')
        Image.fromarray(self.device.image).save(path)
        logger.info(f'Debug screenshot saved: {path}')

    def read_activation_requirement(self):
        """
        Read the "Level Requirement: X/Y" line. The current level is GREEN
        when the requirement is met and RED when unmet - the color decides.

        Returns:
            tuple: (current, required_raw, met). required_raw is the raw
                white OCR of the "/required" part: its digits are not
                always exact, but they are stable per screen and CHANGE
                with every star tier - the only usable star-up signal for
                high-level ships (a Lv.120 reads "met" at every tier, so
                level+met alone cannot detect a successful activation).
                (0, '', False) when unreadable.

        Pages:
            in: somatic activation screen
        """
        green = str(OCR_ACT_REQ_GREEN.ocr(self.device.image)).replace(' ', '')
        match = re.search(r'(\d+)', green)
        if match:
            current, met = int(match.group(1)), True
        else:
            red = str(OCR_ACT_REQ_RED.ocr(self.device.image)).replace(' ', '')
            match = re.search(r'(\d+)', red)
            if not match:
                return 0, '', False
            current, met = int(match.group(1)), False
        required_raw = str(OCR_ACT_REQ_WHITE.ocr(self.device.image)).replace(' ', '')
        return current, required_raw, met

    def wait_activation_requirement(self, skip_first_screenshot=True):
        """
        Patient read of the level requirement line. A single-frame read
        races the screen: the subscreen fades in on entry, and right after
        an activation the star-up celebration covers the panel - one
        unreadable frame does NOT mean the requirement is gone (that race
        cut every activation chain to a single star). Wait the first
        frames out (overlays fade in dimmed; an early tap cancels
        dialogs), then tap between reads to clear celebrations, and only
        call the requirement gone after the full timeout (fully activated
        ships keep an empty requirement line).

        Returns:
            tuple: (current, required_raw, met), (0, '', False) when the
                requirement stayed unreadable for the whole timeout.

        Pages:
            in: somatic activation screen (possibly under an overlay)
        """
        timeout = Timer(12, count=8).start()
        unreadable = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.handle_lab_info_dialog():
                self.device.sleep((0.8, 1.2))
                continue
            if self.appear_then_click(ACT_POPUP_CONFIRM, offset=(30, 30), interval=3):
                continue
            if self.handle_info_bar():
                continue
            state = self.read_activation_requirement()
            if state[0] > 0:
                return state
            if timeout.reached():
                return 0, '', False
            unreadable += 1
            if unreadable > 2:
                self.device.click(LAB_DISMISS)
            self.device.sleep((0.8, 1.2))

    def activation_pass(self):
        """
        On the Somatic Activation screen: press Activation while the level
        requirement is met (star chains allowed). Stops when requirements
        are unmet or nothing changes. Never relies on the screen header
        (art-tinted); the requirement OCR and the red Activation button are
        the opaque anchors, and unreadable stretches are treated as the
        star-up celebration overlay.

        Pages:
            in: somatic activation screen
            out: unchanged (or celebration dismissed)
        """
        for _ in range(5):
            current, required_raw, met = self.wait_activation_requirement()
            if current <= 0:
                logger.info('No level requirement read, activation complete or unreadable')
                return
            if not met:
                logger.info(f'Activation level requirement not met (level {current})')
                return
            logger.info(f'Activation requirement met (level {current}, '
                        f'tier {required_raw}), activating')
            before = (met, required_raw)
            timeout = Timer(30, count=20).start()
            self.interval_clear(ACT_BUTTON)
            unreadable = 0
            clicked = 0
            dialog_logged = False
            while 1:
                self.device.screenshot()
                if timeout.reached():
                    logger.warning('activation confirm timeout')
                    return
                if self.handle_popup_confirm('ACTIVATION'):
                    continue
                # "Are you sure you want this ship to undergo Somatic
                # Activation?" Info dialog (appears at higher star tiers)
                if self.appear_then_click(ACT_POPUP_CONFIRM, offset=(30, 30), interval=3):
                    timeout.reset()
                    continue
                if self.handle_lab_info_dialog():
                    continue
                if self.handle_info_bar():
                    continue
                state = self.read_activation_requirement()
                if state[0] > 0:
                    unreadable = 0
                    if (state[2], state[1]) != before:
                        logger.info(f'Activation done, requirement state now {state}')
                        break
                    if clicked >= 2:
                        # Two clicks, an unknown dialog each time, no state
                        # change: activation is blocked (insufficient META
                        # Crystals or an unhandled confirmation). Do not
                        # keep cancel-looping.
                        logger.warning('Activation blocked by an unrecognized dialog, '
                                       'skipping (see debug screenshot)')
                        return
                    if self.appear_then_click(ACT_BUTTON, offset=(20, 20), interval=4):
                        clicked += 1
                        continue
                    continue
                # Requirement unreadable: either the star-up celebration or
                # a dialog covers the screen.
                unreadable += 1
                if unreadable <= 2:
                    # Dialogs fade in dimmed (with a loading spinner) and
                    # cannot be matched yet - wait for the screen to settle
                    # instead of tapping, or the tap cancels the dialog.
                    self.device.sleep((0.8, 1.2))
                    continue
                if clicked and not dialog_logged:
                    self.save_debug_screenshot('activation_dialog')
                    dialog_logged = True
                self.device.click(LAB_DISMISS)
                self.device.sleep((1.0, 1.4))
                if unreadable >= 8:
                    # Celebration outlasted this loop: hand back to the
                    # chain loop, whose patient read decides between a
                    # next tier and a truly finished ship.
                    logger.info('Requirement still covered, re-reading for a next tier')
                    break

    # ------------------------------------------------------------- per ship

    def process_ship(self):
        """
        Full lab upkeep for the ship currently opened on the detail page.

        Returns:
            str: 'done' ship fully processed, 'skip' not a META/failed

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        logger.hr('Process ship', level=2)
        level = self.get_detail_level()
        # Advisory only: the detail skill strip sits on a semi-transparent
        # panel and is unreadable on ships with bright art. The tactics
        # screen (opaque panel) is the authoritative skill reader.
        skills = self.read_detail_skills()
        logger.info(f'Ship level {level}, detail skills (advisory): {skills}')

        if not self.lab_enter():
            return 'skip'

        # Hub state
        self.device.screenshot()
        activation_badge = self.hub_badge(HUB_ACTIVATION_BADGE)
        fortify_badge = self.hub_badge(HUB_FORTIFY_BADGE)
        research_tag = self.hub_research_tag()
        logger.info(f'Hub: activation={activation_badge}, fortify={fortify_badge}, '
                    f'research={research_tag}')

        # Skills
        if self.config.MetaLab_ActivateSkills or self.config.MetaLab_UseQuickTrain:
            if self.hub_goto(HUB_RESEARCH, TACTICS_CHECK):
                self.tactics_pass()
                self.subscreen_back_to_hub()

        # Rigging fortification (header is art-tinted -> left-hub entry,
        # the pass guards itself with the materials OCR + Fortify button)
        if self.config.MetaLab_DoFortify and fortify_badge == 'alert':
            if self.hub_goto_subscreen(HUB_FORTIFY):
                self.fortify_pass()
                self.subscreen_back_to_hub()

        # Somatic activation (same: left-hub entry, requirement OCR anchors)
        if self.config.MetaLab_DoActivation and activation_badge == 'alert':
            if self.hub_goto_subscreen(HUB_ACTIVATION):
                self.activation_pass()
                self.subscreen_back_to_hub()
        elif self.config.MetaLab_DoActivation and activation_badge == 'none':
            # The game only shows the '!' badge when a star-up is possible
            # NOW: level requirement met AND enough of THIS ship's own META
            # Crystals (they are per-ship items, not a shared pool).
            logger.info('No activation offered by the game (level requirement '
                        "unmet or this ship's META Crystals are short)")

        self.lab_exit()
        return 'done'

    # ------------------------------------------------------------------ run

    def lab_assets_ready(self):
        import os
        files = [
            META_LAB_CHECK.file, HUB_ACTIVATION.file, HUB_RESEARCH.file,
            HUB_FORTIFY.file, TACTICS_CHECK.file, QUICK_TRAIN.file,
            QT_CHECK.file, QT_CONFIRM.file, FORTIFY_BUTTON.file,
            ACT_CHECK.file, ACT_BUTTON.file, DETAIL_RESEARCH_TAB.file,
        ]
        missing = [f for f in files if not os.path.exists(f)]
        if missing:
            logger.warning(f'MetaLab assets missing: {missing}')
            return False
        return True

    @staticmethod
    def dock_row_offset(image):
        """
        How far the card rows have slid from where the grid expects them.

        The dock is a free-scrolling list: a drag stops it wherever the stroke
        ended, not on a row boundary, so after the first page every row sits
        tens of pixels below its nominal y. Reading the Lv badge at the nominal
        position then lands on the row above - on its NAME BAND, which is white
        text and reads as "a card is here". That is how the 2026-07-29 03:22
        run managed to see 21 cards on page 1 and then declare page 2 empty
        with 21 METAs still below it: every crop on that page was aimed at the
        wrong band.

        The rows are located by the dark gaps between them instead. Ship art
        can be any colour, but the background between two rows is always a
        near-black stripe across the whole width of the list.

        Args:
            image: Screenshot.

        Returns:
            int: Pixels to add to the grid's y, in (-pitch/2, pitch/2].
                0 when nothing convincing was found, which is also the right
                answer for an unscrolled dock.
        """
        column = np.array(image)[:, LAB_GAP_X[0]:LAB_GAP_X[1]].astype(int).mean(axis=(1, 2))
        top, bottom = LAB_GAP_SEARCH
        dark = column[top:bottom] < LAB_GAP_DARK

        runs = []
        y = 0
        while y < len(dark):
            if not dark[y]:
                y += 1
                continue
            start = y
            while y < len(dark):
                if dark[y]:
                    y += 1
                    continue
                # A thin bright sliver inside a gap - a card corner, a scrollbar
                # tick - must not split it in two
                gap = y
                while gap < len(dark) and not dark[gap]:
                    gap += 1
                if gap - y < LAB_GAP_MERGE and gap < len(dark):
                    y = gap
                    continue
                break
            runs.append((start + top, y + top))

        if not runs:
            return 0
        start, end = max(runs, key=lambda r: r[1] - r[0])
        if end - start < LAB_GAP_MERGE:
            return 0
        # Any genuine gap answers the same question once taken modulo the pitch,
        # so it does not matter which one the window caught.
        offset = (start - LAB_GAP_ANCHOR) % LAB_CARD_ROW_PITCH
        if offset > LAB_CARD_ROW_PITCH / 2:
            offset -= LAB_CARD_ROW_PITCH
        return int(offset)

    @cached_property
    def _lab_grids(self):
        return {0: (LAB_CARD_GRIDS, LAB_CARD_LEVEL_GRIDS)}

    def lab_grids(self, offset):
        """
        The card grid and its Lv-badge crop, shifted by `offset` pixels.

        Cached per offset: ButtonGrid.move() rebuilds every button, and a
        sweep asks for the same offset once per card.
        """
        if offset not in self._lab_grids:
            cards = LAB_CARD_GRIDS.move((0, offset), name='LAB_CARD')
            self._lab_grids[offset] = (cards, LAB_CARD_LEVEL_GRIDS.move((0, offset),
                                                                        name='LAB_LEVEL'))
        return self._lab_grids[offset]

    def dock_card_present(self, index, offset=0):
        """
        Whether grid cell `index` holds a card, judged on its NAME BAND.

        The band is an opaque chip with white text that every card carries and
        the blurred dock background never does. The Lv badge this used to read
        is not safe: bright ship art clears the same white threshold, so a
        misaligned crop landing on the row above scored it as a card, and a
        dark card could score as empty. The band separates the two absolutely -
        310+ white pixels against 0.
        """
        cards, _ = self.lab_grids(offset)
        x0, y0 = cards.buttons[index].area[0], cards.buttons[index].area[1]
        left, top, right, bottom = LAB_NAME_BAND
        area = (x0 + left, y0 + top, x0 + right, min(y0 + bottom, 719))
        if area[3] - area[1] < 10:
            logger.warning(f'Card {index + 1} name band is off screen, cannot judge it')
            return False
        button = Button(area=area, color=(), button=area, name=f'LAB_NAME_{index}')
        return self.image_color_count(button, color=(255, 255, 255),
                                      threshold=221, count=LAB_NAME_WHITE)

    def dock_card_present_settled(self, index, offset=0):
        """
        dock_card_present with one retry on a fresh screenshot. Right after
        a page scroll the list may still be gliding, and a single mid-glide
        frame reading 'empty' would end the whole dock sweep (observed
        live: a run stopped at 14 ships while two more pages of METAs sat
        below the fold).
        """
        if self.dock_card_present(index, offset):
            return True
        self.device.sleep((1.0, 1.4))
        self.device.screenshot()
        return self.dock_card_present(index, self.dock_row_offset(self.device.image))

    def dock_drag_rows(self, rows, correction=0):
        """
        Move the dock down by `rows` whole card rows.

        Split into strokes of at most LAB_DRAG_ROWS_MAX rows - a single
        3-row stroke would have to start below the card area to finish
        above the top of the screen.

        Args:
            rows (int):
            correction (int): Extra pixels, to cancel the drift the last page
                was found to have. Dragging rows*pitch every time lets that
                drift accumulate until whole rows fall between two pages; adding
                the measured offset lands the next page back on the grid.
        """
        logger.info(f'Dock drag {rows} rows (correction {correction:+}px)')
        first = True
        while rows > 0:
            step = min(rows, LAB_DRAG_ROWS_MAX)
            distance = LAB_CARD_ROW_PITCH * step + (correction if first else 0)
            first = False
            start = np.array([LAB_DRAG_X, LAB_DRAG_Y])
            self.device.drag(start, start - np.array([0, distance]),
                             point_random=(-5, -5, 5, 5))
            # Dozens of consecutive drags are legitimate here; without this
            # the 12-same-button safety raises GameTooManyClickError
            self.device.click_record_clear()
            self.device.sleep((0.9, 1.3))
            rows -= step

    def dock_enter_card(self, button):
        """
        From page_dock, open a dock card's ship detail page.

        The click lands inside the visible list only. When the row offset is
        negative the top row's grid cell reaches above LAB_CARD_TOP, into the
        dock's header - and the click point is random inside the cell, so
        sooner or later it lands there. On the 2026-07-29 13:06 run, page 3
        sat -82px off the grid and the draw came up y=5, on the dock's
        filter button: the filter panel opened, DOCK_CHECK stopped
        appearing, ui_click had nothing left to click and the task sat there
        until GameStuckError restarted the game.

        Pages:
            in: page_dock
            out: SHIP_DETAIL_CHECK
        """
        button = self.dock_card_clickable(button)
        self.ui_click(button, appear_button=DOCK_CHECK, check_button=SHIP_DETAIL_CHECK,
                      skip_first_screenshot=True)

    @staticmethod
    def dock_card_clickable(button):
        """
        A card button trimmed to the part of it the dock actually shows.

        Args:
            button (Button):

        Returns:
            Button: The same card, its area clipped to the list viewport.
                Never degenerate: the largest offset the grid can carry
                (-pitch/2) still leaves ~90px of card below LAB_CARD_TOP.
        """
        x1, y1, x2, y2 = button.area
        area = (x1, max(y1, LAB_CARD_TOP), x2, min(y2, LAB_CARD_VIEW_BOTTOM))
        if area == button.area:
            return button
        return Button(area=area, color=(), button=area, name=button.name)

    def run(self):
        if not self.lab_assets_ready():
            logger.critical('MetaLab assets are missing, task cannot run.')
            logger.critical('MetaLab disables itself now.')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        self.books_exhausted = False
        self.learn_blocked = False
        logger.hr('META Lab pass', level=1)
        self.ui_ensure(page_dock)
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(faction='meta')

        # Iterate dock cards by grid position, paging with the scroll bar.
        # Detail-page swipes (ship_view_next) are NOT usable here: entering
        # the META Lab and backing out drops the dock browsing context and
        # every swipe lands on the same ship.
        processed = 0
        pages = 0
        empty_pages = 0
        while 1:
            self.device.screenshot()
            if self.appear(DOCK_EMPTY, offset=(20, 20)):
                logger.info('No META ships in dock')
                break
            page_seen = 0
            offset = self.dock_row_offset(self.device.image)
            cards, _ = self.lab_grids(offset)
            if offset:
                logger.info(f'Dock rows sit {offset:+}px off the grid, reading them there')
            for index in range(cards.grid_shape[0] * LAB_ROWS_PER_PAGE):
                self.device.screenshot()
                if not self.dock_card_present_settled(index, offset):
                    # A card that reads absent is a hole in the page, not
                    # the end of the roster. Ending the sweep on the first
                    # one cost the 2026-07-28 run 25 ships: it stopped at
                    # "Dock card 4 empty" with half the dock still below.
                    # An entirely empty page is the honest stop signal.
                    # Holes are also how a card whose Lv badge is covered
                    # (a celebration toast can sit over one for a whole
                    # session) drops out - logged so a ship that silently
                    # goes unprocessed is visible in the run log.
                    logger.info(f'Dock card {index + 1} not readable, skipped')
                    continue
                self.dock_enter_card(cards.buttons[index])
                self.process_ship()
                processed += 1
                page_seen += 1
                self.device.click_record_clear()
                self.lb_exit_to_dock()
                if processed >= LAB_SWEEP_SHIP_CAP:
                    break
            pages += 1
            logger.info(f'Dock page {pages}: {page_seen} ships ({processed} total)')
            if not page_seen:
                # An empty page is only the end of the roster if the list agrees
                # it has nothing more to show. Believing the cards alone is what
                # let one bad page-alignment end the 03:22 run with half the
                # METAs unvisited.
                self.device.screenshot()
                if DOCK_SCROLL.appear(main=self) and not DOCK_SCROLL.at_bottom(main=self):
                    logger.warning('No card read on this page but the dock is not at the '
                                   'bottom - nudging the list and trying once more')
                    if empty_pages:
                        logger.warning('Still nothing readable, giving up the sweep here')
                        break
                    empty_pages += 1
                    self.dock_drag_rows(1)
                    continue
                logger.info('Empty dock page, end of ship list')
                break
            empty_pages = 0
            if processed >= LAB_SWEEP_SHIP_CAP:
                logger.warning('MetaLab ship limit reached')
                break
            if pages >= LAB_SWEEP_PAGE_CAP:
                logger.warning('MetaLab page limit reached')
                break
            self.device.screenshot()
            if DOCK_SCROLL.appear(main=self) and DOCK_SCROLL.at_bottom(main=self):
                logger.info('End of dock, no further pages')
                break
            # Advance by exactly the rows just read, so no card falls
            # between two pages. A final page shorter than a full viewport
            # clamps at the bottom and repeats a few ships instead of
            # skipping them - a re-visit finds the research already running
            # and the badges cleared, so it costs seconds and changes
            # nothing.
            # Cancel the drift this page was found to have, so the next one
            # lands back on the grid instead of accumulating it.
            self.dock_drag_rows(LAB_ROWS_PER_PAGE, correction=offset)
        logger.info(f'META Lab pass processed {processed} ships')

        logger.hr('META Lab pass exit', level=1)
        self.dock_filter_set(wait_loading=False)
        self.config.task_delay(server_update=True)

    def lb_exit_to_dock(self, skip_first_screenshot=True):
        """
        Back out from ship detail to page_dock.
        """
        timeout = Timer(10, count=10).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.ui_page_appear(page_dock):
                return
            if timeout.reached():
                logger.warning('lb_exit_to_dock timeout, using ui_ensure')
                self.ui_ensure(page_dock)
                return
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=3):
                self.device.click(BACK_ARROW)
                continue
