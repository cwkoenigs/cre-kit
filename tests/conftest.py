import json

import pytest

from cre_kit import config as C
from cre_kit import state as S
from cre_kit.shell import Result, Shell

BASE_ROWS = [
    {"BASE_IMAGE_TYPE": "CPU", "PYTHON_VERSION": "3.10", "VERSION": "2.10.0",
     "IMAGE_PATH": "/snowflake/images/snowflake_images/container_runtime/cpu_x86_64:2.10.0"},
    {"BASE_IMAGE_TYPE": "GPU", "PYTHON_VERSION": "3.10", "VERSION": "2.10.0",
     "IMAGE_PATH": "/snowflake/images/snowflake_images/container_runtime/gpu_x86_64:2.10.0"},
]
REG = "myorg-myacct.registry.snowflakecomputing.com"


class FakeShell(Shell):
    """Answers commands from a list of (substring, Result-args) rules. First match wins."""

    def __init__(self, rules=None):
        super().__init__()
        self.rules = list(rules or [])
        self.calls = []

    def run(self, cmd, *, input=None, timeout=3600, mask=(), read_only=False):
        line = " ".join(cmd)
        self.calls.append(line)
        for needle, rc, out, err in self.rules + DEFAULT_RULES:
            if needle in line:
                return Result(cmd, rc, out, err)
        return Result(cmd, 0, "", "")


DEFAULT_RULES = [
    ("image-registry url", 0, REG + "\n", ""),
    ("SYSTEM$GET_LATEST_CONTAINER_RUNTIME_IMAGES", 0, json.dumps(BASE_ROWS), ""),
    ("SHOW IMAGE REPOSITORIES", 0, json.dumps([{"name": "MY_REPO"}]), ""),
    ("SHOW CUSTOM RUNTIME ENVIRONMENTS", 0, "[]", ""),
    ("DESCRIBE CUSTOM RUNTIME ENVIRONMENT", 0, json.dumps([{"name": "DS_RUNTIME", "cre_status": "RESOLVED", "digest": "sha256:abc"}]), ""),
]


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def cfg(proj):
    c = C.defaults()
    for k, v in {
        "snowflake.connection": "dev", "snowflake.auth": "sso", "snowflake.warehouse": "WH",
        "snowflake.registry_url": REG,
        "image_repo.database": "MYDB", "image_repo.schema": "MYSCHEMA", "image_repo.name": "MY_REPO",
        "base_image.python_version": "3.10",
        "base_image.image_path": "/snowflake/images/snowflake_images/container_runtime/cpu_x86_64:2.10.0",
        "image.name": "my-custom-image", "image.tag": "v1",
        "build.pip_packages": ["xgboost==2.0.3", "scikit-learn==1.3.2"],
        "cre.name": "DS_RUNTIME", "smoke_test.enabled": False,
    }.items():
        C.set_raw(c, k, v)
    C.save(c)
    return c


@pytest.fixture
def answered(cfg):
    """Mark the whole interview answered so run() does not bounce to it."""
    from cre_kit import interview as I
    st = S.load()
    st["interview"]["answered"] = [q["id"] for q in I.questions()]
    S.save(st)
    return cfg
