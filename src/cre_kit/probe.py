"""Read-only probes of the machine and the Snowflake account.

A probe never raises and never changes anything. It returns
{"observed": ..., "suggestion": ..., "note": ...} so the interviewer can say
"I found X, use it?" instead of asking a cold question.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import os
import platform
import shutil

from . import config as C
from . import render as R
from .shell import Shell


def _snow(cfg, *args, sql: bool = False) -> list[str]:
    cmd = ["snow", *args]
    conn = C.get(cfg, "snowflake.connection")
    if conn:
        cmd += ["-c", conn]
    if C.get(cfg, "snowflake.role"):
        cmd += ["--role", C.get(cfg, "snowflake.role")]
    if sql and C.get(cfg, "snowflake.warehouse"):
        cmd += ["--warehouse", C.get(cfg, "snowflake.warehouse")]
    return cmd


def rows_from_json(text: str) -> list[dict]:
    """Parse `snow ... --format json` output into a list of lower-cased-key dicts."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    if data and isinstance(data[0], list):  # multi-statement output
        data = data[-1]
    return [{str(k).lower(): v for k, v in row.items()} for row in data if isinstance(row, dict)]


def sql(cfg, sh: Shell, query: str):
    res = sh.run(_snow(cfg, "sql", "-q", query, "--format", "json", sql=True), read_only=_is_read_only(query))
    if not res.ok or res.dry:
        return res, None
    try:
        return res, rows_from_json(res.out)
    except (json.JSONDecodeError, AttributeError, IndexError):
        return res, None


def _is_read_only(query: str) -> bool:
    return query.lstrip().split(" ", 1)[0].upper() in ("SELECT", "SHOW", "DESC", "DESCRIBE")


def engine(cfg, sh):
    found = [e for e in ("docker", "podman") if shutil.which(e)]
    running = [e for e in found if sh.run([e, "info"], read_only=True, timeout=60).ok]
    return {"observed": {"installed": found, "daemon_reachable": running},
            "suggestion": (running or found or [None])[0],
            "note": None if running else "No container engine is running. Start Docker/Podman before the build."}


def arch(cfg, sh):
    m = platform.machine().lower()
    note = None
    if m in ("arm64", "aarch64"):
        note = "This machine is ARM. Builds must target linux/amd64, so they run under emulation (slower; needs Rosetta/QEMU)."
    return {"observed": {"machine": m, "system": platform.system()}, "suggestion": "linux/amd64", "note": note}


def snow_connections(cfg, sh):
    res = sh.run(["snow", "connection", "list", "--format", "json"], read_only=True, timeout=60)
    if not res.ok:
        return {"observed": None, "suggestion": None, "note": f"snow CLI not usable: {res.err.strip()[:200]}"}
    try:
        rows = rows_from_json(res.out)
    except Exception:
        return {"observed": res.out[:500], "suggestion": None, "note": "could not parse connection list"}
    names = [r.get("connection_name") for r in rows]
    default = next((r.get("connection_name") for r in rows if r.get("is_default")), None)
    auth = {r.get("connection_name"): (r.get("parameters") or {}).get("authenticator") for r in rows}
    return {"observed": {"connections": names, "default": default, "authenticators": auth},
            "suggestion": default or (names[0] if len(names) == 1 else None), "note": None}


def auth(cfg, sh):
    info = snow_connections(cfg, sh)
    conn = C.get(cfg, "snowflake.connection")
    a = ((info.get("observed") or {}).get("authenticators") or {}).get(conn)
    a = (a or "").lower()
    guess = "sso" if "browser" in a or "okta" in a else "keypair" if "jwt" in a else "pat" if "token" in a else None
    return {"observed": {"authenticator": a or None}, "suggestion": guess,
            "note": "SSO connections open a browser on login; unattended runs need a service account with key-pair auth."}


def context(cfg, sh):
    res, rows = sql(cfg, sh, "SELECT CURRENT_ROLE() AS role, CURRENT_WAREHOUSE() AS warehouse, CURRENT_ACCOUNT() AS account")
    if not rows:
        return {"observed": None, "suggestion": None, "note": f"could not query Snowflake: {res.text[:300]}"}
    return {"observed": rows[0], "suggestion": rows[0], "note": None}


def role(cfg, sh):
    info = context(cfg, sh)
    return {**info, "suggestion": (info.get("observed") or {}).get("role")}


def warehouse(cfg, sh):
    info = context(cfg, sh)
    return {**info, "suggestion": (info.get("observed") or {}).get("warehouse")}


def registry_url(cfg, sh):
    res = sh.run(_snow(cfg, "spcs", "image-registry", "url"), read_only=True, timeout=120)
    url = res.out.strip().splitlines()[-1].strip() if res.ok and res.out.strip() else None
    return {"observed": url, "suggestion": url, "note": None if url else f"could not get registry url: {res.text[:300]}"}


def base_images(cfg, sh):
    res, rows = sql(cfg, sh, R.LIST_BASE_IMAGES_SQL)
    if not rows:
        return {"observed": None, "suggestion": None, "note": f"could not list base images: {res.text[:300]}"}
    want = C.get(cfg, "base_image.type") or "CPU"
    pyv = C.get(cfg, "base_image.python_version")
    match = [r for r in rows if str(r.get("base_image_type", "")).upper() == want
             and (not pyv or str(r.get("python_version")) == pyv)]
    return {"observed": rows, "suggestion": match[0] if len(match) == 1 else None,
            "note": "One row per hardware type and Python version. Pick the one your code needs."}


def base_python(cfg, sh):
    info = base_images(cfg, sh)
    want = C.get(cfg, "base_image.type") or "CPU"
    versions = sorted({str(r.get("python_version")) for r in (info.get("observed") or [])
                       if str(r.get("base_image_type", "")).upper() == want})
    return {"observed": versions, "suggestion": versions[0] if len(versions) == 1 else None, "note": info.get("note")}


def base_path(cfg, sh):
    info = base_images(cfg, sh)
    s = info.get("suggestion")
    return {"observed": info.get("observed"), "suggestion": s.get("image_path") if isinstance(s, dict) else None,
            "note": info.get("note")}


def image_repos(cfg, sh):
    res, rows = sql(cfg, sh, "SHOW IMAGE REPOSITORIES IN ACCOUNT")
    if rows is None:
        return {"observed": None, "suggestion": None, "note": f"could not list image repositories: {res.text[:300]}"}
    slim = [{"database": r.get("database_name"), "schema": r.get("schema_name"), "name": r.get("name"),
             "owner": r.get("owner")} for r in rows]
    return {"observed": slim, "suggestion": slim[0] if len(slim) == 1 else None,
            "note": None if slim else "No image repository visible to this role. One must be created, or access granted."}


def compute_pools(cfg, sh):
    res, rows = sql(cfg, sh, "SHOW COMPUTE POOLS")
    if rows is None:
        return {"observed": None, "suggestion": None, "note": f"could not list compute pools: {res.text[:300]}"}
    slim = [{"name": r.get("name"), "state": r.get("state"), "instance_family": r.get("instance_family")} for r in rows]
    return {"observed": slim, "suggestion": slim[0]["name"] if len(slim) == 1 else None, "note": None}


def pip_index(cfg, sh):
    env = {k: os.environ[k] for k in ("PIP_INDEX_URL", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_EXTRA_INDEX_URL") if k in os.environ}
    res = sh.run(["pip", "config", "list"], read_only=True, timeout=30)
    conf = res.out.strip() if res.ok else None
    guess = env.get("UV_DEFAULT_INDEX") or env.get("UV_INDEX_URL") or env.get("PIP_INDEX_URL")
    return {"observed": {"env": env, "pip_config": conf}, "suggestion": guess,
            "note": "If this machine installs packages from an internal index, the image build must too."}


def ca_bundle(cfg, sh):
    env = {k: os.environ[k] for k in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "PIP_CERT") if k in os.environ}
    guess = next(iter(env.values()), None)
    return {"observed": env, "suggestion": guess,
            "note": "A CA env var on this machine usually means a TLS-inspecting proxy; the build needs the same full bundle."}


def proxy(cfg, sh):
    env = {k: os.environ[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy") if k in os.environ}
    return {"observed": env, "suggestion": env or None, "note": None}


def git_sha(cfg, sh):
    res = sh.run(["git", "rev-parse", "--short", "HEAD"], read_only=True, timeout=15)
    sha = res.out.strip() if res.ok else None
    return {"observed": sha, "suggestion": sha, "note": "A new tag per build avoids the digest-mismatch rejection."}


def ml_python(cfg, sh):
    try:
        v = md.version("snowflake-ml-python")
    except md.PackageNotFoundError:
        v = None
    return {"observed": v, "suggestion": bool(v),
            "note": None if v else "snowflake-ml-python is not installed here; the smoke test needs >= 1.37.0."}


def snow_cli(cfg, sh):
    v = sh.run(["snow", "--version"], read_only=True, timeout=30)
    ci = sh.run(["snow", "custom-image", "--help"], read_only=True, timeout=30)
    return {"observed": {"version": v.out.strip() if v.ok else None, "has_custom_image_command": ci.ok},
            "suggestion": None,
            "note": None if ci.ok else "This Snowflake CLI has no `custom-image` command; upgrade it."}


PROBES = {f.__name__: f for f in (
    engine, arch, snow_cli, snow_connections, auth, context, role, warehouse, registry_url, base_images,
    base_python, base_path, image_repos, compute_pools, pip_index, ca_bundle, proxy, git_sha, ml_python)}


def run(name: str, cfg, sh: Shell) -> dict:
    try:
        return PROBES[name](cfg, sh)
    except Exception as exc:  # a probe must never break the interview
        return {"observed": None, "suggestion": None, "note": f"probe {name} failed: {exc!r}"}
