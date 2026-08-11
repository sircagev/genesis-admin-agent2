import os
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path("/opt/genesis-admin-agent/config/config.yaml")


class AgentConfig:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("GENESIS_AGENT_CONFIG") or DEFAULT_CONFIG)
        self.data = {}
        self.reload()

    def reload(self):
        with self.path.open("r", encoding="utf-8") as handle:
            self.data = yaml.safe_load(handle) or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def section(self, key):
        return self.data.get(key) or {}

    def save_identity(self, agent_id, agent_token):
        self.data["agent_id"] = agent_id
        self.data["agent_token"] = agent_token
        self.data["enrollment_token"] = ""

        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(
                self.data,
                handle,
                allow_unicode=True,
                sort_keys=False,
            )
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)
