"""Facts about HOW a command is called: the shape of the test labels the runner takes, and an
environment variable the code asks for. Both are learned LLM-free from a failure followed, in
the same task, by the run that worked."""
import pytest

from tokeneconomizer.microskills import envfacts as ef, observe
from fixtures import write_claude_transcript

OK = "Ran 4 tests in 0.1s\n\nOK"


def _edit(i):
    return ("Edit", {"file_path": f"/r/pkg/mod{i}.py", "old_string": "a", "new_string": "b"}, "ok")


def _run(label):
    return ("Bash", {"command": f"python tests/runtests.py {label} -v 2 2>&1 | tail -20"})


def _stream(tmp_path, tasks):
    """Run a distiller over tasks (lists of calls); return it and what it rendered per task."""
    d = ef.EnvFactsDistiller("acme/widget")
    rendered = []
    for i, calls in enumerate(tasks):
        rendered.append(d.render())
        d.update(write_claude_transcript(str(tmp_path / f"t{i}.jsonl"), calls), iid=f"t{i}")
    return d, rendered


@pytest.mark.parametrize("a,b,rule", [
    ("tests.model_inheritance", "model_inheritance", ("strip", "tests.")),
    ("tests.admin_views.tests", "admin_views.tests", ("strip", "tests.")),
    ("db.datetime.test_x::DateTests::test_a", "db.datetime.test_x.DateTests.test_a", ("sep", "::")),
    ("db.datetime.test_x::DateTests", "db.datetime.test_x", ("sep", "::")),
    ("model_fields.DurationFieldTests", "model_fields", None),     # a class that is not there
    ("admin_views.ChangeListTests", "admin_views", None),
    ("app.tests", "other.tests", None),
])
def test_only_a_shape_rewrite_counts_as_a_repair(a, b, rule):
    assert observe.label_rule(a, b) == rule


def test_labels_are_the_structured_arguments_after_the_runner():
    o = observe.classify_command("python tests/runtests.py tests.app -v 2 --settings=x 2>&1 | tail")[0]
    assert observe._test_labels(o) == ("tests.app",)
    o = observe.classify_command("python -m pytest pkg/test_a.py::T::test_b -q > /tmp/o.log")[0]
    assert observe._test_labels(o) == ("pkg/test_a.py::T::test_b",)


def test_a_label_prefix_the_runner_rejects_becomes_a_fact(tmp_path):
    tasks = [[_edit(i), _run(f"tests.app{i}") + ("ModuleNotFoundError: No module named 'tests'",),
              _run(f"app{i}") + (OK,)] for i in range(6)]
    d, rendered = _stream(tmp_path, tasks)
    assert "takes test labels without the `tests.` prefix" in rendered[-1]
    claim = next(f.claim() for f in d.facts.values() if f.family == "arg_repair")
    assert claim.startswith("`python tests/runtests.py` takes") and "(`app" in claim
    assert ef.lint_ok(claim)


def test_pytest_style_separators_the_runner_rejects_become_a_fact(tmp_path):
    tasks = [[_edit(i),
              _run(f"app{i}.tests::T::test_x") + (
                  f"ImportError: Failed to import test module: app{i}.tests::T::test_x",),
              _run(f"app{i}.tests.T.test_x") + (OK,)] for i in range(6)]
    _, rendered = _stream(tmp_path, tasks)
    assert "takes dotted test labels" in rendered[-1]


def test_a_plain_mistake_is_not_a_fact(tmp_path):
    tasks = [[_edit(i),
              _run(f"app{i}.tests.Missing") + (
                  f"AttributeError: module 'app{i}.tests' has no attribute 'Missing'",),
              _run(f"app{i}") + (OK,)] for i in range(6)]
    d, rendered = _stream(tmp_path, tasks)
    assert not [f for f in d.facts.values() if f.family == "arg_repair"]
    assert rendered[-1] == ""


def test_a_prefixed_label_that_loads_contradicts_the_fact(tmp_path):
    tasks = [[_edit(i), _run(f"tests.app{i}") + ("ModuleNotFoundError: No module named 'tests'",),
              _run(f"app{i}") + (OK,)] for i in range(4)]
    tasks.append([_edit(9), _run("tests.app9") + (OK,)])
    d, _ = _stream(tmp_path, tasks)
    fid = next(k for k in d.facts if k.startswith("arg_repair:"))
    assert d.last_digest["verdicts"][fid][0] == "contradict"


def _py(code, env=""):
    return ("Bash", {"command": f"{env}python -c '{code}'"})


NEED = ("Traceback (most recent call last):\n  ...\nRuntimeError: settings are missing. Define "
        "the environment variable APP_SETTINGS before importing app.")


def test_an_environment_variable_the_code_asks_for_and_its_working_value_become_a_fact(tmp_path):
    tasks = [[_edit(i), _py("import app; app.run()") + (NEED,),
              _py("import app; app.run()", "APP_SETTINGS=conf.missing ") + (
                  "Traceback (most recent call last):\nModuleNotFoundError: No module named "
                  "'conf.missing'",),
              _py("import app; app.run()", "APP_SETTINGS=conf.dev ") + ("ran",)]
             for i in range(6)]
    d, rendered = _stream(tmp_path, tasks)
    assert ("Code that imports the project needs `APP_SETTINGS` set here: "
            "`APP_SETTINGS=conf.dev` works, `conf.missing` does not.") in rendered[-1]
    assert ef.lint_ok(d.facts["env_value:APP_SETTINGS"].claim())


def test_a_value_set_in_the_code_counts_too(tmp_path):
    code = 'import os; os.environ.setdefault("APP_SETTINGS", "conf.dev"); import app'
    tasks = [[_edit(i), _py("import app") + (NEED,), ("Bash", {"command": f"python -c '{code}'"},
                                                      "ran")] for i in range(6)]
    d, _ = _stream(tmp_path, tasks)
    assert d.facts["env_value:APP_SETTINGS"].params["good"] == {"conf.dev": 6}


def test_no_value_that_worked_means_no_fact(tmp_path):
    tasks = [[_edit(i), _py("import app") + (NEED,),
              _py("import app", "APP_SETTINGS=conf.missing ") + ("Traceback (most recent call "
                                                                 "last):\nNameError: x",)]
             for i in range(6)]
    d, _ = _stream(tmp_path, tasks)
    assert "env_value:APP_SETTINGS" not in d.facts


def test_output_filtered_away_neither_teaches_nor_contradicts(tmp_path):
    """`| grep FAILED` or a silent run shows no tests running: it proves nothing about the label
    (both cases were in real sessions, and once retired a correct fact)."""
    tasks = [[_edit(i), _run(f"app{i}.tests::T::test_x") + (
                 f"app{i}.tests::T::test_x (unittest.loader._FailedTest) ... ERROR",),
              _run(f"app{i}.tests.T.test_x") + (OK,)] for i in range(4)]
    tasks.append([_edit(8), _run("app8.tests::T::test_x") + ("FAILED (errors=1)",),
                  _run("app8.tests::T::test_y") + ("",)])
    d, _ = _stream(tmp_path, tasks)
    fid = "arg_repair:python tests/runtests.py:sep:::"
    assert d.last_digest["verdicts"][fid][0] != "contradict"
    assert d.facts[fid].state != "RETIRED"


def test_a_repair_needs_the_rewritten_label_to_have_run_tests(tmp_path):
    tasks = [[_edit(i), _run(f"tests.app{i}") + ("ModuleNotFoundError: No module named 'tests'",),
              _run(f"app{i}") + ("",)] for i in range(6)]          # no evidence the tests ran
    d, _ = _stream(tmp_path, tasks)
    assert not [f for f in d.facts.values() if f.family == "arg_repair"]


def test_being_asked_for_the_variable_again_does_not_confirm_its_value(tmp_path):
    tasks = [[_edit(0), _py("import app") + (NEED,),
              _py("import app", "APP_SETTINGS=conf.dev ") + ("ran",)]]
    tasks += [[_edit(i), _py("import app") + (NEED,)] for i in range(1, 6)]
    d, rendered = _stream(tmp_path, tasks)
    assert d.facts["env_value:APP_SETTINGS"].state == "CANDIDATE"
    assert "APP_SETTINGS" not in rendered[-1]


def test_mistyped_labels_do_not_teach_that_the_runner_fails(tmp_path):
    """Seen in a live stream: bad labels report `ModuleNotFoundError` inside unittest's
    `_FailedTest`, and a later `cd tests && python runtests.py` worked. That once became
    "`python tests/runtests.py` fails here; `python runtests.py` works" -- wrong twice over."""
    bad = ("tests (unittest.loader._FailedTest) ... ERROR\nImportError: Failed to import test "
           "module: tests\nModuleNotFoundError: No module named 'tests.app'")
    tasks = [[_edit(i), _run(f"tests.app{i}.tests.T") + (bad,),
              ("Bash", {"command": f"cd tests && python runtests.py app{i}.tests.T -v 2"}, OK)]
             for i in range(6)]
    d, rendered = _stream(tmp_path, tasks)
    assert not [f for f in d.facts.values() if f.family == "form_repair"]
    assert "fails here" not in rendered[-1]


def test_a_directory_change_is_part_of_the_form():
    o = observe.classify_command("cd tests && python runtests.py forms -v 2")[0]
    assert o.form == "cd tests && python runtests.py" and observe._test_labels(o) == ("forms",)


def test_a_missing_runner_is_still_a_form_failure_not_a_label_one():
    assert not observe._label_failed(("tests/test_a.py",), "/usr/bin/python3: No module named pytest")
    assert observe._label_failed(("tests.app",), "ModuleNotFoundError: No module named 'tests'")
    assert observe._label_failed(("app.tests.Missing",),
                            "AttributeError: module 'app.tests' has no attribute 'Missing'")
    assert not observe._label_failed(("app.tests",), "AttributeError: 'NoneType' object has no "
                                                "attribute 'pk'")


ROOT = "/r"


def _at(call, cwd):
    return call + (cwd,)


def _sh(cmd, out):
    return ("Bash", {"command": cmd}, out)


def test_a_bare_runner_in_a_directory_an_earlier_cd_left_is_learned_with_that_cd(tmp_path):
    """Seen in a live session: Claude Code's shell keeps its directory, so after `cd tests &&
    python runtests.py ...` the agent ran a bare `python runtests.py ...` -- in `tests/`. Read
    without the directory, that taught "`python runtests.py` works", which fails from the
    directory sessions start in."""
    tasks = [[_at(_edit(i), ROOT),
              _at(_sh("python -m pytest tests/app/tests.py -q",
                      "/venv/bin/python: No module named pytest"), ROOT),
              _at(_sh(f"cd tests && python runtests.py app{i} -v 2", OK), ROOT),
              _at(_sh(f"python runtests.py app{i}.tests -v 2", OK), ROOT + "/tests"),
              _at(_sh("python runtests.py app.tests 2>&1 | tail -3",
                      "python: can't open file 'runtests.py': [Errno 2] No such file or "
                      "directory"), ROOT)]
             for i in range(6)]
    d, rendered = _stream(tmp_path, tasks)
    assert "`(cd tests && python runtests.py …)` works" in rendered[-1]
    assert "`python runtests.py …` works" not in rendered[-1]
    assert not any(k.endswith(":python runtests.py") for k in d.facts), sorted(d.facts)


def test_a_cd_into_the_start_directory_by_absolute_path_is_no_cd():
    o = observe.classify_command("cd /r && python tests/runtests.py app", cwd="", root="/r")[0]
    assert o.form == "python tests/runtests.py"
    o = observe.classify_command("cd /r/tests && python runtests.py app", cwd="", root="/r")[0]
    assert o.form == "cd tests && python runtests.py"
    o = observe.classify_command("cd .. && python runtests.py app", cwd="tests", root="/r")[0]
    assert o.form == "python runtests.py"
    o = observe.classify_command("python runtests.py app", cwd="tests", root="/r")[0]
    assert o.form == "cd tests && python runtests.py"
    o = observe.classify_command("cd /elsewhere && python runtests.py app", cwd="", root="/r")[0]
    assert o.form == "cd /elsewhere && python runtests.py"          # left the start: as typed


def test_a_command_stopped_by_its_own_cd_teaches_nothing(tmp_path):
    p = write_claude_transcript(str(tmp_path / "t.jsonl"), [
        _at(_sh("cd tests && python runtests.py app", "Exit code 1\n/bin/bash: line 1: cd: "
                "tests: No such file or directory"), ROOT + "/tests")])
    assert observe.observe_task(p)["obs"] == []


def test_call_directories_line_up_with_the_calls(tmp_path):
    from tokeneconomizer import trace
    p = write_claude_transcript(str(tmp_path / "t.jsonl"), [
        _at(_sh("ls", "a"), ROOT), ("Read", {"file_path": "/r/a"}, "x"),
        _at(_sh("ls", "b"), ROOT + "/tests")])
    assert trace.call_cwds(p) == [ROOT, None, ROOT + "/tests"]
    assert len(trace.load_trajectory(p)) == 3


CANT_OPEN = ("Exit code 2\npython: can't open file 'runtests.py': [Errno 2] No such file or "
             "directory")


def test_a_runner_script_run_where_it_is_not_becomes_a_fact(tmp_path):
    tasks = [[_at(_edit(i), ROOT), _at(_sh(f"python runtests.py app{i}", CANT_OPEN), ROOT),
              _at(_sh(f"cd tests && python runtests.py app{i}", OK), ROOT)] for i in range(6)]
    _, rendered = _stream(tmp_path, tasks)
    assert ("`python runtests.py …` fails here with a file-not-found error; "
            "`(cd tests && python runtests.py …)` works.") in rendered[-1]


def test_an_option_the_runner_rejects_is_not_a_failing_form(tmp_path):
    """Seen live: `-k name` to Django's runner, from `tests/`, is "unrecognized arguments" --
    the runner ran and refused an argument."""
    bad = "Exit code 2\nusage: runtests.py [-h] [module [module ...]]\nruntests.py: error: " \
          "unrecognized arguments: test_x"
    tasks = [[_at(_edit(i), ROOT), _at(_sh(f"python runtests.py app{i} -k test_x", bad),
                                       ROOT + "/tests"),
              _at(_sh(f"python tests/runtests.py app{i}", OK), ROOT)] for i in range(6)]
    d, rendered = _stream(tmp_path, tasks)
    assert not [f for f in d.facts.values() if f.family == "form_repair"], sorted(d.facts)


def test_a_run_whose_output_was_filtered_away_does_not_show_a_form_works(tmp_path):
    """Seen live: `python runtests.py x 2>&1 | grep -E "^(OK|FAILED|Ran )"` at the root printed
    nothing -- the "can't open file" was filtered out -- and counted as the form working."""
    tasks = [[_at(_edit(i), ROOT), _at(_sh("python -m pytest tests/app", "Exit code 1\n"
                                           "/venv/bin/python: No module named pytest"), ROOT),
              _at(_sh(f"python runtests.py app{i} 2>&1 | grep -E '^(OK|FAILED|Ran )'",
                      "(Bash completed with no output)"), ROOT)] for i in range(6)]
    d, _ = _stream(tmp_path, tasks)
    assert not [f for f in d.facts.values() if f.family == "form_repair"], sorted(d.facts)


def test_a_working_form_that_leaves_the_shell_where_it_is_is_the_one_named(tmp_path):
    """Seen live: the fact named `cd tests && python runtests.py`, agents typed it, and their
    shell stayed in `tests/` -- every later `cd tests && ...` failed. A form that does not move
    the shell is named whenever one has worked, however rarely."""
    tasks = [[_at(_edit(i), ROOT), _at(_sh(f"python runtests.py app{i}", CANT_OPEN), ROOT),
              _at(_sh(f"cd tests && python runtests.py app{i}", OK), ROOT),
              _at(_sh(f"cd tests && python runtests.py app{i}.tests", OK), ROOT)]
             for i in range(6)]
    tasks[3].append(_at(_sh("python tests/runtests.py app3", OK), ROOT))
    d, rendered = _stream(tmp_path, tasks)
    f = d.facts["form_repair:run_tests:python runtests.py"]
    assert f.params["b_counts"]["cd tests && python runtests.py"] > \
        f.params["b_counts"]["python tests/runtests.py"]
    assert "`python tests/runtests.py …` works" in rendered[-1]
