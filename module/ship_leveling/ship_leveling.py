"""
ShipLeveling - the successor to MetaLeveling.

MetaLeveling farms a stage with a fleet of META ships and holds each one until
her skills are maxed. That last part turned out to be waste: META skill EXP
comes from account-wide daily missions, so a META earns it whether or not she
is in the fleet, and the fleet slot she occupies while waiting could have been
levelling somebody else.

So this task levels, and only levels:

1. META ships first, up to MetaTargetLevel. As soon as one arrives, her
   research slot is pointed at an unfinished skill (the same care MetaLeveling
   took) and she is swapped straight out - no waiting on skills.
2. Then regular ships that still have something to gain, up to TargetLevel and
   highest level first, sourced from the dock's own "not level max" filter and
   cross-checked against the ShipCensus store.
3. Then, when nobody on record still wants levels, the lowest-affinity ships
   are rotated through the fleet for the affinity endgame.

METAs and ordinary ships get separate targets because one number cannot serve
both: past 100 a META needs tens of thousands of EXP for a single level, while
the ordinary ships worth picking up on this account sit between 100 and 120.
A shared target either parks METAs in the fleet for weeks or leaves the regular
rung with nobody to offer.

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
from module.retire.dock import DOCK_SCROLL, OCR_DOCK_SELECTED
from module.retire.scanner import ShipScanner
from module.ship_census.census import ShipCensus, _names_alike
from module.ship_census.store import CensusStore
from module.ship_leveling import progress
from module.ship_leveling.dorm_sync import DormRoster
from module.ui.page import page_fleet

# Picker pages walked looking for the level band. Sorted descending, the front
# of the list is everyone above TargetLevel, so the band this task wants can be
# several pages down.
CANDIDATE_PAGES = 8
# Cards tried per rung before giving up on it. A card can refuse the selection
# ("In action"), and the next best is usually just as good.
CANDIDATE_TRIES = 4
# Fuzzy-name cutoff for deciding two reads are the same ship. Looser than the
# census's own 0.8 because the failure this guards against is TRUNCATION:
# 'Yukikaze' read as 'Y ukik' scores 0.77 and slipped through at 0.8, minting a
# second record of a ship who already had one. Every use also requires the match
# to be unique, which is what makes a loose cutoff safe.
NAME_CUTOFF = 0.65
# How far a hull's HP may drift and still be the same ship. HP is NOT constant:
# it climbs as she levels (Yukikaze read 2161 at Lv.114, 2182 at Lv.116), so an
# exact comparison misses her a level later. Different ships are orders apart.
HP_DRIFT_RATIO = 0.06


def _hp_alike(a, b):
    """Whether two HP readings plausibly belong to the same hull."""
    if not a or not b:
        return False
    return abs(a - b) <= max(a, b) * HP_DRIFT_RATIO


class ShipLeveling(MetaLeveling, DormRoster):
    # Set when neither the fleet nor the dock has anybody left who could gain
    # a level or a point of affinity from being carried.
    leveling_complete = False
    # Set by get_meta_candidate: unfinished METAs exist but all sit below
    # MetaMinSwapLevel, where ExpFeed's pack feeding owns them.
    _unfinished_below_min = False
    # Which rung of the candidate ladder the last swap reached, for logging
    # and for the exhaustion check.
    _last_candidate_kind = None
    # Whether the last batch of campaign runs actually happened. A ship can only
    # be judged stuck at a level ceiling if she was given battles to gain one.
    _farmed_last_batch = False

    # ---------------------------------------------------------------- config

    @property
    def target_level(self):
        return int(self.config.ShipLeveling_TargetLevel)

    @property
    def meta_target_level(self):
        """
        METAs get their own target, and it is normally much lower.

        One number cannot serve both. Live on this account: with a single
        target of 100 the vanguard picker had nobody at all in the 70-99 band -
        every vanguard who can still gain EXP is either above 100 or under 45 -
        so the regular rung came up empty every pass. Raising the target to 120
        fills it (117, 114, 113, 110 sit on the first page), but the same
        number would then hold METAs in the fleet for the 100 -> 120 climb,
        which costs 87k EXP for a single level and blocks the slot for weeks.
        """
        return int(self.config.ShipLeveling_MetaTargetLevel)

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

    def census_key(self, detail, expect_key=None):
        """
        Which census record the ship on the detail page belongs to.

        The ladder is ShipCensus.repair_match_key's, for the same reason: the
        name comes off a stylised chip over moving artwork and misreads
        constantly. Live, Yukikaze read as 'Y ukik a ze' on one pass out of
        four, and filing that under a fresh key put a second, permanently
        incomplete record of her in the store - and made the dorm think the
        fleet had changed.

        HP is the sounder handle of the two: a plain digit read on opaque
        chrome, and near-unique per hull. So an unrecognised name falls back to
        HP before it is ever allowed to mint a key.

        Duplicate copies of one ship share name and HP, so those ties break on
        the recorded level - a fleet ship's own record is the one that last read
        closest to (and not above) where she is now.

        Args:
            detail (dict): From ShipCensus.read_ship_detail.
            expect_key (str): The record this slot held last time, when the
                caller is re-reading a slot it has not swapped. That is stronger
                than anything on screen and is checked first.

        Returns:
            str: Record key, or None if the name would not read.
        """
        name, hp = detail.get('name'), detail.get('hp')
        if not name:
            return None
        ships = self.store.ships

        if expect_key and expect_key in ships:
            # The caller knows who was standing in this slot last time, and the
            # task never changes an occupant except through swap_slot, which
            # clears the slot. So the slot's own record beats any reading -
            # provided this really is still her, which the hull settles.
            prior = ships[expect_key]
            if _names_alike(prior.get('name'), name, cutoff=NAME_CUTOFF) \
                    or _hp_alike(prior.get('hp'), hp):
                return expect_key
            logger.info('Slot expected {}, but {!r} at HP {} is somebody else'.format(
                expect_key, name, hp))

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
        if hp:
            by_hp = [k for k, s in ships.items() if s.get('hp') == hp]
            if len(by_hp) == 1:
                logger.info('Name read as {!r}, which matches no record; HP {} is '
                            '{}'.format(name, hp, by_hp[0]))
                return by_hp[0]
        alike = [k for k, s in ships.items()
                 if _names_alike(s.get('name'), name, cutoff=NAME_CUTOFF)]
        if len(alike) == 1:
            logger.info('Name read as {!r}, matched {} on a near miss'.format(name, alike[0]))
            return alike[0]
        if hp:
            drifted = [k for k, s in ships.items() if _hp_alike(s.get('hp'), hp)]
            if len(drifted) == 1:
                logger.info('Name read as {!r}; HP {} is within a level or two of '
                            '{}'.format(name, hp, drifted[0]))
                return drifted[0]
        # New ship (or one the last sweep never reached): file her under the
        # next free copy number
        n = 1
        while '{}#{}'.format(name, n) in ships:
            n += 1
        return '{}#{}'.format(name, n)

    def record_detail(self, detail, expect_key=None):
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

        key = self.census_key(detail, expect_key=expect_key)
        stored = self.store.ships.get(key) if key else None
        if stored and stored.get('name') and stored['name'] != fields['name']:
            # The key was matched on HP or a near miss, so the stored name is
            # the better read of the two. Overwriting it would let one bad OCR
            # pass rename a ship - and the dorm sync compares fleet rosters by
            # name, so it would also read as "the fleet changed".
            logger.info('Name read as {!r} but {} is on record as {!r} - keeping the '
                        'stored name'.format(fields['name'], key, stored['name']))
            fields['name'] = stored['name']

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
        return progress.farm_work_left(self.store.ships.values(), self.target_level,
                                       self.meta_target_level)

    # ------------------------------------------------------------ inspection

    def inspect_slot(self, slot, button):
        """
        Long-click a fleet slot, read the occupant off her detail page, decide
        whether she stays.

        A META below MetaTargetLevel and a regular ship below her reachable
        ceiling both keep farming. A META at her target gets her research slot
        pointed at an unfinished skill and then leaves regardless of skill state
        - the skill EXP arrives from account-wide missions either way, so
        holding the slot for her would only cost somebody else their levels.

        A ship BELOW the swap-in floor leaves too. The floor is the answer to
        "is this ship worth a fleet slot", and it has to mean the same thing
        coming and going: a ship the task would never pick has no business
        keeping the slot from one it would. Without that, one bad placement is
        permanent - live, an early bug put a Lv.1 ship into a main slot and
        every later pass dutifully kept her there because she was, technically,
        below her ceiling.

        Returns:
            str: 'leveled'      done here, swap her out
                 'below_floor'  not worth the slot, swap her out
                 'in_progress'  keep farming with her
                 'unknown'      could not read, keep

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr('Inspect slot {}'.format(slot), level=2)
        self.ship_info_enter(button, long_click=True, skip_first_screenshot=False)
        self.census.ensure_info_view()
        detail = self.census.read_ship_detail()

        level = detail['level']
        key, ship = self.record_detail(detail, expect_key=self.state.get(slot).get('key'))
        # The census record's name, which survives a bad OCR pass; see census_key
        name = (ship or {}).get('name') or detail['name']
        kind = 'meta' if detail['is_meta'] else 'regular'
        record = self.state.note(slot, name, detail['hp'], level, key=key, kind=kind,
                                 count_stall=self._farmed_last_batch)
        self.state.save()

        cap = progress.level_cap(ship, self.target_level, self.meta_target_level) \
            if ship else self.target_level
        floor = self.min_swap_level if detail['is_meta'] else self.regular_min_level
        logger.info('Slot {}: {!r} ({}) Lv.{}, floor {}, ceiling {}, stalls {}'.format(
            slot, name, kind, level, floor, cap, record['stalls']))

        if level is None:
            status = 'unknown'
        elif level < floor:
            logger.info('Slot {}: Lv.{} is below the swap-in floor of {} - this task would '
                        'never have chosen her, so she does not keep the slot'.format(
                            slot, level, floor))
            status = 'below_floor'
        elif detail['is_meta']:
            # cap is min(MetaTargetLevel, 120) here - a META cannot go past 120
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

    def get_meta_candidates(self):
        """
        Free META ships below TargetLevel but at or above MetaMinSwapLevel,
        best first. Ships below the floor belong to ExpFeed's pack feeding.

        The list version of MetaLeveling.get_meta_candidate, so a card that
        refuses the selection does not end the swap. Only the first page is
        scanned: METAs are few and the descending sort puts the finished ones
        first, with the candidates right behind them.

        Also sets _unfinished_below_min - whether unfinished METAs exist below
        the floor, checked with an ascending re-sort so the low-level tail lands
        on the first page.

        Returns:
            list[Ship]:

        Pages:
            in: DOCK_CHECK
        """
        self._unfinished_below_min = False
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(faction='meta')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('No META ship in the deploy picker')
            return []

        ships = self.scan_picker()
        ranked = self.rank_candidates(ships, self.min_swap_level,
                                      self.meta_target_level - 1)
        if ranked:
            return ranked

        logger.info('No free META ship in level range {}-{}'.format(
            self.min_swap_level, self.meta_target_level - 1))
        # Pipeline check: do unfinished METAs exist below the floor? Flip to
        # ascending so the lowest ships are on the first page.
        self.dock_sort_method_dsc_set(False, wait_loading=True)
        self.device.screenshot()
        remain = self.rank_candidates(self.scan_picker(), 1, self.meta_target_level - 1)
        self._unfinished_below_min = bool(remain)
        if self._unfinished_below_min:
            logger.info('Unfinished META ships exist below MetaMinSwapLevel, '
                        'waiting for ExpFeed to level them up')
        return []

    def scan_picker(self):
        """
        Every card on the deploy picker's current page, unfiltered.

        The scanner's own level/fleet limits are applied afterwards instead of
        inside it, because the paging in get_regular_candidates has to see the
        levels it is scrolling past to know when to stop.

        Returns:
            list[Ship]:
        """
        scanner = ShipScanner(level=(1, 125), emotion=(0, 150),
                              fleet=[0, 1, 2, 3, 4, 5, 6], status='any')
        scanner.disable('rarity')
        return scanner.scan(self.device.image, output=False)

    @staticmethod
    def rank_candidates(ships, low, high):
        """Free, unfleeted ships in the level band, best first."""
        pool = [ship for ship in ships
                if ship.fleet == 0 and ship.status == 'free'
                and low <= ship.level <= high]
        # Highest level first; on equal level prefer the higher emotion
        return sorted(pool, key=lambda ship: (ship.level, ship.emotion), reverse=True)

    def get_regular_candidates(self):
        """
        Free regular ships who can still gain EXP, best first.

        The dock's own "not level max" filter is the authority on "can still
        gain EXP" - live, it excludes a Lv.125 ship, excludes a Lv.70 ship at
        zero limit breaks, and offers a Lv.70 ship who is max limit broken. The
        census cannot make that last distinction.

        The list has to be PAGED, which the first version of this did not do.
        Sorted by level descending, the front of the picker is the ships above
        TargetLevel - the awakened ones still climbing to 125 - and the band
        this task wants starts pages further down. Live, scanning only the first
        page found nothing in 70-99 and dropped through to the affinity
        rotation, which put a Lv.1 ship into the fleet.

        The picker is opened from a fleet slot, so it is already restricted to
        hulls that fit the slot (main slots show main ships only).

        Returns:
            list[Ship]:

        Pages:
            in: DOCK_CHECK
        """
        low, high = self.regular_min_level, self.target_level - 1
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(sort='level', extra='not_level_max')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            logger.info('No levellable ship in the deploy picker')
            return []

        for page in range(CANDIDATE_PAGES):
            ships = self.scan_picker()
            ranked = self.rank_candidates(ships, low, high)
            levels = [ship.level for ship in ships if ship.level]
            logger.info('Picker page {}: levels {}'.format(
                page + 1, sorted(levels, reverse=True)))
            if ranked:
                logger.info('Levellable candidates: {}'.format(
                    [ship.level for ship in ranked[:5]]))
                return ranked
            if levels and min(levels) < low:
                # Sorted descending, so the band is behind us now
                break
            if not DOCK_SCROLL.appear(main=self) or DOCK_SCROLL.at_bottom(main=self):
                break
            DOCK_SCROLL.next_page(main=self, page=0.6)
            self.device.sleep((0.5, 0.8))
            self.device.screenshot()

        logger.info('No free levellable ship in level range {}-{}'.format(low, high))
        return []

    def get_affinity_candidates(self):
        """
        The affinity endgame: when nobody is short of levels any more, carry
        the ships who are short of Love instead.

        Gated on the census saying so. Without that gate this rung fires
        whenever the level rungs happen to come up empty on a picker page, and
        it is not choosy - live, it put a Lv.1 ship into the fleet while 400+
        ships on record still wanted levels.

        Affinity is not readable on a dock card, so this leans on the game's own
        intimacy sort - ascending, so the least-loved ships are the front cards
        - and lets the census delta sweeps measure the progress. Ships already
        in a fleet are excluded, so filling several slots in one pass walks down
        the list instead of picking the same ship again.

        Returns:
            list[Ship]:

        Pages:
            in: DOCK_CHECK
        """
        if not self.store.ships:
            logger.info('No census on record, so there is no way to tell that levelling '
                        'is finished - not starting the affinity rotation')
            return []
        levels, affinities = self.farm_work_left()
        if levels:
            logger.info('{} ships on record still want levels, so the affinity rotation '
                        'stays shut'.format(levels))
            return []
        if not affinities:
            return []

        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_filter_set(sort='intimacy', extra='no_limit')

        if self.appear(DOCK_EMPTY, offset=(20, 20)):
            return []

        # Grid order, so the front cards are the least loved the picker offers
        ships = [ship for ship in self.scan_picker()
                 if ship.fleet == 0 and ship.status == 'free' and ship.level]
        if not ships:
            logger.info('No free ship on the intimacy-sorted first page')
        return ships

    def dock_try_select(self, button, tries=3):
        """
        Tap a card in the deploy picker and check the counter actually moved.

        Some cards cannot be taken however often they are tapped - live, META
        ships marked "In action" (a state the stock StatusScanner has no EN
        template for) read as free, and the stock dock_select_one clicked one
        until ALAS raised GameTooManyClickError and the run died. The counter
        settles it without needing to know why a card refuses.

        Returns:
            bool: True if the picker now holds a selection.
        """
        for _ in range(tries):
            self.device.click(button)
            self.device.sleep((0.9, 1.3))
            self.device.click_record_clear()
            self.device.screenshot()
            if self.handle_popup_confirm('SHIP_LEVELING_SELECT'):
                continue
            current, _, total = OCR_DOCK_SELECTED.ocr(self.device.image)
            if total == 1 and current >= 1:
                return True
        return False

    def swap_slot(self, slot, button):
        """
        Plain-click a fleet slot to open the deploy picker and put the best
        available replacement into it - METAs first, then regular ships who can
        still level, then the affinity rotation.

        Each rung offers a ranked list rather than one ship, because a card can
        turn out to be untakeable (see dock_try_select) and the run should move
        on to the next best rather than die on it.

        Returns:
            str: 'swapped' or 'no_candidate'

        Pages:
            in: page_fleet
            out: page_fleet
        """
        logger.hr('Swap slot {}'.format(slot), level=2)
        self.ship_info_enter(button, check_button=DOCK_CHECK,
                             long_click=False, skip_first_screenshot=False)

        for kind, getter in (('meta', self.get_meta_candidates),
                             ('regular', self.get_regular_candidates),
                             ('affinity', self.get_affinity_candidates)):
            for candidate in getter()[:CANDIDATE_TRIES]:
                logger.info('Swap in {} ship: level {}, emotion {}'.format(
                    kind, candidate.level, candidate.emotion))
                if not self.dock_try_select(candidate.button):
                    logger.warning('{} would not take the selection (in action, or '
                                   'already deployed) - trying the next'.format(
                                       candidate.button.name))
                    continue
                self._last_candidate_kind = kind
                self.dock_reset()
                self.dock_select_confirm(check_button=page_fleet.check_button)
                self.record_fleet_emotion(candidate.emotion)
                self.state.clear(slot)
                self.state.save()
                return 'swapped'

        self._last_candidate_kind = None
        self.dock_reset()
        self.ui_back(check_button=page_fleet.check_button)
        return 'no_candidate'

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
        key, ship = self.record_detail(detail)
        name = (ship or {}).get('name') or detail['name']
        self.state.note(slot, name, detail['hp'], detail['level'], key=key,
                        kind='meta' if detail['is_meta'] else 'regular')
        self.state.save()
        logger.info('Slot {} now holds {!r} (Lv.{})'.format(slot, name, detail['level']))
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
            if status in ('leveled', 'below_floor'):
                swap = self.swap_slot(slot, button)
                if swap == 'swapped':
                    self.identify_slot(slot, button)
                    status = 'swapped'
                elif status == 'below_floor':
                    # Nobody better on offer. She is under the floor, but she is
                    # still gaining levels, and farming with her beats not
                    # farming at all - so this is not a reason to stop.
                    logger.info('Slot {}: no better candidate than the below-floor ship, '
                                'keeping her'.format(slot))
                    status = 'in_progress'
                else:
                    status = swap
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

    def ui_goto_fleet(self):
        """
        As MetaLeveling, but escaping the dorm first.

        Neither the dorm roster dialog nor its ship picker is a page ui_ensure
        knows, so a run that ends inside one and comes back here would raise
        GamePageUnknownError rather than navigate.
        """
        self.dorm_escape()
        super().ui_goto_fleet()

    # ------------------------------------------------------------------ dorm

    @property
    def dorm_fleet_index(self):
        """The dorm should hold the ships of the fleet this task farms with."""
        return self.fleet_to_attack_index

    def fleet_roster_names(self):
        """
        Names of all six ships in the managed fleet.

        The managed slots are named already - every maintenance pass reads them
        off the detail page. The healer and clearer are not, because the task
        never touches them, so they are read once and kept in the state file.

        Most recently placed first. The dorm sync walks this list trying to add
        each one, and a ship who is already in the dorm costs two taps and a
        search to discover that - so the ship who just joined the fleet, who is
        the one actually missing, should be tried first.

        Pages:
            in: page_fleet
            out: page_fleet
        """
        found = []
        for slot, button in SLOT_BUTTONS.items():
            name = self.state.get(slot).get('name')
            if not name:
                logger.info('Slot {} has no name on record, reading it for the '
                            'dorm'.format(slot))
                self.identify_slot(slot, button)
                name = self.state.get(slot).get('name')
            if name:
                found.append((self.state.get(slot).get('since') or '', name))
        found.sort(key=lambda item: item[0], reverse=True)
        return [name for _, name in found]

    def dorm_sync(self):
        """
        Keep the dorm holding the farming fleet: 40-50 morale per hour instead
        of 20, plus passive EXP and affinity for everyone in it.

        Skipped when the fleet has not changed since the last successful sync -
        which is most passes, and the whole trip costs nothing then.

        Pages:
            in: page_fleet
            out: page_fleet
        """
        names = self.fleet_roster_names()
        if len(names) < len(SLOT_BUTTONS):
            logger.warning('Only {} of {} fleet ships could be named, the dorm sync would '
                           'not be able to fill every place'.format(len(names),
                                                                    len(SLOT_BUTTONS)))
        last = self.state.data.get('dorm') or {}
        if last.get('roster') == names and last.get('result') in ('synced', 'in_sync'):
            logger.info('Fleet is unchanged since the last dorm sync, skipping')
            return

        result = self.dorm_sync_roster(names)
        self.state.data['dorm'] = dict(roster=names, result=result, at=progress.now())
        self.state.save()
        logger.info('Dorm sync: {}'.format(result))
        if result in ('synced', 'in_sync'):
            self.record_dorm_recovery()
        self.ui_goto_fleet()

    def record_dorm_recovery(self):
        """
        Tell the emotion model the fleet is in the dorm, if it does not know.

        Only ever set to floor 1 (40/h): the roster spans both floors and the
        model takes one rate for the whole fleet, so the lower one is the only
        safe answer - overestimating recovery is what sends a fleet into battle
        with a red face.
        """
        key = 'Emotion_Fleet{}Recover'.format(self.fleet_to_attack)
        current = getattr(self.config, key)
        if current == 'not_in_dormitory':
            logger.info('Fleet {} is in the dorm now, switching emotion recovery to '
                        'dormitory_floor_1'.format(self.fleet_to_attack))
            self.config.set_record(**{key: 'dormitory_floor_1'})

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
            self._farmed_last_batch = self.run_count > 0

            if self.run_count < batch:
                # The inner loop stopped early: emotion recovery, oil limit,
                # run count exhausted or a commission notice. Whatever caused
                # it has already set a task delay or disabled the scheduler.
                logger.info('Campaign stopped before the batch was finished, '
                            'yield to the scheduler')
                self.config.task_stop()
