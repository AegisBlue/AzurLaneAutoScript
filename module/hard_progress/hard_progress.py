"""
HardProgress - clear the hard-mode campaign stage by stage.

The stock Hard task farms one stage forever: it reads Hard.HardStage and
spends the game's three daily hard entries on it. Progressing through hard
mode that way means editing the stage by hand every time one is finished.

This task keeps the pointer itself. It walks the main campaign from NextStage
towards EndStage, and on every stage it:

1. Opens the stage popup and reads clear percentage and stars. Opening the
   popup is free - a daily entry is only spent once the sortie is confirmed -
   so a stage that already satisfies the criteria is recognised and skipped
   at no cost. Scanning and farming are therefore the same operation.
2. If the criteria are not met, sorties until they are. Both fleets are
   emptied on the preparation page and rebuilt with the in-game Recommend
   button, so every sortie goes out with a fleet picked from the dock as it is
   today rather than the one the game remembers from the last clear.
3. Writes the advanced pointer back to HardProgress.NextStage, so a restart
   picks up where it left off.

Two things end the task for good, both loud and both self-disabling, because
neither gets better by trying again tomorrow:

- The pointer walks past EndStage: hard mode is finished.
- A stage takes three entries in a row without gaining a single percent or
  star: either Recommend cannot build a fleet that satisfies the stage, or it
  can enter but loses. A defeat is not an error in ALAS - a lost battle is
  only logged - so this stall counter IS the defeat detector. Stages grinding
  a counted star, such as 5-3's "Defeat escort fleet (12/18)", are exempt:
  they gain nothing visible per entry but are not stuck.

Never enable this together with the stock Hard task. They share the same
three daily entries and would fight over them.
"""
import re

from module.base.timer import Timer
from module.campaign.campaign_ui import MODE_SWITCH_1
from module.campaign.run import CampaignRun
from module.exception import RequestHumanTakeover, ScriptEnd
from module.handler.fast_forward import to_map_file_name, to_map_input_name
from module.hard.hard import OCR_HARD_REMAIN
from module.logger import logger
from module.map.assets import (
    FLEET_1_ADVICE, FLEET_1_BAR, FLEET_1_CHOOSE, FLEET_1_CLEAR, FLEET_1_HARD_SATIESFIED, FLEET_1_IN_USE,
    FLEET_2_ADVICE, FLEET_2_BAR, FLEET_2_CHOOSE, FLEET_2_CLEAR, FLEET_2_HARD_SATIESFIED, FLEET_2_IN_USE,
    FLEET_2_IN_USE_W15, SUBMARINE_ADVICE, SUBMARINE_BAR, SUBMARINE_CHOOSE, SUBMARINE_CLEAR,
    SUBMARINE_HARD_SATIESFIED, SUBMARINE_IN_USE
)
from module.map.map_fleet_preparation import FleetOperator
from module.notify import handle_notify
from module.ui.page import page_campaign

STAGE_PATTERN = re.compile(r'^(\d+)-([1-4])$')
# Consecutive map entries that may gain nothing before the stage is called
# unclearable. Three is exactly one day's worth of entries, so a stall costs
# one wasted day at most and is confirmed by a fresh, free peek the next day.
NO_PROGRESS_LIMIT = 3
# The clear percentage comes off a colour bar, not a number, so it wobbles by
# about a percent between reads. Anything smaller than this is not progress.
PERCENT_NOISE = 0.02
# Recommend taps per fleet slot before giving up on it, and the seconds
# between taps / before the whole attempt is abandoned.
RECOMMEND_CLICKS = 5
RECOMMEND_INTERVAL = 3
RECOMMEND_TIMEOUT = 30
# Seconds to keep re-reading the requirement lines after the last tap. A row
# that is still animating reads unsatisfied, and mistaking that for "this dock
# cannot do it" would self-disable the task over nothing.
RECOMMEND_SETTLE = 6
# Same, for emptying a slot. One click does it; the budget is only there so a
# missed click gets a second chance.
CLEAR_CLICKS = 2
CLEAR_INTERVAL = 2
CLEAR_TIMEOUT = 15
# Consecutive task runs that may fail to even find the stage on screen before
# it is called end-of-content. One is not enough: a stray info bar or a slow
# chapter animation can eat a single attempt.
UI_FAILURE_LIMIT = 2


def parse_stage(name):
    """
    Args:
        name (str): Stage name, such as '7-2' or 'campaign_7_2'.

    Returns:
        tuple[int, int]: Chapter and stage, such as (7, 2).
    """
    res = STAGE_PATTERN.match(to_map_input_name(name))
    if res is None:
        logger.critical(f'"{name}" is not a main campaign stage name such as "7-2"')
        logger.critical('HardProgress.NextStage and HardProgress.EndStage must both look like "7-2"')
        raise RequestHumanTakeover
    return int(res.group(1)), int(res.group(2))


def next_stage(name):
    """
    Local string math, deliberately not campaign_name_increase(). That helper
    validates the new name against the map files of the *current* campaign
    folder, which becomes campaign_hard once the folder swap for 12-4 / 14-4
    happens - and campaign_hard holds only those two files, so the pointer
    would never leave 12-4.

    Args:
        name (str): Stage name, such as '7-4'.

    Returns:
        str: Next stage, such as '8-1'.
    """
    chapter, stage = parse_stage(name)
    if stage >= 4:
        return f'{chapter + 1}-1'
    return f'{chapter}-{stage + 1}'


def stage_le(name, other):
    """
    Returns:
        bool: If stage `name` comes at or before stage `other`.
    """
    return parse_stage(name) <= parse_stage(other)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class HardProgress(CampaignRun):
    # Stage the pointer currently sits on, such as '3-2'.
    current_stage = ''
    # Set from the campaign override when a stage has stalled, read once the
    # generic campaign loop has torn the UI down again.
    no_progress = False
    # run_count as of the last map peek, so a peek can tell whether a sortie
    # actually happened since the previous one. run_count itself is only
    # annotated on CampaignRun, not assigned, so it does not exist until
    # CampaignRun.run() sets it - give it a default rather than let a peek from
    # anywhere else die on AttributeError.
    run_count = 0
    watched_run_count = 0

    """
    Progress record

    Both counters have to survive across task runs - a stall is three entries
    spread over a day, and a UI failure ends the run that noticed it - so they
    live in the bound argument HardProgress.FailureRecord, stored as
    'stage=3-2,pct=85,star=2,fail=1,ui=0'. It is hidden from the GUI.
    """

    def record_read(self):
        """
        Returns:
            dict[str, str]:
        """
        data = {}
        for part in str(self.config.HardProgress_FailureRecord or '').split(','):
            key, sep, value = part.partition('=')
            if sep:
                data[key.strip()] = value.strip()
        return data

    def record_write(self, **kwargs):
        text = ','.join([f'{k}={v}' for k, v in kwargs.items()])
        if text != str(self.config.HardProgress_FailureRecord or ''):
            self.config.HardProgress_FailureRecord = text

    def record_clear(self):
        if self.config.HardProgress_FailureRecord:
            self.config.HardProgress_FailureRecord = ''

    """
    Stop conditions
    """

    def self_disable(self, title, reason):
        """
        Log loudly, notify, turn the task off and end it.

        Raises:
            TaskEnd:
        """
        logger.critical(reason)
        handle_notify(
            self.config.Error_OnePushConfig,
            title=f"Alas <{self.config.config_name}> HardProgress stopped",
            content=f"<{self.config.config_name}> {title}: {reason}"
        )
        self.config.Scheduler_Enable = False
        self.config.task_stop()

    def all_done_stop(self, stage, end):
        self.record_clear()
        self.self_disable(
            title='Hard campaign complete',
            reason=f'Hard stage {stage} is past EndStage {end}, so every hard stage satisfies '
                   f'HardProgress.Criteria. HardProgress disables itself'
        )

    def no_progress_stop(self, stage):
        self.self_disable(
            title='Hard stage stuck',
            reason=f'Hard stage {stage} gained no clear percentage and no star on '
                   f'{NO_PROGRESS_LIMIT} entries in a row. Either the fleets Recommend can build do not '
                   f'satisfy this stage, or they enter and lose. Clear it manually, or point '
                   f'HardProgress.NextStage past it, then re-enable the task. HardProgress disables itself'
        )

    def hard_unsatisfied_stop(self, stage):
        self.self_disable(
            title='Hard fleet unavailable',
            reason=f'Hard stage {stage} still rejects the fleets after tapping Recommend, so no fleet in '
                   f'this dock can satisfy its stat restrictions. Level up, or point HardProgress.NextStage '
                   f'past it, then re-enable the task. HardProgress disables itself'
        )

    def handle_campaign_ui_failure(self, stage, error):
        """
        The generic campaign loop only lets a ScriptEnd escape when
        ensure_campaign_ui() gave up finding the stage. The first time that
        may still be transient, so retry later. Twice in a row means the stage
        is not there: chapter locked, or the end of hard-mode content.

        Raises:
            TaskEnd:
        """
        record = self.record_read()
        strike = _int(record.get('ui')) + 1
        record['ui'] = strike
        self.record_write(**record)

        if strike >= UI_FAILURE_LIMIT:
            self.self_disable(
                title='Hard stage not found',
                reason=f'Hard stage {stage} could not be found on screen {strike} runs in a row ({error}). '
                       f'Its chapter is either locked or past the end of hard mode. Set '
                       f'HardProgress.EndStage to the last stage that exists, then re-enable the task. '
                       f'HardProgress disables itself'
            )

        logger.warning(f'Failed to reach hard stage {stage} ({error}), retry later')
        self.config.task_delay(success=False)
        self.config.task_stop()

    def reset_ui_failure(self):
        record = self.record_read()
        if _int(record.get('ui')):
            record['ui'] = 0
            self.record_write(**record)

    """
    Criteria
    """

    def criteria_met(self, campaign):
        """
        Args:
            campaign (CampaignBase): Holding the map info of the last peek.

        Returns:
            bool: If the stage satisfies HardProgress.Criteria.
        """
        criteria = self.config.HardProgress_Criteria
        if criteria == '100_percent_clear':
            return campaign.map_is_100_percent_clear
        if criteria == 'map_3_stars':
            return campaign.map_is_3_stars
        return campaign.map_is_100_percent_clear and campaign.map_is_3_stars

    @staticmethod
    def grinding_a_counted_star(campaign):
        """
        Some stages hide a star behind a counter that spans sorties. Hard 5-3
        asks for "Defeat escort fleet (12/18)" - eighteen escort fleets sunk in
        total, carried over between runs. Clearing the map adds to that counter
        without moving the clear percentage or lighting a star, so every entry
        looks like a wasted one to watch_progress() and a stage that is
        progressing perfectly well would be called unclearable and self-disable
        the task.

        A stage already at 100% with its defeat-all-enemies star earned has been
        finished by a fleet before, so it is not a defeat loop and it is not
        short of ships. Whatever is left is a counter, and counters need entries,
        not intervention. The stall counter stays for the cases it was written
        for: a stage that cannot be cleared at all.

        Args:
            campaign (CampaignBase): In MAP_PREPARATION, map info just read.

        Returns:
            bool: If the only thing left on this stage is a counter to grind.
        """
        if not campaign.map_is_100_percent_clear:
            return False
        # The star index whose condition is "defeat all enemies", per map config
        all_enemies = _int(campaign.config.STAR_REQUIRE_3)
        if not all_enemies:
            return False
        return bool(getattr(campaign, f'map_achieved_star_{all_enemies}', False))

    def watch_progress(self, campaign):
        """
        Called on every map peek, right after map_get_info(). Compares this
        peek against the best result recorded for the stage and counts the
        entries that gained nothing.

        Args:
            campaign (CampaignBase): In MAP_PREPARATION, map info just read.
        """
        stage = self.current_stage
        percent = float(campaign.map_clear_percentage)
        stars = int(campaign.map_achieved_star_1) \
            + int(campaign.map_achieved_star_2) \
            + int(campaign.map_achieved_star_3)
        record = self.record_read()
        ui = _int(record.get('ui'))

        # A peek is one read of the stage popup, which is NOT one sortie.
        # enter_map() returns to the popup and re-reads it whenever it has to
        # detour on the way in - a full dock sending it off to retire and
        # enhance, an urgent commission, a story, an info bar. Only a peek with
        # a finished sortie behind it can claim the stage gained nothing, so
        # the counter follows run_count, which the generic loop increments once
        # per completed campaign run.
        sortied = self.run_count > self.watched_run_count
        self.watched_run_count = self.run_count

        if record.get('stage') != stage:
            # First peek at this stage, nothing to compare against yet
            self.record_write(stage=stage, pct=int(percent * 100), star=stars, fail=0, ui=ui)
            return

        best_percent = _int(record.get('pct')) / 100
        best_stars = _int(record.get('star'))
        fail = _int(record.get('fail'))
        best_pct = int(max(percent, best_percent) * 100)
        best_star = max(stars, best_stars)

        if percent > best_percent + PERCENT_NOISE or stars > best_stars:
            logger.info(f'Hard stage {stage} progressed to {int(percent * 100)}%, {stars} star(s)')
            self.record_write(stage=stage, pct=best_pct, star=best_star, fail=0, ui=ui)
            return

        if not sortied:
            logger.info(f'Hard stage {stage} popup re-read without a sortie in between, '
                        f'not counting it as a failed attempt')
            self.record_write(stage=stage, pct=best_pct, star=best_star, fail=fail, ui=ui)
            return

        if self.grinding_a_counted_star(campaign):
            logger.info(f'Hard stage {stage} is grinding a counted star condition, '
                        f'not counting it as a failed attempt')
            self.record_write(stage=stage, pct=best_pct, star=best_star, fail=fail, ui=ui)
            return

        fail += 1
        logger.warning(f'Hard stage {stage} gained nothing on {fail} entry/entries in a row, '
                       f'still {int(percent * 100)}%, {stars} star(s)')
        self.record_write(stage=stage, pct=best_pct, star=best_star, fail=fail, ui=ui)
        if fail >= NO_PROGRESS_LIMIT:
            # Don't stop from in here. Returning True from triggered_map_stop()
            # lets the stock code cancel out of the popup and unwind cleanly,
            # and run() reads the flag once the UI is back on page_campaign.
            self.no_progress = True

    """
    Fleet Recommend
    """

    @staticmethod
    def fleet_operators(campaign):
        """
        The same two FleetOperators upstream builds in fleet_preparation().
        Rebuild them after anything that moves the fleet cards around, because
        __init__ is what loads the button offsets against the current layout.

        Args:
            campaign (CampaignBase): In FLEET_PREPARATION.

        Returns:
            dict[str, FleetOperator]: Keyed 'FLEET_1' and 'FLEET_2'.
        """
        fleet_1 = FleetOperator(
            choose=FLEET_1_CHOOSE, advice=FLEET_1_ADVICE, bar=FLEET_1_BAR, clear=FLEET_1_CLEAR,
            in_use=FLEET_1_IN_USE, hard_satisfied=FLEET_1_HARD_SATIESFIED, main=campaign)
        # FLEET_1_CLEAR moves up on the W15 layout and FLEET_2_IN_USE moves with it,
        # same check as module/map/map_fleet_preparation.py
        y = FLEET_1_CLEAR.button[1] - FLEET_1_CLEAR.area[1]
        if y < -10:
            logger.info('FLEET_1_CLEAR moves up, load W15 assets')
            in_use = FLEET_2_IN_USE_W15
        else:
            in_use = FLEET_2_IN_USE
        fleet_2 = FleetOperator(
            choose=FLEET_2_CHOOSE, advice=FLEET_2_ADVICE, bar=FLEET_2_BAR, clear=FLEET_2_CLEAR,
            in_use=in_use, hard_satisfied=FLEET_2_HARD_SATIESFIED, main=campaign)
        return {'FLEET_1': fleet_1, 'FLEET_2': fleet_2}

    def hard_progress_recommend(self, campaign):
        """
        Empty fleet 1 and fleet 2, then let the in-game Recommend button fill
        them again from the current dock.

        Clearing first is the whole point. The game remembers the fleet used on
        the last clear of a hard stage, and Recommend only fills what is empty,
        so tapping it on a crewed slot changes nothing - the sortie goes out
        with whatever was good enough the last time this stage was touched,
        which on an account that has moved on is a bad fleet that still passes
        the stat check. An emptied slot has nothing to pass the check with, so
        Recommend has to build a fresh one.

        Submarine is left untouched: the stock hard path clears it unless the
        user set Submarine.Fleet, and submarines carry no stat restriction.

        Args:
            campaign (CampaignBase): In FLEET_PREPARATION.

        Returns:
            dict[str, bool]: Slot name -> is_hard_satisfied() after the rebuild
                (True satisfied, False cannot be satisfied, None unreadable).
                Empty if there was nothing to rebuild.
        """
        if campaign.map_fleet_checked:
            return {}

        # Decide which slots to touch from ONE clean screenshot, before any
        # clicking. Clearing a fleet raises a confirmation popup that covers the
        # other fleet's row, so a slot examined after the first clear looks like
        # it is not on this stage at all and would be left with its stale fleet.
        campaign.device.screenshot()
        targets = []
        for name, fleet in self.fleet_operators(campaign).items():
            if not fleet.allow():
                logger.info(f'{fleet} is not used on this stage, leave it alone')
                continue
            if not fleet.is_hard():
                logger.info(f'{fleet} has no Recommend button, leave it alone')
                continue
            targets.append(name)

        if not targets:
            logger.warning('No fleet slot to rebuild')
            return {}

        # Empty every slot before recommending any of them, so Recommend picks
        # fleet 1 out of the whole dock instead of avoiding whatever fleet 2 is
        # still holding. Reload the operators before each step: clicking reflows
        # the fleet cards and the offsets are loaded in FleetOperator.__init__.
        for name in targets:
            campaign.device.screenshot()
            self.clear_fleet(campaign, self.fleet_operators(campaign)[name])

        results = {}
        for name in targets:
            campaign.device.screenshot()
            results[name] = self.recommend_fleet(campaign, self.fleet_operators(campaign)[name])
        return results

    def clear_fleet(self, campaign, fleet):
        """
        Empty a fleet slot.

        NOT FleetOperator.clear(): that one clicks until in_use() reads False,
        and in_use() decides "crewed" from how much the slot's image varies. On
        a hard stage an emptied slot is not blank - it shows the ship types the
        stage demands as coloured placeholder tags (BB, CA, CL, DD) - so the
        variance stays high, in_use() never goes False, and the loop clicks
        until ALAS's own GameTooManyClickError fires. Upstream never meets this
        because it skips fleet handling entirely once it detects hard mode.

        Bounded clicks instead, confirmed by the stat requirements going
        unsatisfied: an empty fleet cannot meet a level or firepower minimum.

        Args:
            campaign (CampaignBase): In FLEET_PREPARATION.
            fleet (FleetOperator):

        Returns:
            bool: If the slot was confirmed empty.
        """
        logger.info(f'Clear {fleet}')
        click_timer = Timer(CLEAR_INTERVAL, count=4)
        timeout = Timer(CLEAR_TIMEOUT).start()
        clicks = 0
        while 1:
            campaign.device.screenshot()

            # Clearing a hard fleet asks for confirmation
            if campaign.handle_popup_confirm('HARD_CLEAR'):
                continue

            # Surface slots confirm through the requirement lines going out;
            # the submarine row has no lines, so in_use() answers for it
            if clicks and (fleet.is_hard_satisfied() is False or not fleet.in_use()):
                logger.info(f'{fleet} emptied after {clicks} click(s)')
                return True
            if clicks >= CLEAR_CLICKS:
                logger.info(f'{fleet} clicked CLEAR {clicks} time(s) without confirming it emptied, '
                            f'letting Recommend decide')
                return False
            if timeout.reached():
                logger.warning(f'{fleet} clear timeout, letting Recommend decide')
                return False

            if click_timer.reached():
                campaign.device.click(fleet._clear)
                clicks += 1
                click_timer.reset()

    def recommend_fleet(self, campaign, fleet):
        """
        Tap Recommend until the slot satisfies the stage's stat restrictions.
        Always taps at least once - it is called on a slot that was just
        emptied on purpose.

        in_use() is not consulted: it reads the same placeholder tags that make
        clear_fleet() necessary, so on a hard stage it says "crewed" about an
        empty slot. The orange requirement lines are the honest signal.

        Args:
            campaign (CampaignBase): In FLEET_PREPARATION.
            fleet (FleetOperator):

        Returns:
            bool: is_hard_satisfied() as last read. True satisfied, False the
                dock cannot satisfy this stage, None unreadable (the Recommend
                button is hidden while the refilled row animates, and that is a
                transient state, not a verdict).
        """
        logger.info(f'Recommend {fleet}')
        click_timer = Timer(RECOMMEND_INTERVAL, count=6)
        timeout = Timer(RECOMMEND_TIMEOUT).start()
        clicks = 0
        satisfied = None
        while 1:
            campaign.device.screenshot()

            # Recommend may ask to confirm replacing the current fleet
            if campaign.handle_popup_confirm('HARD_RECOMMEND'):
                continue

            if clicks:
                satisfied = fleet.is_hard_satisfied()
                if satisfied is True:
                    logger.info(f'{fleet} filled and satisfied after {clicks} Recommend tap(s)')
                    return True
            if clicks >= RECOMMEND_CLICKS:
                logger.info(f'{fleet} tapped {clicks} time(s), waiting for the row to settle')
                break
            if timeout.reached():
                logger.info(f'{fleet} Recommend budget timed out, waiting for the row to settle')
                break

            if click_timer.reached():
                campaign.device.click(fleet._advice)
                clicks += 1
                click_timer.reset()

        # Out of taps. The row may still be animating and a mid-animation read
        # is not a verdict, so keep looking for a while before reporting one.
        settle = Timer(RECOMMEND_SETTLE).start()
        while not settle.reached():
            campaign.device.screenshot()
            if campaign.handle_popup_confirm('HARD_RECOMMEND'):
                continue
            satisfied = fleet.is_hard_satisfied()
            if satisfied is True:
                logger.info(f'{fleet} satisfied once the row settled')
                return True

        logger.warning(f'{fleet} not confirmed satisfied after {clicks} Recommend tap(s), '
                       f'is_hard_satisfied={satisfied}')
        return satisfied

    def skip_upstream_fleet_preparation(self, campaign):
        """
        Do what upstream's fleet_preparation() does once it decides the stage is
        hard mode, without letting it decide.

        Its test is "is a Recommend button visible", evaluated on whatever the
        screen looks like at that moment. Right after Recommend refills a slot
        the row is still animating and no button is visible, so it reads not-hard
        for every slot, drops into the normal-mode path, and clears fleet 2 with
        FleetOperator.clear() - which cannot terminate on a hard slot and takes
        the game down with GameTooManyClickError. This task only ever runs hard
        stages, so the answer is known and does not need re-deriving.

        Args:
            campaign (CampaignBase): In FLEET_PREPARATION.

        Returns:
            bool: False, matching upstream's return.
        """
        logger.info('Hard Campaign. No fleet preparation')
        campaign.map_is_hard_mode = True

        campaign.device.screenshot()
        submarine = FleetOperator(
            choose=SUBMARINE_CHOOSE, advice=SUBMARINE_ADVICE, bar=SUBMARINE_BAR, clear=SUBMARINE_CLEAR,
            in_use=SUBMARINE_IN_USE, hard_satisfied=SUBMARINE_HARD_SATIESFIED, main=campaign)
        if not submarine.allow():
            logger.info('Submarine is not used on this stage')
            campaign.config.SUBMARINE = 0
            return False
        if campaign.config.Submarine_Fleet:
            logger.info('Keeping the submarine fleet the user configured')
            return False
        self.clear_fleet(campaign, submarine)
        return False

    """
    Campaign
    """

    def load_campaign(self, name, folder='campaign_main'):
        super().load_campaign(name, folder=folder)
        outer = self

        class HardProgressCampaign(self.module.Campaign):
            def triggered_map_stop(self):
                """
                Replaces the stock StopCondition.MapAchievement evaluation:
                the combined 3-star AND 100% criteria has no stock equivalent,
                and a stalled stage has to unwind through the same path.
                """
                if outer.criteria_met(self):
                    return True
                if outer.no_progress:
                    logger.warning('Hard stage made no progress, cancel out instead of entering again')
                    return True
                return False

            def handle_map_stop(self):
                """
                MANDATORY no-op. The stock version sets Scheduler.Enable=False
                on what looks like a throwaway deepcopy of the config, but the
                copy keeps the bound paths and the config name, so the write
                lands in config/alas.json and turns this task off every time a
                stage is finished.
                """
                pass

            def map_get_info(self):
                super().map_get_info()
                outer.watch_progress(self)

            def fleet_preparation(self):
                if self.map_fleet_checked:
                    return False

                results = outer.hard_progress_recommend(self)
                if not results:
                    # Most likely a row still animating, so the Recommend
                    # buttons were not visible. Look once more.
                    logger.warning('No hard fleet slot found, retrying once')
                    self.device.sleep(1.5)
                    results = outer.hard_progress_recommend(self)

                if False in results.values():
                    outer.hard_unsatisfied_stop(outer.current_stage)
                if not results:
                    logger.warning('Still no hard fleet slot found, '
                                   'sortieing with the fleets as they are')

                # super().fleet_preparation() is never called. This task only
                # ever runs hard stages, and upstream decides hard-vs-normal by
                # whether a Recommend button happens to be visible right then -
                # get that wrong once and its normal-mode path clears fleet 2
                # with FleetOperator.clear(), which cannot terminate on a hard
                # slot and takes the game down with GameTooManyClickError.
                return outer.skip_upstream_fleet_preparation(self)

        self.campaign = HardProgressCampaign(device=self.campaign.device, config=self.campaign.config)
        return True

    """
    Run
    """

    def hard_entries_exhausted(self):
        """
        Read the remaining hard entries off page_campaign. The generic loop
        already delays the task when they run out, but it breaks out of the
        loop the same way an oil limit does, so the count has to be read again
        to tell the two apart.

        Returns:
            bool: If today's three hard entries are spent.
        """
        self.device.screenshot()
        if not self.ui_page_appear(page_campaign):
            logger.info('Not on page_campaign, cannot read hard entries')
            return False
        # MODE_SWITCH_1 is named after the mode it switches TO,
        # so 'normal' means hard mode is the one currently shown
        if MODE_SWITCH_1.get(main=self) != 'normal':
            logger.info('Not in hard mode, cannot read hard entries')
            return False

        remain = OCR_HARD_REMAIN.ocr(self.device.image)
        logger.attr('Hard remain', remain)
        return not remain

    def run(self, name='', folder='campaign_main', mode='hard', total=0):
        """
        Args:
            name (str): Ignored, the stage comes from HardProgress.NextStage.
            folder (str): Ignored, hard mode only exists in campaign_main.
            mode (str): Ignored, always 'hard'.
            total (int): Ignored, the daily entry limit is the only cap.

        Raises:
            TaskEnd:
        """
        logger.hr('Hard progress', level=1)
        self.override_config()

        stage = to_map_input_name(self.config.HardProgress_NextStage)
        end = to_map_input_name(self.config.HardProgress_EndStage)
        logger.attr('HardProgress_Criteria', self.config.HardProgress_Criteria)
        logger.attr('HardProgress_NextStage', stage)
        logger.attr('HardProgress_EndStage', end)

        while 1:
            if not stage_le(stage, end):
                self.all_done_stop(stage, end)

            self.current_stage = stage
            self.no_progress = False
            self.watched_run_count = 0
            logger.hr(f'Hard stage {stage}', level=1)
            try:
                super().run(name=to_map_file_name(stage), folder='campaign_main', mode='hard', total=0)
            except ScriptEnd as e:
                # Only ensure_campaign_ui() lets a ScriptEnd out of the generic
                # loop. A criteria-met ScriptEnd is raised and caught inside it.
                self.handle_campaign_ui_failure(stage, e)
            self.reset_ui_failure()

            if self.no_progress:
                self.no_progress_stop(stage)

            # Entries first: when they run out the loop breaks before peeking,
            # so the map info still describes the state before the last sortie.
            # Tomorrow's peek is free and advances the pointer if it is due.
            if self.hard_entries_exhausted():
                logger.hr('Hard entries spent for today', level=1)
                self.config.task_delay(server_update=True)
                self.config.task_call('Reward')
                self.config.task_stop()

            if self.criteria_met(self.campaign):
                new = next_stage(stage)
                logger.hr(f'Hard stage {stage} satisfies {self.config.HardProgress_Criteria}', level=1)
                logger.info(f'Advance pointer {stage} -> {new}')
                self.config.HardProgress_NextStage = new
                self.record_clear()
                stage = new
                continue

            # Oil limit, commission notice, task switch and friends. Whatever
            # stopped the generic loop has already set a delay of its own.
            logger.info('Campaign stopped before the stage was finished, yield to the scheduler')
            self.config.task_stop()

    def override_config(self):
        """
        Every farming knob the task pins. In memory only - none of these groups
        are bound to the task, so nothing here reaches config/alas.json.
        """
        self.config.override(
            Campaign_Mode='hard',
            Campaign_UseClearMode=True,
            Campaign_UseFleetLock=True,
            Campaign_UseAutoSearch=True,
            Campaign_Use2xBook=False,
            Fleet_FleetOrder='fleet1_all_fleet2_standby',
            Emotion_Mode='nothing',  # Dont calculate and dont ignore, same as the stock Hard task
            # MAP_CLEAR_ALL_THIS_TIME, which routes through every node while a star is
            # still missing, only arms on 'map_3_stars' / 'threat_safe'
            StopCondition_MapAchievement='100_percent_clear'
            if self.config.HardProgress_Criteria == '100_percent_clear' else 'map_3_stars',
            StopCondition_StageIncrease=False,
            StopCondition_RunCount=0,
            StopCondition_GetNewShip=False,
            StopCondition_ReachLevel=0,
        )
