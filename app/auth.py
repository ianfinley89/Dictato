from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import HTTPException, Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from app.config import SECRET_KEY, SESSION_COOKIE_NAME

_ph = PasswordHasher()
_signer = URLSafeTimedSerializer(SECRET_KEY, salt="dictato-session")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except (VerifyMismatchError, VerificationError):
        return False


def make_session_token(user_id: int) -> str:
    return _signer.dumps(user_id)


def decode_session_token(token: str, max_age_days: int = 30) -> int | None:
    try:
        return _signer.loads(token, max_age=max_age_days * 86400)
    except (BadSignature, SignatureExpired):
        return None


def get_current_user_id(request: Request) -> int:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    uid = decode_session_token(token)
    if uid is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return uid
