"""
ShipCensus - Tools-section task that walks the dock and records every ship's
progression state (level, affinity, enhance, limit break, skill levels) into
config/ship_census.json, then regenerates the standalone dashboard at
config/ship_census_dashboard.html.

Phase A scaffold: the sweep loop, store and dashboard are complete; the
detail-page readers below need a capture session to cut OCR areas/assets and
are gated behind READERS_READY. Until then only ScanMode=dashboard_only does
anything useful (renders whatever the store already holds).

Iteration strategy (same as ExpFeed): enter the first filtered dock card, then
swipe ship-to-ship on the detail page - the list is frozen while browsing, so
one pass covers the dock without paging. CAUTION for Phase B: MetaLab learned
that leaving the detail context (into the META Lab) drops the browsing
context; if visiting the Enhance/LimitBreak/Info tabs turns out to do the
same, switch this loop to MetaLab's grid iteration (dock_card_present /
dock_enter_card / DOCK_SCROLL paging) instead of swipes.
"""
from module.base.button import Button
from module.base.timer import Timer
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.assets import DOCK_CHECK, DOCK_EMPTY, SHIP_DETAIL_CHECK
from module.retire.dock import Dock
from module.ship_census.dashboard import generate_dashboard
from module.ship_census.store import CensusStore
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock

# Phase B gate: detail-page readers (name, affinity, skills, enhance, limit
# break, rarity, META detection) need a capture session before the sweep can
# store anything. Flip only when every reader below is implemented.
READERS_READY = False

# Ship level on the detail page, right of the "Level:" label - same area
# ExpFeed reads in production (module/exp_feed/exp_feed.py).
OCR_DETAIL_LEVEL = Digit(
    Button(area=(758, 283, 798, 319), color=(), button=(758, 283, 798, 319), name='DETAIL_LEVEL'),
    letter=(255, 255, 255), threshold=128, name='OCR_CENSUS_LEVEL')

RARITY_SCOPE = {
    'elite_and_above': ['elite', 'super_rare', 'ultra_rare'],
    'rare_and_above': ['rare', 'elite', 'super_rare', 'ultra_rare'],
    'all': 'all',
}
SWEEP_SAFETY_LIMIT = 1500


class ShipCensus(Dock):
    def run(self):
        mode = self.config.ShipCensus_ScanMode
        store = CensusStore().load()
        logger.hr('Ship census', level=1)
        logger.info('Mode: {}, ships on record: {}'.format(mode, len(store.ships)))

        if mode != 'dashboard_only':
            if READERS_READY:
                self.census_sweep(store, full=(mode == 'full'))
            else:
                logger.warning('ShipCensus detail readers are not captured yet (Phase B '
                               'pending) - skipping the scan, regenerating the dashboard '
                               'from existing data only.')

        path = generate_dashboard(store)
        logger.info('Dashboard written to {}'.format(path))
        logger.info('Open it in a browser to view the census.')

    # ---------------- sweep ----------------

    def census_sweep(self, store, full=False):
        """
        Walk the filtered dock on the detail page and record every ship.

        Pages:
            in: Any
            out: page_dock, filters reset
        """
        scope = self.config.ShipCensus_RarityScope
        stale_days = int(self.config.ShipCensus_StaleDays)
        mode = 'full' if full else 'delta'
        skip = store.sweep_begin(mode, scope)
        store.save()
        if skip:
            logger.info('Resuming sweep, skipping past {} processed ships'.format(skip))

        logger.hr('Census sweep', level=2)
        self.ui_ensure(page_dock)
        self.dock_favourite_set(enable=False, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(rarity=RARITY_SCOPE[scope])

        if self.appear(DOCK_EMPTY, offset=(20, 20)) or not self.dock_enter_first():
            logger.info('Dock empty under census filter, nothing to scan')
            store.sweep_end(complete=True)
            store.save()
            self.dock_filter_set(wait_loading=False)
            return

        # Fast-forward past ships already processed in an interrupted sweep
        for _ in range(skip):
            if not self.ship_view_next(check_button=SHIP_DETAIL_CHECK):
                logger.info('Dock ended during resume skip, sweep was already complete')
                store.sweep_end(complete=True)
                store.save()
                self.detail_exit_to_dock()
                self.dock_filter_set(wait_loading=False)
                return

        visited = 0
        complete = False
        while 1:
            visited += 1
            self.read_ship(store, stale_days=stale_days, full=full)
            store.save()
            self.device.click_record_clear()
            if visited >= SWEEP_SAFETY_LIMIT:
                logger.warning('Census sweep safety limit reached')
                break
            if not self.ship_view_next(check_button=SHIP_DETAIL_CHECK):
                logger.info('Census sweep reached the end of the dock')
                complete = True
                break

        store.sweep_end(complete=complete)
        store.save()
        self.detail_exit_to_dock()
        self.dock_filter_set(wait_loading=False)
        logger.info('Census sweep processed {} ships this run'.format(visited))

    def read_ship(self, store, stale_days=7, full=False):
        """
        Read the ship currently open on the detail page and upsert its record.
        Quick read = name + level (no tab visits); deep read adds affinity,
        skills, enhance, limit break.

        Pages:
            in: SHIP_DETAIL_CHECK
            out: SHIP_DETAIL_CHECK (on the ship's main detail tab)
        """
        name = self.read_name()
        level = self.read_level()
        if name is None:
            logger.warning('Ship name unreadable, recording skipped')
            store.sweep_advance(None)
            return
        key = store.sweep_key(name)
        logger.hr('Ship {} (Lv.{})'.format(key, level), level=2)

        if not store.needs_deep_scan(key, level, stale_days, full=full):
            logger.info('Record fresh, quick pass only')
            store.record(key, name=name, copy=int(key.rsplit('#', 1)[1]), level=level)
            store.sweep_advance(key)
            return

        fields = dict(
            name=name,
            copy=int(key.rsplit('#', 1)[1]),
            level=level,
            is_meta=self.detect_meta(),
            rarity=self.read_rarity(),
        )
        fields.update(self.read_info_tab())      # affinity, oathed, skills, cognition
        if not fields.get('is_meta'):
            fields.update(self.read_enhance())   # enhance_maxed
            fields.update(self.read_limit_break())  # lb_current, lb_max
        store.record(key, deep=True, **fields)
        store.sweep_advance(key)

    # ---------------- readers (Phase B: capture session required) ----------------
    # Each reader must leave the ship on its main detail tab so ship_view_next
    # keeps working. Areas/assets to cut are listed per reader.

    def read_level(self):
        """Level from the detail header - production-proven area from ExpFeed."""
        timeout = Timer(5, count=10).start()
        skip_first_screenshot = True
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
                logger.warning('read_level timeout, OCR failed')
                return None

    def read_name(self):
        """
        TODO Phase B: OCR area of the ship name on the detail page header.
        Needs captures across ship types (short/long names, retrofits, METAs)
        to pin the crop and validate the cnocr model on stylized text.
        """
        raise NotImplementedError('read_name needs a capture session')

    def detect_meta(self):
        """
        TODO Phase B: META ships show a Research sidebar tab instead of
        LimitBreak on the detail page (see alas-debugging-notes).
        """
        raise NotImplementedError('detect_meta needs a capture session')

    def read_rarity(self):
        """
        TODO Phase B: rarity from the detail page (frame color / star row),
        or carried over from the dock card before entering.
        """
        raise NotImplementedError('read_rarity needs a capture session')

    def read_info_tab(self):
        """
        TODO Phase B: open the Info tab and read affinity value, oath state,
        skill cards ("Locked" / "LEVEL: N" - same text style MetaLab reads on
        META info pages), cognition awakening. Return to the main detail tab.

        Returns:
            dict: affinity, oathed, skills, cognition_awakened
        """
        raise NotImplementedError('read_info_tab needs a capture session')

    def read_enhance(self):
        """
        TODO Phase B: open the Enhance tab and read whether all enhance stats
        are full. Return to the main detail tab.

        Returns:
            dict: enhance_maxed
        """
        raise NotImplementedError('read_enhance needs a capture session')

    def read_limit_break(self):
        """
        TODO Phase B: read limit break stage - either the star row on the
        detail page or the LimitBreak tab state. Retrofit-capable ships shift
        the sidebar tab positions (see alas-debugging-notes).

        Returns:
            dict: lb_current, lb_max
        """
        raise NotImplementedError('read_limit_break needs a capture session')

    # ---------------- navigation ----------------

    def detail_exit_to_dock(self, skip_first_screenshot=True):
        """
        Back out from ship detail to page_dock (MetaLab's lb_exit_to_dock).
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
                logger.warning('detail_exit_to_dock timeout, using ui_ensure')
                self.ui_ensure(page_dock)
                return
            if self.appear(SHIP_DETAIL_CHECK, offset=(20, 20), interval=3):
                self.device.click(BACK_ARROW)
                continue
