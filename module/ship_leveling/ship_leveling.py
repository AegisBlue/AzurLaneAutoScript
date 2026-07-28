"""
ShipLeveling - the successor to MetaLeveling.

MetaLeveling farms a stage with a fleet of META ships and holds each one until
her skills are maxed. That last part turned out to be waste: META skill EXP
comes from account-wide daily missions, so a META earns it whether or not she
is in the fleet, and the fleet slot she occupies while waiting could have been
levelling somebody else.

So this task levels, and only levels:

1. META ships first, up to TargetLevel. As soon as one arrives, her research
   slot is pointed at an unfinished skill (the same care MetaLeveling took)
   and she is swapped straight out - no waiting on skills.
2. Then regular ships that still have something to gain, highest level first,
   sourced from the dock's own "not level max" filter and cross-checked
   against the ShipCensus store.
3. Then, when nobody on record still wants levels, the lowest-affinity ships
   are rotated through the fleet for the affinity endgame.

Everything the maintenance pass reads about a ship is written back into
config/ship_census.json, so the census dashboard stays current between sweeps.

MetaLab (skills / fortification / Somatic Activation) is untouched and still
does its own job on its own schedule. MetaLeveling is left in place but must
not be enabled at the same time - two campaign farmers would fight over the
same fleet.
"""
from module.base.decorator import cached_property
from module.logger import logger
from module.meta_leveling.meta_leveling import SLOT_BUTTONS, MetaLeveling
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY
from module.retire.scanner import ShipScanner
from module.ship_census.census import ShipCensus
from module.ship_census.store import CensusStore
from module.ship_leveling import progress
from module.ui.page import page_fleet


class ShipLeveling(MetaLeveling):
    # Set when neither the fleet nor the dock has anybody left who could gain
    # a level or a point of affinity from being carried.
    leveling_complete = False
    # Set by get_meta_candidate: unfinished METAs exist but all sit below
    # MetaMinSwapLevel, where ExpFeed's pack feeding owns them.
    _unfinished_below_min = False
    # Which rung of the candidate ladder the last swap reached, for logging
    # and for the exhaustion check.
    _last_candidate_kind = None

    # ---------------------------------------------------------------- config

    @property
    def target_level(self):
        return int(self.config.ShipLeveling_TargetLevel)

    @property
    def min_swap_level(self):
        """META floor - inherited get_meta_candidate reads this."""
        return int(self.config.ShipLeveling_MetaMinSwapLevel)

    @property
    def regular_min_level(self):
        return int(self.config.ShipLeveling_RegularMinSwapLevel)

    @property
    def manage_skills(self):
        return bool(self.config.ShipLeveling_ActivateSkills)

    @property
    def update_census(self):
        return bool(self.config.ShipLeveling_UpdateCensus)

    def managed_slots(self):
        """
        Yields:
            str, Button: Slot name and click target for every fleet slot this
                task manages. The healer slot and the clearer slot (the
                mob-clearing carry in the vanguard) are never touched.
        """
        reserved = {
            self.config.ShipLeveling_HealerSlot,
            self.config.ShipLeveling_ClearerSlot,
        }
        for name, button in SLOT_BUTTONS.items():
            if name in reserved:
                continue
            yield name, button

    # ---------------------------------------------------------------- state

    @cached_property
    def store(self):
        return CensusStore().load()

    @cached_property
    def state(self):
        return progress.SlotState().load()

    @cached_property
    def census(self):
        """
        A ShipCensus bound to this task's device and config, used purely as a
        reader for the ship detail page (name / HP / limit break / skills).

        A separate object rather than a base class on purpose: ShipCensus and
        MetaLab both define process_ship() with different signatures, and
        inheriting both would leave one of them shadowed by the other.
        """
        reader = ShipCensus(config=self.config, device=self.device)
        reader.star_totals = ShipCensus.learn_star_totals(self.store)
        return reader

    # ---------------------------------------------------------------- census

    def census_key(self, detail):
        """
        Which census record the ship on the detail page belongs to.

        Name plus HP identifies a hull; duplicate copies of the same ship share
        both, so the tie is broken on the recorded level - a fleet ship's own
        record is the one that last read closest to (and not above) where she
        is now.

        Returns:
            str: Record key, or None if the name would not read.
        """
        name, hp = detail.get('name'), detail.get('hp')
        if not name:
            return None
        ships = self.store.ships

        def closest(keys):
            level = detail.get('level')
            if level is None or len(keys) == 1:
                return keys[0]
            below = [k for k in keys if (ships[k].get('level') or 0) <= level]
            pool = below or keys
            return min(pool, key=lambda k: abs((ships[k].get('level') or 0) - level))

        exact = [k for k, s in ships.items() if s.get('name') == name and s.get('hp') == hp]
        if exact:
            return closest(exact)
        by_name = [k for k, s in ships.items() if s.get('name') == name]
        if by_name:
            logger.info('No record of {!r} at HP {}, matching on the name alone'.format(name, hp))
            return closest(by_name)
        # New ship (or one the last sweep never reached): file her under the
        # next free copy number
        n = 1
        while '{}#{}'.format(name, n) in ships:
            n += 1
        return '{}#{}'.format(name, n)

    def record_detail(self, detail):
        """
        Write a detail-page read back into the census store.

        Never deep=True: this read has no affinity and no enhance state, and
        marking the record deep-scanned would tell the census delta sweep it
        has nothing left to collect on this ship.

        Returns:
            (str, dict): Record key (None if the name would not read) and a
                record to judge her by - the stored one when the census is
                being written, otherwise this read on its own.
        """
        fields = dict(
            name=detail['name'],
            level=detail['level'],
            hp=detail['hp'],
            is_meta=detail['is_meta'],
            is_research=detail['is_research'],
            no_enhance=detail['no_enhance'],
            lb_current=detail['lb_current'],
            lb_max=detail['lb_max'],
            skills=detail['skills'],
        )
        if detail.get('rarity') is not None:
            fields['rarity'] = detail['rarity']
        if detail.get('oath_badge'):
            fields['oathed'] = True

        key = self.census_key(detail)
        if key is None or not self.update_census:
            stored = self.store.ships.get(key) if key else None
            # Judge her by what was just read, not by a record that may predate
            # the last ten levels she gained
            merged = dict(stored) if stored else {}
            merged.update(fields)
            return key, merged

        fields['copy'] = int(key.rsplit('#', 1)[1])
        ship = self.store.record(key, **fields)
        self.store.save()
        return key, ship

    def farm_work_left(self):
        """
        Whether anybody on record could still gain a level or reach Love.

        Returns:
            (int, int): ships wanting levels, ships wanting affinity.
        """
        return progress.farm_work_left(self.store.ships.values(), self.target_level)

    # ------------------------------------------------------------ inspection

    def inspect_slot(self, slot, button):
        """
        Long-click a fleet slot, read the occupant off her detail page, decide
        whether she stays.

        A META below TargetLevel and a regular ship below her reachable ceiling
        both keep farming. A META at TargetLevel gets her research slot pointed
        at an unfinished skill and then leaves regardless of skill state - the
        skill EXP arrives from account-wide missions either way, so holding the
        slot for her would only cost somebody else their levels.

        Returns:
            str: 'leveled'     done here, swap her out
                 'in_progress' keep farming with her
                 'unknown'     could not read, keep

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr('Inspect slot {}'.format(slot), level=2)
        self.ship_info_enter(button, long_click=True, skip_first_screenshot=False)
        self.census.ensure_info_view()
        detail = self.census.read_ship_detail()

        level, name = detail['level'], detail['name']
        key, ship = self.record_detail(detail)
        kind = 'meta' if detail['is_meta'] else 'regular'
        record = self.state.note(slot, name, detail['hp'], level, key=key, kind=kind)
        self.state.save()

        cap = progress.level_cap(ship, self.target_level) if ship else self.target_level
        logger.info('Slot {}: {!r} ({}) Lv.{}/{}, stalls {}'.format(
            slot, name, kind, level, cap, record['stalls']))

        if level is None:
            status = 'unknown'
        elif detail['is_meta']:
            # cap is min(TargetLevel, 120) here - a META cannot go past 120
            # however high the target is set
            if level < cap:
                status = 'in_progress'
                if self.manage_skills:
                    # She stays a while longer, so keep her research slot busy
                    self.check_skills_maxed(start_research=True, allow_learn=True)
            else:
                if self.manage_skills:
                    # Last chance to hand her research slot a new skill before
                    # she leaves the fleet - MetaLab keeps feeding it books
                    self.check_skills_maxed(start_research=True, allow_learn=True)
                logger.info('Slot {}: META at {}, swapping her out; her skill research '
                            'runs on account-wide missions from here'.format(slot, cap))
                status = 'leveled'
        elif level >= cap:
            logger.info('Slot {}: at her ceiling ({}), swapping her out'.format(slot, cap))
            status = 'leveled'
        elif progress.looks_walled(level, record['stalls']):
            # A Cognitive Awakening step the census cannot see: she has not
            # gained a level in two full maintenance passes and sits on a
            # multiple of 5 at or above 100.
            logger.info('Slot {}: Lv.{} has not moved in {} passes - she is at an awakening '
                        'ceiling, swapping her out'.format(slot, level, record['stalls']))
            status = 'leveled'
        else:
            status = 'in_progress'

        self.ui_back(check_button=page_fleet.check_button)
        return status

    # ------------------------------------------------------------ candidates

    def get_regular_candidate(self):
        """
        On the deploy picker, the highest-level free regular ship who can still
        gain EXP.

        The dock's own "not level max" filter is the authority on that - it
        knows about limit-break walls and awakening steps the census cannot
        see. Descending level sort then puts the best candidates on the first
        page, which is the only page a ShipScanner can read.

        The picker is opened from a fleet slot, so it is already restricted to
        hulls that fit the slot (main slots show main ships only).

        Returns:
            Ship: from module.retire.scanner, or None.

        Pages:
            in: DOCK_CHECK
        """
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(sort='level', extra='not_level_max')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('No levellable ship in the deploy picker')
            return None

        scanner = ShipScanner(level=(self.regular_min_level, self.target_level - 1),
                              emotion=(0, 150), fleet=0, status='free')
        scanner.disable('rarity')
        ships = scanner.scan(self.device.image, output=True)
        if ships:
            # Highest level first; on equal level prefer the higher emotion
            return max(ships, key=lambda ship: (ship.level, ship.emotion))

        logger.info('No free levellable ship in level range {}-{}'.format(
            self.regular_min_level, self.target_level - 1))
        return None

    def get_affinity_candidate(self):
        """
        The affinity endgame: when nobody is short of levels any more, carry
        the ships who are short of Love instead.

        Affinity is not readable on a dock card, so this leans on the game's
        own intimacy sort - ascending, so the least-loved ships are the front
        cards - and lets the census delta sweeps measure the progress. Ships
        already in a fleet are excluded by the scanner, so filling several
        slots in one pass walks down the list instead of picking the same ship
        again.

        Returns:
            Ship: or None.

        Pages:
            in: DOCK_CHECK
        """
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_filter_set(sort='intimacy', extra='no_limit')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            return None

        scanner = ShipScanner(level=(1, 125), emotion=(0, 150), fleet=0, status='free')
        scanner.disable('rarity')
        ships = scanner.scan(self.device.image, output=True)
        if not ships:
            logger.info('No free ship on the intimacy-sorted first page')
            return None
        # Grid order, so the first card is the least loved ship the picker offers
        return ships[0]

    def swap_slot(self, slot, button):
        """
        Plain-click a fleet slot to open the deploy picker and put the best
        available replacement into it - METAs first, then regular ships who
        can still level, then the affinity rotation.

        Returns:
            str: 'swapped' or 'no_candidate'

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr('Swap slot {}'.format(slot), level=2)
        self.ship_info_enter(button, check_button=DOCK_CHECK,
                             long_click=False, skip_first_screenshot=False)

        kind = 'meta'
        candidate = self.get_meta_candidate()
        if candidate is None:
            kind = 'regular'
            candidate = self.get_regular_candidate()
        if candidate is None:
            kind = 'affinity'
            candidate = self.get_affinity_candidate()
        if candidate is None:
            self._last_candidate_kind = None
            self.dock_reset()
            self.ui_back(check_button=page_fleet.check_button)
            return 'no_candidate'

        self._last_candidate_kind = kind
        logger.info('Swap in {} ship: level {}, emotion {}'.format(
            kind, candidate.level, candidate.emotion))
        self.dock_select_one(candidate.button)
        self.dock_reset()
        self.dock_select_confirm(check_button=page_fleet.check_button)
        self.record_fleet_emotion(candidate.emotion)
        self.state.clear(slot)
        self.state.save()
        return 'swapped'

    def identify_slot(self, slot, button):
        """
        Read a freshly swapped-in ship off her detail page so the census and
        the slot state know who is standing there before the next batch of
        runs starts.

        Pages:
            in: page_fleet
            out: page_fleet
        """
        self.ship_info_enter(button, long_click=True, skip_first_screenshot=False)
        self.census.ensure_info_view()
        detail = self.census.read_ship_detail()
        key, _ = self.record_detail(detail)
        self.state.note(slot, detail['name'], detail['hp'], detail['level'], key=key,
                        kind='meta' if detail['is_meta'] else 'regular')
        self.state.save()
        logger.info('Slot {} now holds {!r} (Lv.{})'.format(slot, detail['name'], detail['level']))
        self.ui_back(check_button=page_fleet.check_button)

    # ----------------------------------------------------------- maintenance

    def fleet_maintenance(self):
        """
        The between-batches pass: inspect every managed fleet slot, swap out
        whoever is finished, and read in whoever replaced her.

        Returns:
            bool: True if the fleet is worth farming with.

        Pages:
            in: Any
            out: page_fleet
        """
        logger.hr('Fleet maintenance', level=1)
        self.ui_goto_fleet()
        results = {}
        for slot, button in self.managed_slots():
            status = self.inspect_slot(slot, button)
            if status == 'leveled':
                status = self.swap_slot(slot, button)
                if status == 'swapped':
                    self.identify_slot(slot, button)
            results[slot] = status
            self.device.click_record_clear()
        logger.info('Fleet maintenance results: {}'.format(results))

        if not results:
            logger.warning('No managed fleet slot - HealerSlot and ClearerSlot cover the '
                           'whole fleet, there is nothing for this task to do')
            return False

        if all(status == 'no_candidate' for status in results.values()):
            levels, affinities = self.farm_work_left()
            if self._unfinished_below_min:
                logger.info('Every managed slot is finished and the remaining METAs are '
                            'below MetaMinSwapLevel; waiting for ExpFeed to feed them up')
            elif levels or affinities:
                logger.warning('The dock offered no candidate, but the census still lists '
                               '{} ships short of level and {} short of Love - the census '
                               'may be stale, or they are all in fleets or below '
                               'RegularMinSwapLevel'.format(levels, affinities))
            else:
                self.leveling_complete = True
            return False

        # Farming pays as long as one managed slot is still gaining something
        if not any(status in ('in_progress', 'swapped', 'unknown')
                   for status in results.values()):
            logger.warning('No slot is levelling and some could not be swapped')
            return False
        return True

    def dorm_sync(self):
        """
        Put the farming fleet into the dorm, where emotion recovers at 2-2.5x
        the outside rate and ships gain passive affinity and EXP.

        Not implemented yet: the dorm's ship-management UI is unmapped (no ALAS
        task touches it - the Dorm task only feeds and collects), so it needs a
        capture session of its own before any of it can be automated.
        """
        logger.warning('DormSync is enabled but the dorm roster UI has not been mapped yet, '
                       'skipping. Turn it off until the dorm capture session is done.')

    # ------------------------------------------------------------------- run

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

        levels, affinities = self.farm_work_left()
        logger.info('Census: {} ships on record, {} short of level, {} short of Love'.format(
            len(self.store.ships), levels, affinities))
        if not self.store.ships:
            logger.warning('The ship census is empty - candidate picking falls back on the '
                           'dock filters alone. Run ShipCensus to fill it in.')

        while 1:
            ready = self.fleet_maintenance()
            if bool(self.config.ShipLeveling_DormSync):
                self.dorm_sync()
            if self.leveling_complete:
                logger.hr('Nothing left to level', level=1)
                logger.info('Every ship on record is at her ceiling and at Love, and the '
                            'dock offers no candidate. ShipLeveling disables itself.')
                self.config.Scheduler_Enable = False
                self.config.task_stop()
            if not ready:
                logger.warning('Fleet is not ready to farm, delay and retry later')
                self.config.task_delay(minute=360)
                self.config.task_stop()

            batch = max(1, int(self.config.ShipLeveling_CheckInterval))
            if total:
                batch = min(batch, total - farmed)
                if batch <= 0:
                    break
            logger.hr('Farm {} runs until the next fleet check'.format(batch), level=1)
            super(MetaLeveling, self).run(name=name, folder=folder, mode=mode, total=batch)
            farmed += self.run_count

            if self.run_count < batch:
                # The inner loop stopped early: emotion recovery, oil limit,
                # run count exhausted or a commission notice. Whatever caused
                # it has already set a task delay or disabled the scheduler.
                logger.info('Campaign stopped before the batch was finished, '
                            'yield to the scheduler')
                self.config.task_stop()
