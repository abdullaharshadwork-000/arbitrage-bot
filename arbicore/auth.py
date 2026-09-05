"""Small, dependency-free authentication primitives (RFC 6238 TOTP)."""

import base64
import hashlib
import hmac
import secrets
import struct
import time


def new_totp_secret(bytes_count=20):
    return base64.b32encode(secrets.token_bytes(bytes_count)).decode("ascii").rstrip("=")


def totp(secret, moment=None, interval=30, digits=6):
    padded = str(secret).upper() + "=" * ((8 - len(str(secret)) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = int(time.time() if moment is None else moment) // int(interval)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF)
    return str(number % (10 ** digits)).zfill(digits)


def verify_totp(secret, code, moment=None, window=1):
    now = time.time() if moment is None else float(moment)
    supplied = str(code or "").strip()
    return any(hmac.compare_digest(totp(secret, now + step * 30), supplied)
               for step in range(-int(window), int(window) + 1))


def recovery_codes(count=8):
    return [secrets.token_hex(4).upper() for _ in range(count)]


def hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()
