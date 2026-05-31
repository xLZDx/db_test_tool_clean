"""Application configuration."""
from pydantic_settings import BaseSettings
from pathlib import Path
import os
import shutil

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    configured = (os.getenv("DB_TESTING_TOOL_DATA_DIR") or "").strip()
    if configured:
        return Path(configured)
    local_app_data = (os.getenv("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "DBTestingTool"
    return BASE_DIR / "data"


DATA_DIR = _default_data_dir()
LEGACY_DB_PATH = BASE_DIR / "data" / "app.db"
DEFAULT_DB_PATH = DATA_DIR / "app.db"

class Settings(BaseSettings):
    APP_NAME: str = "DB Testing Tool"
    APP_VERSION: str = "1.0.0"
    DATABASE_URL: str = f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}"
    SYNC_DATABASE_URL: str = f"sqlite:///{DEFAULT_DB_PATH}"
    # AI provider settings
    AI_PROVIDER: str = "githubcopilot"   # githubcopilot
    # Avoid setting default OpenAI model names that may be unsupported by Copilot.
    AI_MODEL: str = ""
    AI_BASE_URL: str = ""         # for compatible/internal OpenAI-style endpoint
    AI_API_KEY: str = ""          # optional generic key (fallbacks below)
    AI_HTTP_TIMEOUT_SECONDS: int = 25
    GITHUBCOPILOT_BASE_URL: str = ""
    GITHUBCOPILOT_API_KEY: str = ""
    GITHUBCOPILOT_MODEL: str = ""
    GITHUBCOPILOT_EDITOR_VERSION: str = "vscode/1.98.0"
    GITHUBCOPILOT_EDITOR_PLUGIN_VERSION: str = "copilot-chat/0.26.7"
    GITHUBCOPILOT_INTEGRATION_ID: str = "vscode-chat"
    GITHUB_OAUTH_CLIENT_ID: str = ""
    GITHUB_OAUTH_SCOPE: str = "read:user copilot"
    GITHUB_VERIFY_SSL: bool = True
    GITHUB_CA_BUNDLE: str = ""
    REDSHIFT_DEFAULT_SESSION_MINUTES: int = 480
    AUTO_APPROVE_CONFIRMATIONS: bool = False
    COPILOT_AUTH_MODE: str = "manual"   # automatic | manual
    CONTROL_TABLE_SCHEMA: str = "ikorostelev"   # schema for control/reference tables used in per-attribute tests
    DATASOURCES_JSON: str = "[]"         # JSON array of datasource objects to keep synced on startup
    # SharePoint/OneDrive/Confluence auth
    SHAREPOINT_BEARER_TOKEN: str = ""   # Optional: Bearer token for SharePoint/OneDrive/Confluence downloads
    # Certificate-based SharePoint/Graph OAuth (long-term solution)
    SHAREPOINT_CERT_PATH: str = ""  # Path to PEM certificate
    SHAREPOINT_CERT_THUMBPRINT: str = ""  # Certificate thumbprint (no spaces)
    SHAREPOINT_CLIENT_ID: str = ""  # Azure AD App Registration (Application ID)
    SHAREPOINT_TENANT_ID: str = ""  # Azure AD Tenant ID
    # OpenAI settings (legacy/optional)
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = ""
    OPENAI_VERIFY_SSL: bool = True
    OPENAI_CA_BUNDLE: str = ""
    # Azure OpenAI settings
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_API_VERSION: str = "2024-02-01"
    AZURE_OPENAI_DEPLOYMENT: str = ""
    # TFS / Azure DevOps settings
    TFS_BASE_URL: str = ""
    TFS_PAT: str = ""
    TFS_PROJECT: str = "CDSIntegration,Lighthouse"  # comma-separated list of allowed projects
    TFS_COLLECTION: str = "DefaultCollection"
    # External desktop tools (optional): used by External Tools tab
    ODI_STUDIO_PATH: str = ""
    SQLDEVELOPER_PATH: str = ""
    ODI_STREAM_URL: str = ""
    SQLDEVELOPER_STREAM_URL: str = ""
    # Background watchdog for stale/zombie sessions
    WATCHDOG_ENABLED: bool = True
    WATCHDOG_INTERVAL_SECONDS: int = 3
    WATCHDOG_OPERATION_STALE_MINUTES: int = 20
    WATCHDOG_OPERATION_QUEUE_STALE_MINUTES: int = 30
    WATCHDOG_OPERATION_RETAIN_MINUTES: int = 120
    WATCHDOG_ODI_STALE_SECONDS: int = 600
    WATCHDOG_ODI_MAX_RUNTIME_SECONDS: int = 7200
    WATCHDOG_ODI_RETAIN_SECONDS: int = 3600
    # API key required by high-risk SQL/admin endpoints. If unset, those endpoints fail closed.
    DBTOOL_API_KEY: str = ""
    # Secret used to encrypt newly stored datasource credentials at rest.
    # Existing plaintext rows remain readable for backward compatibility.
    DBTOOL_SECRET_KEY: str = ""
    # Bounded schema metadata/background task queue. Prevents request bursts from
    # spawning unbounded asyncio tasks inside the API process.
    SCHEMA_TASK_WORKER_COUNT: int = 2
    SCHEMA_TASK_MAX_QUEUE: int = 50

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"

settings = Settings()

# Ensure directories and migrate legacy DB once if needed
os.makedirs(BASE_DIR / "data", exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

if DEFAULT_DB_PATH != LEGACY_DB_PATH and LEGACY_DB_PATH.exists() and not DEFAULT_DB_PATH.exists():
    try:
        shutil.copy2(LEGACY_DB_PATH, DEFAULT_DB_PATH)
    except Exception:
        pass
