---
name: cre-interview
description: Interview this machine and its user to fill cre.yaml for a Snowflake Custom Runtime Environment (custom ML runtime image). Use when setting up cre-kit on a new machine, when `cre run` says the interview is incomplete, or when a handoff has fix_layer "interview".
---

# cre-interview

You fill `cre.yaml` by relaying questions. You do not invent questions, guess
answers, or edit `cre.yaml` by hand. The `cre` command owns the question list,
the probing, and the validation.

## Setup (once per machine)

```bash
uv pip install -e ".[dev]"     # from the cre-kit folder; add ,smoke for the smoke test
cre init                       # creates cre.yaml in the current folder
```

Run every `cre` command from the folder that holds `cre.yaml`.

## Loop

1. Run `cre interview next`. It prints one JSON object.
2. If `"done": true`, stop and go to "Finish".
3. Ask the human the `ask` text. Then:
   - If `probe.suggestion` is not null, offer it: "I found `<suggestion>` on this
     machine. Use it?" If `probe.observed` is a list of options, show the options.
   - If `probe.note` is not null, say it. It is usually a warning.
   - If `choices` is set, the answer must be one of them.
   - If the human asks why, give the `why` text.
4. Record the answer exactly as given:
   - `cre interview answer <id> '<value>'`
   - Lists: comma separated, e.g. `'xgboost==2.0.3, scikit-learn==1.3.2'`
   - Mappings: YAML, e.g. `'{HTTPS_PROXY: "http://proxy:8080"}'`
   - Optional question the human does not need: `cre interview answer <id> --skip`
5. If the result has `"accepted": false`, tell the human the `problem` in plain
   words and ask again. Do not rephrase the value yourself to make it pass.
6. If the result has `warnings`, read them to the human, then continue.
7. Go to 1.

## Rules

- One question per turn. Never batch.
- Never answer for the human, except to offer a probe suggestion they confirm.
- Never put a password, token, or key in an answer. If the human offers one,
  refuse and say credentials belong in the Snowflake CLI connection, not here.
- If the human does not know an answer, do not guess. Run `cre probe` (all
  probes) and show what was found. If still unknown, stop and say which person
  usually knows (Snowflake admin for roles, privileges, repositories and pools;
  network or security team for proxy, index and CA bundle).
- If a question is confusing, wrong for this environment, or missing, do NOT
  edit `questions.yaml`. Record it for the watcher and carry on:
  `cre issue add --layer interview --stage interview --summary "<what was unclear>" --fix "<what would have worked>"`

## Finish

1. Run `cre config validate`. Read any warnings to the human.
2. Run `cre run --dry-run` and show the human the commands it would run.
3. Hand off to the watcher (`$cre-watch`) for the pre-build review. Do not
   start the real build yourself.
