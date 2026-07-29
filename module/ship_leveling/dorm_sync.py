"""
Keep the dorm roster equal to the farming fleet.

Ships in the dorm recover morale at 40/h (floor 1) or 50/h (floor 2) instead of
20/h outside it, and they gain passive EXP and affinity while they sit there.
The fleet this task farms with is exactly the set of ships that should be
getting that, and it changes every time a slot is swapped - so the dorm has to
follow it.

Mapped live on 2026-07-28 (screenshots/ship_leveling_capture/). No other ALAS
task touches this screen; the Dorm task only feeds and collects, and DORM_MANAGE
is furniture. What the capture session established:

- page_dorm carries a "Train N/6" chip in the bottom left. It opens the roster
  dialog: the Train tab, six ship cards, an X in the top right.
- Tapping a roster card opens an ordinary dock multi-select. Everything ALAS
  already knows about the dock works there: DOCK_CHECK appears, the cards sit on
  CARD_GRIDS, ShipScanner reads level/morale/fleet badge/status off them,
  OCR_DOCK_SELECTED reads the "Selected: N/6" counter, the search box takes
  adb-typed text, and SHIP_CONFIRM is the Confirm button.
- The picker floats the dorm's own ships to the front of the grid, in dorm slot
  order, and they stay put while the selection is toggled. So the front block is
  a readable picture of the current roster - including each ship's fleet badge,
  which is the whole test for whether she belongs there.
- Opening the picker from an OCCUPIED slot deselects that slot's ship (the
  dialog is a "replace this one" flow) and puts a REMOVE pseudo-card in the
  first grid cell. That card is not a ship: tapping it applies the removal and
  closes the dialog immediately. It reads as level 0, so ShipScanner's level
  limitation keeps it out of every scan - never tap a raw grid button here.

Selection state per card is deliberately NOT read off the screen: the
"- SELECTED -" band is semi-transparent white over ship art, and template
matching on it separates the two states by less than 0.08 similarity (measured
over 28 cards). The selected counter is the oracle instead - every tap is
verified by the direction the counter moves, which also makes each toggle
self-correcting.
"""
import re

import numpy as np

from module.base.button import Button
from module.base.timer import Timer
from module.base.utils import crop
from module.exception import GamePageUnknownError
from module.logger import logger
from module.retire.assets import DOCK_CHECK, SHIP_CONFIRM
from module.retire.dock import OCR_DOCK_SELECTED, Dock
from module.retire.scanner import ShipScanner
from module.ship_census.census import (DOCK_SEARCH_BUTTON, DOCK_SEARCH_CLEAR_KEYS,
                                       DOCK_SEARCH_FIELD, DOCK_SEARCH_STATE_AREA)
from module.ship_leveling.assets import DORM_ROSTER_CHECK
from module.ui.assets import BACK_ARROW
from module.ui.page import DORM_CHECK, page_dorm


def _area_button(area, name):
    return Button(area=area, color=(), button=area, name=name)


# The "Train N/6" chip in the bottom left of page_dorm. Clicked until the roster
# dialog answers, so it needs no template of its own - the digits in it change
# with the roster anyway.
DORM_TRAIN_ENTER = _area_button((40, 658, 180, 698), 'DORM_TRAIN_ENTER')
# The X that closes the roster dialog
DORM_ROSTER_CLOSE = _area_button((1112, 82, 1152, 120), 'DORM_ROSTER_CLOSE')
# The six roster cards. Any of them opens the picker; the task uses the first.
DORM_SLOTS = [_area_button((150 + 170 * i, 300, 290 + 170 * i, 420), 'DORM_SLOT_%s' % (i + 1))
              for i in range(6)]
# Cancel, for backing out of the picker without touching the roster
DORM_PICKER_CANCEL = _area_button((730, 648, 885, 692), 'DORM_PICKER_CANCEL')

# How many dorm slots there are. Read from the counter in practice; this is only
# the fallback when the OCR will not parse.
DORM_SIZE = 6
# Taps allowed on one card before its toggle is given up on. The first tap after
# a search only dismisses the suggestion dropdown, hence more than one.
TOGGLE_TRIES = 4
# Frames the fleet badges are read over before a ship is judged fleet-less. See
# dorm_scan_fleet - a badge is only ever lost, never invented.
FLEET_READ_FRAMES = 3


def _grid_order(name):
    """CARD_<col>_<row> back into the reading order the grid draws them in."""
    try:
        _, col, row = name.split('_')
        return int(row), int(col)
    except ValueError:
        return 0, 0


class DormRoster(Dock):
    """
    Mixed into ShipLeveling. Everything here assumes the ordinary ALAS dock
    helpers, which the picker turns out to support.
    """

    @property
    def dorm_fleet_index(self):
        """In-game fleet number whose ships belong in the dorm."""
        raise NotImplementedError

    # ------------------------------------------------------------ navigation

    def dorm_escape(self):
        """
        Get off any dorm dialog and back onto ground ui_ensure understands.

        Neither the roster dialog nor its ship picker is a known page, so a run
        that ends inside one - or a crash that leaves the game there - would
        make the next ui_ensure raise GamePageUnknownError instead of
        navigating.
        """
        self.device.screenshot()
        if self.appear(DOCK_CHECK, offset=(20, 20)):
            # Six places is what tells the dorm's picker apart from the fleet
            # deploy picker, which offers exactly one
            _, capacity = self.dorm_selected_count()
            if capacity == DORM_SIZE:
                logger.info('Left over in the dorm ship picker, cancelling out of it')
                self.dorm_picker_exit(confirm=False)
        if self.appear(DORM_ROSTER_CHECK, offset=(30, 30)):
            logger.info('Left over on the dorm roster dialog, closing it')
            self.dorm_roster_exit()

    def dorm_roster_enter(self, skip_first_screenshot=True):
        """
        Open the dorm's Train roster dialog.

        Returns:
            bool: True if it opened.

        Pages:
            in: Any
            out: DORM_ROSTER_CHECK
        """
        self.dorm_escape()
        self.ui_ensure(page_dorm)
        timeout = Timer(25, count=25).start()
        click = Timer(2.5)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(DORM_ROSTER_CHECK, offset=(30, 30)):
                return True
            if timeout.reached():
                logger.warning('Dorm roster dialog did not open')
                return False
            if self.handle_info_bar():
                continue
            if not self.appear(DORM_CHECK, offset=(20, 20)):
                # The chip shares its corner with the dorm's wandering chibi
                # ships, and a tap that lands on one opens HER instead - live,
                # eight taps in a row went nowhere because the first had left
                # the dorm entirely. Get back before tapping again.
                logger.info('Not on the dorm any more - a tap probably opened a ship, '
                            'going back')
                self.device.click(BACK_ARROW)
                self.device.sleep((1.2, 1.6))
                self.device.click_record_clear()
                try:
                    self.ui_ensure(page_dorm)
                except GamePageUnknownError:
                    # Still somewhere unrecognised; the next round clicks back
                    # again rather than ending the whole task here
                    logger.info('Page still unrecognised, backing out again')
                click.reset()
                continue
            if click.reached():
                self.device.click(DORM_TRAIN_ENTER)
                self.device.click_record_clear()
                click.reset()

    def dorm_roster_exit(self, skip_first_screenshot=True):
        """Close the roster dialog, back to page_dorm."""
        timeout = Timer(15, count=15).start()
        click = Timer(2.5)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if not self.appear(DORM_ROSTER_CHECK, offset=(30, 30)):
                return True
            if timeout.reached():
                logger.warning('Dorm roster dialog would not close')
                return False
            if click.reached():
                self.device.click(DORM_ROSTER_CLOSE)
                self.device.click_record_clear()
                click.reset()

    def dorm_picker_enter(self, slot=0, skip_first_screenshot=True):
        """
        Open the ship picker from a roster slot.

        The slot's own occupant comes back deselected - that is what the dialog
        is for - so the caller must treat her as "not currently chosen".

        Returns:
            bool:

        Pages:
            in: DORM_ROSTER_CHECK
            out: DOCK_CHECK
        """
        timeout = Timer(20, count=20).start()
        click = Timer(2.5)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(DOCK_CHECK, offset=(20, 20)):
                return True
            if timeout.reached():
                logger.warning('Dorm ship picker did not open')
                return False
            if self.handle_info_bar():
                continue
            if click.reached():
                self.device.click(DORM_SLOTS[slot])
                self.device.click_record_clear()
                click.reset()

    # ------------------------------------------------------------- selection

    def dorm_selected_count(self, skip_first_screenshot=True):
        """
        Read the "Selected: N/6" counter.

        Retried on fresh frames: the picker slides in and the bottom bar is the
        last part of it to be drawn, so a read taken the moment DOCK_CHECK
        appears finds nothing there (live, that aborted the first sync).

        Returns:
            (int, int): chosen, capacity - or (None, None) if it never parsed.
        """
        timeout = Timer(4, count=8).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            current, _, total = OCR_DOCK_SELECTED.ocr(self.device.image)
            if total:
                return current, total
            if timeout.reached():
                return None, None

    def dorm_picker_exit(self, confirm, skip_first_screenshot=True):
        """
        Leave the ship picker, keeping the new roster or dropping it.

        Returns:
            bool: True if the roster dialog came back.

        Pages:
            in: DOCK_CHECK
            out: DORM_ROSTER_CHECK
        """
        button = SHIP_CONFIRM if confirm else DORM_PICKER_CANCEL
        timeout = Timer(20, count=20).start()
        click = Timer(2.5)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(DORM_ROSTER_CHECK, offset=(30, 30)):
                return True
            if timeout.reached():
                logger.warning('Dorm picker would not close ({})'.format(
                    'confirm' if confirm else 'cancel'))
                return False
            if self.handle_popup_confirm('DORM_ROSTER'):
                continue
            if click.reached():
                if confirm:
                    # The asset sits to the right of where the button is drawn,
                    # so the match position is what has to be clicked
                    if not self.appear_then_click(SHIP_CONFIRM, offset=(200, 50)):
                        self.device.click(button)
                else:
                    self.device.click(button)
                self.device.click_record_clear()
                click.reset()

    def dorm_scan_cards(self):
        """
        Every ship card on the picker's first page, in grid order.

        The REMOVE pseudo-card reads level 0 and is dropped by the scanner's
        level limitation, so the result is only ever real ships.

        Returns:
            list[Ship]:
        """
        scanner = ShipScanner(level=(1, 125), emotion=(0, 150),
                              fleet=[0, 1, 2, 3, 4, 5, 6], status='any')
        scanner.disable('rarity')
        return scanner.scan(self.device.image, output=False)

    def dorm_scan_fleet(self, frames=FLEET_READ_FRAMES):
        """
        Read every card's fleet badge over several frames, believing any badge
        that shows up at all.

        The read is asymmetric and so is the rule. FleetScanner thresholds the
        green channel at 205 and template-matches the digit, so a badge over
        bright artwork can wash out and come back as "no fleet" - live,
        Dunkerque META wears fleet 3 over bare skin and read 0 on roughly half
        the frames, which was enough to have her thrown out of the dorm and put
        straight back by name. A badge is never invented, only lost, so one
        sighting of a number beats any amount of nothing.

        Returns:
            list[(Ship, int)]: Card and its fleet number - None only when two
                frames claimed two different fleets, which should not happen and
                is treated as "do not touch her".
        """
        seen = {}
        cards = {}
        for index in range(max(1, frames)):
            if index:
                self.device.sleep((0.4, 0.7))
                self.device.screenshot()
            for card in self.dorm_scan_cards():
                cards[card.button.name] = card
                if card.fleet:
                    seen.setdefault(card.button.name, set()).add(card.fleet)

        out = []
        for name, card in sorted(cards.items(), key=lambda kv: _grid_order(kv[0])):
            badges = seen.get(name, set())
            if not badges:
                out.append((card, 0))
            elif len(badges) == 1:
                out.append((card, badges.pop()))
            else:
                logger.warning('Card {} read as fleets {} on different frames'.format(
                    name, sorted(badges)))
                out.append((card, None))
        return out

    def dorm_toggle(self, button, want_more, note=''):
        """
        Tap a card until the selected counter moves the way it should.

        Args:
            button (Button): Card to tap.
            want_more (bool): True to select, False to deselect.
            note (str): For the log.

        Returns:
            bool: True if the counter moved as asked.
        """
        before, _ = self.dorm_selected_count()
        if before is None:
            logger.warning('Selected counter would not read, skipping the toggle')
            return False
        original = before
        for attempt in range(TOGGLE_TRIES):
            self.device.click(button)
            self.device.sleep((1.0, 1.4))
            self.device.click_record_clear()
            self.device.screenshot()
            current, _ = self.dorm_selected_count()
            if current is None:
                continue
            if want_more and current > before:
                logger.info('Selected {} ({} -> {})'.format(note or button.name,
                                                            before, current))
                return current != original
            if not want_more and current < before:
                logger.info('Deselected {} ({} -> {})'.format(note or button.name,
                                                              before, current))
                return current != original
            if current != before:
                # Moved the wrong way: the card was in the opposite state to
                # what the caller assumed. One more tap puts it back and then
                # the loop tries again from there.
                logger.info('{} toggled the other way ({} -> {}), correcting'.format(
                    note or button.name, before, current))
                before = current
                continue
            logger.info('Tap {} on {} did not register yet'.format(attempt + 1,
                                                                   note or button.name))
        logger.warning('Could not {} {}'.format('select' if want_more else 'deselect',
                                                note or button.name))
        return False

    # ---------------------------------------------------------------- search

    def dorm_search_is_open(self):
        """The magnifier is amber while the search box is open, blue when not."""
        mean = np.array(crop(self.device.image, DOCK_SEARCH_STATE_AREA)) \
            .reshape(-1, 3).mean(axis=0)
        return mean[0] > 120 and mean[0] > mean[2]

    def dorm_search_set(self, open_):
        """
        Open or close the search box, checking what state it is in first.

        The magnifier TOGGLES. Clicking it blind on the second search of a
        session closes the box instead of opening it, the query then goes
        nowhere, and the grid comes back empty - live, that lost Laffey II and
        Kawakaze META out of a six-ship sync.

        Returns:
            bool: True if the box ended up in the requested state.
        """
        for _ in range(4):
            self.device.screenshot()
            if self.dorm_search_is_open() == open_:
                return True
            self.device.click(DOCK_SEARCH_BUTTON)
            self.device.sleep((1.2, 1.5))
            self.device.click_record_clear()
        logger.warning('Could not {} the picker search box'.format(
            'open' if open_ else 'close'))
        return False

    def dorm_search(self, name):
        """
        Filter the picker down to one ship with the dock's search box.

        The query is the first two plain words of the name, the same reduction
        ShipCensus.dock_enter_by_name uses, because the box does not take
        brackets or the stylised suffixes.

        Returns:
            list[Ship]: Cards left after the search.
        """
        query = re.sub(r'\(.*?\)', ' ', str(name))
        query = re.sub(r'[^A-Za-z0-9 ]+', ' ', query)
        query = ' '.join(query.split()[:2]).strip()
        if not query:
            return []
        logger.info('Searching the picker for {!r}'.format(query))
        if not self.dorm_search_set(True):
            return []
        self.device.click(DOCK_SEARCH_FIELD)
        self.device.sleep((1.2, 1.5))
        # The box keeps the previous query, and closing it does not clear it
        self.device.adb_shell(['input', 'keyevent', '123'] + ['67'] * DOCK_SEARCH_CLEAR_KEYS)
        self.device.sleep((0.5, 0.8))
        self.device.adb_shell(['input', 'text', query.replace(' ', '%s')])
        self.device.sleep((1.0, 1.4))
        self.device.adb_shell(['input', 'keyevent', '66'])
        self.device.sleep((1.8, 2.2))
        self.device.screenshot()
        return self.dorm_scan_cards()

    def dorm_search_close(self):
        """Empty the search box and close it, so the grid comes back whole."""
        self.device.screenshot()
        if not self.dorm_search_is_open():
            return
        self.device.click(DOCK_SEARCH_FIELD)
        self.device.sleep((1.2, 1.5))
        self.device.adb_shell(['input', 'keyevent', '123'] + ['67'] * DOCK_SEARCH_CLEAR_KEYS)
        self.device.sleep((0.5, 0.8))
        self.device.adb_shell(['input', 'keyevent', '66'])
        self.device.sleep((1.2, 1.6))
        self.dorm_search_set(False)

    # ------------------------------------------------------------------ sync

    def dorm_sync_roster(self, names):
        """
        Make the dorm hold the ships of the farming fleet.

        Args:
            names (list[str]): Names of the fleet's ships, as read from their
                detail pages. Used only to fill empty places - who has to LEAVE
                is decided by the fleet badge on the picker card, which needs no
                name at all.

        Returns:
            str: 'synced', 'in_sync', 'partial' or 'failed'.

        Pages:
            in: Any
            out: page_dorm
        """
        logger.hr('Dorm sync', level=2)
        fleet = self.dorm_fleet_index

        if not self.dorm_roster_enter():
            return 'failed'
        if not self.dorm_picker_enter(slot=0):
            self.dorm_roster_exit()
            return 'failed'

        # Scan first: three settled frames go by, which is also what the counter
        # needs. Reading it the instant the picker appears can catch the value
        # from before the slot-0 ship was deselected, and a count one too high
        # makes the front block reach past the dorm into the ordinary cards
        # behind it - live, that had the sync trying seven times to "deselect" a
        # fleet-2 ship who was never in the dorm at all.
        cards = self.dorm_scan_fleet()
        chosen, capacity = self.dorm_selected_count()
        if chosen is None:
            logger.warning('Selected counter would not read, leaving the dorm alone')
            self.dorm_picker_exit(confirm=False)
            self.dorm_roster_exit()
            return 'failed'
        capacity = capacity or DORM_SIZE
        # Slot 0's ship was deselected by opening the picker, so the dorm held
        # one more than the counter says
        block = chosen + 1
        logger.info('Dorm holds {} ships, picker shows {} cards, fleet {} is the '
                    'target'.format(block, len(cards), fleet))

        changed = 0
        # Phase 1: the front block is the dorm roster in slot order, and only
        # slot 0's ship is currently deselected
        for index, (card, badge) in enumerate(cards[:block]):
            selected = index != 0
            label = 'dorm card {} (Lv.{}, fleet {})'.format(index, card.level, badge)
            if badge is None:
                # An unreadable badge is not evidence that she left the fleet.
                # She was in the dorm when the picker opened, so put the roster
                # back the way it was and let the next sync decide.
                logger.info('{} - fleet badge would not read the same twice, keeping '
                            'her in the dorm'.format(label))
                keep = True
            else:
                keep = badge == fleet
            if selected == keep:
                continue
            if not self.dorm_toggle(card.button, want_more=keep, note=label):
                if not keep:
                    # She would not come out because she was never in: a card
                    # this far along is not a dorm ship, so the block ends here
                    # and everything after it belongs to the ordinary dock.
                    logger.info('{} would not deselect - the dorm block ends before '
                                'her, stopping the sweep'.format(label))
                    break
                continue
            # Every card in the front block was in the dorm when the picker
            # opened, so the roster only really moves when one of them should
            # not be. Re-selecting slot 0's ship just undoes what opening the
            # picker did and leaves the dorm exactly as it was.
            if not keep:
                changed += 1

        # Phase 2: fill whatever is still empty with the fleet's own ships.
        # Toggling is idempotent - a ship who is already in gets tapped twice
        # and ends up where she started - so the list can be walked blindly.
        for name in names:
            current, _ = self.dorm_selected_count()
            if current is None or current >= capacity:
                break
            cards = self.dorm_search(name)
            if not cards:
                logger.info('{!r} is not in the picker, skipping'.format(name))
                continue
            card = cards[0]
            if self.dorm_toggle(card.button, want_more=True, note=repr(name)):
                changed += 1
        self.dorm_search_close()

        current, _ = self.dorm_selected_count()
        if not changed:
            # Opening the picker deselected slot 0's ship, so cancelling is the
            # only way out that leaves the dorm as it was
            logger.info('Dorm roster already matches the fleet')
            self.dorm_picker_exit(confirm=False)
            self.dorm_roster_exit()
            return 'in_sync'

        logger.info('Confirming the dorm roster ({}/{})'.format(current, capacity))
        if not self.dorm_picker_exit(confirm=True):
            self.dorm_roster_exit()
            return 'failed'

        self.dorm_roster_exit()
        if current is not None and current < capacity:
            logger.warning('Dorm is only {}/{} full - not every fleet ship could be '
                           'found in the picker'.format(current, capacity))
            return 'partial'
        return 'synced'
