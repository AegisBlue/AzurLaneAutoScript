import os

from module.base.button import Button, ButtonGrid, color_similar, get_color
from module.base.timer import Timer
from module.base.utils import crop
from module.limit_break.assets import *
from module.logger import logger
from module.ocr.ocr import DigitCounter
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY, DOCK_FIRST_NPC, SHIP_DETAIL_CHECK
from module.retire.dock import CARD_GRIDS, Dock
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock, page_main

# Card grid of the material selection screen. Assumed to share the dock layout;
# tune from real screenshots when assets are captured.
MATERIAL_GRIDS = ButtonGrid(
    origin=(93, 76), delta=(164 + 2 / 3, 227), button_shape=(138, 204), grid_shape=(7, 2), name='MATERIAL')

OCR_MATERIAL_SELECTED = DigitCounter(MATERIAL_SELECTED, threshold=64, name='OCR_MATERIAL_SELECTED')

# Background color of an empty card slot, same as dock
EMPTY_CARD_COLOR = (34, 34, 42)
# A click on this area only dismisses "touch to continue" screens
SAFE_CLICK = Button(area=(1150, 90, 1250, 130), color=(), button=(1150, 90, 1250, 130), name='SAFE_CLICK')


class LimitBreak(Dock):
    def lb_assets_ready(self):
        """
        Returns:
            bool: True if all button assets have been captured.
        """
        files = [
            LIMIT_BREAK_ENTER.file, LIMIT_BREAK_CHECK.file, LB_SLOT_ADD.file,
            LB_COST_COIN.file, LB_EXECUTE.file, MATERIAL_CHECK.file,
            MATERIAL_SELECTED.file, MATERIAL_CONFIRM.file, MATERIAL_CANCEL.file,
            TEMPLATE_BULIN_UNIVERSAL.file, TEMPLATE_BULIN_MKII.file,
        ]
        missing = [f for f in files if not os.path.exists(f)]
        if missing:
            logger.warning(f'LimitBreak assets missing: {missing}')
            return False
        return True

    @property
    def target_rarity(self):
        """
        Dock rarity filter derived from the allowed materials.
        Ultra rarity is always excluded: Specialized Bulin MKIII is never spent.

        Returns:
            list[str]: Rarity options for dock_filter_set, or [] if no material allowed.
        """
        rarity = set()
        if self.config.LimitBreak_UseUniversalBulin:
            rarity |= {'common', 'rare', 'elite'}
        if self.config.LimitBreak_UsePrototypeBulin:
            rarity |= {'common', 'rare', 'elite', 'super_rare'}
        if self.config.LimitBreak_UseDuplicates:
            rarity |= {'common', 'rare', 'elite', 'super_rare'}
        return [r for r in ['common', 'rare', 'elite', 'super_rare'] if r in rarity]

    def dock_enter_index(self, index, skip_first_screenshot=True):
        """
        Enter the Nth ship on the first page of dock. Generalized dock_enter_first,
        used to skip over ships that could not be limit broken.

        Args:
            index (int): 0 to 13, position in the 7x2 card grid.
            skip_first_screenshot:

        Returns:
            bool: True if entered ship detail, False if no such ship.

        Pages:
            in: page_dock
            out: SHIP_DETAIL_CHECK if True, page_dock if False
        """
        if index == 0:
            return self.dock_enter_first()

        logger.info(f'Dock enter index {index}')
        self.interval_clear(DOCK_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20)):
                return True
            if self.appear(DOCK_EMPTY, offset=(20, 20)):
                logger.info('Dock empty')
                return False

            # Click
            if self.appear(DOCK_CHECK, offset=(20, 20), interval=3):
                # If the first card is an NPC, real ships are shifted right by one
                if DOCK_FIRST_NPC.match_luma(self.device.image, offset=(20, 20)):
                    shifted = index + 1
                else:
                    shifted = index
                if shifted >= 14:
                    logger.info('Ship index out of first dock page')
                    return False
                button = CARD_GRIDS[(shifted % 7, shifted // 7)]
                color = get_color(self.device.image, button.area)
                if color_similar(color, EMPTY_CARD_COLOR):
                    logger.info('No more ships in dock')
                    return False
                self.device.click(button)
                continue
            if self.handle_game_tips():
                continue

    def is_in_lb(self):
        return self.appear(LIMIT_BREAK_CHECK, offset=(20, 20))

    def lb_enter(self, skip_first_screenshot=True):
        """
        From ship detail page, open the limit break screen.

        Returns:
            bool: True if in limit break screen, False if timed out
                (e.g. the Limit Break button is absent or disabled).

        Pages:
            in: SHIP_DETAIL_CHECK
            out: LIMIT_BREAK_CHECK if True
        """
        logger.info('Limit break enter')
        timeout = Timer(10, count=10).start()
        self.interval_clear(SHIP_DETAIL_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_in_lb():
                return True
            if timeout.reached():
                logger.warning('lb_enter timeout, ship skipped')
                return False

            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=3) \
                    and self.appear(LIMIT_BREAK_ENTER, offset=(20, 20)):
                self.device.click(LIMIT_BREAK_ENTER)
                continue
            if self.handle_game_tips():
                continue

    def lb_gold_insufficient(self):
        """
        Red letters below the coin icon mean not enough coins.
        Same detection as Awaken._get_button_state.
        """
        if LB_COST_COIN.match(self.device.image, offset=(75, 20)):
            area = LB_COST_COIN.button
            area = (area[0], area[3], area[2], area[3] + 60)
            if self.image_color_count(area, color=(214, 53, 33), threshold=180, count=16):
                return True
        return False

    def material_scan(self):
        """
        Classify the cards of the material selection screen.

        Returns:
            dict: {'dupe': [Button], 'universal': [Button], 'mkii': [Button]}
        """
        result = {'dupe': [], 'universal': [], 'mkii': []}
        for button in MATERIAL_GRIDS.buttons:
            color = get_color(self.device.image, button.area)
            if color_similar(color, EMPTY_CARD_COLOR):
                continue
            card = crop(self.device.image, button.area)
            if TEMPLATE_BULIN_UNIVERSAL.match(card):
                result['universal'].append(button)
            elif TEMPLATE_BULIN_MKII.match(card):
                result['mkii'].append(button)
            else:
                result['dupe'].append(button)
        logger.info('Materials: '
                    f'{len(result["dupe"])} dupe, '
                    f'{len(result["universal"])} universal bulin, '
                    f'{len(result["mkii"])} bulin MKII')
        return result

    def material_candidates(self):
        """
        Returns:
            list[Button]: Allowed material cards in spending order:
                duplicates first, then Universal Bulin, then Prototype Bulin MKII.
        """
        scan = self.material_scan()
        candidates = []
        if self.config.LimitBreak_UseDuplicates:
            candidates += scan['dupe']
        if self.config.LimitBreak_UseUniversalBulin:
            candidates += scan['universal']
        if self.config.LimitBreak_UsePrototypeBulin:
            candidates += scan['mkii']
        return candidates

    def lb_select_materials(self, skip_first_screenshot=True):
        """
        In the material selection screen, select the required number of
        materials and confirm.

        Returns:
            str: 'success' or 'no_material'

        Pages:
            in: MATERIAL_CHECK
            out: LIMIT_BREAK_CHECK if success, LIMIT_BREAK_CHECK after cancel if no_material
        """
        logger.info('Select materials')
        if not skip_first_screenshot:
            self.device.screenshot()

        candidates = self.material_candidates()
        clicked = 0
        timeout = Timer(20, count=20).start()
        while 1:
            self.device.screenshot()

            if timeout.reached():
                logger.warning('lb_select_materials timeout')
                self.material_cancel()
                return 'no_material'

            current, _, total = OCR_MATERIAL_SELECTED.ocr(self.device.image)
            if total > 0 and current >= total:
                self.material_confirm()
                return 'success'
            if clicked >= len(candidates):
                logger.info('Not enough allowed materials to fill slots')
                self.material_cancel()
                return 'no_material'

            self.device.click(candidates[clicked])
            clicked += 1
            self.device.sleep((0.3, 0.5))

    def material_confirm(self, skip_first_screenshot=True):
        logger.info('Material confirm')
        self.interval_clear(MATERIAL_CONFIRM)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if not self.appear(MATERIAL_CHECK, offset=(20, 20)) and self.is_in_lb():
                break
            if self.appear_then_click(MATERIAL_CONFIRM, offset=(20, 20), interval=3):
                continue
            if self.handle_popup_confirm('MATERIAL_CONFIRM'):
                continue

    def material_cancel(self, skip_first_screenshot=True):
        logger.info('Material cancel')
        self.interval_clear(MATERIAL_CANCEL)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if not self.appear(MATERIAL_CHECK, offset=(20, 20)) and self.is_in_lb():
                break
            if self.appear_then_click(MATERIAL_CANCEL, offset=(20, 20), interval=3):
                continue

    def lb_fill_slots(self, skip_first_screenshot=True):
        """
        From the limit break screen, open the material list via an empty slot
        and fill all slots.

        Returns:
            str: 'success', 'no_material' or 'skip'

        Pages:
            in: LIMIT_BREAK_CHECK
            out: LIMIT_BREAK_CHECK
        """
        logger.info('Fill limit break slots')
        timeout = Timer(10, count=10).start()
        self.interval_clear(LB_SLOT_ADD)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(MATERIAL_CHECK, offset=(20, 20)):
                return self.lb_select_materials()
            if timeout.reached():
                logger.warning('lb_fill_slots timeout, ship skipped')
                return 'skip'

            # Wide offset: find whichever slot is empty
            if self.is_in_lb() and self.appear_then_click(LB_SLOT_ADD, offset=(300, 100), interval=3):
                continue
            if self.handle_game_tips():
                continue

    def lb_execute(self, skip_first_screenshot=True):
        """
        Slots are filled; press the limit break button and click through
        the confirm popup and success screens.

        Returns:
            str: 'success' or 'insufficient_gold'

        Pages:
            in: LIMIT_BREAK_CHECK
            out: LIMIT_BREAK_CHECK or SHIP_DETAIL_CHECK
        """
        logger.info('Limit break execute')
        if not skip_first_screenshot:
            self.device.screenshot()

        if self.lb_gold_insufficient():
            logger.info('Not enough coins to limit break')
            return 'insufficient_gold'

        executed = False
        timeout = Timer(30, count=30).start()
        click_timer = Timer(2)
        self.interval_clear(LB_EXECUTE)
        while 1:
            self.device.screenshot()

            # End
            if timeout.reached():
                logger.warning('lb_execute timeout, assume finished')
                break
            if executed and (self.is_in_lb() or self.appear(SHIP_DETAIL_CHECK, offset=(20, 20))):
                logger.info('Limit break finished')
                break

            # Click
            if not executed and self.appear_then_click(LB_EXECUTE, offset=(20, 20), interval=3):
                continue
            if self.handle_popup_confirm('LIMIT_BREAK'):
                executed = True
                click_timer.reset()
                continue
            # Success animation, "touch to continue"
            if executed and click_timer.reached():
                self.device.click(SAFE_CLICK)
                click_timer.reset()
                continue

        self.device.click_record_clear()
        return 'success'

    def lb_ship(self):
        """
        Perform one limit break stage on the ship currently opened.

        Returns:
            str: 'success', 'no_material', 'insufficient_gold' or 'skip'

        Pages:
            in: SHIP_DETAIL_CHECK
            out: LIMIT_BREAK_CHECK or SHIP_DETAIL_CHECK
        """
        logger.hr('Limit break ship', level=2)
        if not self.lb_enter():
            return 'skip'
        result = self.lb_fill_slots()
        if result != 'success':
            return result
        return self.lb_execute()

    def lb_exit(self, skip_first_screenshot=True):
        """
        Pages:
            in: Any limit break related screen
            out: page_dock
        """
        logger.info('Limit break exit')
        interval = Timer(3)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.ui_page_appear(page_dock):
                logger.info(f'Limit break exit at {page_dock}')
                break
            if self.appear(MATERIAL_CHECK, offset=(20, 20)):
                self.material_cancel()
                continue
            if interval.reached() and (self.is_in_lb() or self.appear(SHIP_DETAIL_CHECK, offset=(20, 20))):
                self.device.click(BACK_ARROW)
                interval.reset()
                continue
            if self.handle_popup_cancel('LB_EXIT'):
                continue
            if self.is_in_main(interval=5):
                self.device.click(page_main.links[page_dock])
                continue

    def run(self):
        if not self.lb_assets_ready():
            logger.critical('LimitBreak assets have not been captured yet, task cannot run.')
            logger.critical('See module/limit_break/assets.py for capture instructions.')
            logger.critical('LimitBreak disables itself now; enable it again after capturing assets.')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        rarity = self.target_rarity
        if not rarity:
            logger.warning('All limit break materials are disallowed in config, nothing to do')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        logger.hr('Limit break run', level=1)
        logger.info(f'Target rarity: {rarity}')
        self.ui_ensure(page_dock)
        self.dock_favourite_set(enable=self.config.LimitBreak_Favourite, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(extra=['can_limit_break'], rarity=rarity)

        skipped = 0
        while 1:
            # page_dock
            if self.appear(DOCK_EMPTY, offset=(20, 20)):
                logger.info('limit_break_run finished, no ships to limit break')
                break

            # page_dock -> SHIP_DETAIL_CHECK
            entered = self.dock_enter_index(skipped)
            if not entered:
                logger.info('limit_break_run finished, all eligible ships processed')
                break

            result = self.lb_ship()
            self.lb_exit()
            self.device.click_record_clear()
            if result == 'success':
                # Ship dropped off the filter (or is ready for its next stage
                # and stays first); either way re-enter at the same index.
                continue
            if result in ['no_material', 'skip']:
                # Leave this ship and try the next one
                skipped += 1
                continue
            if result == 'insufficient_gold':
                logger.info('limit_break_run finished, coins exhausted')
                break

        # Reset dock filters
        logger.hr('Limit break run exit', level=1)
        if self.config.LimitBreak_Favourite:
            self.dock_favourite_set(enable=False, wait_loading=False)
        self.dock_filter_set(wait_loading=False)

        # Scheduler
        self.config.task_delay(server_update=True)
