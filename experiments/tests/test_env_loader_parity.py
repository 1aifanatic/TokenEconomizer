"""Both launcher entry points must load the same values from a shared env file."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.mark.parametrize("existing", [None, "", "from-shell"])
@pytest.mark.parametrize("entries", [
    'TE_TEST_VALUE=first\nTE_TEST_VALUE=last\n',
    'TE_TEST_VALUE=first\nTE_TEST_VALUE=\n',
    'TE_TEST_VALUE=first\nTE_TEST_VALUE="literal $HOME $(false)"\n',
])
def test_duplicate_assignments_match_python_loader(tmp_path, monkeypatch, existing, entries):
    env_file = tmp_path / "test.env"
    env_file.write_text(entries)
    monkeypatch.delenv("TE_TEST_VALUE", raising=False)
    if existing is not None:
        monkeypatch.setenv("TE_TEST_VALUE", existing)
    shell_env = {"PATH": os.environ["PATH"], "TOKENECONOMIZER_ENV_FILE": str(env_file)}
    if existing is not None:
        shell_env["TE_TEST_VALUE"] = existing
    spec = importlib.util.spec_from_file_location("te_script_env", SCRIPTS / "_env.py")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    loader.load_env(str(env_file))
    expected = os.environ.get("TE_TEST_VALUE")
    result = subprocess.run([
        "bash", "-uc", '. "$1"; te_load_env; exec "$2" -c '
        "'import json, os; print(json.dumps(os.environ.get(\"TE_TEST_VALUE\")))'",
        "_", str(SCRIPTS / "launch/_env.sh"), sys.executable,
    ], env=shell_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
