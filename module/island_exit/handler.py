from module.logger import logger

# Set False to disable the workaround and navigate out of the island normally,
# e.g. once a GPU driver update fixes the client freeze described below.
ISLAND_EXIT_VIA_RESTART = True


def is_island_exit(page):
    """
    Whether walking from `page` to `page.parent` leaves the island entirely.

    Island subpages (shop, map, season, technology, phone, order) are safe to
    move between, so only the final hop out of the island group matters:
    page_island -> page_dormmenu, or page_island_phone -> page_main.

    Args:
        page (Page): Current page. `page.parent` is the next hop toward the destination.

    Returns:
        bool:
    """
    return page.is_island() and not page.parent.is_island()


def island_exit_restart(main, page):
    """
    Leave the island by restarting the game client instead of navigating out.

    Leaving the island makes the client tear down the island scene, releasing the
    modular player-avatar assets (`Assets/Island/Character/9001/...`). On some GPU
    drivers that bulk release deadlocks inside the emulator's graphics translation
    layer: the Unity main thread stops ~2s into the transition and never resumes,
    the emulator stops producing frames, and the host eventually kills it as hung.
    Every exit route is affected, including the in-game phone.

    Force-restarting the client skips the teardown entirely - the graphics context
    is destroyed wholesale by the OS, which drivers handle correctly. `ui_goto`
    then resumes routing from the login screen, which `ui_additional` clicks
    through on its own.

    Args:
        main (ModuleBase): Caller, provides `device`.
        page (Page): Current page.

    Returns:
        bool: True if the client was restarted and `ui_goto` should re-evaluate.
    """
    if not ISLAND_EXIT_VIA_RESTART:
        return False
    if not is_island_exit(page):
        return False

    logger.info(f'Island exit ({page} -> {page.parent}), '
                f'restarting client to avoid a client freeze')
    main.device.app_stop()
    main.device.app_start()
    # Nothing is recognisable during the client's startup logos, so skip ahead a
    # little instead of letting ui_goto screenshot a black screen. It then keeps
    # looping until ui_additional() clicks through the login screen.
    main.device.sleep(10)
    return True
