"""Generate a VAPID keypair for Web Push. Paste the output into .env.

    uv run python scripts/gen_vapid.py
"""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def main() -> None:
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_bytes = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    print("VAPID_PRIVATE_KEY=" + b64url(priv_bytes))
    print("VAPID_PUBLIC_KEY=" + b64url(pub_bytes))
    print("VAPID_SUBJECT=mailto:you@example.com")


if __name__ == "__main__":
    main()
