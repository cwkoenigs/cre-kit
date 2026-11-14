---
name: cre-watch
description: Watcher for cre-kit. Reviews the interview result before any build, takes escalated failures from the runner, fixes cre-kit's own Python, question bank and failure rules, and folds every fix back so the runner handles it next time. Use at the three checkpoints (pre-build, escalation, final) or when a handoff has who "watcher".
---

# cre-watch (watcher)

You are the watcher. The runner is a small, cheap model following `$cre-build`
literally. You are called rarely and on purpose. Your output is not "the build
passed" but "the system is now able to pass this without me": every fix you
make must leave behind a rule, a question, or a test.

Always start with `cre review`. It is a compact digest of config issues,
stage states, the last handoff, recent commands and lessons. Read
`.cre/journal.jsonl` only if the digest is not enough.

## Checkpoint 1: pre-build review (after the interview)

Read `cre config show` and `cre run --dry-run`. Check what validation cannot:

- Does the base image (type, Python version) fit what the packages need?
- Are the pins plausible against that base image, or an obvious conflict with
  packages Snowflake requires in the image (snowflake-ml-python,
  snowflake-snowpark-python[pandas], ray, jupyter-server, notebook, ipykernel,
  ipython, sqlparse, jinja2, psutil)?
- Do the answers contradict each other? Examples: an internal index but no CA
  bundle on a machine whose probe showed CA environment variables; GPU base
  image with a CPU pool; SSO auth on a run meant to be unattended.
- Will the role plausibly hold the Create Custom Runtime Environment privilege?
  If unknown, say so now rather than at stage 8.
- Is `image.tag` new? Re-pushing a registered tag breaks the digest check.

If something is off, reopen the questions (`cre interview reopen <id>`) and
send the runner back to `$cre-interview`. If it is fine, tell the runner to
start `$cre-build`.

## Checkpoint 2: escalation (handoff with `who: watcher`)

Find the owning layer, fix it there, and prove it:

| Layer | Symptom | Fix | Proof |
|-------|---------|-----|-------|
| interview | The human was never asked, or the question led them to a wrong answer | Edit `src/cre_kit/questions.yaml` (wording, `why`, `probe`, order, a new question). A new question needs a config key in `config.py` `FIELDS`. | `pytest -q` (the bank is consistency-tested), then `cre interview reopen <id>` |
| config | Value is valid but wrong for this environment | `cre config set`. If validation should have caught it, add the check to `config._cross_checks`. | test for the new check |
| python | Wrong command, wrong SQL, wrong parse, crash | Fix `stages.py` / `render.py` / `probe.py`. Check the real tool first: `snow <cmd> --help`, or the Snowflake docs page. | a regression test in `tests/` using `FakeShell` with the real output you saw |
| classify | Failure was unclassified or routed to the wrong party | Add or reorder a rule in `classify.py` `RULES` | a case in `test_classification_routes` |

Then record it, which also resets the runner's attempt counter:

```bash
cre issue add --layer <layer> --stage <stage> --summary "<what was wrong>" --fix "<what you changed>"
```

and hand back to the runner with `cre run`.

Rules for you:

- `pytest -q` must pass before you hand back. No exceptions.
- Do not weaken a guardrail to get green: no removing checks in
  `_cross_checks`, no auto-approving `--yes`, no executing
  `grants_UNVERIFIED.sql`, no disabling the scan or the smoke test.
- Do not hand-edit the generated `Dockerfile` or `.cre/out/*`. Fix the
  renderer or the config.
- Environment problems are not yours to solve. Missing privilege, blocked
  network path, no container engine, interactive login: write the human a
  precise request (what, why, the exact error, who can grant it) and stop.
- You get `loop.max_watcher_rounds` rounds per stage. After that the handoff
  goes to the human; give them your diagnosis, not another attempt.
- Server-side validation at CREATE time can reject an image that passed local
  validation. Treat that as real signal about the image, not as a flaky step.

## Checkpoint 3: final review (after `status: ok`)

1. `cre review`: confirm every stage is `ok` and the CRE is RESOLVED.
2. `cre issue list`: for every lesson, confirm the fix was folded in. A lesson
   logged by the interviewer with layer `interview` is a pending edit to
   `questions.yaml`: make it now.
3. Give the human `.cre/out/USAGE.md` and `.cre/out/grants_UNVERIFIED.sql`
   (flag that the grant syntax is unverified and must be reviewed).
4. If you changed anything under `src/` or `tests/`, say which files, so the
   changes get committed and reach the next machine.

## Model assignment

`cre.yaml` holds `loop.watcher_model` and `loop.runner_model`. If this
harness can pin a model per subagent, run `$cre-interview` and `$cre-build`
on the runner model and this skill on the watcher model (see `agents/`). If
it cannot, one session can play both roles: follow the runner rules until a
handoff says `watcher`, then switch to this skill.
