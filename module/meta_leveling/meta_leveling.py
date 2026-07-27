from module.base.button import Button
from module.base.timer import Timer
from module.campaign.run import CampaignRun
from module.equipment.assets import FLEET_NEXT, FLEET_PREV, OCR_FLEET_INDEX
from module.logger import logger
from module.meta_leveling.meta_lab import MetaLab
from module.ocr.ocr import Digit
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY, SHIP_DETAIL_CHECK
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

# Ship slots on the fleet formation page (new Formation UI, isometric
# platforms; surface fleet). Plain click on a slot opens the deploy picker,
# long click opens the ship detail. main_2 and vanguard_1 match the stock
# FLEET_ENTER_FLAGSHIP / FLEET_ENTER assets; the rest were measured from the
# 2026-07-24 capture session (screenshots/meta_lab_capture/02_formation.png).
SLOT_BUTTONS = {
    'main_1': _area_button((345, 275, 405, 325), 'SLOT_MAIN_1'),
    'main_2': _area_button((565, 260, 620, 305), 'SLOT_MAIN_2'),
    'main_3': _area_button((755, 240, 815, 285), 'SLOT_MAIN_3'),
    'vanguard_1': _area_button((480, 455, 540, 505), 'SLOT_VANGUARD_1'),
    'vanguard_2': _area_button((735, 400, 795, 450), 'SLOT_VANGUARD_2'),
    'vanguard_3': _area_button((935, 365, 995, 415), 'SLOT_VANGUARD_3'),
}


class MetaLeveling(CampaignRun, MetaLab):
    """
    Campaign farmer for META ship LEVELS. Skills / fortification / somatic
    activation are handled by the MetaLab task; this task keeps the lowest-
    level META ships in the designated fleet, repeats the configured stage
    for level EXP (which also feeds the daily skill missions), and swaps a
    ship out for a lower-level META once she reaches TargetLevel.

    MetaLab is inherited for its lab navigation and skill reading
    (check_skills_maxed): a ship that reached TargetLevel is only swapped
    out once her skills are maxed as well, and while she waits her research
    slot is kept on an unfinished skill (skill EXP comes from the account
    wide missions this farming generates, so an idle slot wastes it). Only
    config-free MetaLab helpers may be used here - this task has no
    MetaLab_* config keys; book feeding stays MetaLab's job.
    """

    # Set when neither the fleet nor the dock has a META ship below
    # TargetLevel left to level.
    leveling_complete = False
    # Set by get_meta_candidate when unfinished META ships exist but all sit
    # below MinSwapLevel (the ExpFeed pack-feeding pipeline owns them).
    _unfinished_below_min = False

    @property
    def min_swap_level(self):
        return int(self.config.MetaLeveling_MinSwapLevel)

    @property
    def fleet_to_attack(self):
        """
        Emotion slot of the managed fleet. By convention fleet 1 is ALWAYS
        the META fleet regardless of FleetOrder; fleet 2, when used
        (mob/boss orders), is a user-managed boss fleet this task never
        touches.
        """
        return 1

    @property
    def fleet_to_attack_index(self):
        """
        In-game fleet number (1-6) of the managed META fleet.
        """
        return self.config.Fleet_Fleet1

    @property
    def target_level(self):
        return int(self.config.MetaLeveling_TargetLevel)

    @property
    def require_max_skills(self):
        return bool(self.config.MetaLeveling_RequireMaxSkills)

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
                task manages. The healer slot and the clearer slot (the
                mob-clearing carry in the vanguard) are never touched.
        """
        reserved = {
            self.config.MetaLeveling_HealerSlot,
            self.config.MetaLeveling_ClearerSlot,
        }
        for name, button in SLOT_BUTTONS.items():
            if name in reserved:
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

    def inspect_slot(self, slot, button):
        """
        Long-click a fleet slot, read the ship's level on her detail page
        (and her skill levels), return to the formation page.

        With ActivateSkills on, every inspected ship also gets her research
        slot checked: a META ship has ONE, and when the skill it sat on
        maxed out it falls idle - the account-wide skill EXP the farming
        generates then goes nowhere. The next unfinished skill is started
        (learning it if needed); feeding books stays MetaLab's job.

        Returns:
            str: 'leveled'         at or above TargetLevel and skills done,
                                   swap her out
                 'skills_pending'  at or above TargetLevel but a skill is
                                   still trainable, keep her until MetaLab
                                   maxes it
                 'in_progress'     below TargetLevel, keep leveling
                 'unknown'         could not read, keep

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr(f'Inspect slot {slot}', level=2)
        self.ship_info_enter(button, long_click=True, skip_first_screenshot=False)

        level = self.get_detail_level()
        logger.info(f'Slot {slot}: level {level}')

        manage_skills = bool(self.config.MetaLeveling_ActivateSkills)

        if level == 0:
            status = 'unknown'
        elif level < self.target_level:
            status = 'in_progress'
            if manage_skills:
                # She stays in the fleet a while longer, so keep her
                # research slot busy while she levels
                self.check_skills_maxed(start_research=True, allow_learn=True)
        elif not self.require_max_skills:
            status = 'leveled'
        else:
            # Level-finished: the skills decide whether she leaves the fleet
            skills = self.check_skills_maxed(start_research=manage_skills,
                                             allow_learn=manage_skills)
            if skills == 'unmaxed':
                logger.info(f'Slot {slot}: level {level} reached but skills are not '
                            'maxed yet, keeping her until MetaLab finishes them')
                status = 'skills_pending'
            else:
                if skills == 'unknown':
                    logger.warning(f'Slot {slot}: skill state could not be read, '
                                   'swapping on the level criterion alone')
                status = 'leveled'

        self.ui_back(check_button=page_fleet.check_button)
        return status

    def get_meta_candidate(self):
        """
        On the deploy picker, find the replacement: the HIGHEST-level free
        META ship below TargetLevel but at or above MinSwapLevel (ships
        below the floor belong to ExpFeed's pack feeding, not the fleet).
        Only the first page is scanned; with descending level sort the
        finished 120+ ships lead and the candidates follow right after.
        The REMOVE card in the first grid cell yields no level and is
        excluded by the scanner's level limitation.

        Also sets _unfinished_below_min: whether unfinished META ships
        exist below the floor (checked with an ascending re-sort so the
        low-level tail lands on the first page).

        Returns:
            Ship: from module.retire.scanner, or None if no candidate.

        Pages:
            in: DOCK_CHECK (deploy picker opened from a fleet slot)
        """
        self._unfinished_below_min = False
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(faction='meta')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('No META ship in the deploy picker')
            return None

        scanner = ShipScanner(level=(self.min_swap_level, self.target_level - 1),
                              emotion=(0, 150), fleet=0, status='free')
        scanner.disable('rarity')
        ships = scanner.scan(self.device.image, output=True)
        if ships:
            # Highest level first; on equal level prefer the higher emotion
            return max(ships, key=lambda ship: (ship.level, ship.emotion))

        logger.info(f'No free META ship in level range '
                    f'{self.min_swap_level}-{self.target_level - 1}')
        # Pipeline check: do unfinished METAs exist below the floor? Flip to
        # ascending so the lowest ships are on the first page.
        self.dock_sort_method_dsc_set(False, wait_loading=True)
        self.device.screenshot()
        scanner.set_limitation(level=(1, self.target_level - 1))
        remain = scanner.scan(self.device.image, output=False)
        self._unfinished_below_min = bool(remain)
        if self._unfinished_below_min:
            logger.info('Unfinished META ships exist below MinSwapLevel, '
                        'waiting for ExpFeed to level them up')
        return None

    def swap_slot(self, slot, button):
        """
        Plain-click a fleet slot to open the deploy picker and put the best
        replacement META ship into it.

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
        The between-batches pass: check every managed fleet slot and swap
        out ships that reached TargetLevel with all skills maxed. Sets
        leveling_complete when the whole fleet is finished and the dock has
        nobody left to swap in.

        Returns:
            bool: True if the fleet is ready to keep farming.

        Pages:
            in: Any
            out: page_fleet
        """
        logger.hr('META fleet maintenance', level=1)
        self.ui_goto_fleet()
        results = {}
        for slot, button in self.managed_slots():
            status = self.inspect_slot(slot, button)
            if status == 'leveled':
                status = self.swap_slot(slot, button)
            results[slot] = status
            self.device.click_record_clear()
        logger.info(f'META fleet maintenance results: {results}')

        if results and all(status == 'no_candidate' for status in results.values()):
            if self._unfinished_below_min:
                logger.info('All managed slots are leveled and the remaining METAs '
                            'are below MinSwapLevel; waiting for ExpFeed to feed '
                            'them up instead of disabling')
            else:
                self.leveling_complete = True
            return False
        # Farming is useful as long as one managed slot is still leveling
        if results and not any(status in ('in_progress', 'swapped', 'unknown')
                               for status in results.values()):
            if 'skills_pending' in results.values():
                logger.info('Every managed slot is at TargetLevel and some are still '
                            'waiting for MetaLab to max their skills before they can '
                            'be swapped out')
            else:
                logger.warning('No slot is leveling and some could not be swapped')
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
            if self.leveling_complete:
                logger.hr('All META ships are at TargetLevel', level=1)
                logger.info('MetaLeveling has no ship left to level and disables itself. '
                            'MetaLab keeps handling skills/fortification/activation.')
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
            logger.hr(f'Farm {batch} runs until the next fleet check', level=1)
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
