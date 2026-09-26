from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class User:
    id: int
    email: str
    password_hash: str
    created_at: datetime
    last_login: Optional[datetime] = None
    is_active: bool = True


@dataclass
class RefreshToken:
    token: str
    user_id: int
    expires_at: datetime
    revoked: bool = False