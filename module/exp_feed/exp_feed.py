import os

from module.base.button import Button
from module.base.timer import Timer
from module.exp_feed.assets import *
from module.limit_break.limit_break import LimitBreak
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.assets import DOCK_EMPTY, SHIP_DETAIL_CHECK
from module.ui.page import page_dock


def _area_button(area, name):
    return Button(area=area, color=(), button=area, name=name)


# White digits in the dark count fields of the Boost EXP dialog
OCR_T1_SELECTED = Digit(_area_button((630, 330, 782, 360), 'T1_SELECTED'),
                        letter=(255, 255, 255), threshold=128, name='OCR_T1_SELECTED')
OCR_T2_SELECTED = Digit(_area_button((630, 430, 782, 460), 'T2_SELECTED'),
                        letter=(255, 255, 255), threshold=128, name='OCR_T2_SELECTED')
# Owned count badge on the bottom right of the T1 pack icon
OCR_T1_OWNED = Digit(_area_button((382, 346, 431, 371), 'T1_OWNED'),
                     letter=(255, 255, 255), threshold=128, name='OCR_T1_OWNED')
# Ship level on the detail page, right of the "Level:" label. Right edge ends
# before the Boost EXP / Awakening button so 3-digit levels don't pick up letters.
OCR_DETAIL_LEVEL = Digit(_area_button((758, 283, 798, 319), 'DETAIL_LEVEL'),
                         letter=(255, 255, 255), threshold=128, name='OCR_DETAIL_LEVEL')

# Selection stepper buttons of the T1 row, click positions only
BOOST_T1_MINUS10 = _area_button((473, 327, 565, 362), 'BOOST_T1_MINUS10')
BOOST_T1_MINUS = _area_button((587, 328, 616, 361), 'BOOST_T1_MINUS')

# Rarities this task feeds and limit breaks
FEED_RARITY = ['rare', 'super_rare']
# Feed below this level, skip at or above (awakened ships appear in the
# not_level_max filter too and must not be fed)
TARGET_LEVEL = 100
# feed -> limit break -> feed chains converge in 2 cycles; extra headroom
MAX_CYCLES = 4
# With ascending level sort the 100+ ships form the tail of the list, so a
# long streak of them means no feedable ship remains. Streak must stay above
# anything the fleet-pinned prefix can produce (fleet ships sort first
# regardless of level and may include awakened 100+ ships).
LEVEL_MAX_STREAK_STOP = 20


class ExpFeed(LimitBreak):
    packs_exhausted = False

    def feed_assets_ready(self):
        """
        Returns:
            bool: True if all button assets exist.
        """
        files = [
            BOOST_EXP.file, BOOST_EXP_CHECK.file, BOOST_T1_LABEL.file,
            BOOST_AUTO.file, BOOST_CONFIRM.file, BOOST_CLOSE.file,
        ]
        missing = [f for f in files if not os.path.exists(f)]
        if missing:
            logger.warning(f'ExpFeed assets missing: {missing}')
            return False
        return True

    def is_in_boost(self):
        # "Boost EXP" dialog title, does not match when dimmed behind a popup
        return self.appear(BOOST_EXP_CHECK, offset=(20, 20))

    def get_detail_level(self, skip_first_screenshot=True):
        """
        OCR the ship level on the ship detail page. The "Enhanced!" info bar
        overlaps the level area, so wait it out before reading.

        Returns:
            int: 1 to 125, or 0 if OCR failed.

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

    def boost_enter(self, skip_first_screenshot=True):
        """
        From the ship detail page, open the Boost EXP dialog.

        Returns:
            bool: True if in the dialog. False if the ship has no Boost EXP
                button (at level cap 100+, META, NPC).

        Pages:
            in: SHIP_DETAIL_CHECK
            out: BOOST_EXP_CHECK if True
        """
        logger.info('Boost EXP enter')
        timeout = Timer(6, count=6).start()
        self.interval_clear(SHIP_DETAIL_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_in_boost():
                return True
            if timeout.reached():
                logger.info('boost_enter timeout, ship has no Boost EXP button')
                return False

            # The stats panel is semi-transparent, so the ship art tints the
            # background behind "Boost EXP" — luma match on a tight text crop,
            # plain rgb match fails on light-background ships.
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=3) \
                    and BOOST_EXP.match_luma(self.device.image, offset=(20, 20), similarity=0.70):
                self.device.click(BOOST_EXP)
                continue
            if self.handle_game_tips():
                continue

    def boost_close(self, skip_first_screenshot=True):
        """
        Pages:
            in: BOOST_EXP_CHECK
            out: SHIP_DETAIL_CHECK
        """
        logger.info('Boost EXP close')
        self.interval_clear(BOOST_CLOSE)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if not self.is_in_boost() and self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                break
            if self.appear_then_click(BOOST_CLOSE, offset=(20, 20), interval=3):
                continue
            if self.handle_popup_cancel('BOOST_CLOSE'):
                continue

    def boost_reduce_t1(self, current, allowed):
        """
        Reduce the T1 selection down to `allowed` with the -10 / - buttons.

        Returns:
            int: Selection after reduction, 0 on failure.

        Pages:
            in: BOOST_EXP_CHECK
        """
        diff = current - allowed
        logger.info(f'Reduce T1 selection {current} -> {allowed}')
        clicks = 0
        for _ in range(diff // 10):
            self.device.click(BOOST_T1_MINUS10)
            clicks += 1
            if clicks % 8 == 0:
                self.device.click_record_clear()
            self.device.sleep((0.15, 0.25))
        for _ in range(diff % 10):
            self.device.click(BOOST_T1_MINUS)
            clicks += 1
            if clicks % 8 == 0:
                self.device.click_record_clear()
            self.device.sleep((0.15, 0.25))
        self.device.click_record_clear()
        self.device.sleep((0.3, 0.5))
        self.device.screenshot()
        result = OCR_T1_SELECTED.ocr(self.device.image)
        if result > allowed:
            logger.warning(f'T1 reduction failed, selection still {result}')
            return 0
        return result

    def boost_feed_once(self):
        """
        In the Boost EXP dialog: press Auto, check the selection, confirm.
        Auto fills exactly up to the ship's current level cap, so one round
        feeds the ship to its cap. The "EXP exceeding the level cap will be
        lost" popup (sub-pack rounding) is confirmed.

        Returns:
            str: 'fed'          confirmed, dialog closed
                 'at_cap'       nothing to feed, ship at its level cap
                 'packs_empty'  out of T1 packs (or down to the reserve)
                 'tier_blocked' Auto wants T2 packs but SpendTier2 is off
                 'skip'         unexpected state, skip this ship
            All results except 'fed' leave the dialog open.

        Pages:
            in: BOOST_EXP_CHECK
            out: SHIP_DETAIL_CHECK if 'fed', BOOST_EXP_CHECK otherwise
        """
        # Layout sanity: the T1 row must be the first row
        if not self.appear(BOOST_T1_LABEL, offset=(20, 20)):
            logger.warning('T1 EXP Data Pack is not the first row of the Boost EXP dialog, '
                           'ship skipped')
            return 'skip'

        # Press Auto and let the selection preview settle
        self.device.click(BOOST_AUTO)
        self.device.sleep((0.8, 1.2))
        self.device.screenshot()

        t1 = OCR_T1_SELECTED.ocr(self.device.image)
        t2 = OCR_T2_SELECTED.ocr(self.device.image)
        owned = OCR_T1_OWNED.ocr(self.device.image)
        reserve = int(self.config.ExpFeed_KeepReserve)
        logger.info(f'Auto selected: T1={t1}, T2={t2}, T1_owned={owned}, reserve={reserve}')

        if t2 > 0 and not self.config.ExpFeed_SpendTier2:
            logger.warning('Auto selected T2 packs but SpendTier2 is disabled, ship skipped')
            return 'tier_blocked'

        if t1 <= 0 and t2 <= 0:
            if owned <= reserve:
                logger.info('No T1 packs left to spend')
                return 'packs_empty'
            logger.info('Nothing selected, ship is at its level cap')
            return 'at_cap'

        allowed = owned - reserve
        if t1 > allowed:
            if allowed <= 0:
                logger.info('T1 packs down to the reserve')
                return 'packs_empty'
            t1 = self.boost_reduce_t1(t1, allowed)
            if t1 <= 0:
                return 'packs_empty'

        logger.info(f'Boost EXP confirm, spending {t1} T1'
                    + (f' + {t2} T2' if t2 else '') + ' packs')
        timeout = Timer(20, count=20).start()
        self.interval_clear(BOOST_CONFIRM)
        while 1:
            self.device.screenshot()

            if timeout.reached():
                logger.warning('boost_feed_once confirm timeout, ship skipped')
                return 'skip'
            if self.handle_popup_confirm('BOOST_EXP'):
                continue
            if self.is_in_boost():
                if self.appear_then_click(BOOST_CONFIRM, offset=(20, 20), interval=3):
                    continue
                continue
            # Dialog gone and no popup left
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                break

        return 'fed'

    def feed_ship(self):
        """
        Feed the ship currently opened up to its level cap.

        Returns:
            str: 'fed', 'level_max', 'skip' or 'packs_empty'

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        logger.hr('Feed ship', level=2)
        level = self.get_detail_level()
        if level == 0:
            return 'skip'
        if level >= TARGET_LEVEL:
            logger.info(f'Ship level {level} >= {TARGET_LEVEL}, skip')
            return 'level_max'
        logger.info(f'Ship level {level}')

        if not self.boost_enter():
            return 'skip'

        result = self.boost_feed_once()
        if result == 'fed':
            new_level = self.get_detail_level(skip_first_screenshot=False)
            logger.info(f'Ship fed, level {level} -> {new_level}')
            return 'fed'
        if result == 'packs_empty':
            self.boost_close()
            return 'packs_empty'
        # 'at_cap', 'tier_blocked', 'skip'
        self.boost_close()
        return 'skip'

    def feed_pass(self):
        """
        One pass over the dock, feeding every Rare / Super Rare ship below 100
        up to its current level cap. Sorted by level ascending, so the lowest
        ships come first (after any fleet-pinned ships) and the 100+ ships
        that share the not_level_max filter form the tail; a long streak of
        them ends the pass early. Ships are iterated on the detail page via
        swipes, so the dock is never paged.

        Returns:
            int: Number of ships fed.

        Pages:
            in: Any
            out: page_dock
        """
        logger.hr('EXP feed pass', level=1)
        self.ui_ensure(page_dock)
        self.dock_favourite_set(enable=self.config.ExpFeed_Favourite, wait_loading=False)
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_filter_set(extra=['not_level_max'], rarity=FEED_RARITY)

        fed = 0
        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('feed_pass finished, no ships in filter')
            return fed
        if not self.dock_enter_first():
            logger.info('feed_pass finished, no ships in filter')
            return fed

        visited = 0
        level_max_streak = 0
        while 1:
            visited += 1
            result = self.feed_ship()
            if result == 'fed':
                fed += 1
            self.device.click_record_clear()
            if result == 'packs_empty':
                self.packs_exhausted = True
                logger.info('feed_pass ended, T1 packs exhausted')
                break
            if result == 'level_max':
                level_max_streak += 1
                if level_max_streak >= LEVEL_MAX_STREAK_STOP:
                    logger.info(f'feed_pass finished, {level_max_streak} ships at 100+ in a row, '
                                'reached the end of feedable ships')
                    break
            else:
                level_max_streak = 0
            if visited >= 700:
                logger.warning('feed_pass safety limit reached')
                break
            if not self.ship_view_next(check_button=SHIP_DETAIL_CHECK):
                logger.info('feed_pass finished, end of dock list')
                break

        self.lb_exit()
        self.device.click_record_clear()
        return fed

    @property
    def feed_lb_rarity(self):
        """
        Rarities the limit break pass may touch: this task's Rare / Super Rare
        scope, narrowed by the allowed materials.
        """
        return [r for r in self.target_rarity if r in FEED_RARITY]

    def lb_pass(self):
        """
        Limit break every Rare / Super Rare ship that can be limit broken.

        Returns:
            int: Number of successful limit breaks.

        Pages:
            in: Any
            out: page_dock
        """
        rarity = self.feed_lb_rarity
        if not rarity:
            logger.info('lb_pass skipped, all limit break materials disallowed in config')
            return 0
        logger.hr('Limit break pass', level=1)
        logger.info(f'Target rarity: {rarity}')
        self.ui_ensure(page_dock)
        self.dock_favourite_set(enable=self.config.ExpFeed_Favourite, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(extra=['can_limit_break'], rarity=rarity)

        _, success = self.lb_process_dock()
        return success

    def run(self):
        if not self.lb_assets_ready() or not self.feed_assets_ready():
            logger.critical('ExpFeed assets are missing, task cannot run.')
            logger.critical('ExpFeed disables itself now.')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        self.packs_exhausted = False
        for cycle in range(1, MAX_CYCLES + 1):
            logger.hr(f'EXP feed cycle {cycle}', level=1)
            lb_count = self.lb_pass()
            fed_count = self.feed_pass()
            logger.info(f'Cycle {cycle}: {lb_count} limit breaks, {fed_count} ships fed')
            if self.packs_exhausted:
                break
            if lb_count == 0 and fed_count == 0:
                logger.info('No further progress possible, run finished')
                break

        if self.packs_exhausted:
            # Packs ran out mid-feed; ships fed to 70 this run still deserve
            # their limit breaks so the next run can take them to 100.
            self.lb_pass()

        # Reset dock filters
        logger.hr('EXP feed run exit', level=1)
        if self.config.ExpFeed_Favourite:
            self.dock_favourite_set(enable=False, wait_loading=False)
        self.dock_filter_set(wait_loading=False)

        # Scheduler
        self.config.task_delay(server_update=True)
