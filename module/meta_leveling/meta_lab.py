import re

from module.base.button import Button
from module.base.timer import Timer
from module.logger import logger
from module.meta_leveling.assets import *
from module.ocr.ocr import Digit, DigitCounter, Ocr
from module.retire.assets import DOCK_EMPTY, SHIP_DETAIL_CHECK
from module.retire.dock import Dock
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
TACTICS_SKILL_LEVELS = [
    _area_button((752, 613, 862, 637), 'TACTICS_SKILL_LEVEL_1'),
    _area_button((937, 613, 1047, 637), 'TACTICS_SKILL_LEVEL_2'),
    _area_button((1122, 613, 1232, 637), 'TACTICS_SKILL_LEVEL_3'),
]

# --- quick train dialog ---
# "current[+added]/total" line: the white DigitCounter reads current/total,
# the green [+added] part is dropped by the letter filter (validated offline)
OCR_QT_PROGRESS = DigitCounter(_area_button((790, 158, 970, 184), 'QT_PROGRESS'),
                               letter=(255, 255, 255), threshold=128,
                               name='OCR_QT_PROGRESS')
OCR_QT_T1_SELECTED = Digit(_area_button((618, 296, 750, 330), 'QT_T1_SELECTED'),
                           letter=(255, 255, 255), threshold=128, name='OCR_QT_T1_SELECTED')
OCR_QT_T1_OWNED = Digit(_area_button((366, 313, 407, 335), 'QT_T1_OWNED'),
                        letter=(255, 255, 255), threshold=128, name='OCR_QT_T1_OWNED')
OCR_QT_T2_OWNED = Digit(_area_button((366, 417, 407, 439), 'QT_T2_OWNED'),
                        letter=(255, 255, 255), threshold=128, name='OCR_QT_T2_OWNED')
# Single minus of the T1 row, click position only (not cut as an asset)
QT_T1_MINUS = _area_button((555, 288, 602, 334), 'QT_T1_MINUS')

# --- rigging fortification screen ---
# "Materials Needed: 7192/4" -> owned/needed. Grey digits on the light bar.
# NOT a DigitCounter: that class clamps current to total, and owned is
# usually far larger than needed.
OCR_FORTIFY_MATS = Ocr(_area_button((248, 582, 322, 608), 'FORTIFY_MATS'),
                       lang='azur_lane', letter=(100, 100, 105), threshold=96,
                       alphabet='0123456789/', name='OCR_FORTIFY_MATS')
# Category click points across the top of the fortification screen
FORTIFY_CATEGORIES = [
    _area_button((190, 320, 250, 370), 'FORTIFY_CAT_1'),
    _area_button((455, 320, 515, 370), 'FORTIFY_CAT_2'),
    _area_button((745, 320, 805, 370), 'FORTIFY_CAT_3'),
    _area_button((995, 320, 1055, 370), 'FORTIFY_CAT_4'),
]

# --- somatic activation screen ---
# "Level Requirement: 1/10". The current level is RED when unmet and light
# when met, the "/required" part is always light -> two reads (validated
# offline): white catches "/10" (unmet) or "12/10" (met), red catches the
# unmet current.
OCR_ACT_REQ_WHITE = Ocr(_area_button((995, 525, 1050, 550), 'ACT_REQ_WHITE'),
                        lang='azur_lane', letter=(255, 255, 255), threshold=128,
                        alphabet='0123456789/', name='OCR_ACT_REQ_WHITE')
OCR_ACT_REQ_RED = Ocr(_area_button((995, 525, 1050, 550), 'ACT_REQ_RED'),
                      lang='azur_lane', letter=(230, 60, 50), threshold=128,
                      alphabet='0123456789', name='OCR_ACT_REQ_RED')

# Badge search regions on the lab hub (top-left corner of the hub boxes)
HUB_ACTIVATION_BADGE = (195, 195, 265, 265)
HUB_FORTIFY_BADGE = (1010, 175, 1085, 240)
# State tag under TACTICAL RESEARCH
HUB_RESEARCH_TAG = (1170, 395, 1270, 425)

SKILL_MAX_LEVEL = 10
# EXP one T1 META Universal Skill Book gives. Used until the task has
# observed one confirmed batch and calibrated the real value from the
# progress counter delta.
T1_BOOK_EXP_DEFAULT = 100
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
            if self.is_in_lab():
                self.device.click(BACK_ARROW)
                self.device.sleep((1.0, 1.4))
                continue
            if self.handle_popup_cancel('LAB_EXIT'):
                continue

    def hub_goto(self, button, check, skip_first_screenshot=True):
        """
        From the hub, enter a subscreen.

        Args:
            button (Button): hub box to click (HUB_ACTIVATION etc.)
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
            if self.is_in_lab():
                self.device.click(BACK_ARROW)
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

    def read_tactics_skills(self):
        """
        Read the three skill cards on the Tactical Training screen.

        Returns:
            list[dict]: per card {'state': 'trainable'|'researching'|
                'learned'|'empty', 'level': int|None}

        Pages:
            in: TACTICS_CHECK
        """
        results = []
        image = self.device.image
        for card, level_area in zip(TACTICS_SKILL_CARDS, TACTICS_SKILL_LEVELS):
            crop = self.image_crop(card, copy=False)
            state = 'learned'
            if TEMPLATE_SKILL_TRAINABLE.match(crop, similarity=0.70):
                state = 'trainable'
            elif TEMPLATE_SKILL_RESEARCHING.match(crop, similarity=0.70):
                state = 'researching'
            level_ocr = Ocr(level_area, lang='cnocr', letter=(255, 255, 255),
                            threshold=128, alphabet='0123456789LEVEL:? ',
                            name='TACTICS_SKILL_LEVEL')
            level = self._skill_level_from_text(str(level_ocr.ocr(image)))
            if state == 'learned' and level is None:
                state = 'empty'
            results.append({'state': state, 'level': level})
        logger.info(f'Tactics skills: {results}')
        return results

    # -------------------------------------------------------------- tactics

    @property
    def t1_reserve(self):
        return int(self.config.MetaLab_T1BookReserve)

    def learn_skill(self, card, skip_first_screenshot=True):
        """
        Click a trainable skill card and confirm the learn dialog
        (spends 5 red T3 skill books).

        Returns:
            bool: learned

        Pages:
            in: TACTICS_CHECK
            out: TACTICS_CHECK
        """
        logger.hr('Learn skill', level=2)
        confirmed = False
        timeout = Timer(15, count=15).start()
        self.interval_clear(TACTICS_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('learn_skill timeout')
                return False
            if self.appear(LEARN_CHECK, offset=(20, 20)):
                if self.appear_then_click(LEARN_CONFIRM, offset=(20, 20), interval=3):
                    confirmed = True
                continue
            if confirmed and self.appear(TACTICS_CHECK, offset=(20, 20)):
                return True
            if not confirmed and self.appear(TACTICS_CHECK, offset=(20, 20), interval=3):
                self.device.click(card)
                continue
            if self.handle_popup_confirm('LEARN_SKILL'):
                continue

    # Learned T1 book EXP value; 0 = not calibrated yet this session
    t1_book_exp = 0
    # (current, total, selected) of the last confirmed batch, for calibration
    _qt_last = None

    def _qt_calibrate(self, current, total):
        """
        After a confirmed batch, derive the per-book EXP from the counter
        delta of the previous round. Only possible when the skill did not
        level up in between (same total, grown current).
        """
        if self.t1_book_exp or not self._qt_last:
            return
        last_current, last_total, last_selected = self._qt_last
        if total == last_total and current > last_current and last_selected > 0:
            per_book = (current - last_current) / last_selected
            if per_book >= 1 and abs(per_book - round(per_book)) < 0.01:
                self.t1_book_exp = int(round(per_book))
                logger.info(f'Calibrated T1 book EXP: {self.t1_book_exp}')

    def quick_train_once(self, skip_first_screenshot=True):
        """
        Open the Quick Train dialog and feed T1 books into the researching
        skill, aiming to complete the current skill level, respecting the
        book reserve. Until the per-book EXP has been calibrated from an
        observed batch, batches are capped at 10 books.

        Returns:
            str: 'fed'         confirmed a book batch
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

        owned = OCR_QT_T1_OWNED.ocr(self.device.image)
        current, _, total = OCR_QT_PROGRESS.ocr(self.device.image)
        self._qt_calibrate(current, total)
        allowance = owned - self.t1_reserve
        logger.info(f'Quick Train: T1 owned={owned}, reserve={self.t1_reserve}, '
                    f'progress {current}/{total}')
        if allowance <= 0:
            self.qt_close()
            self.books_exhausted = True
            return 'no_books'
        if total <= 0 or current >= total:
            self.qt_close()
            return 'no_need'

        per_book = self.t1_book_exp if self.t1_book_exp else T1_BOOK_EXP_DEFAULT
        need_books = -(-(total - current) // per_book)  # ceil
        target = min(need_books, allowance)
        if not self.t1_book_exp:
            # Calibration round: small batch so a wrong default cannot
            # overshoot by much
            target = min(target, 10)
        logger.info(f'Book EXP={per_book}{"" if self.t1_book_exp else " (default)"}, '
                    f'books needed={need_books}, target={target}')

        # Build up the selection with +10 / +1 clicks
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
            self.device.sleep((0.10, 0.18))
        self.device.click_record_clear()
        self.device.sleep((0.4, 0.7))
        self.device.screenshot()
        selected = OCR_QT_T1_SELECTED.ocr(self.device.image)
        if selected <= 0:
            logger.warning('Quick Train selection reads 0 after clicking, aborting')
            self.qt_close()
            return 'failed'
        if selected > allowance:
            logger.warning(f'Quick Train selection {selected} exceeds allowance '
                           f'{allowance}, aborting')
            self.qt_close()
            return 'failed'

        logger.info(f'Quick Train confirm, spending {selected} T1 books')
        self._qt_last = (current, total, selected)
        timeout = Timer(15, count=15).start()
        self.interval_clear(QT_CONFIRM)
        while 1:
            self.device.screenshot()
            if not self.appear(QT_CHECK, offset=(20, 20)) \
                    and self.appear(TACTICS_CHECK, offset=(20, 20)):
                return 'fed'
            if timeout.reached():
                logger.warning('Quick Train confirm timeout')
                self.qt_close()
                return 'failed'
            if self.appear(QT_CHECK, offset=(20, 20)) \
                    and self.appear_then_click(QT_CONFIRM, offset=(20, 20), interval=3):
                continue
            if self.handle_popup_confirm('QUICK_TRAIN'):
                continue

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
        for _ in range(QT_ROUND_CAP):
            self.device.screenshot()
            skills = self.read_tactics_skills()
            states = [s['state'] for s in skills]
            levels = [s['level'] for s in skills]

            all_maxed = all(
                s['state'] not in ('trainable', 'researching')
                and s['level'] == SKILL_MAX_LEVEL
                for s in skills if s['state'] != 'empty'
            ) and any(s['state'] != 'empty' for s in skills)
            if all_maxed:
                logger.info('All skills maxed')
                return True

            if 'researching' in states:
                if not self.config.MetaLab_UseQuickTrain or self.books_exhausted:
                    logger.info('Skill researching, Quick Train disabled/exhausted')
                    return False
                result = self.quick_train_once()
                if result == 'fed':
                    continue
                if result == 'no_need':
                    # level may have advanced; loop re-reads the cards
                    continue
                return False

            if 'trainable' in states and self.config.MetaLab_ActivateSkills:
                index = states.index('trainable')
                if not self.learn_skill(TACTICS_SKILL_CARDS[index]):
                    return False
                continue

            logger.info(f'No actionable skill (states={states}, levels={levels})')
            return False
        logger.warning('tactics_pass round cap reached')
        return False

    # -------------------------------------------------------------- fortify

    def read_fortify_mats(self):
        """
        Returns:
            tuple: (owned, needed), zeros when unreadable.

        Pages:
            in: FORTIFY_BUTTON visible
        """
        raw = str(OCR_FORTIFY_MATS.ocr(self.device.image)).replace(' ', '')
        match = re.search(r'(\d+)/(\d+)', raw)
        if match:
            return int(match.group(1)), int(match.group(2))
        logger.warning(f'Cannot parse fortify materials: {raw}')
        return 0, 0

    def fortify_pass(self):
        """
        On the Rigging Fortification screen: for each category, click
        Fortify while the material count keeps dropping.

        Pages:
            in: FORTIFY_BUTTON visible
            out: unchanged
        """
        total_clicks = 0
        for category in FORTIFY_CATEGORIES:
            self.device.click(category)
            self.device.sleep((0.8, 1.2))
            self.device.screenshot()
            while total_clicks < FORTIFY_CLICK_CAP:
                owned, need = self.read_fortify_mats()
                if need <= 0 or owned < need:
                    logger.info(f'Fortify {category.name}: stop (owned={owned}, need={need})')
                    break
                self.device.click(FORTIFY_BUTTON)
                total_clicks += 1
                if total_clicks % 8 == 0:
                    self.device.click_record_clear()
                self.device.sleep((0.6, 0.9))
                self.device.screenshot()
                if self.handle_popup_confirm('FORTIFY'):
                    self.device.screenshot()
                new_owned, _ = self.read_fortify_mats()
                if new_owned >= owned:
                    logger.info(f'Fortify {category.name}: no material change, '
                                'category maxed or locked')
                    break
        self.device.click_record_clear()
        logger.info(f'Fortify pass done, {total_clicks} clicks')

    # ----------------------------------------------------------- activation

    def read_activation_requirement(self):
        """
        Read the "Level Requirement: X/Y" line. The current value is red
        when unmet and light when met.

        Returns:
            tuple: (current, required, met) with 0s when unreadable.

        Pages:
            in: ACT_CHECK
        """
        white = str(OCR_ACT_REQ_WHITE.ocr(self.device.image)).replace(' ', '')
        match = re.search(r'(\d*)/(\d+)', white)
        if not match:
            return 0, 0, False
        required = int(match.group(2))
        if match.group(1):
            # current is light -> requirement met
            return int(match.group(1)), required, True
        red = str(OCR_ACT_REQ_RED.ocr(self.device.image)).replace(' ', '')
        match = re.search(r'(\d+)', red)
        current = int(match.group(1)) if match else 0
        return current, required, False

    def activation_pass(self):
        """
        On the Somatic Activation screen: press Activation while the level
        requirement is met (star chains allowed). Stops when requirements
        are unmet or nothing changes.

        Pages:
            in: ACT_CHECK
            out: ACT_CHECK
        """
        for _ in range(5):
            self.device.screenshot()
            current, required, met = self.read_activation_requirement()
            if required <= 0:
                logger.info('No level requirement read, activation complete or unreadable')
                return
            if not met:
                logger.info(f'Activation level requirement not met ({current}/{required})')
                return
            logger.info(f'Activation requirements met ({current}/{required}), activating')
            before = (current, required)
            timeout = Timer(15, count=15).start()
            self.interval_clear(ACT_BUTTON)
            while 1:
                self.device.screenshot()
                if timeout.reached():
                    logger.warning('activation confirm timeout')
                    return
                if self.handle_popup_confirm('ACTIVATION'):
                    continue
                if self.handle_info_bar():
                    continue
                if self.appear(ACT_CHECK, offset=(20, 20)):
                    current2, required2, _ = self.read_activation_requirement()
                    if (current2, required2) != before:
                        logger.info('Activation done, requirements now '
                                    f'{current2}/{required2}')
                        break
                    if self.appear_then_click(ACT_BUTTON, offset=(20, 20), interval=4):
                        continue

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
        skills = self.read_detail_skills()
        logger.info(f'Ship level {level}, skills {skills}')

        skills_maxed = all(s == SKILL_MAX_LEVEL for s in skills if s != 'empty') \
            and any(s != 'empty' for s in skills)

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
        if not skills_maxed and (self.config.MetaLab_ActivateSkills
                                 or self.config.MetaLab_UseQuickTrain):
            if self.hub_goto(HUB_RESEARCH, TACTICS_CHECK):
                self.tactics_pass()
                self.subscreen_back_to_hub()

        # Rigging fortification
        if self.config.MetaLab_DoFortify and fortify_badge == 'alert':
            if self.hub_goto(HUB_FORTIFY, FORTIFY_BUTTON):
                self.fortify_pass()
                self.subscreen_back_to_hub()

        # Somatic activation
        if self.config.MetaLab_DoActivation and activation_badge == 'alert':
            if self.hub_goto(HUB_ACTIVATION, ACT_CHECK):
                self.activation_pass()
                self.subscreen_back_to_hub()

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

    def run(self):
        if not self.lab_assets_ready():
            logger.critical('MetaLab assets are missing, task cannot run.')
            logger.critical('MetaLab disables itself now.')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        self.books_exhausted = False
        logger.hr('META Lab pass', level=1)
        self.ui_ensure(page_dock)
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(faction='meta')

        processed = 0
        if self.appear(DOCK_EMPTY, offset=(20, 20)) or not self.dock_enter_first():
            logger.info('No META ships in dock')
        else:
            while 1:
                self.process_ship()
                processed += 1
                self.device.click_record_clear()
                if processed >= 60:
                    logger.warning('MetaLab safety limit reached')
                    break
                if not self.ship_view_next(check_button=SHIP_DETAIL_CHECK):
                    logger.info('End of META ship list')
                    break
            self.lb_exit_to_dock()

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
