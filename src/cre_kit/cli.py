"""`cre` command line. Every command prints one JSON object so a model (or a
script) can act on it without parsing prose. Exit code 0 = ok, 1 = failed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as C
from . import interview as I
from . import probe as P
from . import render as R
from . import runner
from . import state as S
from .shell import Shell
from .stages import NAMES


def emit(obj, code: int = 0) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cre", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create cre.yaml with defaults")

    iv = sub.add_parser("interview", help="data-driven interview").add_subparsers(dest="sub", required=True)
    n = iv.add_parser("next")
    n.add_argument("--no-probe", action="store_true")
    a = iv.add_parser("answer")
    a.add_argument("id")
    a.add_argument("value", nargs="?")
    a.add_argument("--skip", action="store_true")
    r = iv.add_parser("reopen")
    r.add_argument("ids", nargs="*")
    r.add_argument("--key", nargs="*", default=[])
    iv.add_parser("status")

    cf = sub.add_parser("config", help="read / edit cre.yaml").add_subparsers(dest="sub", required=True)
    cf.add_parser("show")
    cf.add_parser("validate")
    g = cf.add_parser("get")
    g.add_argument("key")
    s = cf.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")

    pr = sub.add_parser("probe", help="run a read-only probe")
    pr.add_argument("name", nargs="?", choices=sorted(P.PROBES))

    sub.add_parser("render", help="print the Dockerfile and SQL without running anything")

    rn = sub.add_parser("run", help="run the pipeline; prints ok or a handoff")
    rn.add_argument("--from", dest="start", choices=NAMES)
    rn.add_argument("--only", choices=NAMES)
    rn.add_argument("--dry-run", action="store_true", help="print every command, change nothing")
    rn.add_argument("--yes", action="store_true", help="HUMAN ONLY: approve replacing an existing CRE")
    rn.add_argument("--force", action="store_true", help="ignore cached stage results")

    sub.add_parser("status", help="stage table")
    sub.add_parser("review", help="compact digest for the watcher")

    isp = sub.add_parser("issue", help="record a fix so it is not rediscovered").add_subparsers(dest="sub", required=True)
    ia = isp.add_parser("add")
    ia.add_argument("--layer", required=True, choices=["interview", "config", "python", "environment", "classify"])
    ia.add_argument("--stage", required=True)
    ia.add_argument("--summary", required=True)
    ia.add_argument("--fix", required=True)
    isp.add_parser("list")

    args = ap.parse_args(argv)
    root = Path.cwd()

    if args.cmd == "init":
        path = root / C.CONFIG_FILE
        if path.exists():
            return emit({"created": False, "path": str(path), "note": "already exists"})
        C.save(C.defaults(), root)
        return emit({"created": True, "path": str(path), "next": "cre interview next"})

    cfg = C.load(root)

    if args.cmd == "interview":
        st = S.load()
        sh = Shell(journal=S.journal_path(), stage="interview")
        if args.sub == "next":
            return emit(I.next_question(cfg, st, sh, use_probe=not args.no_probe))
        if args.sub == "answer":
            res = I.answer(cfg, st, args.id, args.value, skip=args.skip)
            return emit(res, 0 if res["accepted"] else 1)
        if args.sub == "reopen":
            return emit(I.reopen(st, args.ids, args.key))
        todo = I.pending(cfg, st)
        return emit({"pending": [q["id"] for q in todo], "answered": st["interview"]["answered"]})

    if args.cmd == "config":
        if args.sub == "show":
            return emit(cfg)
        if args.sub == "validate":
            issues = C.validate(cfg, root)
            bad = any(i.severity == "error" for i in issues)
            return emit({"valid": not bad, "issues": [i.as_dict() for i in issues]}, 1 if bad else 0)
        if args.sub == "get":
            return emit({"key": args.key, "value": C.get(cfg, args.key)})
        try:
            value = C.coerce(args.key, args.value)
        except Exception as exc:
            return emit({"accepted": False, "problem": str(exc)}, 1)
        old = C.get(cfg, args.key)
        trial = json.loads(json.dumps(cfg))
        C.set_raw(trial, args.key, value)
        problems = [i.problem for i in C.validate(trial, root) if i.key == args.key and i.severity == "error"]
        if problems:
            return emit({"accepted": False, "key": args.key, "problem": "; ".join(problems)}, 1)
        C.save(trial, root)
        return emit({"accepted": True, "key": args.key, "old": old, "new": value, "next": "cre run"})

    if args.cmd == "probe":
        sh = Shell(journal=S.journal_path(), stage="probe")
        names = [args.name] if args.name else sorted(P.PROBES)
        return emit({n: P.run(n, cfg, sh) for n in names})

    if args.cmd == "render":
        errs = C.errors(cfg, root)
        if errs:
            return emit({"rendered": False, "errors": [e.as_dict() for e in errs]}, 1)
        return emit({"dockerfile": R.dockerfile(cfg), "create_cre_sql": R.create_cre_sql(cfg),
                     "base_image": R.base_ref(cfg), "push_target": R.remote_ref(cfg)})

    if args.cmd == "run":
        res = runner.run(cfg, start=args.start, only=args.only, dry_run=args.dry_run, yes=args.yes,
                         force=args.force, root=root)
        return emit(res, 0 if res["status"] in ("ok", "dry-run") else 1)

    if args.cmd == "status":
        st = S.load()
        return emit({n: S.stage(st, n)["status"] for n in NAMES})

    if args.cmd == "review":
        return emit(runner.review(cfg))

    if args.cmd == "issue":
        if args.sub == "list":
            return emit(S.lessons())
        S.add_lesson({"layer": args.layer, "stage": args.stage, "summary": args.summary, "fix": args.fix})
        st = S.load()
        if args.stage in NAMES:
            S.stage(st, args.stage)["watcher_rounds"] += 1
            S.stage(st, args.stage)["attempts"] = 0
            S.save(st)
        return emit({"recorded": True, "next": "cre run"})

    return 1


if __name__ == "__main__":
    sys.exit(main())
