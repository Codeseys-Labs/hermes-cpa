import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / 'src'

BLOCK = ('pydantic', 'httpx', 'fastapi', 'ruamel', 'psutil', 'bcrypt', 'starlette')


def test_enrolled_key_helper_imports_without_third_party_packages():
    # The enrolled key_cmd runs under a bare interpreter with only this source
    # tree on PYTHONPATH. Importing the helper must not need any plugin dependency.
    code = ''.join(f"sys.modules[{name!r}] = None\n" for name in BLOCK)
    code = 'import sys\n' + code + (
        'import hermes_cpa.subscription_client as helper\n'
        'assert callable(helper.grant_token)\n')
    result = subprocess.run([sys.executable, '-c', code], env={'PYTHONPATH': str(SRC)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-600:]
