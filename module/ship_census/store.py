"""
Ship census data store.

Pure stdlib on purpose: the dashboard generator and any future auto-max task
import this without pulling in the ALAS stack. One JSON file, one record per
owned ship copy, plus the sweep cursor that makes scans resumable.

Schema v1 record fields (None / [] = not collected yet):
    key                 "Bismarck#1" - name plus copy number for duplicates
    name                OCR'd ship name from the detail page
    copy                1-based duplicate counter in sweep order
    rarity              'elite' / 'super_rare' / 'ultra_rare' / 'rare' / 'normal'
    is_meta             bool - META ships have no Enhance/LimitBreak, they use the Lab
    is_research         bool - PR/DR research ships enhance via the Shipyard instead
    no_enhance          bool - limit-break fodder (Bulins): no Enhance tab at all
    level               int
    affinity            float, 0-200 (100 = Love, >100 only after oath)
    oathed              bool
    enhance_maxed       bool - all enhance stats full
    lb_current, lb_max  limit break stage; for METAs these hold activation stars
    skills              list of {"level": int, "max": int, "locked": bool}
    cognition_awakened  bool - the 125-cap mechanic
    missing             bool - not seen in the last completed sweep (retired?)
    first_seen / last_seen / last_deep_scan   'YYYY-MM-DD HH:MM:SS'
"""
import json
import os
from datetime import datetime, timedelta

STORE_FILE = './config/ship_census.json'
SCHEMA_VERSION = 1
# Bumped whenever a reader in census.py changes what it can see. Records
# deep-scanned by an older reader are re-scanned on the next delta run instead
# of sitting on a stale (possibly wrong) value until StaleDays expires.
#   1 -> 2: enhance rows judged by green dominance + bar fill, so ships whose
#           art washed out the old brightness test stop reading "open"
#   2 -> 3: the star row no longer needs its dark stars to be seen (they sink
#           into ship art), so limit break reads on ships that came back null -
#           and the 5-slot rows recorded for 6-slot SR ships - get corrected
READER_VERSION = 3

SHIP_DEFAULTS = {
    'name': None,
    'copy': 1,
    'rarity': None,
    'is_meta': False,
    'is_research': False,
    'no_enhance': False,
    'level': None,
    'hp': None,
    'affinity': None,
    'oathed': None,
    'enhance_maxed': None,
    'lb_current': None,
    'lb_max': None,
    'skills': [],
    'cognition_awakened': None,
    'missing': False,
    'first_seen': None,
    'last_seen': None,
    'last_deep_scan': None,
    'reader_version': None,
}


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def missing_fields(ship):
    """
    Which pieces of a record never came back.

    Lab ships (META / PR research) have no Enhance tab at all, so a null
    enhance_maxed is right for them and is not reported; nor do Bulins, which
    carry no_enhance (live, 7 Prototype Bulin MKII records were re-read over and
    over for a tab that does not exist). `rarity` is left out too: it comes from
    the name dictionary rather than the screen, so a gap there means the name did
    not canonicalise and re-reading will not help.

    Args:
        ship (dict): One record.

    Returns:
        list[str]: Field names, empty when the record is complete.
    """
    missing = [field for field in ('level', 'hp', 'affinity', 'lb_current', 'lb_max')
               if ship.get(field) is None]
    if not ship.get('skills'):
        missing.append('skills')
    lab = ship.get('is_meta') or ship.get('is_research') or ship.get('no_enhance')
    if ship.get('enhance_maxed') is None and not lab:
        missing.append('enhance_maxed')
    return missing


class CensusStore:
    def __init__(self, file=STORE_FILE):
        self.file = file
        self.data = self._empty()

    @staticmethod
    def _empty():
        return {
            'version': SCHEMA_VERSION,
            'updated': None,
            'scan': {
                'in_progress': False,
                'cursor': 0,
                'mode': None,
                'scope': None,
                'started': None,
                'completed': None,
                'seen_this_sweep': [],
            },
            'ships': {},
        }

    def load(self):
        if not os.path.exists(self.file):
            return self
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (ValueError, OSError):
            # Corrupt store: keep the broken file aside instead of overwriting
            backup = self.file + '.corrupt'
            try:
                os.replace(self.file, backup)
            except OSError:
                pass
            return self
        if data.get('version') == SCHEMA_VERSION:
            self.data = data
        return self

    def save(self):
        self.data['updated'] = _now()
        tmp = self.file + '.tmp'
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.file)

    @property
    def ships(self):
        return self.data['ships']

    @property
    def scan(self):
        return self.data['scan']

    # ---------------- sweep lifecycle ----------------

    def sweep_begin(self, mode, scope):
        """
        Start a sweep, or resume the interrupted one if mode+scope match.

        Returns:
            int: cursor - number of ships already processed this sweep,
                i.e. how many to skip past when resuming.
        """
        scan = self.scan
        if scan['in_progress'] and scan['mode'] == mode and scan['scope'] == scope:
            return scan['cursor']
        self.data['scan'] = {
            'in_progress': True,
            'cursor': 0,
            'mode': mode,
            'scope': scope,
            'started': _now(),
            'completed': None,
            'seen_this_sweep': [],
        }
        return 0

    def sweep_key(self, name):
        """
        Key for the next occurrence of `name` in this sweep. Duplicate copies
        get #2, #3... in dock order; resuming rebuilds the counters from
        seen_this_sweep so copies keep their numbers.
        """
        n = 1
        while '{}#{}'.format(name, n) in self.scan['seen_this_sweep']:
            n += 1
        return '{}#{}'.format(name, n)

    def sweep_advance(self, key):
        self.scan['cursor'] += 1
        if key is not None and key not in self.scan['seen_this_sweep']:
            self.scan['seen_this_sweep'].append(key)

    def sweep_end(self, complete):
        """
        Args:
            complete (bool): True if the sweep reached the end of the dock.
                Ships in scope that were not seen get flagged missing
                (retired / merged); an aborted sweep flags nothing.
        """
        scan = self.scan
        scan['in_progress'] = False
        if complete:
            scan['completed'] = _now()
            seen = set(scan['seen_this_sweep'])
            for key, ship in self.ships.items():
                if key not in seen and self._in_scope(ship, scan['scope']):
                    ship['missing'] = True

    @staticmethod
    def _in_scope(ship, scope):
        rarity = ship.get('rarity')
        if rarity is None:
            return True
        if scope == 'elite_and_above':
            return rarity in ('elite', 'super_rare', 'ultra_rare')
        if scope == 'rare_and_above':
            return rarity in ('rare', 'elite', 'super_rare', 'ultra_rare')
        return True

    # ---------------- records ----------------

    def record(self, key, deep=False, **fields):
        """
        Upsert a ship record. Only keys present in `fields` are touched, so a
        quick pass (name+level) never wipes deep-scanned data.
        """
        now = _now()
        ship = self.ships.get(key)
        if ship is None:
            ship = dict(SHIP_DEFAULTS)
            ship['key'] = key
            ship['first_seen'] = now
            self.ships[key] = ship
        for k, v in fields.items():
            if k in SHIP_DEFAULTS:
                ship[k] = v
        ship['missing'] = False
        ship['last_seen'] = now
        if deep:
            ship['last_deep_scan'] = now
            ship['reader_version'] = READER_VERSION
        return ship

    def incomplete_ships(self):
        """
        Records with gaps, for the repair scan.

        Returns:
            dict[str, list[str]]: key -> missing field names.
        """
        out = {}
        for key, ship in self.ships.items():
            missing = missing_fields(ship)
            if missing:
                out[key] = missing
        return out

    def prune_phantom_copies(self, grid_hp_counts):
        """
        Drop duplicate records the dock has no card for.

        A sweep that could not tell the end of the dock from a swallowed swipe
        re-walked its last cards and filed each of them again under the next
        free copy number - live, 46 such records, every one permanently
        incomplete and every one a target the repair scan can never satisfy
        because there is no second card to open.

        The grid pass is the arbiter: it over-counts rather than under-counts
        (it re-reads a row when two screens fail to share one), so "more records
        with this HP than the dock has cards with this HP" is a conservative
        test - it cannot accuse a real duplicate. Only incomplete records are
        ever dropped, newest first, and never the last record of a ship name.

        Args:
            grid_hp_counts (dict): HP -> number of dock cards, from a grid pass.

        Returns:
            list[str]: Keys removed.
        """
        if not grid_hp_counts:
            return []
        names = {}
        by_hp = {}
        for key, ship in self.ships.items():
            names[ship.get('name')] = names.get(ship.get('name'), 0) + 1
            hp = ship.get('hp')
            if hp:
                by_hp.setdefault(hp, []).append(key)

        removed = []
        for hp, keys in by_hp.items():
            allowed = grid_hp_counts.get(hp, 0)
            if not allowed or len(keys) <= allowed:
                continue
            spare = [k for k in keys if missing_fields(self.ships[k])]
            # Newest first: the earlier copy of a pair is the one the sweep met
            # on its way down the dock, the later one the re-read
            spare.sort(key=lambda k: (self.ships[k].get('first_seen') or '',
                                      self.ships[k].get('copy') or 1), reverse=True)
            surplus = len(keys) - allowed
            for key in spare:
                if surplus <= 0:
                    break
                name = self.ships[key].get('name')
                if names.get(name, 0) <= 1:
                    continue
                names[name] -= 1
                del self.ships[key]
                removed.append(key)
                surplus -= 1
        return removed

    def needs_deep_scan(self, key, level, stale_days, full=False):
        """
        Delta rule: deep-scan a ship unless we have a fresh, complete record
        at the same level, read by the current readers. Level can only rise,
        so a changed level always forces a rescan; affinity drifts silently,
        hence the staleness window.
        """
        if full:
            return True
        ship = self.ships.get(key)
        if ship is None or ship.get('last_deep_scan') is None:
            return True
        if ship.get('reader_version') != READER_VERSION:
            return True
        if ship.get('level') != level:
            return True
        core = ('affinity', 'enhance_maxed', 'lb_current', 'lb_max')
        if not ship.get('is_meta') and any(ship.get(k) is None for k in core):
            return True
        if not ship.get('skills'):
            return True
        try:
            scanned = datetime.strptime(ship['last_deep_scan'], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return True
        return datetime.now() - scanned > timedelta(days=stale_days)

    # ---------------- export ----------------

    def to_payload(self):
        """Dict embedded into the dashboard HTML."""
        return {
            'version': SCHEMA_VERSION,
            'generated': _now(),
            'updated': self.data.get('updated'),
            'scan': dict(self.scan),
            'ships': [dict(s) for s in self.ships.values()],
        }
