import os

from dotenv import load_dotenv

load_dotenv()

LLM_URL = os.environ["LLM_URL"]
LLM_MODEL = os.environ["LLM_MODEL"]
TEI_URL = os.environ["TEI_URL"].rstrip("/")
QDRANT_URL = os.environ["QDRANT_URL"]
REDIS_URL = os.environ["REDIS_URL"]
AUTH_JWKS_URL = os.getenv("AUTH_JWKS_URL")
AUTH_ISSUER = os.getenv("AUTH_ISSUER")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE")
APP_ENV = os.getenv("APP_ENV", "production").lower()
AUTH_DEV_MODE = os.getenv("AUTH_DEV_MODE", "false").lower() == "true"
AUTH_DEV_TOKEN = os.getenv("AUTH_DEV_TOKEN")
AUTH_DEV_TENANT_ID = os.getenv("AUTH_DEV_TENANT_ID", "local-dev-tenant")
AUTH_DEV_ROLE = os.getenv("AUTH_DEV_ROLE", "SecOps_Lead")