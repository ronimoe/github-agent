"""ghapp — mint a GitHub App installation token (issue #3).

The bot App is the sole privileged identity and sole trunk writer. Inside GitHub Actions, prefer
the official `actions/create-github-app-token` action (see `.github/workflows/conductor-tick.yml`).
This module is the dependency-free local/CLI path: it builds an RS256 JWT signed by `openssl`
(no Python crypto dependency) and exchanges it for an installation token via `curl`.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def build_signing_input(app_id, iat: int, exp: int) -> str:
    """The `header.payload` portion of the App JWT (RS256), base64url-encoded."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iat": iat, "exp": exp, "iss": str(app_id)},
                                 separators=(",", ":")).encode())
    return f"{header}.{payload}"


def sign_rs256(signing_input: str, private_key_pem_path: str) -> str:
    """RS256 signature via openssl (base64url). No Python crypto dependency."""
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", private_key_pem_path],
                       input=signing_input.encode(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"openssl sign failed: {p.stderr.decode().strip()}")
    return _b64url(p.stdout)


def app_jwt(app_id, private_key_pem_path: str, now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    si = build_signing_input(app_id, now - 60, now + 540)   # 9 min, clock-skew tolerant
    return f"{si}.{sign_rs256(si, private_key_pem_path)}"


def installation_token(app_id, private_key_pem_path: str, installation_id, now: int | None = None) -> str:
    jwt = app_jwt(app_id, private_key_pem_path, now)
    p = subprocess.run(
        ["curl", "-sS", "-X", "POST",
         "-H", f"Authorization: Bearer {jwt}",
         "-H", "Accept: application/vnd.github+json",
         "-H", "X-GitHub-Api-Version: 2022-11-28",
         f"https://api.github.com/app/installations/{installation_id}/access_tokens"],
        capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"token request failed: {p.stderr.strip()}")
    data = json.loads(p.stdout)
    if "token" not in data:
        raise RuntimeError(f"no token in response: {p.stdout[:300]}")
    return data["token"]
