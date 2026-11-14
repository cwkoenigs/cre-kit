"""Run state under .cre/: stage status, attempt counters, lessons, journal."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DIR = ".cre"


def root() -> Path:
    return Path.cwd() / DIR


def out_dir() -> Path:
    p = root() / "out"
    p.mkdir(parents=True, exist_ok=True)
    return p


def journal_path() -> Path:
    return root() / "journal.jsonl"


def lessons_path() -> Path:
    return root() / "lessons.jsonl"


def blank() -> dict:
    return {"stages": {}, "interview": {"answered": []}, "last_handoff": None}


def load() -> dict:
    path = root() / "state.json"
    st = blank()
    if path.exists():
        st.update(json.loads(path.read_text()))
    st.setdefault("interview", {}).setdefault("answered", [])
    st.setdefault("stages", {})
    return st


def save(st: dict) -> None:
    root().mkdir(parents=True, exist_ok=True)
    path = root() / "state.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, path)


def stage(st: dict, name: str) -> dict:
    return st["stages"].setdefault(name, {"status": "pending", "attempts": 0, "watcher_rounds": 0,
                                         "fingerprint": None, "detail": ""})


def add_lesson(entry: dict) -> None:
    root().mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with lessons_path().open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def lessons() -> list[dict]:
    if not lessons_path().exists():
        return []
    return [json.loads(line) for line in lessons_path().read_text().splitlines() if line.strip()]


def journal_tail(n: int = 8) -> list[dict]:
    if not journal_path().exists():
        return []
    lines = journal_path().read_text().splitlines()[-n:]
    return [json.loads(line) for line in lines if line.strip()]
