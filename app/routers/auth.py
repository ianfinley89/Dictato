from fastapi import APIRouter, HTTPException, Response, Request
from app.models import RegisterRequest, LoginRequest, GoalsUpdate
from app.auth import hash_password, verify_password, make_session_token, get_current_user_id
from app.database import get_conn
from app.config import SESSION_COOKIE_NAME, SECURE_COOKIES

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
async def register(req: RegisterRequest, response: Response):
    with get_conn() as conn:
        if conn.execute("SELECT id FROM users WHERE email=?", (req.email,)).fetchone():
            raise HTTPException(400, "Email already registered")
        pw_hash = hash_password(req.password)
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
            (req.email, pw_hash, req.display_name),
        )
        user_id = cur.lastrowid
    _set_session(response, user_id)
    return {"user_id": user_id, "display_name": req.display_name}


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, display_name FROM users WHERE email=?", (req.email,)
        ).fetchone()
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    _set_session(response, row["id"])
    return {"user_id": row["id"], "display_name": row["display_name"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, calorie_goal, protein_g, carbs_g, fat_g FROM users WHERE id=?",
            (uid,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return dict(row)


@router.put("/goals")
async def update_goals(req: GoalsUpdate, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET calorie_goal=?, protein_g=?, carbs_g=?, fat_g=? WHERE id=?",
            (req.calorie_goal, req.protein_g, req.carbs_g, req.fat_g, uid),
        )
        row = conn.execute(
            "SELECT id, email, display_name, calorie_goal, protein_g, carbs_g, fat_g FROM users WHERE id=?",
            (uid,),
        ).fetchone()
    return dict(row)


def _set_session(response: Response, user_id: int) -> None:
    token = make_session_token(user_id)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
        max_age=30 * 86400,
    )
