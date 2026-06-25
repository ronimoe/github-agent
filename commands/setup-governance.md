---
description: Bootstrap Conductor's server-side governance (linear-history ruleset + required check + sole-writer bot App) on a GitHub repo. Idempotent. Requires gh auth + repo admin.
argument-hint: "<owner>/<repo> [app-installation-id]"
---

Install Conductor's **non-bypassable safety backstop** on the GitHub repo `$ARGUMENTS` — the
server-side half that holds even when local hooks are bypassed.

Before running, confirm with the user:
- `gh auth status` is authenticated to an account with **admin** on the repo;
- they understand this protects `main` (linear history, required `conductor-landed` status check,
  no force-push / no deletion) and designates the Conductor bot App as the sole trunk writer.

Then run (it is idempotent — safe to re-run):

```
conductor setup $ARGUMENTS
```

Afterwards, explain exactly what the ruleset now enforces and what the human still needs to do
(create/connect the bot App and set its installation id, provision a real approving reviewer so an
all-agent fleet doesn't deadlock on required reviews). This is detection + enforcement, not the
merge engine itself — the engine drives PRs *through* these gates.
