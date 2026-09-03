"""
CustomAlas addition: watchdog for OpSi auto search.

Why this exists
---------------
`OSMap.os_auto_search_daemon()` calls `device.stuck_record_clear()` on every loop
iteration while the fleet is on the map, because a fleet travelling between enemies
legitimately produces no UI change for a while. That also means the stock stuck
detection can never fire while the game shows the map.

On this setup (BlueStacks, EN server) battles sometimes freeze at "NOW LOADING 50%".
ALAS restarts the game, but the server still believes that stage is in progress, so
the next auto search parks the fleet on the enemy with an "In action / ESCAPE" bubble,
the client reports "failed to begin stage: invalid repeat operation", and no battle
ever starts. Auto search stays enabled, the fleet stays on the map, and the daemon
loops silently forever (observed hangs of 46 min to 3.6 h, 2026-09-01/02).

What it does
------------
`auto_search_guard_check()` is called from the daemon on every *idle* iteration, i.e.
an iteration where nothing was handled. Any iteration that handled something (combat,
retirement, map events, ...) skips the check, so a gap between two checks means the
loop was busy and the idle clock restarts. After GUARD_TIMEOUT seconds without any
activity the guard restarts the game, delays the current task and stops it, so the
scheduler moves on to other tasks and retries OpSi later. Observed recovery on
2026-09-01/02: after a game restart plus a few hours away from OpSi the same zone
fought normally again.
"""
import time

from module.logger import logger
from module.notify import handle_notify
from module.os_handler.assets import AUTO_SEARCH_OS_MAP_OPTION_ON


class AutoSearchGuard:
    # Seconds of idle auto search before we give up on this attempt.
    # Fleet travel between enemies is well under a minute; 180 s matches ALAS's long stuck timer.
    GUARD_TIMEOUT = 180
    # Minutes to delay the current OpSi task before retrying.
    GUARD_DELAY_MINUTE = 30
    # If no check happened for this long, the daemon was busy (combat etc.), so it is progress.
    GUARD_BUSY_GAP = 5

    _guard_idle_since = 0.
    _guard_last_check = 0.

    def auto_search_guard_reset(self):
        now = time.time()
        self._guard_idle_since = now
        self._guard_last_check = now

    def auto_search_guard_check(self):
        """
        Call on idle iterations of os_auto_search_daemon().

        Raises:
            TaskEnd: after GUARD_TIMEOUT seconds of idle auto search,
                with a game restart queued and the current task delayed.
        """
        now = time.time()
        if now - self._guard_last_check > self.GUARD_BUSY_GAP:
            self._guard_idle_since = now
        self._guard_last_check = now
        if now - self._guard_idle_since < self.GUARD_TIMEOUT:
            return False

        enabled = self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120))
        logger.warning(f'OS auto search idle for {self.GUARD_TIMEOUT}s (auto search enabled={enabled}), '
                       f'fleet probably stuck "In action" because the server refused to begin the stage')
        file = f'./log/error/{int(now * 1000)}_auto_search_guard.png'
        try:
            self.device.image_save(file)
            logger.info(f'Screenshot saved to {file}')
        except Exception as e:
            logger.warning(f'Failed to save screenshot: {e}')

        logger.warning(f'Restarting game and delaying task `{self.config.task.command}` '
                       f'for {self.GUARD_DELAY_MINUTE} minutes')
        handle_notify(
            self.config.Error_OnePushConfig,
            title=f'Alas <{self.config.config_name}> OpSi auto search stuck',
            content=f'<{self.config.config_name}> auto search idle for {self.GUARD_TIMEOUT}s in '
                    f'`{self.config.task.command}`. Restarting game, retrying in {self.GUARD_DELAY_MINUTE} min.',
        )
        self.auto_search_guard_reset()
        self.config.task_call('Restart')
        self.config.task_delay(minute=self.GUARD_DELAY_MINUTE)
        self.config.task_stop()
