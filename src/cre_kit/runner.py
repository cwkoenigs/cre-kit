"""Runs stages in order and, on the first failure, emits a HANDOFF: a small
JSON object saying what failed, which layer owns the fix, who acts, and the
exact commands allowed. The handoff is the whole contract between the Python,
the runner model and the watcher model."""
from __future__ import annotations

from pathlib import Path

from . import config as C
from . import interview as I
from . import state as S
from .classify import Failure
from .shell import Shell
from .stages import NAMES, STAGES, Ctx, Outcome


def _next_steps(layer: str, stage: str, keys: list[str]) -> list[str]:
    k = " ".join(keys)
    return {
        "retry": ["cre run --from registry_login" if "registry" in stage or stage in ("push", "base_image") else f"cre run --from {stage}"],
        "config": [f"cre config set <key> <value>   # one of: {k}", "cre run"],
        "interview": [f"cre interview reopen --key {k}".rstrip(), "cre interview next   # ask the human, then `cre interview answer`", "cre run"],
        "python": ["cre review", "fix the code under src/cre_kit, add a test, run `pytest -q`",
                   "cre issue add --layer python --stage " + stage + " --summary ... --fix ...", "cre run"],
        "unknown": ["cre review", "diagnose; fix the owning layer",
                    "add a rule to classify.py + test so the runner handles it next time",
                    "cre issue add --layer <layer> --stage " + stage + " --summary ... --fix ...", "cre run"],
        "environment": ["STOP. Tell the human exactly what is needed (see hint). Do not work around it.",
                        "after they fix it: cre run"],
    }[layer]


def handoff(stage: str, out: Outcome, st: dict, cfg: dict) -> dict:
    f: Failure = out.failure
    rec = S.stage(st, stage)
    max_attempts = C.get(cfg, "loop.max_runner_attempts") or 3
    max_rounds = C.get(cfg, "loop.max_watcher_rounds") or 2
    who, escalated = f.who, None
    if who == "runner" and rec["attempts"] >= max_attempts:
        who, escalated = "watcher", f"runner used {rec['attempts']} attempts on this stage"
    if who == "watcher" and rec["watcher_rounds"] >= max_rounds:
        who, escalated = "human", f"watcher used {rec['watcher_rounds']} rounds on this stage"
    layer = f.layer if who == f.who else ("unknown" if who == "watcher" else "environment")
    return {
        "status": "failed", "stage": stage, "detail": out.detail,
        "failure_class": f.cls, "fix_layer": f.layer, "who": who, "escalated": escalated,
        "hint": f.hint, "keys": f.keys, "attempt": rec["attempts"], "max_runner_attempts": max_attempts,
        "watcher_rounds": rec["watcher_rounds"], "max_watcher_rounds": max_rounds,
        "evidence": out.evidence[-2500:], "next": _next_steps(layer, stage, f.keys),
    }


def run(cfg: dict, *, start: str | None = None, only: str | None = None, dry_run: bool = False,
        yes: bool = False, force: bool = False, shell: Shell | None = None, root: Path | None = None) -> dict:
    root = root or Path.cwd()
    st = S.load()
    errs = C.errors(cfg, root)
    if errs:
        missing = [e.key for e in errs if e.problem.startswith("required")]
        result = {"status": "failed", "stage": "config", "failure_class": "config_invalid",
                  "fix_layer": "interview" if missing else "config", "who": "runner",
                  "detail": "config is not valid; nothing was run",
                  "errors": [e.as_dict() for e in errs],
                  "next": (["cre interview next   # unanswered required questions remain"] if missing
                           else ["cre config set <key> <value>", "cre run"])}
        st["last_handoff"] = result
        S.save(st)
        return result
    if I.pending(cfg, st) and not dry_run:
        result = {"status": "failed", "stage": "interview", "failure_class": "interview_incomplete",
                  "fix_layer": "interview", "who": "runner", "detail": "the interview has unanswered questions",
                  "next": ["cre interview next"]}
        st["last_handoff"] = result
        S.save(st)
        return result

    sh = shell or Shell(dry_run=dry_run, journal=S.journal_path())
    sh.dry_run = dry_run
    fp = C.fingerprint(cfg)
    names = [only] if only else NAMES[NAMES.index(start):] if start else NAMES
    done: list[dict] = []
    for name, fn in STAGES:
        if name not in names:
            continue
        rec = S.stage(st, name)
        if not (force or start or only or dry_run) and rec["status"] == "ok" and rec["fingerprint"] == fp:
            done.append({"stage": name, "status": "cached", "detail": rec["detail"]})
            continue
        sh.stage = name
        try:
            out = fn(cfg, sh, Ctx(yes=yes, root=root))
        except Exception as exc:  # cre-kit bug: route to the watcher with the traceback
            import traceback
            out = Outcome(False, f"cre-kit raised {type(exc).__name__}",
                          Failure("cre_kit_crash", "python", "cre-kit itself raised. Fix the code and add a regression test."),
                          traceback.format_exc())
        if dry_run:
            done.append({"stage": name, "status": "dry-run" if out.ok else "failed", "detail": out.detail,
                         "commands": [" ".join(r.cmd) for r in sh.history if r.dry]})
            sh.history.clear()
            if not out.ok:
                return {"status": "failed", "dry_run": True, "stage": name, "detail": out.detail,
                        "hint": out.failure.hint if out.failure else "", "completed": done}
            continue
        if out.ok:
            rec.update(status="ok", attempts=0, watcher_rounds=0, fingerprint=fp, detail=out.detail)
            done.append({"stage": name, "status": "ok", "detail": out.detail})
            S.save(st)
            continue
        rec.update(status="failed", fingerprint=fp, detail=out.detail)
        rec["attempts"] += 1
        result = handoff(name, out, st, cfg)
        result["completed"] = done
        st["last_handoff"] = result
        S.save(st)
        return result
    result = {"status": "dry-run" if dry_run else "ok", "completed": done,
              "next": ["review the commands above, then `cre run`"] if dry_run else
              ["cre review   # watcher: final check, fold lessons back into questions.yaml / classify.py",
               "see .cre/out/USAGE.md"]}
    if not dry_run:
        st["last_handoff"] = None
        S.save(st)
    return result


def review(cfg: dict) -> dict:
    """Compact digest for the watcher. Small on purpose: the expensive model
    should read this, not the raw journal."""
    st = S.load()
    issues = C.validate(cfg)
    lessons = S.lessons()
    return {
        "config_fingerprint": C.fingerprint(cfg),
        "config_errors": [i.as_dict() for i in issues if i.severity == "error"],
        "config_warnings": [i.as_dict() for i in issues if i.severity == "warning"],
        "interview_pending": [q["id"] for q in I.pending(cfg, st)],
        "stages": {n: {k: S.stage(st, n)[k] for k in ("status", "attempts", "watcher_rounds", "detail")} for n in NAMES},
        "last_handoff": st.get("last_handoff"),
        "recent_commands": [{k: e[k] for k in ("stage", "cmd", "rc")} for e in S.journal_tail(6)],
        "lessons": {"count": len(lessons), "by_layer": _count(lessons), "recent": lessons[-5:]},
    }


def _count(lessons: list[dict]) -> dict:
    out: dict = {}
    for entry in lessons:
        out[entry.get("layer", "?")] = out.get(entry.get("layer", "?"), 0) + 1
    return out
