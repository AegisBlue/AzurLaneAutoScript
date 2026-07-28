"""
Census-facing helpers for ShipLeveling: who still has something to gain from
being carried through a campaign stage, and what the fleet looked like last
time round.

Pure stdlib on purpose - same rule as module/ship_census/store.py - so the
offline validation script can import it without the ALAS stack.

Two things live here:

- A Python port of the dashboard's completion rules (judge / summarize). The
  dashboard is the user's picture of "is this ship done", and the task must
  agree with it or the two will argue about when there is nothing left to do.
- The slot state file, config/ship_leveling.json: who the task believes is
  standing in each managed fleet slot, and how many maintenance passes in a
  row she has failed to gain a level. That counter is the only way to notice a
  ship who has hit a level ceiling the census cannot see (see LB_WALLS).
"""
import json
import os
from datetime import datetime

STATE_FILE = './config/ship_leveling.json'
STATE_VERSION = 1

# Level ceiling by limit-break stage. A ship at her ceiling earns no EXP at
# all, so carrying her through a stage is pure waste.
#
# Max limit break (stage 3) is deliberately absent. Its nominal ceiling is 100,
# but Cognitive Awakening lifts it in +5 steps to 125 and nothing on the ship
# detail page says whether a ship has taken those steps - the census field
# cognition_awakened is never actually read by any scanner. Live proof that
# guessing 100 would be wrong: Oumi sits at Lv.100 with 6/6 stars and still
# passes the dock's "not level max" filter, i.e. the game says she can gain
# more EXP. So a max-limit-broken ship gets no wall from this table; the dock
# filter picks her (or not) and looks_walled() below catches her if she turns
# out to be stuck.
LB_WALLS = {0: 70, 1: 80, 2: 90}
# METAs have no limit break - their star row is Somatic Activation - and cap
# at 120 whatever the row says.
META_CAP = 120
# The far end of Cognitive Awakening. Only reachable when cognition_awakened is
# recorded, which no reader sets today.
AWAKEN_CAP = 125
# Maintenance passes a ship may sit at the same level before she is treated as
# walled. Two passes of CheckInterval runs each is far more EXP than any level
# needs, and the level must also look like an awakening ceiling (>=100 and a
# multiple of 5) before the verdict is taken.
STALL_LIMIT = 2

CRIT_LABEL = {
    'level': 'Level',
    'affinity': 'Affinity',
    'enhance': 'Enhance',
    'lb': 'Limit break',
    'skills': 'Skills',
}


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


_now = now


def lb_stage(ship):
    """
    Limit break stage 0-3 from the recorded star row.

    The row is rarity base + 3: an Elite reads 2 gold stars at 0 limit breaks
    and 5 when full, an SR reads 3 and 6 (see ShipCensus.star_row_to_limit_break).

    Returns:
        int: 0-3, or None if the row was never read or does not add up.
    """
    current, total = ship.get('lb_current'), ship.get('lb_max')
    if not current or not total:
        return None
    stage = current - (total - 3)
    return stage if 0 <= stage <= 3 else None


def level_cap(ship, target):
    """
    Highest level this ship can actually reach right now, never above `target`.

    Returning the *reachable* ceiling rather than the configured target is what
    keeps the task from parking a ship who physically cannot gain another point
    of EXP in a fleet slot forever.

    Args:
        ship (dict): Census record.
        target (int): Configured TargetLevel.

    Returns:
        int:
    """
    if ship.get('is_meta'):
        return min(target, META_CAP)
    if ship.get('cognition_awakened'):
        return min(target, AWAKEN_CAP)
    stage = lb_stage(ship)
    if stage is None or stage not in LB_WALLS:
        # Nothing readable to go on, or she is max limit broken and her real
        # ceiling depends on Cognitive Awakening. The dock's not_level_max
        # filter is the backstop for candidates and looks_walled() for fleet
        # occupants.
        return target
    cap = LB_WALLS[stage]
    level = ship.get('level') or 0
    if level > cap:
        # Past a wall she should not be past - a stale limit-break reading.
        # Believe the level, not the stars.
        return target
    return min(target, cap)


def judge(ship, target=120):
    """
    One criterion -> (state, text), state in 'done' / 'pending' / 'na' /
    'unknown'. Port of the dashboard's judge(); keep the two in step.

    Returns:
        dict[str, (str, str)]:
    """
    cap = level_cap(ship, target)
    out = {}

    level = ship.get('level')
    out['level'] = ('unknown', '-') if level is None else \
        ('done' if level >= cap else 'pending', '{}/{}'.format(level, cap))

    affinity = ship.get('affinity')
    out['affinity'] = ('unknown', '-') if affinity is None else \
        ('done' if affinity >= 100 else 'pending', str(affinity))

    # Bulins (no_enhance) have no Enhance tab either - their sidebar is Gear+Info
    if ship.get('is_meta') or ship.get('is_research') or ship.get('no_enhance'):
        out['enhance'] = ('na', 'n/a')
    elif ship.get('enhance_maxed') is None:
        out['enhance'] = ('unknown', '-')
    else:
        out['enhance'] = ('done', 'MAX') if ship['enhance_maxed'] else ('pending', 'open')

    current, total = ship.get('lb_current'), ship.get('lb_max')
    out['lb'] = ('unknown', '-') if current is None or total is None else \
        ('done' if current >= total else 'pending', '{}/{}'.format(current, total))

    skills = ship.get('skills')
    if not skills:
        out['skills'] = ('unknown', '-')
    else:
        done = all(not s.get('locked') and (s.get('level') or 0) >= (s.get('max') or 0)
                   for s in skills)
        text = '.'.join('L' if s.get('locked') else str(s.get('level')) for s in skills)
        out['skills'] = ('done' if done else 'pending', text)
    return out


def summarize(ship, target=120):
    """
    Roll the criteria up the way the dashboard's summarize() does.

    Returns:
        dict: crit, done, applicable, unknown, missing, progress, complete
    """
    crit = judge(ship, target)
    done = applicable = unknown = 0
    missing = []
    for key, (state, _) in crit.items():
        if state == 'na':
            continue
        applicable += 1
        if state == 'done':
            done += 1
        else:
            missing.append(CRIT_LABEL[key])
            if state == 'unknown':
                unknown += 1
    return dict(crit=crit, done=done, applicable=applicable, unknown=unknown,
                missing=missing,
                progress=(done / applicable) if applicable else 0.0,
                complete=applicable > 0 and done == applicable)


# ---------------- what farming can still fix ----------------

def wants_level(ship, target):
    """
    Whether carrying this ship through a stage would earn her anything.

    An unread level is not a claim in either direction, so it counts as no
    work: the dock's own filters find levellable ships without the census, and
    treating unknowns as work would keep the task alive forever.
    """
    if ship.get('missing'):
        return False
    level = ship.get('level')
    if level is None:
        return False
    return level < level_cap(ship, target)


def wants_affinity(ship):
    """
    Whether this ship is still short of Love (100). Oathed ships read above
    100 and are done; an unread affinity counts as no work, as above.
    """
    if ship.get('missing'):
        return False
    affinity = ship.get('affinity')
    if affinity is None:
        return False
    return affinity < 100


def farm_work_left(ships, target):
    """
    Args:
        ships (iterable[dict]): Census records.
        target (int): TargetLevel.

    Returns:
        (int, int): How many ships still want levels, and how many still want
            affinity. (0, 0) means a campaign stage has nothing left to give
            anyone on record.
    """
    levels = affinities = 0
    for ship in ships:
        if wants_level(ship, target):
            levels += 1
        if wants_affinity(ship):
            affinities += 1
    return levels, affinities


def looks_walled(level, stalls):
    """
    Whether a fleet occupant has stopped gaining EXP at a ceiling the census
    cannot see - a Cognitive Awakening step (105/110/115/120) or the plain
    max-limit-break 100.

    Both halves matter: a level that is not a multiple of 5 at or above 100 is
    simply a slow climb, and one pass without a level-up says nothing at all
    once the ship is over 100.
    """
    if level is None or level < 100 or level % 5:
        return False
    return stalls >= STALL_LIMIT


# ---------------- slot state ----------------

class SlotState:
    """
    config/ship_leveling.json - who the task put in each managed fleet slot.

    Only used for continuity between runs: the fleet itself is always re-read
    from the ship detail pages, and a state file that disagrees loses.
    """

    def __init__(self, file=STATE_FILE):
        self.file = file
        self.data = {'version': STATE_VERSION, 'updated': None, 'slots': {}}

    def load(self):
        if not os.path.exists(self.file):
            return self
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (ValueError, OSError):
            return self
        if data.get('version') == STATE_VERSION:
            self.data = data
        return self

    def save(self):
        self.data['updated'] = _now()
        tmp = self.file + '.tmp'
        directory = os.path.dirname(self.file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.file)

    @property
    def slots(self):
        return self.data['slots']

    def get(self, slot):
        return self.slots.get(slot) or {}

    def clear(self, slot):
        self.slots.pop(slot, None)

    def note(self, slot, name, hp, level, key=None, kind=None):
        """
        Record who is standing in `slot` and count level stalls.

        The stall counter only survives while the same ship keeps standing
        there: a different name or hull, or any level gain, resets it.

        Returns:
            dict: The slot record, including 'stalls'.
        """
        prior = self.get(slot)
        same = prior.get('name') == name and prior.get('hp') == hp
        gained = level is not None and prior.get('level') is not None \
            and level > prior['level']
        if same and not gained and level is not None and prior.get('level') == level:
            stalls = int(prior.get('stalls') or 0) + 1
        else:
            stalls = 0
        record = dict(name=name, hp=hp, level=level, key=key, kind=kind,
                      stalls=stalls, seen=_now())
        if not same:
            record['since'] = _now()
        else:
            record['since'] = prior.get('since') or _now()
        self.slots[slot] = record
        return record
