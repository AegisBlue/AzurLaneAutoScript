"""
Custom combat assets (fork-only, see CLAUDE.md).

The EN client (2026-08 layout) shows a defeat as an "ANNIHILATED D" result screen whose
Confirm button sits at (1120, 635)-(1235, 690); the generated BATTLE_STATUS_D button in
assets.py points at the combat-report icon next to it. After Confirm the game shows a
second screen, "DEFEAT - Improve your fleet in the following ways", with a single Close
button. Neither is an image asset upstream, so both are described here by area + mean
colour (measured 2026-09-11 from live defeat screens on 15-4).
"""
from module.base.button import Button

# Red title band "DEFEAT" at the top left of the improve-your-fleet screen.
DEFEAT_ADVICE_TITLE = Button(
    area=(60, 55, 420, 130), color=(157, 82, 86), button=(60, 55, 420, 130),
    name='DEFEAT_ADVICE_TITLE')
# Dark "Close" button at the bottom centre of that screen.
DEFEAT_ADVICE_CLOSE = Button(
    area=(573, 593, 693, 637), color=(63, 69, 88), button=(573, 593, 693, 637),
    name='DEFEAT_ADVICE_CLOSE')
