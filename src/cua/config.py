"""Settings from the environment and the git-ignored `.env`.

Owner: Agent C (secrets handling). Seeded by the orchestrator so every agent reads configuration one way.
Secrets are `SecretStr`: they print as '**********' and are only unwrapped at the point of use.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)

    anthropic_api_key: SecretStr | None = None
    dolibarr_port: int = 8085
    dolibarr_db_root_password: SecretStr | None = None
    dolibarr_db_password: SecretStr | None = None
    dolibarr_admin_password: SecretStr | None = None
    dolibarr_api_key: SecretStr | None = None
    cua_run_login: str = "cua_operator"
    cua_run_password: SecretStr | None = None
    cua_noperm_password: SecretStr | None = None

    @property
    def browser_base_url(self) -> str:
        return f"http://localhost:{self.dolibarr_port}"

    @property
    def api_base_url(self) -> str:
        # Python clients use 127.0.0.1: on Windows, localhost resolves to IPv6 first and WSL forwards IPv4 only.
        return f"http://127.0.0.1:{self.dolibarr_port}/api/index.php"

    def policy_variables(self) -> dict[str, str]:
        """Non-secret values a policy file may reference as `${NAME}` (see `cua.safety.policy.load_policy`)."""
        return {"DOLIBARR_PORT": str(self.dolibarr_port)}

    def secret_values(self) -> list[str]:
        """Every configured secret, for the redactor and the evidence lint."""
        return [
            v.get_secret_value()
            for v in (
                self.anthropic_api_key,
                self.dolibarr_db_root_password,
                self.dolibarr_db_password,
                self.dolibarr_admin_password,
                self.dolibarr_api_key,
                self.cua_run_password,
                self.cua_noperm_password,
            )
            if v is not None and v.get_secret_value()
        ]


@lru_cache(maxsize=1)
def settings() -> Settings:
    merged = _read_dotenv(REPO_ROOT / ".env") | {k: v for k, v in os.environ.items()}
    fields = Settings.model_fields
    return Settings(**{k.lower(): v for k, v in merged.items() if k.lower() in fields and v != ""})
