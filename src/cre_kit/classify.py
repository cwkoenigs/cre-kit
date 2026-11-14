"""Turn a failure into a routed fix.

Every failure is assigned a LAYER, and the layer decides who acts:

  retry        runner   re-run (after the prescribed re-login, if any)
  config       runner   change a value with `cre config set`
  interview    runner   a question was missed or answered wrongly: reopen and re-ask
                        (watcher later improves the question text)
  python       watcher  cre-kit's own code or a rendered artifact is wrong
  unknown      watcher  no rule matched; needs judgment
  environment  human    missing tool, privilege, network path, or interactive login

Rules are data. When the watcher meets a new failure, it adds a rule here and
a test, so the next occurrence is handled by the cheap runner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

WHO = {"retry": "runner", "config": "runner", "interview": "runner",
       "python": "watcher", "unknown": "watcher", "environment": "human"}


@dataclass
class Failure:
    cls: str
    layer: str
    hint: str
    keys: list[str] = field(default_factory=list)

    @property
    def who(self) -> str:
        return WHO[self.layer]


PRIVILEGE_HINTS = {
    "register": "The role needs the Create Custom Runtime Environment privilege on the account.",
    "push": "The role needs write access to the image repository (and CREATE IMAGE REPOSITORY on the schema if it must be created).",
    "verify": "The role needs MONITOR on the CRE to describe it.",
    "smoke": "The role needs USAGE on the CRE, USAGE on the compute pool, and write access to the stage.",
    "base_image": "The role needs USAGE on the warehouse.",
}

# (stages or None, regex, class, layer, hint, keys). First match wins.
RULES: list[tuple] = [
    (None, r"Traceback \(most recent call last\)[\s\S]*cre_kit", "cre_kit_crash", "python",
     "cre-kit itself raised. Fix the code and add a regression test.", []),
    (None, r"command not found|No such file or directory: '(snow|docker|podman)'", "tool_missing", "environment",
     "A required tool is not installed or not on PATH.", ["build.engine"]),
    (None, r"No such command", "cli_too_old", "environment",
     "The Snowflake CLI on this machine lacks a command this stage needs. Upgrade it.", []),
    (None, r"No such option|Got unexpected extra argument|Missing option|Missing argument|Invalid value for|unknown flag|unknown shorthand flag", "bad_cli_args", "python",
     "cre-kit passed arguments this CLI version does not accept. Check `<tool> <command> --help`, fix the command in stages.py/probe.py, add a test.", []),
    (None, r"Cannot connect to the Docker daemon|Is the docker daemon running|Cannot connect to Podman|podman machine", "daemon_down", "environment",
     "The container engine is installed but not running.", []),
    (None, r"no space left on device", "disk_full", "environment", "The machine is out of disk for image layers.", []),
    (None, r"external ?browser|SAML|Failed to connect to DB.*authenticat|MFA|Incorrect username or password|JWT token is invalid", "login_needs_human", "environment",
     "Interactive or credential login failed. A person has to sign in, or use a key-pair service account.", ["snowflake.connection", "snowflake.auth"]),
    (None, r"Insufficient privileges|not authorized to perform|Access denied|must have .{0,60}privilege", "privilege", "environment",
     "The role lacks a privilege.", ["snowflake.role"]),
    (("push", "base_image", "registry_login"), r"unauthorized|authentication required|denied: requested access|token.{0,20}expired|no basic auth credentials", "registry_auth", "retry",
     "Registry session is missing or expired. Re-run from registry_login.", []),
    (("base_image",), r"manifest unknown|manifest for .* not found|not found: manifest|pull access denied|repository does not exist", "base_image_missing", "interview",
     "The base image path does not exist in the registry. Re-probe the base image list and re-ask.", ["base_image.image_path", "snowflake.registry_url"]),
    (None, r"does not exist or not authorized|Object .* does not exist|Schema .* does not exist|Database .* does not exist", "object_missing", "interview",
     "A named Snowflake object does not exist for this role. Re-ask for the right names.", []),
    (None, r"CERTIFICATE_VERIFY_FAILED|self[- ]signed certificate|unable to get local issuer certificate|certificate signed by unknown authority|invalid peer certificate|UnknownIssuer", "tls_untrusted", "interview",
     "TLS inspection: the build does not trust the corporate CA. Ask for the CA bundle.", ["build.ca_bundle"]),
    (("build",), r"Could not resolve host|Temporary failure in name resolution|Name or service not known|Connection timed out|Network is unreachable|Connection refused|ProxyError|dns error|error sending request|Failed to fetch|Max retries exceeded", "no_network_in_build", "interview",
     "The build cannot reach the package index. Ask about the internal index and proxy build args.", ["build.pip_index_url", "build.build_args"]),
    (("build", "validate", "register"), r"No matching distribution|Could not find a version|No solution found|ResolutionImpossible|unsatisfiable|not found in the package registry|conflict", "dependency_conflict", "config",
     "The pinned packages do not resolve against the base image. Change the pins.", ["build.pip_packages"]),
    (("build", "validate"), r"exec format error|does not match the (specified|detected|expected) platform", "wrong_platform", "environment",
     "linux/amd64 emulation is not available on this machine (Rosetta/QEMU).", ["build.platform"]),
    (("validate", "register"), r"entrypoint|DASHBOARD_PORT", "runtime_contract_broken", "config",
     "The image changed the entrypoint or DASHBOARD_PORT. Remove the offending extra Dockerfile lines.", ["build.dockerfile_extra"]),
    (("validate", "register"), r"(missing|required) (python )?package|not installed", "required_package_removed", "config",
     "A package Snowflake requires was removed or broken by the pins.", ["build.pip_packages", "build.dockerfile_extra"]),
    (("smoke", "register", "verify"), r"digest", "digest_mismatch", "config",
     "The image behind this tag changed after registration. Use a new image.tag and register again.", ["image.tag", "cre.name"]),
    (None, r"SQL compilation error|syntax error|unexpected '", "bad_sql", "python",
     "A rendered SQL statement is wrong for this account/version. Fix render.py and add a test.", []),
    (None, r"timed out after", "timeout", "retry", "The command timed out. Retry once; if it repeats, escalate.", []),
]


def classify(stage: str, text: str) -> Failure:
    for stages, pattern, cls, layer, hint, keys in RULES:
        if stages and stage not in stages:
            continue
        if re.search(pattern, text, re.IGNORECASE):
            if cls == "privilege" and stage in PRIVILEGE_HINTS:
                hint = f"{hint} {PRIVILEGE_HINTS[stage]}"
            return Failure(cls, layer, hint, list(keys))
    return Failure("unclassified", "unknown",
                   "No rule matched. Diagnose from the evidence; once fixed, add a rule to classify.py with a test.")
