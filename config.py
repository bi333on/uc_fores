import os
import secrets
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    SECRET_KEY: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    SQLALCHEMY_DATABASE_URI: str = os.getenv("DATABASE_URL", "sqlite:///uc_fores.db")
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")
    SITE_NAME: str = os.getenv("SITE_NAME", "Учебный центр")
    SITE_URL: str = os.getenv("SITE_URL", "https://uc-fores.example.com")
    DEFAULT_PASS_SCORE: int = int(os.getenv("DEFAULT_PASS_SCORE", "80"))
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "128"))
    EMPLOYEE_DEFAULT_PASSWORD: str = os.getenv("EMPLOYEE_DEFAULT_PASSWORD", "123456")
    UPDATE_BRANCH: str = os.getenv("UPDATE_BRANCH", "main")
    UPDATE_RESTART_CMD: str = os.getenv("UPDATE_RESTART_CMD", "")

    def __post_init__(self) -> None:
        if not self.SECRET_KEY or self.SECRET_KEY == "change-me":
            object.__setattr__(self, "SECRET_KEY", secrets.token_hex(32))


config = Config()
