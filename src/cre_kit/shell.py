"""Command execution with a journal. Everything the pipeline runs goes through
here so the watcher can read exactly what happened, with secrets masked."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

TAIL = 4000


@dataclass
class Result:
    cmd: list[str]
    rc: int
    out: str = ""
    err: str = ""
    seconds: float = 0.0
    dry: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return (self.out + "\n" + self.err).strip()


def tail(text: str, n: int = TAIL) -> str:
    return text if len(text) <= n else "...[truncated]...\n" + text[-n:]


@dataclass
class Shell:
    dry_run: bool = False
    journal: Path | None = None
    stage: str = ""
    history: list[Result] = field(default_factory=list)

    def run(self, cmd: list[str], *, input: str | None = None, timeout: int = 3600,
            mask: tuple[str, ...] = (), read_only: bool = False) -> Result:
        """Run a command. `mask` values are replaced in the journal.
        `read_only` commands still run during a dry run (they change nothing)."""
        shown = [_mask(c, mask) for c in cmd]
        if self.dry_run and not read_only:
            res = Result(shown, 0, dry=True)
            self._log(res)
            return res
        start = time.time()
        try:
            p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
            res = Result(shown, p.returncode, p.stdout, p.stderr, time.time() - start)
        except FileNotFoundError:
            res = Result(shown, 127, "", f"command not found: {cmd[0]}", time.time() - start)
        except subprocess.TimeoutExpired:
            res = Result(shown, 124, "", f"timed out after {timeout}s", time.time() - start)
        res.out, res.err = _mask(res.out, mask), _mask(res.err, mask)
        self._log(res)
        return res

    def _log(self, res: Result) -> None:
        self.history.append(res)
        if not self.journal:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": self.stage, "cmd": res.cmd,
            "rc": res.rc, "dry": res.dry, "seconds": round(res.seconds, 2),
            "out": tail(res.out, 1500), "err": tail(res.err, 1500),
        }
        with self.journal.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")


def _mask(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
