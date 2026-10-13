"""Adapters: DomainAdapter protocol conformance + env-var placeholder behavior."""
import os

import pytest

from tokeneconomizer.adapters import (
    DomainAdapter, SwebenchAdapter, WebBenchAdapter, CredDataAdapter,
    ReposVulAdapter, CyberGymAdapter, WebArenaAdapter)


ALL_CONCRETE = [SwebenchAdapter, CredDataAdapter, ReposVulAdapter, CyberGymAdapter]


def test_swebench_is_domain_adapter():
    assert isinstance(SwebenchAdapter(), DomainAdapter)


def test_all_adapters_importable_and_named():
    assert SwebenchAdapter().name == "swebench"
    assert WebBenchAdapter("dom").name == "webbench"
    assert CredDataAdapter().name == "creddata"
    assert ReposVulAdapter().name == "reposvul"
    assert CyberGymAdapter().name == "cybergym"
    assert WebArenaAdapter().name == "webarena"


def test_swebench_reading_methods_delegate():
    a = SwebenchAdapter()
    assert a.protected_tools() is not None
    assert a.inapplicable_tools() is not None
    # codex default catalog is available without a transcript
    assert "shell" in a.parse_catalog("/nonexistent", "codex")


def test_no_hardcoded_dataset_roots():
    # Constructing without env vars must not bake in a host path; the error only fires
    # when the task spec is actually requested.
    for env in ("TOKENECONOMIZER_CREDDATA_ROOT", "TOKENECONOMIZER_REPOSVUL_ROOT"):
        os.environ.pop(env, None)
    with pytest.raises(RuntimeError):
        CredDataAdapter().repo_ids()
    with pytest.raises(RuntimeError):
        ReposVulAdapter().instance_ids()


def test_swebench_problem_statements_requires_config():
    os.environ.pop("TOKENECONOMIZER_SWEBENCH_TASKS", None)
    with pytest.raises(RuntimeError):
        SwebenchAdapter().problem_statements()


def test_swebench_problem_statements_from_file(tmp_path):
    import json
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps([
        {"instance_id": "django-1",
         "problem_statement": "see https://github.com/django/django/pull/42 for the fix"}]))
    ps = SwebenchAdapter(tasks_path=str(p)).problem_statements()
    assert "django-1" in ps
    assert "[link redacted]" in ps["django-1"]     # gold URL was redacted


def test_creddata_classify_domain_differs():
    assert SwebenchAdapter().classify_domain() is None
    assert CredDataAdapter().classify_domain() is not None


def test_webarena_stub_raises():
    with pytest.raises(NotImplementedError):
        WebArenaAdapter().load_trajectory("x")
