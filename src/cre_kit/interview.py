"""Data-driven interview. The model does not decide what to ask; it relays
`next()` and records answers with `answer()`. That keeps a small model on
rails and makes a bad question a one-line fix in questions.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

from . import config as C
from . import probe as P
from . import state as S
from .shell import Shell

QUESTIONS_FILE = Path(__file__).with_name("questions.yaml")


def questions() -> list[dict]:
    qs = yaml.safe_load(QUESTIONS_FILE.read_text())
    seen = set()
    for q in qs:
        for field in ("id", "key", "ask", "why"):
            if not q.get(field):
                raise ValueError(f"question {q.get('id', '?')} is missing {field!r}")
        if q["key"] not in C.FIELDS:
            raise ValueError(f"question {q['id']} targets unknown config key {q['key']}")
        if q.get("probe") and q["probe"] not in P.PROBES:
            raise ValueError(f"question {q['id']} uses unknown probe {q['probe']}")
        if q["id"] in seen:
            raise ValueError(f"duplicate question id {q['id']}")
        seen.add(q["id"])
    return qs


def _applies(q: dict, cfg: dict) -> bool:
    return not q.get("when") or bool(C.get(cfg, q["when"]))


def pending(cfg: dict, st: dict) -> list[dict]:
    answered = set(st["interview"]["answered"])
    out = []
    for q in questions():
        if not _applies(q, cfg):
            continue
        invalid = any(i.severity == "error" for i in C.check_key(cfg, q["key"]))
        if q["id"] not in answered or invalid:
            out.append(q)
    return out


def next_question(cfg: dict, st: dict, sh: Shell, use_probe: bool = True) -> dict:
    todo = pending(cfg, st)
    total = len([q for q in questions() if _applies(q, cfg)])
    if not todo:
        errs = [i.as_dict() for i in C.errors(cfg)]
        return {"done": not errs, "remaining": 0, "total": total, "config_errors": errs,
                "next": "cre review" if not errs else "fix config_errors with `cre config set`"}
    q = todo[0]
    out = {"done": False, "remaining": len(todo), "total": total, "id": q["id"], "key": q["key"],
           "ask": q["ask"], "why": q["why"], "optional": bool(q.get("optional")),
           "current": C.get(cfg, q["key"]), "choices": list(C.FIELDS[q["key"]].choices) or None}
    if use_probe and q.get("probe"):
        out["probe"] = P.run(q["probe"], cfg, sh)
    return out


def answer(cfg: dict, st: dict, qid: str, raw: str | None, skip: bool = False) -> dict:
    q = next((x for x in questions() if x["id"] == qid), None)
    if not q:
        return {"accepted": False, "problem": f"no question with id {qid!r}"}
    if skip:
        if not q.get("optional"):
            return {"accepted": False, "problem": f"{qid} is required and cannot be skipped"}
    else:
        try:
            value = C.coerce(q["key"], raw or "")
        except Exception as exc:
            return {"accepted": False, "problem": f"could not read the value: {exc}"}
        trial = __import__("copy").deepcopy(cfg)
        C.set_raw(trial, q["key"], value)
        problems = [i.problem for i in C.validate(trial) if i.key == q["key"] and i.severity == "error"]
        if problems:
            return {"accepted": False, "key": q["key"], "problem": "; ".join(problems),
                    "next": "re-ask the question, quoting the problem"}
        C.set_raw(cfg, q["key"], value)
        C.save(cfg)
    if qid not in st["interview"]["answered"]:
        st["interview"]["answered"].append(qid)
    S.save(st)
    warn = [i.problem for i in C.validate(cfg) if i.key == q["key"] and i.severity == "warning"]
    return {"accepted": True, "key": q["key"], "value": C.get(cfg, q["key"]), "warnings": warn,
            "next": "cre interview next"}


def reopen(st: dict, ids: list[str] | None = None, keys: list[str] | None = None) -> dict:
    """Mark questions unanswered so they are asked again (after a failure showed
    the earlier answer was wrong or the question was never really answered)."""
    targets = set(ids or [])
    for q in questions():
        if keys and q["key"] in keys:
            targets.add(q["id"])
    st["interview"]["answered"] = [a for a in st["interview"]["answered"] if a not in targets]
    S.save(st)
    return {"reopened": sorted(targets), "next": "cre interview next"}
