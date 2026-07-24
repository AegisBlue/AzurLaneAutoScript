import os

from module.base.button import Button, ButtonGrid
from module.base.timer import Timer
from module.base.utils import crop
from module.limit_break.assets import *
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY, DOCK_FIRST_NPC, SHIP_DETAIL_CHECK
from module.retire.dock import CARD_GRIDS, Dock
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock, page_main

# Card grid of the material selection screen, same layout as dock
MATERIAL_GRIDS = ButtonGrid(
    origin=(93, 76), delta=(164 + 2 / 3, 227), button_shape=(138, 204), grid_shape=(7, 2), name='MATERIAL')

# Coin cost on the limit break screen, white digits in the dark pill left of the confirm button
OCR_LB_COST = Digit(
    Button(area=(1030, 646, 1128, 680), color=(), button=(1030, 646, 1128, 680), name='LB_COST'),
    letter=(255, 255, 255), threshold=128, name='OCR_LB_COST')
# Coin balance in the top resource bar
OCR_COIN_BALANCE = Digit(
    Button(area=(808, 22, 940, 48), color=(), button=(808, 22, 940, 48), name='COIN_BALANCE'),
    letter=(255, 255, 255), threshold=128, name='OCR_COIN_BALANCE')

# A click here only dismisses "touch to continue" screens,
# empty background on both the limit break screen and the success screen
SAFE_CLICK = Button(area=(560, 85, 660, 120), color=(), button=(560, 85, 660, 120), name='SAFE_CLICK')


class LimitBreak(Dock):
    def lb_assets_ready(self):
        """
        Returns:
            bool: True if all button assets exist.
        """
        files = [
            LIMIT_BREAK_ENTER.file, LIMIT_BREAK_CHECK.file, LB_SLOT_ADD.file, LB_EXECUTE.file,
            MATERIAL_CHECK.file, MATERIAL_CONFIRM.file, MATERIAL_CANCEL.file,
            TEMPLATE_BULIN_UNIVERSAL.file, TEMPLATE_BULIN_MKII.file,
            TEMPLATE_SLOT_EMPTY.file, TEMPLATE_SELECTED.file,
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
        # The dock background is not a flat color, so unlike dock_enter_first there is
        # no reliable empty-slot check. Clicking an empty slot does nothing, so entering
        # times out and the run ends.
        timeout = Timer(10, count=10).start()
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
            if timeout.reached():
                logger.info('dock_enter_index timeout, assume no more ships')
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
                self.device.click(CARD_GRIDS[(shifted % 7, shifted // 7)])
                continue
            if self.handle_game_tips():
                continue

    def is_in_lb(self):
        # "Materials" section header, only shown on the limit break view
        return self.appear(LIMIT_BREAK_CHECK, offset=(20, 20))

    def lb_enter(self, skip_first_screenshot=True):
        """
        From ship detail page, open the limit break view.

        Returns:
            bool: True if in limit break view, False if timed out.
                META ships have no LimitBreak button at all and end up here as timeout.

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
        Compare the limit break coin cost against the coin balance in the top bar.

        Pages:
            in: LIMIT_BREAK_CHECK
        """
        cost = OCR_LB_COST.ocr(self.device.image)
        balance = OCR_COIN_BALANCE.ocr(self.device.image)
        if cost > 0 and balance > 0 and balance < cost:
            logger.info(f'Not enough coins to limit break, cost={cost}, balance={balance}')
            return True
        return False

    def material_card_present(self, button):
        """
        Args:
            button (Button): Card button of MATERIAL_GRIDS

        Returns:
            bool: True if a ship card is on this grid position.
                Detected by the white "Lv." badge across the card top,
                the blurred dock background behind empty positions has no such white.
        """
        area = button.area
        strip = (area[0] + 55, area[1] + 2, area[2] - 2, area[1] + 30)
        # Present cards show 13-49 white pixels depending on subpixel position, empty shows 0
        return self.image_color_count(strip, color=(255, 255, 255), threshold=221, count=5)

    def material_scan(self):
        """
        Classify the cards of the material selection screen.

        Returns:
            dict: {'dupe': [Button], 'universal': [Button], 'mkii': [Button]}
        """
        result = {'dupe': [], 'universal': [], 'mkii': []}
        for button in MATERIAL_GRIDS.buttons:
            if not self.material_card_present(button):
                continue
            card = crop(self.device.image, button.area)
            if TEMPLATE_BULIN_MKII.match(card):
                result['mkii'].append(button)
            elif TEMPLATE_BULIN_UNIVERSAL.match(card, similarity=0.70):
                # Template is synthesized from shop assets, not a real capture,
                # so it matches with lower similarity
                result['universal'].append(button)
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

    def material_selected_count(self):
        """
        Returns:
            int: Number of cards with the "SELECTED" overlay.
        """
        return len(TEMPLATE_SELECTED.match_multi(self.device.image))

    def lb_select_materials(self, required, skip_first_screenshot=True):
        """
        In the material selection screen, select the required number of
        materials and confirm.

        Args:
            required (int): Number of materials to select.
            skip_first_screenshot:

        Returns:
            str: 'success' or 'no_material'

        Pages:
            in: MATERIAL_CHECK
            out: LIMIT_BREAK_CHECK
        """
        logger.info(f'Select {required} materials')
        if not skip_first_screenshot:
            self.device.screenshot()

        candidates = self.material_candidates()
        clicked = 0
        timeout = Timer(25, count=25).start()
        while 1:
            self.device.screenshot()

            if timeout.reached():
                logger.warning('lb_select_materials timeout')
                self.material_cancel()
                return 'no_material'
            if self.handle_popup_confirm('MATERIAL_SELECT'):
                continue

            if self.material_selected_count() >= required:
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
        """
        Confirm material selection. Selecting rare materials shows an
        "Elite and above ship" warning popup, which is confirmed as well.
        """
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

    def lb_fill_slots(self, required, skip_first_screenshot=True):
        """
        From the limit break view, open the material list via an empty slot
        and fill all slots.

        Args:
            required (int): Number of empty material slots.
            skip_first_screenshot:

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
                return self.lb_select_materials(required)
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
        Slots are filled; press the confirm button. There is no further popup,
        the limit break plays its success screen immediately, which is clicked
        through until back on the limit break view.

        Returns:
            str: 'success', 'insufficient_gold' or 'skip'

        Pages:
            in: LIMIT_BREAK_CHECK
            out: LIMIT_BREAK_CHECK, on the next limit break tier or maxed
        """
        logger.info('Limit break execute')
        if not skip_first_screenshot:
            self.device.screenshot()

        if self.lb_gold_insufficient():
            return 'insufficient_gold'

        left_lb_screen = False
        timeout = Timer(45, count=45).start()
        click_timer = Timer(2)
        self.interval_clear(LB_EXECUTE)
        while 1:
            self.device.screenshot()

            if timeout.reached():
                if left_lb_screen:
                    logger.warning('lb_execute timeout on success screen, assume finished')
                    break
                else:
                    logger.warning('lb_execute timeout, limit break not executed, ship skipped')
                    return 'skip'

            if not left_lb_screen:
                # Waiting for the success screen, which has no Materials header
                if not self.is_in_lb():
                    left_lb_screen = True
                    click_timer.reset()
                    continue
                if self.appear_then_click(LB_EXECUTE, offset=(20, 20), interval=3):
                    continue
                if self.handle_popup_confirm('LIMIT_BREAK'):
                    continue
            else:
                # End: back on the limit break view
                if self.is_in_lb():
                    logger.info('Limit break finished')
                    break
                if self.handle_popup_confirm('LIMIT_BREAK'):
                    continue
                # Success animation, touch to continue
                if click_timer.reached():
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

        self.device.screenshot()
        required = len(TEMPLATE_SLOT_EMPTY.match_multi(self.device.image))
        if required < 1:
            logger.warning('No empty material slots found, ship skipped')
            return 'skip'
        logger.info(f'Limit break requires {required} materials')

        result = self.lb_fill_slots(required)
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
            logger.critical('LimitBreak assets are missing, task cannot run.')
            logger.critical('LimitBreak disables itself now.')
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
                # Ship dropped off the filter (or is ready for its next tier
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
