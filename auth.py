import jwt
import os
from datetime import datetime, timedelta

SECRET = os.getenv('JWT_SECRET', 'changeme')


def create_token(user_id: int) -> str:
    payload = {
        'sub': user_id,
        'exp': datetime.utcnow() + timedelta(hours=24),
        'iat': datetime.utcnow()
    }
    return jwt.encode(payload, SECRET, algorithm='HS256')


def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        raise ValueError('Token expired')
    except jwt.InvalidTokenError:
        raise ValueError('Invalid token')


def refresh_token(token: str) -> str:
    payload = verify_token(token)
    return create_token(payload['sub'])