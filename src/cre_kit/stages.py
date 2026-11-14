"""The pipeline: one function per step of the Snowflake custom runtime image
workflow. Each returns an Outcome; failures carry a routed Failure."""
from __future__ import annotations

import importlib.metadata as md
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config as C
from . import render as R
from . import state as S
from .classify import Failure, classify
from .probe import _snow, sql
from .shell import Result, Shell, tail

MIN_ML_PYTHON = (1, 37, 0)


@dataclass
class Outcome:
    ok: bool
    detail: str = ""
    failure: Failure | None = None
    evidence: str = ""


@dataclass
class Ctx:
    yes: bool = False  # human approved destructive steps
    root: Path = Path(".")


def _fail(stage: str, res: Result, what: str) -> Outcome:
    return Outcome(False, what, classify(stage, res.text), tail(res.text))


def _version(text: str) -> tuple:
    parts = []
    for p in text.strip().split(".")[:3]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def _unparsed(stage: str, res: Result, what: str) -> Outcome:
    return Outcome(False, what, Failure("unparsed_output", "python",
                   "cre-kit could not parse this command's output. Fix the parser in probe.rows_from_json or the stage, and add a test."),
                   tail(res.text))


# --------------------------------------------------------------------------
def preflight(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    eng = C.get(cfg, "build.engine")
    for cmd, what in (([eng, "--version"], f"{eng} installed"), ([eng, "info"], f"{eng} daemon reachable"),
                      (["snow", "--version"], "Snowflake CLI installed"),
                      (["snow", "custom-image", "--help"], "Snowflake CLI has custom-image")):
        res = sh.run(cmd, read_only=True, timeout=120)
        if not res.ok:
            f = classify("preflight", res.text)
            if f.layer == "unknown":
                f = Failure("preflight_check_failed", "environment", f"Check failed: {what}.")
            return Outcome(False, f"failed check: {what}", f, tail(res.text))
    res = sh.run(_snow(cfg, "connection", "test"), read_only=True, timeout=300)
    if not res.ok:
        f = classify("preflight", res.text)
        if f.layer == "unknown":
            f = Failure("connection_failed", "interview", "The connection does not work. Re-ask which connection to use.",
                        ["snowflake.connection"])
        return Outcome(False, "snow connection test failed", f, tail(res.text))
    if C.get(cfg, "smoke_test.enabled"):
        try:
            v = md.version("snowflake-ml-python")
        except md.PackageNotFoundError:
            v = None
        if not v or _version(v) < MIN_ML_PYTHON:
            return Outcome(False, f"snowflake-ml-python {v or 'not installed'}; need >= 1.37.0",
                           Failure("ml_python_too_old", "environment",
                                   "Install snowflake-ml-python>=1.37.0 in this environment (uv pip install 'cre-kit[smoke]'), or set smoke_test.enabled false.",
                                   ["smoke_test.enabled"]), "")
    return Outcome(True, "tools, daemon and connection OK")


def registry_login(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    eng = C.get(cfg, "build.engine")
    reg = C.get(cfg, "snowflake.registry_url")
    if eng == "docker":
        res = sh.run(_snow(cfg, "spcs", "image-registry", "login"), timeout=600)
        if not res.ok:
            return _fail("registry_login", res, "registry login failed")
    else:
        tok = sh.run(_snow(cfg, "spcs", "image-registry", "token", "--format", "json"), timeout=600)
        if not tok.ok:
            return _fail("registry_login", tok, "could not get a registry token")
        if not tok.dry:
            try:
                token = json.loads(tok.out)["token"]
            except Exception:
                return _unparsed("registry_login", Result(tok.cmd, 0, "<token output withheld>", ""), "registry token output not understood")
            tok.out = "<withheld>"
            res = sh.run([eng, "login", reg, "-u", "0sessiontoken", "--password-stdin"], input=token, mask=(token,), timeout=120)
            if not res.ok:
                return _fail("registry_login", res, "podman login failed")
    url = sh.run(_snow(cfg, "spcs", "image-registry", "url"), read_only=True, timeout=120)
    if url.ok and url.out.strip():
        actual = url.out.strip().splitlines()[-1].strip()
        if actual != reg:
            return Outcome(False, f"registry_url is {reg!r} but the account reports {actual!r}",
                           Failure("registry_url_mismatch", "config", f"Set snowflake.registry_url to {actual}.",
                                   ["snowflake.registry_url"]), actual)
    return Outcome(True, f"logged in to {reg}")


def base_image(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    res, rows = sql(cfg, sh, R.LIST_BASE_IMAGES_SQL)
    note = ""
    if not res.ok:
        return _fail("base_image", res, "could not list base images")
    if not res.dry:
        if rows is None:
            return _unparsed("base_image", res, "base image list not understood")
        path = C.get(cfg, "base_image.image_path")
        hit = next((r for r in rows if r.get("image_path") == path), None)
        if hit:
            for key, col in (("base_image.type", "base_image_type"), ("base_image.python_version", "python_version")):
                if str(hit.get(col, "")).upper() != str(C.get(cfg, key)).upper():
                    return Outcome(False, f"{key} is {C.get(cfg, key)!r} but that image is {hit.get(col)!r}",
                                   Failure("base_image_inconsistent", "config", f"Set {key} to {hit.get(col)}.", [key]),
                                   json.dumps(hit))
        else:
            note = " (not in the latest list: an older runtime version, which is allowed)"
    pull = sh.run([C.get(cfg, "build.engine"), "pull", "--platform", C.get(cfg, "build.platform"), R.base_ref(cfg)])
    if not pull.ok:
        return _fail("base_image", pull, "base image pull failed")
    return Outcome(True, f"pulled {R.base_ref(cfg)}{note}")


def dockerfile(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    ctx_dir = ctx.root / C.get(cfg, "build.context_dir")
    ctx_dir.mkdir(parents=True, exist_ok=True)
    ca = C.get(cfg, "build.ca_bundle")
    if ca:
        src = Path(ca).expanduser()
        src = src if src.exists() else ctx.root / ca
        shutil.copyfile(src, ctx_dir / R.CA_IN_CONTEXT)
    (ctx_dir / "Dockerfile").write_text(R.dockerfile(cfg))
    out = S.out_dir()
    (out / "create_cre.sql").write_text(R.create_cre_sql(cfg) + ";\n")
    (out / "grants_UNVERIFIED.sql").write_text(R.grants_sql(cfg))
    (out / "USAGE.md").write_text(R.usage_md(cfg))
    if C.get(cfg, "smoke_test.enabled"):
        (out / "smoke_job.py").write_text(R.smoke_job_py(cfg))
    return Outcome(True, f"rendered {ctx_dir / 'Dockerfile'} and {out}/")


def build(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    ctx_dir = ctx.root / C.get(cfg, "build.context_dir")
    cmd = [C.get(cfg, "build.engine"), "build", "--platform", C.get(cfg, "build.platform"), "-t", R.local_ref(cfg)]
    args = C.get(cfg, "build.build_args") or {}
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    cmd.append(str(ctx_dir))
    res = sh.run(cmd, mask=tuple(str(v) for v in args.values()), timeout=7200)
    if not res.ok:
        return _fail("build", res, "image build failed")
    return Outcome(True, f"built {R.local_ref(cfg)}")


def validate(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    cmd = ["snow", "custom-image", "validate", R.local_ref(cfg)]
    if C.get(cfg, "validation.scan_vulnerabilities"):
        cmd.append("--scan-vulnerabilities")
    res = sh.run(cmd, timeout=3600)
    (S.out_dir() / "validate_report.txt").write_text(res.text + "\n")
    if not res.ok:
        return _fail("validate", res, "local validation failed")
    return Outcome(True, "local validation passed (advisory; report in .cre/out/validate_report.txt)")


def push(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    res, rows = sql(cfg, sh, R.show_repo_sql(cfg))
    if not res.ok:
        return _fail("push", res, "could not look up the image repository")
    if not res.dry:
        if rows is None:
            return _unparsed("push", res, "image repository list not understood")
        if not rows:
            if not C.get(cfg, "image_repo.create_if_missing"):
                return Outcome(False, f"image repository {R.repo_fqn(cfg)} not found",
                               Failure("repo_missing", "interview",
                                       "The repository does not exist for this role. Re-ask its name, or whether it may be created.",
                                       ["image_repo.database", "image_repo.schema", "image_repo.name", "image_repo.create_if_missing"]),
                               res.text)
            made, _ = sql(cfg, sh, R.create_repo_sql(cfg))
            if not made.ok:
                return _fail("push", made, "could not create the image repository")
    eng = C.get(cfg, "build.engine")
    for cmd in ([eng, "tag", R.local_ref(cfg), R.remote_ref(cfg)], [eng, "push", R.remote_ref(cfg)]):
        r = sh.run(cmd, timeout=7200)
        if not r.ok:
            return _fail("push", r, f"{cmd[1]} failed")
    return Outcome(True, f"pushed {R.remote_ref(cfg)}")


def _existing_cre(cfg, sh: Shell):
    res, rows = sql(cfg, sh, R.SHOW_CRES_SQL)
    if not res.ok or res.dry:
        return res, None, None
    if rows is None:
        return res, None, "unparsed"
    name = str(C.get(cfg, "cre.name")).upper()
    return res, next((r for r in rows if str(r.get("name", "")).upper() == name), None), "ok"


def register(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    name = C.get(cfg, "cre.name")
    res, existing, parsed = _existing_cre(cfg, sh)
    if not res.ok:
        return _fail("register", res, "could not list custom runtime environments")
    if parsed == "unparsed":
        return _unparsed("register", res, "CRE list not understood")
    if existing:
        same = str(existing.get("tag", "")) == str(C.get(cfg, "image.tag")) and \
            str(existing.get("image_name", "")).lower().endswith(str(C.get(cfg, "image.name")).lower())
        if same and str(existing.get("cre_status", "")).upper() == "RESOLVED" and not C.get(cfg, "cre.replace_existing"):
            return Outcome(True, f"CRE {name} already registered for {R.local_ref(cfg)}")
        if not C.get(cfg, "cre.replace_existing"):
            return Outcome(False, f"CRE {name} already exists and points at {existing.get('image_name')}:{existing.get('tag')}",
                           Failure("cre_exists", "interview",
                                   "A CRE with this name exists. Ask for a new name, or for permission to replace it.",
                                   ["cre.name", "cre.replace_existing"]), json.dumps(existing, default=str))
        if not ctx.yes:
            return Outcome(False, f"replacing CRE {name} needs a human to re-run with --yes",
                           Failure("needs_approval", "environment",
                                   f"Replacing {name} repoints every job and notebook using it. A person must run `cre run --from register --yes`."),
                           json.dumps(existing, default=str))
    made, _ = sql(cfg, sh, R.create_cre_sql(cfg))
    if not made.ok:
        return _fail("register", made, "CREATE CUSTOM RUNTIME ENVIRONMENT failed (server-side validation or privileges)")
    return Outcome(True, f"registered CRE {name} -> {R.logical_image_path(cfg)}")


def verify(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    res, rows = sql(cfg, sh, R.describe_cre_sql(cfg))
    if not res.ok:
        return _fail("verify", res, "DESCRIBE failed")
    if res.dry:
        return Outcome(True, "dry run")
    if not rows:
        return _unparsed("verify", res, "DESCRIBE output not understood")
    row = rows[0]
    if "cre_status" not in row and {"property", "value"} <= set(row):  # property/value layout
        row = {str(r["property"]).lower(): r["value"] for r in rows}
    status = str(row.get("cre_status", "")).upper()
    if status != "RESOLVED":
        return Outcome(False, f"CRE status is {status or 'unknown'}, expected RESOLVED",
                       Failure("cre_not_resolved", "unknown", "The CRE exists but is not usable. Inspect the DESCRIBE output."),
                       json.dumps(row, default=str))
    (S.out_dir() / "cre_describe.json").write_text(json.dumps(row, indent=2, default=str))
    return Outcome(True, f"CRE {C.get(cfg, 'cre.name')} is RESOLVED, digest {row.get('digest')}")


def smoke(cfg, sh: Shell, ctx: Ctx) -> Outcome:
    if not C.get(cfg, "smoke_test.enabled"):
        return Outcome(True, "skipped (smoke_test.enabled is false)")
    script = S.out_dir() / "smoke_job.py"
    script.write_text(R.smoke_job_py(cfg))
    res = sh.run([sys.executable, str(script)], timeout=3600)
    if not res.ok:
        f = classify("smoke", res.text)
        if f.layer == "unknown" and "Traceback" in res.text:
            f = Failure("smoke_script_error", "python",
                        "The generated smoke job failed before or during submission. Check render.smoke_job_py against the installed snowflake-ml-python API.")
        return Outcome(False, "smoke test job failed", f, tail(res.text))
    if res.dry:
        return Outcome(True, "dry run")
    line = next((ln for ln in res.out.splitlines() if ln.startswith("CRE_SMOKE_RESULT ")), None)
    if not line:
        return _unparsed("smoke", res, "smoke job produced no result line")
    found = json.loads(line.split(" ", 1)[1])
    wrong = {p: (want, found.get(p)) for p, want in R.expected_pins(cfg).items() if found.get(p) != want}
    if wrong:
        return Outcome(False, f"job ran but package versions differ: {wrong}",
                       Failure("wrong_image_in_use", "unknown",
                               "The job did not see the pinned versions, so it may not be running on this image."),
                       json.dumps(found))
    return Outcome(True, f"ML Job ran on cre@{C.get(cfg, 'cre.name')} with the pinned packages")


STAGES = [("preflight", preflight), ("registry_login", registry_login), ("base_image", base_image),
          ("dockerfile", dockerfile), ("build", build), ("validate", validate), ("push", push),
          ("register", register), ("verify", verify), ("smoke", smoke)]
NAMES = [n for n, _ in STAGES]
