"""Echter Programmstart: LinDeck und Linewave starten und laufen weiter (kein Absturz beim Start)."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("args", [[], ["run"], ["linewave"]], ids=["lindeck", "lindeck-run", "linewave"])
def test_program_starts_and_keeps_running(tmp_path, args):
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display")
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_STATE_HOME=str(tmp_path / "state"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), STREAMLIN_NO_UPDATE_CHECK="1",
               PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "streamlin", *args], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        time.sleep(6)
        alive = proc.poll() is None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out = proc.communicate()[0]
    assert "Traceback" not in out, out
    assert alive, out
