import os
from dataclasses import dataclass


@dataclass
class Config:
    jwt_secret: str = os.getenv("JWT_SECRET", "dev-secret-change-in-prod")
    jwt_algorithm: str = "HS256"
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 604800
    db_url: str = os.getenv("DATABASE_URL", "sqlite:///app.db")


config = Config()