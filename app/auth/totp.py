import base64
import io

import pyotp
import qrcode


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def build_totp_uri(*, secret: str, email: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp_code(*, secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def build_totp_qr_base64(*, otpauth_url: str) -> str:
    image = qrcode.make(otpauth_url)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
