import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / 'src'

# Everything the enrolled key_cmd cannot import: the plugin's third-party
# dependencies and Hermes itself.
BLOCK = ('pydantic', 'httpx', 'fastapi', 'ruamel', 'psutil', 'bcrypt', 'starlette', 'hermes_constants')


def bare(body):
    code = 'import sys\n' + ''.join(f'sys.modules[{name!r}] = None\n' for name in BLOCK) + body
    return subprocess.run([sys.executable, '-c', code], env={'PYTHONPATH': str(SRC)},
                          capture_output=True, text=True, timeout=30)


def test_enrolled_key_helper_imports_without_third_party_packages():
    result = bare('import hermes_cpa.subscription_client as helper\nassert callable(helper.grant_token)\n')
    assert result.returncode == 0, result.stderr[-600:]


def test_enrolled_key_helper_entry_point_runs_bare(tmp_path):
    # Exactly the enrolled command shape: --home pinned, no Hermes on the path. An
    # absent broker must be the only failure, reported by the helper itself.
    result = bare(
        'import runpy\n'
        f"sys.argv = ['helper', '--control', {str(tmp_path / 'absent.sock')!r}, '--home', {str(tmp_path)!r}, '--enrolled']\n"
        "runpy.run_module('hermes_cpa.subscription_client', run_name='__main__')\n")
    assert result.returncode == 1
    assert 'Subscription broker unavailable' in result.stderr
    assert 'ModuleNotFoundError' not in result.stderr
