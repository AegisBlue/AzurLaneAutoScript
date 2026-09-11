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
    area=(60, 55, 420, 130), color=(156, 83, 86), button=(60, 55, 420, 130),
    name='DEFEAT_ADVICE_TITLE')
# Dark "Close" button at the bottom centre of that screen.
DEFEAT_ADVICE_CLOSE = Button(
    area=(573, 593, 693, 637), color=(51, 71, 93), button=(573, 593, 693, 637),
    name='DEFEAT_ADVICE_CLOSE')

# "ANNIHILATED D" result screen: orange Confirm button (bottom right) plus the red title band.
DEFEAT_RESULT_CONFIRM = Button(
    area=(1140, 645, 1215, 680), color=(230, 181, 113), button=(1140, 645, 1215, 680),
    name='DEFEAT_RESULT_CONFIRM')
DEFEAT_RESULT_BAND = Button(
    area=(40, 20, 600, 45), color=(148, 77, 80), button=(40, 20, 600, 45),
    name='DEFEAT_RESULT_BAND')
