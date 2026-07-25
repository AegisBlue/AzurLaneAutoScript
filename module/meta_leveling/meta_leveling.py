from module.base.button import Button
from module.base.timer import Timer
from module.campaign.run import CampaignRun
from module.equipment.assets import FLEET_NEXT, FLEET_PREV, OCR_FLEET_INDEX
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY, SHIP_DETAIL_CHECK
from module.retire.dock import Dock
from module.retire.scanner import ShipScanner
from module.ui.page import page_fleet


def _area_button(area, name):
    return Button(area=area, color=(), button=area, name=name)


# Fleet 1-6 selector on the formation page, same reading as GemsFarming
FLEET_INDEX = Digit(OCR_FLEET_INDEX, letter=(90, 154, 255), threshold=128, alphabet='123456')

# Ship level on the detail page, right of the "Level:" label. Same crop as
# ExpFeed: right edge ends before the Boost EXP / Awakening button so 3-digit
# levels don't pick up letters.
OCR_DETAIL_LEVEL = Digit(_area_button((758, 283, 798, 319), 'DETAIL_LEVEL'),
                         letter=(255, 255, 255), threshold=128, name='OCR_DETAIL_LEVEL')

# ---------------------------------------------------------------------------
# Phase gate.
# The META-specific screens (detail sidebar, skill research page, the
# strengthening UI for limit break / rigging fortification) have no assets
# yet. Until the capture session fills SLOT_BUTTONS and the checks below stop
# being stubs, the maintenance pass only logs and the task behaves like a
# plain Main farm on the configured stage.
# ---------------------------------------------------------------------------
MAINTENANCE_READY = False

# Ship slots on the fleet formation page (page_fleet): 3 main fleet + 3
# vanguard. Click targets, to be measured from a live capture. A plain click
# on a slot opens the dock swap view; a long click opens the ship detail.
SLOT_BUTTONS = {
    'main_1': None,
    'main_2': None,
    'main_3': None,
    'vanguard_1': None,
    'vanguard_2': None,
    'vanguard_3': None,
}


class MetaLeveling(CampaignRun, Dock):
    # Set by meta_maintenance() when every managed slot holds a maxed META
    # ship and the dock has no unfinished META ship left to swap in.
    all_meta_complete = False

    @property
    def fleet_to_attack(self):
        """
        Slot index (1 or 2) of the attacking fleet on the formation page.
        """
        if self.config.Fleet_FleetOrder == 'fleet1_standby_fleet2_all':
            return 2
        else:
            return 1

    @property
    def fleet_to_attack_index(self):
        """
        In-game fleet number (1-6) of the attacking fleet.
        """
        if self.config.Fleet_FleetOrder == 'fleet1_standby_fleet2_all':
            return self.config.Fleet_Fleet2
        else:
            return self.config.Fleet_Fleet1

    @property
    def target_level(self):
        return int(self.config.MetaLeveling_TargetLevel)

    def ui_goto_fleet(self):
        """
        Go to the formation page and select the attacking fleet number.

        Pages:
            in: Any
            out: page_fleet, fleet number = fleet_to_attack_index
        """
        self.ui_ensure(page_fleet)

        retry = Timer(1, count=2)
        for _ in self.loop():
            current = FLEET_INDEX.ocr(self.device.image)
            logger.attr('FleetIndex', current)

            # Ignore the default value 0 (bad OCR), otherwise we would click
            # once too often when switching
            if current == 0:
                continue

            diff = self.fleet_to_attack_index - current
            if diff == 0:
                break

            if retry.reached():
                button = FLEET_NEXT if diff > 0 else FLEET_PREV
                self.device.multi_click(button, n=abs(diff), interval=(0.2, 0.3))
                retry.reset()

    def managed_slots(self):
        """
        Yields:
            str, Button: Slot name and click target for every fleet slot this
                task manages. The healer slot is never touched; slots without
                measured coordinates are skipped with a warning.
        """
        healer = self.config.MetaLeveling_HealerSlot
        for name, button in SLOT_BUTTONS.items():
            if name == healer:
                continue
            if button is None:
                logger.warning(f'Fleet slot {name} has no click coordinates yet, skipped')
                continue
            yield name, button

    def get_detail_level(self, skip_first_screenshot=True):
        """
        OCR the ship level on the ship detail page.

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

    def check_skills(self):
        """
        Read the META skill states of the ship currently opened: which skills
        exist, which one is actively gaining EXP from battles, which are maxed.
        Activate the next unfinished skill when the active one is maxed.

        Returns:
            str: 'all_maxed', 'in_progress', 'unknown'

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        # Stub until the META skill page is captured
        logger.info('check_skills: META skill page not implemented yet, assuming in_progress')
        return 'unknown'

    def check_strengthening(self):
        """
        Read limit break and rigging fortification state of the ship currently
        opened, and perform them when materials allow.

        Returns:
            str: 'all_maxed', 'in_progress', 'unknown'

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK
        """
        # Stub until the META strengthening UI is captured
        logger.info('check_strengthening: META strengthening UI not implemented yet, '
                    'assuming in_progress')
        return 'unknown'

    def inspect_slot(self, slot, button):
        """
        Long-click a fleet slot to open the ship detail page and read the
        ship's progression state, then return to the formation page.

        Args:
            slot (str): Slot name for logging.
            button (Button): Slot click target.

        Returns:
            str: 'maxed'        every category done, swap her out
                 'in_progress'  keep leveling
                 'unknown'      could not determine, keep and continue

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr(f'Inspect slot {slot}', level=2)
        self.ship_info_enter(button, long_click=True, skip_first_screenshot=False)

        level = self.get_detail_level()
        logger.info(f'Slot {slot}: level {level}')
        skills = self.check_skills()
        strengthening = self.check_strengthening()

        self.ui_back(check_button=page_fleet.check_button)

        if level == 0:
            return 'unknown'
        if level < self.target_level:
            return 'in_progress'
        if skills == 'all_maxed' and strengthening == 'all_maxed':
            return 'maxed'
        if skills == 'unknown' or strengthening == 'unknown':
            return 'unknown'
        return 'in_progress'

    def get_meta_candidate(self):
        """
        On the dock swap view, find the replacement META ship: the
        lowest-level free META ship below the target level, not assigned to
        any fleet. Only the first dock page is scanned; with the level sort
        ascending the lowest ships are on it.

        Returns:
            Ship: from module.retire.scanner, or None if no candidate.

        Pages:
            in: DOCK_CHECK (swap view opened from a fleet slot)
        """
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_filter_set(faction='meta')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('No META ship in the swap view')
            return None

        scanner = ShipScanner(level=(1, self.target_level - 1), emotion=(0, 150),
                              fleet=0, status='free')
        scanner.disable('rarity')
        ships = scanner.scan(self.device.image, output=True)
        if not ships:
            logger.info('No unfinished META ship available as replacement')
            return None
        # Lowest level first; on equal level prefer the higher emotion
        return min(ships, key=lambda ship: (ship.level, -ship.emotion))

    def swap_slot(self, slot, button):
        """
        Plain-click a fleet slot to open the dock swap view and put the best
        replacement META ship into it.

        Args:
            slot (str): Slot name for logging.
            button (Button): Slot click target.

        Returns:
            str: 'swapped' or 'no_candidate'

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr(f'Swap slot {slot}', level=2)
        self.ship_info_enter(button, check_button=DOCK_CHECK,
                             long_click=False, skip_first_screenshot=False)

        candidate = self.get_meta_candidate()
        if candidate is None:
            self.dock_reset()
            self.ui_back(check_button=page_fleet.check_button)
            return 'no_candidate'

        logger.info(f'Swap in META ship: level {candidate.level}, emotion {candidate.emotion}')
        self.dock_select_one(candidate.button)
        self.dock_reset()
        self.dock_select_confirm(check_button=page_fleet.check_button)
        self.record_fleet_emotion(candidate.emotion)
        return 'swapped'

    def record_fleet_emotion(self, value):
        """
        After a swap, lower the tracked fleet emotion to the swapped-in ship's
        value so the emotion control never overestimates.
        """
        value_name = f'Emotion_Fleet{self.fleet_to_attack}Value'
        configs = [self.config]
        # self.campaign only exists after the first load_campaign()
        campaign = getattr(self, 'campaign', None)
        if campaign is not None and campaign.config is not self.config:
            configs.append(campaign.config)
        for config in configs:
            current = getattr(config, value_name)
            if value < current:
                config.set_record(**{value_name: value})

    def meta_maintenance(self):
        """
        The between-batches pass: inspect every managed fleet slot, swap out
        finished META ships, keep skills progressing. Sets all_meta_complete
        when nothing is left to level.

        Returns:
            bool: True if the fleet is ready to keep farming.

        Pages:
            in: Any
            out: page_fleet if maintenance ran, unchanged otherwise
        """
        logger.hr('META maintenance', level=1)
        if not MAINTENANCE_READY:
            logger.info('META maintenance skipped: META screen assets not captured yet. '
                        'Farming continues with the fleet as it is.')
            return True

        self.ui_goto_fleet()
        results = {}
        for slot, button in self.managed_slots():
            status = self.inspect_slot(slot, button)
            if status == 'maxed':
                status = self.swap_slot(slot, button)
            results[slot] = status
            self.device.click_record_clear()
        logger.info(f'META maintenance results: {results}')

        # Every managed slot is done and the dock has nobody left to swap in
        if results and all(status == 'no_candidate' for status in results.values()):
            self.all_meta_complete = True
            return False
        return True

    def run(self, name='', folder='campaign_main', mode='normal', total=0):
        """
        Outer loop: maintenance pass, then farm CheckInterval campaign runs,
        repeat. Any early stop of the inner loop (emotion recovery wait, oil
        limit, run count exhausted, task switch) yields to the scheduler.

        Args:
            name (str): Stage name, e.g. '9-1'. Defaults to Campaign_Name.
            folder (str): Defaults to Campaign_Event (pinned campaign_main).
            mode (str): 'normal' (pinned).
            total (int): Optional overall cap of campaign runs, 0 = no cap.
        """
        name = name if name else self.config.Campaign_Name
        folder = folder if folder else self.config.Campaign_Event
        farmed = 0

        while 1:
            ready = self.meta_maintenance()
            if self.all_meta_complete:
                logger.hr('All META ships are fully maxed', level=1)
                logger.info('MetaLeveling has nothing left to do and disables itself.')
                self.config.Scheduler_Enable = False
                self.config.task_stop()
            if not ready:
                logger.warning('Fleet is not ready to farm, delay and retry later')
                self.config.task_delay(minute=360)
                self.config.task_stop()

            batch = max(1, int(self.config.MetaLeveling_CheckInterval))
            if total:
                batch = min(batch, total - farmed)
                if batch <= 0:
                    break
            logger.hr(f'Farm {batch} runs until the next META check', level=1)
            super().run(name=name, folder=folder, mode=mode, total=batch)
            farmed += self.run_count

            if self.run_count < batch:
                # The inner loop stopped early: emotion recovery, oil limit,
                # run count exhausted or a commission notice. Whatever caused
                # it has already set a task delay or disabled the scheduler,
                # so hand control back.
                logger.info('Campaign stopped before the batch was finished, '
                            'yield to the scheduler')
                self.config.task_stop()
