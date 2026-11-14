"""Config schema, load/save, validation.

The config file (cre.yaml) is machine-managed: the interview writes it, the
runner edits it through `cre config set`, and every stage reads it. Nothing in
the pipeline is hard-coded to an account, so the same package works on any
machine once the interview has been run there.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_FILE = "cre.yaml"
IDENT = r"^[A-Za-z_][A-Za-z0-9_$]*$"
PREDEFINED_BUILD_ARGS = {
    "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "ftp_proxy", "no_proxy", "all_proxy",
}
TYPES = {"str": str, "bool": bool, "int": int, "list": list, "dict": dict}


@dataclass(frozen=True)
class Field:
    type: str
    default: object = None
    # True, False, or a dotted key that must be truthy for this to be required.
    required: object = False
    choices: tuple = ()
    pattern: str | None = None
    help: str = ""


FIELDS: dict[str, Field] = {
    # --- Snowflake connection -------------------------------------------
    "snowflake.connection": Field("str", required=True, help="Snowflake CLI connection name (snow connection list)."),
    "snowflake.auth": Field("str", required=True, choices=("sso", "keypair", "pat", "other"),
                            help="How this connection authenticates. Informational; drives login guidance."),
    "snowflake.role": Field("str", help="Role to use. Empty = connection default."),
    "snowflake.warehouse": Field("str", required=True, help="Warehouse for the metadata queries."),
    "snowflake.registry_url": Field("str", required=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z0-9.\-]+$",
                                    help="Host from `snow spcs image-registry url` (no scheme, no path)."),
    # --- Image repository -----------------------------------------------
    "image_repo.database": Field("str", required=True, pattern=IDENT),
    "image_repo.schema": Field("str", required=True, pattern=IDENT),
    "image_repo.name": Field("str", required=True, pattern=IDENT),
    "image_repo.create_if_missing": Field("bool", default=False),
    # --- Base image ------------------------------------------------------
    "base_image.type": Field("str", default="CPU", required=True, choices=("CPU", "GPU")),
    "base_image.python_version": Field("str", required=True, pattern=r"^\d+\.\d+$"),
    "base_image.image_path": Field("str", required=True, pattern=r"^/\S+:\S+$",
                                   help="image_path from SYSTEM$GET_LATEST_CONTAINER_RUNTIME_IMAGES()."),
    # --- Custom image ----------------------------------------------------
    "image.name": Field("str", required=True, pattern=r"^[a-z0-9][a-z0-9._\-]*$"),
    "image.tag": Field("str", required=True, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$"),
    # --- Build -----------------------------------------------------------
    "build.engine": Field("str", default="docker", required=True, choices=("docker", "podman")),
    "build.platform": Field("str", default="linux/amd64", required=True, choices=("linux/amd64",)),
    "build.context_dir": Field("str", default="image", required=True),
    "build.pip_packages": Field("list", default=[]),
    "build.pip_index_url": Field("str", help="Internal package index, if PyPI is not reachable."),
    "build.pip_extra_index_urls": Field("list", default=[]),
    "build.ca_bundle": Field("str", help="Path to a full CA bundle (PEM) needed to reach the index through a TLS-inspecting proxy."),
    "build.build_args": Field("dict", default={}, help="--build-arg values, e.g. HTTPS_PROXY."),
    "build.dockerfile_extra": Field("list", default=[], help="Raw Dockerfile lines appended after the pip install."),
    # --- Validation ------------------------------------------------------
    "validation.scan_vulnerabilities": Field("bool", default=True),
    # --- CRE ---------------------------------------------------------------
    "cre.name": Field("str", required=True, pattern=IDENT),
    "cre.replace_existing": Field("bool", default=False),
    "cre.grant_usage_to": Field("list", default=[]),
    # --- Smoke test ------------------------------------------------------
    "smoke_test.enabled": Field("bool", default=True),
    "smoke_test.compute_pool": Field("str", required="smoke_test.enabled", pattern=IDENT),
    "smoke_test.stage_name": Field("str", required="smoke_test.enabled"),
    # --- Self-correction loop -------------------------------------------
    "loop.watcher_model": Field("str", default="claude-opus-5-5"),
    "loop.runner_model": Field("str", default="claude-haiku-5-5"),
    "loop.max_runner_attempts": Field("int", default=3),
    "loop.max_watcher_rounds": Field("int", default=2),
}


@dataclass
class Issue:
    key: str
    severity: str  # "error" | "warning"
    problem: str

    def as_dict(self) -> dict:
        return {"key": self.key, "severity": self.severity, "problem": self.problem}


def defaults() -> dict:
    cfg: dict = {"schema_version": 1}
    for key, f in FIELDS.items():
        set_raw(cfg, key, copy.deepcopy(f.default))
    return cfg


def get(cfg: dict, key: str, default=None):
    node = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_raw(cfg: dict, key: str, value) -> None:
    node = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def coerce(key: str, raw: str):
    """Turn a CLI string into the field's type. Strings are kept verbatim so
    '3.10' never becomes the float 3.1."""
    if key not in FIELDS:
        raise KeyError(f"unknown config key: {key}")
    f = FIELDS[key]
    if raw.strip() in ("", "null", "~", "None"):
        return copy.deepcopy(f.default) if f.type in ("list", "dict") else None
    if f.type == "str":
        return raw.strip()
    value = yaml.safe_load(raw)
    if f.type == "list" and isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    return value


def check_key(cfg: dict, key: str) -> list[Issue]:
    f = FIELDS[key]
    value = get(cfg, key)
    out: list[Issue] = []
    empty = value is None or value == ""
    required = f.required is True or (isinstance(f.required, str) and bool(get(cfg, f.required)))
    if empty:
        if required:
            out.append(Issue(key, "error", "required but not set"))
        return out
    if not isinstance(value, TYPES[f.type]) or (f.type == "int" and isinstance(value, bool)):
        hint = " (quote it in YAML)" if f.type == "str" else ""
        out.append(Issue(key, "error", f"must be {f.type}, got {type(value).__name__}{hint}"))
        return out
    if f.choices and value not in f.choices:
        out.append(Issue(key, "error", f"must be one of {list(f.choices)}, got {value!r}"))
    if f.pattern and isinstance(value, str) and not re.match(f.pattern, value):
        out.append(Issue(key, "error", f"{value!r} does not match {f.pattern}"))
    return out


def _cross_checks(cfg: dict, root: Path) -> list[Issue]:
    out: list[Issue] = []
    reg = get(cfg, "snowflake.registry_url") or ""
    if "/" in reg or "://" in reg:
        out.append(Issue("snowflake.registry_url", "error", "host only: no scheme and no path"))
    for key in ("build.pip_index_url",):
        url = get(cfg, key) or ""
        if re.search(r"://[^/@\s]+@", url):
            out.append(Issue(key, "error", "credentials in the index URL would be baked into the image history"))
    for url in get(cfg, "build.pip_extra_index_urls") or []:
        if re.search(r"://[^/@\s]+@", str(url)):
            out.append(Issue("build.pip_extra_index_urls", "error", "credentials in an index URL would be baked into the image history"))
    for spec in get(cfg, "build.pip_packages") or []:
        if not isinstance(spec, str):
            out.append(Issue("build.pip_packages", "error", f"entries must be strings, got {spec!r}"))
        elif "==" not in spec:
            out.append(Issue("build.pip_packages", "warning", f"{spec!r} is not pinned with ==; the image will not be reproducible"))
    for line in get(cfg, "build.dockerfile_extra") or []:
        head = str(line).strip().split(" ", 1)[0].upper()
        if head in ("ENTRYPOINT", "CMD", "FROM"):
            out.append(Issue("build.dockerfile_extra", "error",
                             f"{head} is not allowed: the entrypoint must stay /usr/local/bin/entrypoint.sh and the base must stay the Snowflake image"))
        if "DASHBOARD_PORT" in str(line):
            out.append(Issue("build.dockerfile_extra", "error", "DASHBOARD_PORT must stay 12003 as set by the base image"))
    ca = get(cfg, "build.ca_bundle")
    if ca and not (root / ca).expanduser().exists() and not Path(ca).expanduser().exists():
        out.append(Issue("build.ca_bundle", "error", f"file not found: {ca}"))
    for k, v in (get(cfg, "build.build_args") or {}).items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)):
            out.append(Issue("build.build_args", "error", f"bad build-arg name {k!r}"))
    for role in get(cfg, "cre.grant_usage_to") or []:
        if not re.match(IDENT, str(role)):
            out.append(Issue("cre.grant_usage_to", "error", f"bad role name {role!r}"))
    if get(cfg, "base_image.type") == "GPU":
        out.append(Issue("base_image.type", "warning", "GPU base image: confirm a GPU compute pool actually exists in this account"))
    for key in ("loop.max_runner_attempts", "loop.max_watcher_rounds"):
        v = get(cfg, key)
        if isinstance(v, int) and not isinstance(v, bool) and v < 1:
            out.append(Issue(key, "error", "must be >= 1"))
    return out


def validate(cfg: dict, root: Path | None = None) -> list[Issue]:
    root = root or Path.cwd()
    out: list[Issue] = []
    for key in FIELDS:
        out.extend(check_key(cfg, key))
    known = set(FIELDS) | {"schema_version"}
    for key in _flatten(cfg):
        if key not in known and not any(key.startswith(k + ".") for k, f in FIELDS.items() if f.type == "dict"):
            out.append(Issue(key, "warning", "unknown key (typo?)"))
    if not any(i.severity == "error" for i in out):
        out.extend(_cross_checks(cfg, root))
    else:
        out.extend(i for i in _cross_checks(cfg, root) if i.key not in {o.key for o in out})
    return out


def errors(cfg: dict, root: Path | None = None) -> list[Issue]:
    return [i for i in validate(cfg, root) if i.severity == "error"]


def _flatten(node: dict, prefix: str = "") -> list[str]:
    keys: list[str] = []
    for k, v in node.items():
        full = f"{prefix}{k}"
        if isinstance(v, dict) and full not in FIELDS:
            keys.extend(_flatten(v, full + "."))
        else:
            keys.append(full)
    return keys


def load(root: Path | None = None) -> dict:
    root = root or Path.cwd()
    path = root / CONFIG_FILE
    cfg = defaults()
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must be a YAML mapping")
        _merge(cfg, data)
    return cfg


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def save(cfg: dict, root: Path | None = None) -> Path:
    root = root or Path.cwd()
    path = root / CONFIG_FILE
    header = "# Managed by cre-kit. Edit with `cre config set <key> <value>` so changes are validated.\n"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False))
    os.replace(tmp, path)
    return path


def fingerprint(cfg: dict) -> str:
    """Hash of everything that changes what gets built. Loop settings are excluded."""
    relevant = {k: v for k, v in cfg.items() if k != "loop"}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()[:16]
