"""Reset a user's password directly (admin/self-hosted use — no email flow exists).

    uv run python scripts/reset_password.py <email> <new_password>
"""
import sys

sys.path.insert(0, ".")

from app.auth import hash_password
from app.database import get_conn


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: uv run python scripts/reset_password.py <email> <new_password>")
        sys.exit(1)

    email, new_password = sys.argv[1], sys.argv[2]
    if len(new_password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)

    pw_hash = hash_password(new_password)
    with get_conn() as conn:
        row = conn.execute("SELECT id, display_name FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            print(f"No user found with email {email}")
            sys.exit(1)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, row["id"]))

    print(f"Password reset for {row['display_name']} ({email}).")


if __name__ == "__main__":
    main()
