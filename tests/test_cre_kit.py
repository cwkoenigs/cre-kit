import json

from cre_kit import classify as K
from cre_kit import config as C
from cre_kit import interview as I
from cre_kit import render as R
from cre_kit import runner
from cre_kit import state as S
from cre_kit.cli import main
from conftest import REG, FakeShell


# ---- config ---------------------------------------------------------------
def test_defaults_are_incomplete_but_typed(proj):
    errs = {e.key for e in C.errors(C.defaults())}
    assert "snowflake.connection" in errs and "cre.name" in errs
    assert "build.engine" not in errs


def test_valid_config(cfg):
    assert C.errors(cfg) == []


def test_python_version_stays_a_string():
    assert C.coerce("base_image.python_version", "3.10") == "3.10"


def test_list_coercion_from_commas():
    assert C.coerce("build.pip_packages", "a==1, b==2") == ["a==1", "b==2"]


def test_rejects_entrypoint_override(cfg):
    C.set_raw(cfg, "build.dockerfile_extra", ["ENTRYPOINT [\"/bin/sh\"]"])
    assert any(e.key == "build.dockerfile_extra" for e in C.errors(cfg))


def test_rejects_credentials_in_index_url(cfg):
    C.set_raw(cfg, "build.pip_index_url", "https://user:pw@nexus.corp/simple")
    assert any(e.key == "build.pip_index_url" for e in C.errors(cfg))


def test_unpinned_package_is_a_warning(cfg):
    C.set_raw(cfg, "build.pip_packages", ["pandas"])
    issues = C.validate(cfg)
    assert any(i.severity == "warning" and i.key == "build.pip_packages" for i in issues)
    assert C.errors(cfg) == []


def test_smoke_fields_required_only_when_enabled(cfg):
    C.set_raw(cfg, "smoke_test.enabled", True)
    assert {"smoke_test.compute_pool", "smoke_test.stage_name"} <= {e.key for e in C.errors(cfg)}


def test_fingerprint_ignores_loop_settings(cfg):
    before = C.fingerprint(cfg)
    C.set_raw(cfg, "loop.max_runner_attempts", 9)
    assert C.fingerprint(cfg) == before
    C.set_raw(cfg, "image.tag", "v2")
    assert C.fingerprint(cfg) != before


# ---- render: must match the Snowflake doc examples --------------------------
def test_refs_match_doc_examples(cfg):
    assert R.base_ref(cfg) == f"{REG}/snowflake/images/snowflake_images/container_runtime/cpu_x86_64:2.10.0"
    assert R.remote_ref(cfg) == f"{REG}/mydb/myschema/my_repo/my-custom-image:v1"
    assert R.logical_image_path(cfg) == "/mydb/myschema/my_repo/my-custom-image:v1"


def test_dockerfile_matches_doc_shape(cfg):
    df = R.dockerfile(cfg)
    assert f"FROM {R.base_ref(cfg)}" in df
    assert "RUN uv pip install --system --break-system-packages" in df
    assert "'xgboost==2.0.3'" in df and "ENTRYPOINT" not in df


def test_dockerfile_with_index_ca_and_args(cfg):
    C.set_raw(cfg, "build.pip_index_url", "https://nexus.corp/simple")
    C.set_raw(cfg, "build.ca_bundle", "ca.pem")
    C.set_raw(cfg, "build.build_args", {"HTTPS_PROXY": "http://p:8080", "ORG_FLAG": "1"})
    df = R.dockerfile(cfg)
    assert "--index-url https://nexus.corp/simple" in df
    assert f"COPY {R.CA_IN_CONTEXT}" in df and "SSL_CERT_FILE=" in df
    assert "ARG ORG_FLAG" in df and "ARG HTTPS_PROXY" not in df  # predefined proxy args stay out of history


def test_create_sql(cfg):
    assert R.create_cre_sql(cfg) == ("CREATE CUSTOM RUNTIME ENVIRONMENT DS_RUNTIME "
                                     "IMAGE_PATH = '/mydb/myschema/my_repo/my-custom-image:v1' BASE_IMAGE_TYPE = CPU")
    C.set_raw(cfg, "cre.replace_existing", True)
    assert R.create_cre_sql(cfg).startswith("CREATE OR REPLACE CUSTOM RUNTIME ENVIRONMENT")


def test_smoke_script_compiles(cfg):
    C.set_raw(cfg, "smoke_test.compute_pool", "POOL")
    C.set_raw(cfg, "smoke_test.stage_name", "payload_stage")
    src = R.smoke_job_py(cfg)
    compile(src, "smoke_job.py", "exec")
    assert 'runtime_environment="cre@DS_RUNTIME"' in src


# ---- classify -------------------------------------------------------------
def test_classification_routes():
    cases = [
        ("build", "SSL: CERTIFICATE_VERIFY_FAILED", "interview", "runner"),
        ("build", "error: Could not resolve host: pypi.org", "interview", "runner"),
        ("build", "No solution found when resolving dependencies", "config", "runner"),
        ("push", "unauthorized: authentication required", "retry", "runner"),
        ("register", "SQL access control error: Insufficient privileges to operate on account", "environment", "human"),
        ("register", "SQL compilation error: syntax error line 1", "python", "watcher"),
        ("validate", "Error: No such option: --scan-vulnerabilities", "python", "watcher"),
        ("preflight", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", "environment", "human"),
        ("build", "something nobody has seen before", "unknown", "watcher"),
    ]
    for stage, text, layer, who in cases:
        f = K.classify(stage, text)
        assert (f.layer, f.who) == (layer, who), (stage, text, f)


def test_privilege_hint_is_stage_specific():
    assert "Create Custom Runtime Environment" in K.classify("register", "Insufficient privileges").hint


# ---- interview ------------------------------------------------------------
def test_question_bank_is_consistent():
    qs = I.questions()
    required = {k for k, f in C.FIELDS.items() if f.required is True and f.default is None}
    assert required <= {q["key"] for q in qs}, "every required key with no default needs a question"


def test_interview_flow(proj):
    cfg, st, sh = C.defaults(), S.load(), FakeShell()
    q = I.next_question(cfg, st, sh, use_probe=False)
    assert q["id"] == "engine" and not q["done"]
    assert I.answer(cfg, st, "engine", "docker")["accepted"]
    bad = I.answer(cfg, st, "auth", "magic")
    assert not bad["accepted"] and "one of" in bad["problem"]
    assert not I.answer(cfg, st, "connection", None, skip=True)["accepted"]  # required
    assert I.answer(cfg, st, "role", None, skip=True)["accepted"]  # optional
    assert C.load()["build"]["engine"] == "docker"


def test_conditional_questions_and_reopen(cfg):
    st = S.load()
    st["interview"]["answered"] = [q["id"] for q in I.questions()]
    S.save(st)
    assert I.pending(cfg, st) == []
    C.set_raw(cfg, "smoke_test.enabled", True)
    assert [q["id"] for q in I.pending(cfg, st)] == ["smoke_pool", "smoke_stage"]
    C.set_raw(cfg, "smoke_test.enabled", False)
    assert I.reopen(st, keys=["build.ca_bundle"])["reopened"] == ["ca_bundle"]
    assert [q["id"] for q in I.pending(cfg, st)] == ["ca_bundle"]


# ---- pipeline + loop -------------------------------------------------------
def test_happy_path(answered):
    sh = FakeShell()
    res = runner.run(answered, shell=sh)
    assert res["status"] == "ok", res
    joined = "\n".join(sh.calls)
    assert "docker build --platform linux/amd64 -t my-custom-image:v1" in joined
    assert "snow custom-image validate my-custom-image:v1 --scan-vulnerabilities" in joined
    assert f"docker push {REG}/mydb/myschema/my_repo/my-custom-image:v1" in joined
    assert "CREATE CUSTOM RUNTIME ENVIRONMENT DS_RUNTIME" in joined
    order = [joined.index(s) for s in ("image-registry login", "docker pull", "docker build", "custom-image validate",
                                       "docker push", "CREATE CUSTOM", "DESCRIBE CUSTOM")]
    assert order == sorted(order)
    # second run is fully cached
    again = runner.run(answered, shell=FakeShell())
    assert {s["status"] for s in again["completed"]} == {"cached"}


def test_config_change_invalidates_cache(answered):
    runner.run(answered, shell=FakeShell())
    C.set_raw(answered, "image.tag", "v2")
    res = runner.run(answered, shell=FakeShell())
    assert all(s["status"] == "ok" for s in res["completed"])


def test_tls_failure_routes_to_interview_then_escalates(answered):
    rules = [("docker build", 1, "", "error: invalid peer certificate: UnknownIssuer")]
    res = runner.run(answered, shell=FakeShell(rules))
    assert (res["stage"], res["fix_layer"], res["who"]) == ("build", "interview", "runner")
    assert res["keys"] == ["build.ca_bundle"] and "reopen" in res["next"][0]
    runner.run(answered, shell=FakeShell(rules))
    third = runner.run(answered, shell=FakeShell(rules))
    assert third["who"] == "watcher" and third["escalated"]


def test_watcher_rounds_cap_goes_to_human(answered):
    rules = [("docker build", 1, "", "never seen before")]
    assert runner.run(answered, shell=FakeShell(rules))["who"] == "watcher"
    for _ in range(2):
        main(["issue", "add", "--layer", "python", "--stage", "build", "--summary", "s", "--fix", "f"])
    assert runner.run(answered, shell=FakeShell(rules))["who"] == "human"
    assert len(S.lessons()) == 2


def test_registry_mismatch_is_a_config_fix(answered):
    res = runner.run(answered, shell=FakeShell([("image-registry url", 0, "other.registry.snowflakecomputing.com\n", "")]))
    assert (res["stage"], res["fix_layer"]) == ("registry_login", "config")
    assert "other.registry" in res["hint"]


def test_existing_cre_needs_decision_then_human_yes(answered):
    show = ("SHOW CUSTOM RUNTIME ENVIRONMENTS", 0, json.dumps(
        [{"name": "DS_RUNTIME", "image_name": "my-custom-image", "tag": "v0", "cre_status": "RESOLVED"}]), "")
    res = runner.run(answered, shell=FakeShell([show]))
    assert (res["stage"], res["failure_class"], res["who"]) == ("register", "cre_exists", "runner")
    C.set_raw(answered, "cre.replace_existing", True)
    res = runner.run(answered, shell=FakeShell([show]))
    assert (res["failure_class"], res["who"]) == ("needs_approval", "human")
    sh = FakeShell([show])
    assert runner.run(answered, shell=sh, yes=True)["status"] == "ok"
    assert any("CREATE OR REPLACE CUSTOM RUNTIME ENVIRONMENT" in c for c in sh.calls)


def test_unparseable_output_routes_to_python(answered):
    res = runner.run(answered, shell=FakeShell([("SYSTEM$GET_LATEST", 0, "+---+ a table, not json", "")]))
    assert (res["stage"], res["fix_layer"], res["who"]) == ("base_image", "python", "watcher")


def test_cre_kit_crash_is_caught(answered, monkeypatch):
    def boom(cfg):
        raise RuntimeError("renderer bug")
    monkeypatch.setattr(R, "dockerfile", boom)
    res = runner.run(answered, shell=FakeShell())
    assert (res["stage"], res["failure_class"], res["who"]) == ("dockerfile", "cre_kit_crash", "watcher")
    assert "renderer bug" in res["evidence"]


def test_incomplete_config_points_back_to_interview(proj):
    res = runner.run(C.defaults(), shell=FakeShell())
    assert (res["stage"], res["fix_layer"]) == ("config", "interview")


def test_build_arg_values_are_masked_in_journal(answered):
    from cre_kit.shell import Shell
    C.set_raw(answered, "build.build_args", {"HTTPS_PROXY": "http://user:secret@proxy:8080"})
    sh = Shell(journal=S.journal_path())
    r = sh.run(["echo", "HTTPS_PROXY=http://user:secret@proxy:8080"], mask=("http://user:secret@proxy:8080",))
    assert "secret" not in S.journal_path().read_text() and "secret" not in r.out


def test_cli_config_set_validates(cfg, capsys):
    assert main(["config", "set", "image.name", "Bad Name"]) == 1
    assert main(["config", "set", "image.tag", "v9"]) == 0
    assert C.load()["image"]["tag"] == "v9"
    capsys.readouterr()
    assert main(["review"]) == 0
    assert "stages" in json.loads(capsys.readouterr().out)


def test_example_config_is_valid(proj):
    import pathlib
    import yaml
    example = pathlib.Path(__file__).parent.parent / "cre.example.yaml"
    cfg = C.defaults()
    C._merge(cfg, yaml.safe_load(example.read_text()))
    assert C.errors(cfg) == []
    assert not [i for i in C.validate(cfg) if "unknown key" in i.problem]
