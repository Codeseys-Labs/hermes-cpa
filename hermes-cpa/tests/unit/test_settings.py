from pathlib import Path
from hermes_cpa.settings import CpaSettings, MANAGEMENT_SECRET_ENV, INFERENCE_SECRET_ENV

def test_settings_paths(tmp_path):
    settings = CpaSettings(tmp_path)
    assert settings.instances_dir == tmp_path / "cpa" / "instances" / "default"
    assert settings.config_path == tmp_path / "cpa" / "instances" / "default" / "config.yaml"
    assert settings.socket_path == tmp_path / "cpa" / "run" / "supervisor.sock"
    assert MANAGEMENT_SECRET_ENV == "HERMES_CPA_MANAGEMENT_KEY"
    assert INFERENCE_SECRET_ENV == "HERMES_CPA_INFERENCE_KEY"
    settings.ensure_directories()
    assert settings.instances_dir.exists()
    assert settings.run_dir.exists()
