"""تنظیمات ربات — همه چیز از فایل .env یا متغیرهای محیطی خوانده می‌شود."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _read_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset
    data_dir: Path
    api_base: str
    grace_minutes: int
    tz: ZoneInfo
    org_name: str
    panel_password: str = ""
    panel_host: str = "127.0.0.1"
    panel_port: int = 8080
    bale_proxy: str = ""
    use_system_proxy: bool = False
    lock_port: int = 47231

    @property
    def db_path(self) -> str:
        return str(self.data_dir / "roshd.db")

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


def load_config(env_file: str = ".env") -> Config:
    _read_env_file(Path(env_file))
    admins = frozenset(
        int(x) for x in os.environ.get("ADMIN_IDS", "").replace("،", ",").split(",") if x.strip().isdigit()
    )
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        token=os.environ.get("BALE_TOKEN", ""),
        admin_ids=admins,
        data_dir=data_dir,
        api_base=os.environ.get("BALE_API_BASE", "https://tapi.bale.ai"),
        grace_minutes=int(os.environ.get("GRACE_MINUTES", "20")),
        tz=ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tehran")),
        org_name=os.environ.get("ORG_NAME", "مجموعه تربیتی، آموزشی رشد"),
        panel_password=os.environ.get("PANEL_PASSWORD", ""),
        panel_host=os.environ.get("PANEL_HOST", "127.0.0.1"),
        panel_port=int(os.environ.get("PANEL_PORT", "8080")),
        bale_proxy=os.environ.get("BALE_PROXY", ""),
        use_system_proxy=os.environ.get("USE_SYSTEM_PROXY", "0") == "1",
        lock_port=int(os.environ.get("LOCK_PORT", "47231")),
    )
