# Wheelhouse — operating rules for Claude Code

## HARD RULE: how Wheelhouse gets updated

The **only** supported way to change anything running in production is:

> **edit → commit → push → autodeploy**

Edit the code in a working clone, commit it, and **push to `origin/main`**. A
systemd timer (`wheelhouse-autodeploy.timer`, every 2 min) running on the
Beelink server fast-forwards `/opt/wheelhouse` and restarts
`wheelhouse.service`. That timer is the deployment mechanism. There is no other.

This is a rule, not a suggestion. The following are **forbidden**:

- **No hand-editing `/opt/wheelhouse` over SSH.** Do not open files in the live
  checkout and change them in place. `/opt/wheelhouse` is a deploy target, not a
  workspace.
- **No ad-hoc `sed`/`awk`/`patch`/inline edits** against the live tree, ever.
- **Never leave `/opt/wheelhouse` with an uncommitted or dirty tree.** The
  autodeploy script refuses to pull when the tracked tree is dirty — a stray
  edit there silently **blocks every future deploy** until someone cleans it up.
- **Push, don't pull.** Never `git pull` (or fetch-and-merge) by hand on the
  server, and never push *from* the server. Changes flow one direction: your
  clone → GitHub → the server pulls itself via the timer.

If the live tree is ever dirty, the fix is to **restore it clean**
(`git checkout -- <file>` / `git restore`), never to commit from the server.
Verify the result of a change by watching the autodeploy journal, not by editing
on the box:

```
journalctl -u wheelhouse-autodeploy -f
```

## Deployment facts

- **Repo on server:** `/opt/wheelhouse` (owned by `rednun`)
- **Service:** `wheelhouse.service` (gunicorn, `127.0.0.1:8090`)
- **Autodeploy:** `wheelhouse-autodeploy.timer` → `.service` →
  `/usr/local/bin/wheelhouse-autodeploy.sh` (root, **lives outside the repo**)
- **Restart policy:** the autodeploy restarts the service on any code/asset
  change; it skips the restart when a pull only touched `docs/*` or `*.md`.
