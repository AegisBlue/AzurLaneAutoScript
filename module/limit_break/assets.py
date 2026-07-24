from module.base.button import Button
from module.base.template import Template

# PLACEHOLDER ASSETS — hand-written scaffolding for the custom LimitBreak task.
#
# The areas below are rough guesses and the referenced PNG files do not exist yet.
# The task refuses to run until the PNGs are captured (see LimitBreak.lb_assets_ready).
#
# To finish the feature:
#   1. Capture 1280x720 screenshots of each screen listed below.
#   2. For each asset, save a copy of the screenshot as
#      assets/en/limit_break/<ASSET_NAME>.png with everything painted
#      black (0, 0, 0) except the region of the button/marker.
#   3. Run: toolkit/python.exe dev_tools/button_extract.py
#      which regenerates this file from the PNGs (same as every other module).

# Ship detail page: the "Limit Break" button on the right-side button column.
LIMIT_BREAK_ENTER = Button(
    area=(1130, 320, 1250, 360), color=(0, 0, 0), button=(1130, 320, 1250, 360),
    file='./assets/en/limit_break/LIMIT_BREAK_ENTER.png')
# Limit break screen: a static marker unique to this screen (e.g. the "Limit Break" title).
LIMIT_BREAK_CHECK = Button(
    area=(50, 20, 200, 60), color=(0, 0, 0), button=(50, 20, 200, 60),
    file='./assets/en/limit_break/LIMIT_BREAK_CHECK.png')
# Limit break screen: the "+" of an empty material slot. Matched with a wide offset
# so one asset finds whichever of the 1-3 slots is empty.
LB_SLOT_ADD = Button(
    area=(400, 400, 460, 460), color=(0, 0, 0), button=(400, 400, 460, 460),
    file='./assets/en/limit_break/LB_SLOT_ADD.png')
# Limit break screen: the coin icon next to the gold cost. The area below it is checked
# for red letters, which the game shows when coins are insufficient.
LB_COST_COIN = Button(
    area=(900, 600, 960, 660), color=(0, 0, 0), button=(900, 600, 960, 660),
    file='./assets/en/limit_break/LB_COST_COIN.png')
# Limit break screen: the button that performs the limit break once slots are filled.
LB_EXECUTE = Button(
    area=(1050, 620, 1230, 680), color=(0, 0, 0), button=(1050, 620, 1230, 680),
    file='./assets/en/limit_break/LB_EXECUTE.png')

# Material selection screen: a static marker unique to this screen.
MATERIAL_CHECK = Button(
    area=(50, 20, 200, 60), color=(0, 0, 0), button=(50, 20, 200, 60),
    file='./assets/en/limit_break/MATERIAL_CHECK.png')
# Material selection screen: the "n/m" selected-ships counter, read by OCR.
MATERIAL_SELECTED = Button(
    area=(1000, 660, 1090, 700), color=(0, 0, 0), button=(1000, 660, 1090, 700),
    file='./assets/en/limit_break/MATERIAL_SELECTED.png')
# Material selection screen: the confirm button.
MATERIAL_CONFIRM = Button(
    area=(1100, 650, 1250, 700), color=(0, 0, 0), button=(1100, 650, 1250, 700),
    file='./assets/en/limit_break/MATERIAL_CONFIRM.png')
# Material selection screen: the cancel/back button, used when materials are insufficient.
MATERIAL_CANCEL = Button(
    area=(930, 650, 1080, 700), color=(0, 0, 0), button=(930, 650, 1080, 700),
    file='./assets/en/limit_break/MATERIAL_CANCEL.png')

# Card portraits used to tell bulins apart from duplicate ships in the material list.
# Universal Bulin: the purple/elite rarity one.
TEMPLATE_BULIN_UNIVERSAL = Template(file='./assets/en/limit_break/TEMPLATE_BULIN_UNIVERSAL.png')
# Prototype Bulin MKII: the gold/SR rarity one.
TEMPLATE_BULIN_MKII = Template(file='./assets/en/limit_break/TEMPLATE_BULIN_MKII.png')
