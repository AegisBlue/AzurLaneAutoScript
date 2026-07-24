# CustomAlas — Custom Fork of AzurLaneAutoScript (ALAS)

This is the user's **custom fork** of [AzurLaneAutoScript](https://github.com/LmeSzinc/AzurLaneAutoScript),
an Azur Lane automation bot. The purpose of this repo: **add the user's own AI-generated
features/tools while still receiving updates from the official project.**

There is a second, untouched ALAS install in the sibling folder `..\AzurLaneAutoScript`
(the user's stable daily-driver, web UI on port 22267). Never modify that folder.

## Git layout

| Ref | Points to | Rule |
|---|---|---|
| branch `custom` | tracks `origin/custom` | **The working branch.** All custom features are committed here. |
| branch `master` | tracks `upstream/master` | Clean mirror of official ALAS. Never commit to it. |
| remote `origin` | https://github.com/AegisBlue/AzurLaneAutoScript | The user's fork (backup of `custom`). |
| remote `upstream` | https://github.com/LmeSzinc/AzurLaneAutoScript | Official repo (source of updates). |

## Receiving official updates

On branch `custom`:

```
git fetch upstream
git merge upstream/master
git push
```

Resolve conflicts only if upstream touched the same lines as a custom change.

## Conventions for custom features

- Put new features in **new files/folders** (e.g. `module/<feature_name>/`) — new files can
  never produce merge conflicts with upstream.
- When a feature must hook into existing ALAS files, keep the edit to 1–2 lines.
- Commit on `custom` with clear messages; `git push` afterward to back up to the fork.
- New GUI-visible tasks are defined via the config-generation system in
  `module/config/argument/` (`task.yaml`, `argument.yaml`, etc. — `args.json` is generated,
  don't hand-edit it); check how existing tasks are wired before adding one.

## CRITICAL: the built-in ALAS updater is disarmed — keep it that way

ALAS's self-updater (`deploy/git.py`) runs `git reset --hard` against the configured
repository, which **destroys local commits**. In this copy, `config/deploy.yaml`
(git-ignored, local only) is set to:

- `AutoUpdate: false`, `EnableReload: false`, `CheckUpdateInterval: 0`, `AutoRestartTime: null`
- `Repository: https://github.com/AegisBlue/AzurLaneAutoScript`, `Branch: custom`
  (defense in depth — an accidental update resets to the user's own fork, not official master)
- `WebuiPort: 22270` (the original install uses 22267)

Never re-enable these settings, never use the Update button in this copy's web GUI, and
never run `git reset --hard`. Updates arrive **only** via the fetch/merge workflow above.

## Runtime pieces (git-ignored, local only — copied from the original install 2026-07-24)

- `toolkit\` — bundled Python 3.7.6, Git, ADB (Easy Install toolkit)
- `Alas.exe` — launcher; open http://127.0.0.1:22270 after starting
- `config\alas.json` — the user's game/instance settings
- `config\deploy.yaml` — deployment settings (see hazard section above)

Do not run this copy and the original install at the same time — both grab the emulator's
ADB connection and will kill each other's servers.
