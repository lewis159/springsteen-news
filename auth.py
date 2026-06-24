import os
import time
import json
import functools

import requests
import jwt
from flask import request, redirect, url_for

JWKS_CACHE: dict = {
    "keys": [],
    "fetched_at": 0,
}
JWKS_TTL = 3600  # seconds


def _get_jwks() -> list:
    now = int(time.time())
    if JWKS_CACHE["keys"] and (now - JWKS_CACHE["fetched_at"]) < JWKS_TTL:
        return JWKS_CACHE["keys"]

    clerk_domain = os.environ["CLERK_DOMAIN"]
    url = f"https://clerk.{clerk_domain}/.well-known/jwks.json"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    keys = response.json().get("keys", [])

    JWKS_CACHE["keys"] = keys
    JWKS_CACHE["fetched_at"] = now
    return keys


def verify_clerk_token(token: str) -> dict | None:
    keys = _get_jwks()
    for key in keys:
        try:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                options={"verify_exp": True},
            )
            return payload
        except Exception:
            continue
    return None


def get_current_user(req) -> dict | None:
    cookie = req.cookies.get("__session")
    if not cookie:
        return None
    return verify_clerk_token(cookie)


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user(request)
        if user is None:
            return "Unauthorized", 401
        return f(*args, user=user, **kwargs)
    return decorated
