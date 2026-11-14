# cre-kit

Interview a machine, fill a config, and turn the Snowflake
[custom runtime images](https://docs.snowflake.com/en/developer-guide/snowflake-ml/custom-runtime-images)
workflow into a real, registered **Custom Runtime Environment (CRE)**, with a
self-correcting loop between a cheap runner model and a pinned watcher model.

```
 interview  ->  cre.yaml  ->  pipeline  ->  cre@<name> usable in ML Jobs / Notebooks
 (questions.yaml)             (stages.py)
        ^            ^            |
        |            |            v  on failure: a HANDOFF (JSON)
        +--- runner fixes: retry / config / re-ask
        +--- watcher fixes: code, questions, failure rules (+ a test, every time)
        +--- human: privileges, network, logins, replacing a live CRE
```

## What is in the box

| Path | What it is |
|------|------------|
| `skills/cre-interview` | Runner skill. Relays questions from `cre interview next`, records answers. |
| `skills/cre-build` | Runner skill. Runs `cre run`, applies the mechanical fix each handoff names. |
| `skills/cre-watch` | Watcher skill. Pre-build review, escalations, final review; repairs the kit itself. |
| `agents/` | Subagent definitions pinning the runner and watcher models. |
| `src/cre_kit/questions.yaml` | The interview, as data. A bad question is a one-line fix here. |
| `src/cre_kit/config.py` | Config schema and validation for `cre.yaml`. |
| `src/cre_kit/probe.py` | Read-only probes (engine, connections, registry URL, base images, repos, pools, proxy, CA, index). |
| `src/cre_kit/stages.py` | The pipeline, one function per step of the Snowflake workflow. |
| `src/cre_kit/classify.py` | Failure rules: error text -> layer -> who acts. |
| `src/cre_kit/runner.py` | Stage runner, caching, attempt counters, handoff, watcher digest. |
| `cre.example.yaml` | A filled-in config, for reference. |

## Install

```bash
uv venv && uv pip install -e ".[dev]"        # add ,smoke for the ML Job smoke test
pytest -q                                    # 31 tests, no Snowflake or Docker needed
```

Install editable (`-e`) on purpose: the watcher edits `questions.yaml`,
`classify.py` and the stages in place, and those edits must take effect.

Copy the skills where your agent looks for them. For Cortex Code that is
`.cortex/skills/` in the project or `~/.snowflake/cortex/skills/` globally
(`.claude/skills/` also works):

```bash
mkdir -p .cortex/skills && cp -r /path/to/cre-kit/skills/* .cortex/skills/
```

## Use

```bash
cre init
cre interview next            # the $cre-interview skill drives this
cre run --dry-run             # every command it would run; reads only
cre run                       # the $cre-build skill drives this
```

Stages, mapped to the docs: `preflight` (prerequisites), `registry_login`
(step 1), `base_image` (step 2), `dockerfile` (step 3), `build` (step 4),
`validate` (step 5), `push` (step 6), `register` (step 7), `verify`
(DESCRIBE, must be RESOLVED), `smoke` (a real ML Job on `cre@<name>` that
reports the installed versions of your pinned packages).

Results are cached per stage against a hash of the config. Change any build
input and everything re-runs; change only `loop.*` and nothing does.

## The self-correction loop

Every failure becomes a handoff:

```json
{
  "status": "failed", "stage": "build",
  "failure_class": "tls_untrusted", "fix_layer": "interview", "who": "runner",
  "hint": "TLS inspection: the build does not trust the corporate CA. Ask for the CA bundle.",
  "keys": ["build.ca_bundle"], "attempt": 1, "max_runner_attempts": 3,
  "evidence": "...last lines of the real error...",
  "next": ["cre interview reopen --key build.ca_bundle", "cre interview next ...", "cre run"]
}
```

| `fix_layer` | Meaning | Who |
|-------------|---------|-----|
| `retry` | Expired registry session, timeout | runner |
| `config` | A value is wrong and the hint says what to set | runner |
| `interview` | A question was missed or answered wrongly: reopen and re-ask | runner |
| `python` | cre-kit's command, SQL, parser or renderer is wrong | watcher |
| `unknown` | No rule matched | watcher |
| `environment` | Missing tool, privilege, network path, login, or approval | human |

Escalation is by counter, not by the runner's opinion: after
`loop.max_runner_attempts` failures on a stage the handoff goes to the
watcher; after `loop.max_watcher_rounds` watcher rounds it goes to a human.
The watcher must log each fix with `cre issue add`, which is also what resets
the runner's counter, and must leave a test or a rule behind, so the same
failure is handled by the runner next time.

The watcher reads `cre review`, a compact digest, rather than raw logs. With
an expensive watcher that is most of the cost control.

## Guardrails that are not negotiable

- Replacing an existing CRE needs `cre.replace_existing: true` **and** a human
  running `cre run --from register --yes`.
- `ENTRYPOINT`, `CMD`, `FROM` and `DASHBOARD_PORT` in `dockerfile_extra` are
  rejected: the image must keep `/usr/local/bin/entrypoint.sh` and port 12003.
- Credentials in an index URL are rejected; build-arg values and registry
  tokens are masked in the journal.
- `grants_UNVERIFIED.sql` is rendered for review and never executed.

## Unverified against a live account

This was built from the docs page and tested against a fake shell, not a real
Snowflake account. Expect the first real run to exercise the loop. Most likely
spots, all routed to the watcher with a clear hint:

1. **Output shapes** of `snow sql --format json` for `SHOW CUSTOM RUNTIME
   ENVIRONMENTS` / `DESCRIBE` (column names assumed from the docs' column list).
2. **`snow custom-image validate`** flags and exit codes beyond what the page shows.
3. **Podman**: login goes through `snow spcs image-registry token`; whether
   `snow custom-image validate` works without Docker is unknown.
4. **The smoke job** (`render.smoke_job_py`): `session=`, `job.wait()`,
   `job.result()` and the `DONE` status are from memory of the ML Jobs API.
5. **GRANT syntax** for CREs: not on the docs page, hence never executed.
6. **Subagent `model:` key** in `agents/*.md`: check your Cortex Code version's
   subagent format and model identifiers.
7. **`SSL_CERT_FILE` for `uv pip`** during the build, when a CA bundle is set.

## Facts from the docs worth remembering

- Every custom image must derive from an official Snowflake ML runtime base
  image, and be built for `linux/amd64`.
- Local validation is advisory. `CREATE CUSTOM RUNTIME ENVIRONMENT` runs
  server-side validation and creates nothing if it fails.
- The image digest is checked at run time. Push a changed image under a
  registered tag and submissions are rejected until the CRE is recreated.
- `DROP` removes only the metadata object; the image stays in the repository.
- A `DEPRECATED` CRE cannot be used for new jobs or notebooks.
- Notebook support for CREs is in Preview.
