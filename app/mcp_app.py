from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid

from fastapi import FastAPI, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from fastmcp import FastMCP

import requests
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent
from app.skills_registry import (
    list_skill_names,
    load_all_skills_markdown,
    read_skill_markdown,
    search_skills,
)

mcp = FastMCP("skills-agent-skills")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def _jwt_sign(payload: dict, key: bytes) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"


def _jwt_verify(token: str, key: bytes) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid_token")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected_sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(parts[2])
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("invalid_token")
    payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    exp = payload.get("exp")
    if exp is not None and int(exp) < int(time.time()):
        raise ValueError("expired_token")
    return payload


def _signing_key() -> bytes:
    key = os.environ.get("MCP_OAUTH_SIGNING_KEY")
    if key:
        return key.encode("utf-8")
    shared = os.environ.get("MCP_OAUTH_SHARED_SECRET")
    if shared:
        return hashlib.sha256(shared.encode("utf-8")).digest()
    raise RuntimeError("oauth_not_configured")


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _resource_metadata_url(request: Request) -> str:
    return f"{_base_url(request)}/.well-known/oauth-protected-resource/mcp"


def _unauthorized(request: Request) -> Response:
    return Response(
        content=json.dumps({"error": "unauthorized"}).encode("utf-8"),
        media_type="application/json",
        status_code=401,
        headers={
            "WWW-Authenticate": f'Bearer realm="mcp", resource_metadata="{_resource_metadata_url(request)}"'
        },
    )


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _b64url_encode(digest)


def _oauth_google_client_id() -> str:
    value = os.environ.get("MCP_OAUTH_GOOGLE_CLIENT_ID", "").strip()
    if not value:
        raise RuntimeError("oauth_not_configured")
    return value


def _oauth_google_client_secret() -> str:
    value = os.environ.get("MCP_OAUTH_GOOGLE_CLIENT_SECRET", "").strip()
    if not value:
        raise RuntimeError("oauth_not_configured")
    return value


def _oauth_allowed_domain() -> str | None:
    value = os.environ.get("MCP_OAUTH_ALLOWED_DOMAIN", "").strip()
    return value or None


def _oauth_allowed_emails() -> set[str] | None:
    raw = os.environ.get("MCP_OAUTH_ALLOWED_EMAILS", "").strip()
    if not raw:
        return None
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _cursor_redirect_uri() -> str:
    return "cursor://anysphere.cursor-mcp/oauth/callback"


def _google_redirect_uri(request: Request) -> str:
    return f"{_base_url(request)}/oauth/google/callback"


def _google_authorize_url(request: Request, *, state: str) -> str:
    params = {
        "client_id": _oauth_google_client_id(),
        "redirect_uri": _google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def _google_exchange_code(request: Request, code: str) -> dict:
    token_endpoint = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": _oauth_google_client_id(),
        "client_secret": _oauth_google_client_secret(),
        "redirect_uri": _google_redirect_uri(request),
        "grant_type": "authorization_code",
    }
    resp = requests.post(token_endpoint, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _verify_google_id_token(raw_id_token: str) -> dict:
    request_adapter = google_auth_requests.Request()
    return id_token.verify_oauth2_token(
        raw_id_token,
        request_adapter,
        audience=_oauth_google_client_id(),
    )


def _run_agent(prompt: str) -> str:
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="mcp", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    message = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])

    events = list(
        runner.run(
            new_message=message,
            user_id="mcp",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )

    parts: list[str] = []
    for event in events:
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.text:
                parts.append(part.text)

    return "".join(parts).strip()


def _execute_skill(tool_name: str, request: str) -> str:
    skill_md = read_skill_markdown(tool_name)
    if not skill_md:
        raise ValueError(f"Unknown skill: {tool_name}")

    prompt = (
        f"Skill: {tool_name}\n"
        f"Invocation ID: {uuid.uuid4()}\n\n"
        "Follow the skill instructions exactly.\n\n"
        "Skill instructions:\n"
        f"{skill_md}\n\n"
        "User request:\n"
        f"{request}\n"
    )
    return _run_agent(prompt)


@mcp.tool()
def list_skills() -> list[str]:
    return list_skill_names()


@mcp.tool()
def get_skill(tool_name: str) -> str:
    content = read_skill_markdown(tool_name)
    if not content:
        raise ValueError(f"Unknown skill: {tool_name}")
    return content


@mcp.tool()
def search_skill_docs(query: str, max_results: int = 20) -> list[dict]:
    return search_skills(query, max_results=max_results)


@mcp.tool()
def get_all_skills() -> str:
    return load_all_skills_markdown()


@mcp.resource("skills://list")
def skills_list_resource() -> str:
    names = list_skill_names()
    return "\n".join(names)


@mcp.resource("skills://{tool_name}")
def skill_resource(tool_name: str) -> str:
    content = read_skill_markdown(tool_name)
    if not content:
        raise ValueError(f"Unknown skill: {tool_name}")
    return content


_RESERVED_TOOL_NAMES = {
    "list_skills",
    "get_skill",
    "search_skill_docs",
    "get_all_skills",
}

for _tool_name in list_skill_names():
    if _tool_name in _RESERVED_TOOL_NAMES:
        continue

    def _make_tool(tool_name: str):
        def _tool(request: str) -> str:
            return _execute_skill(tool_name, request)

        _tool.__name__ = tool_name
        _tool.__annotations__ = {"request": str, "return": str}
        return mcp.tool(name=tool_name)(_tool)

    _make_tool(_tool_name)


mcp_asgi = mcp.http_app(path="/", transport="streamable-http", stateless_http=True)

app = FastAPI(lifespan=mcp_asgi.lifespan)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource(request: Request) -> JSONResponse:
    base = _base_url(request)
    return JSONResponse(
        {
            "resource": f"{base}/mcp/",
            "authorization_servers": [base],
        }
    )


@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: Request) -> JSONResponse:
    base = _base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp", "offline_access"],
        }
    )


@app.post("/register")
async def oauth_register(request: Request) -> JSONResponse:
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or body.get("redirect_uris".upper()) or []
    if not isinstance(redirect_uris, list):
        redirect_uris = []

    allowed_redirect = _cursor_redirect_uri()
    if allowed_redirect not in redirect_uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "unsupported redirect_uri"},
            status_code=400,
        )

    fingerprint = hashlib.sha256(("|".join(sorted(redirect_uris))).encode("utf-8")).hexdigest()
    client_id = f"cursor-{fingerprint[:24]}"

    return JSONResponse(
        {
            "client_id": client_id,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "redirect_uris": redirect_uris,
            "client_id_issued_at": int(time.time()),
        }
    )


@app.get("/authorize")
async def oauth_authorize(request: Request) -> Response:
    qp = request.query_params
    response_type = qp.get("response_type")
    client_id = qp.get("client_id")
    redirect_uri = qp.get("redirect_uri")
    state = qp.get("state", "")
    scope = qp.get("scope", "mcp")
    code_challenge = qp.get("code_challenge")
    code_challenge_method = qp.get("code_challenge_method", "S256")

    if response_type != "code" or not client_id or not redirect_uri:
        return HTMLResponse("Invalid request", status_code=400)

    if redirect_uri != _cursor_redirect_uri():
        return HTMLResponse("Invalid redirect_uri", status_code=400)

    if code_challenge_method != "S256" or not code_challenge:
        return HTMLResponse("PKCE required", status_code=400)

    now = int(time.time())
    oauth_state_payload = {
        "typ": "google_oauth_state",
        "iat": now,
        "exp": now + 10 * 60,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "cursor_state": state,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    signed_state = _jwt_sign(oauth_state_payload, _signing_key())
    return RedirectResponse(url=_google_authorize_url(request, state=signed_state), status_code=302)


@app.get("/oauth/google/callback")
async def oauth_google_callback(request: Request) -> Response:
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(f"OAuth error: {error}", status_code=400)

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return HTMLResponse("Missing code/state", status_code=400)

    try:
        state_payload = _jwt_verify(state, _signing_key())
    except Exception:
        return HTMLResponse("Invalid state", status_code=400)

    if state_payload.get("typ") != "google_oauth_state":
        return HTMLResponse("Invalid state", status_code=400)

    try:
        token_response = _google_exchange_code(request, code)
        google_id_token = token_response.get("id_token")
        if not google_id_token:
            return HTMLResponse("Missing id_token", status_code=400)
        claims = _verify_google_id_token(google_id_token)
    except Exception:
        return HTMLResponse("OAuth token exchange failed", status_code=400)

    email = str(claims.get("email") or "").lower()
    if not email:
        return HTMLResponse("Missing email", status_code=400)

    allowed_emails = _oauth_allowed_emails()
    if allowed_emails is not None and email not in allowed_emails:
        return HTMLResponse("Forbidden", status_code=403)

    allowed_domain = _oauth_allowed_domain()
    if allowed_domain is not None and not email.endswith(f"@{allowed_domain.lower()}"):
        return HTMLResponse("Forbidden", status_code=403)

    now = int(time.time())
    auth_code_payload = {
        "typ": "auth_code",
        "iat": now,
        "exp": now + 300,
        "client_id": state_payload.get("client_id"),
        "redirect_uri": state_payload.get("redirect_uri"),
        "scope": state_payload.get("scope", "mcp"),
        "code_challenge": state_payload.get("code_challenge"),
        "code_challenge_method": state_payload.get("code_challenge_method", "S256"),
        "email": email,
    }
    auth_code = _jwt_sign(auth_code_payload, _signing_key())

    redirect_params = {"code": auth_code}
    cursor_state = state_payload.get("cursor_state") or ""
    if cursor_state:
        redirect_params["state"] = cursor_state

    return RedirectResponse(
        url=f"{_cursor_redirect_uri()}?{urllib.parse.urlencode(redirect_params)}",
        status_code=302,
    )


@app.post("/token")
async def oauth_token(request: Request) -> JSONResponse:
    body = (await request.body()).decode("utf-8", errors="replace")
    form = urllib.parse.parse_qs(body)

    grant_type = (form.get("grant_type") or [""])[0]
    client_id = (form.get("client_id") or [""])[0]

    key = _signing_key()
    now = int(time.time())

    if grant_type == "authorization_code":
        code = (form.get("code") or [""])[0]
        redirect_uri = (form.get("redirect_uri") or [""])[0]
        code_verifier = (form.get("code_verifier") or [""])[0]

        try:
            code_payload = _jwt_verify(code, key)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        if code_payload.get("typ") != "auth_code":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        if code_payload.get("client_id") != client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        if code_payload.get("redirect_uri") != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        if not code_verifier:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        expected = code_payload.get("code_challenge")
        if expected != _pkce_challenge(code_verifier):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        scope = code_payload.get("scope", "mcp")
        access_payload = {
            "typ": "access",
            "iat": now,
            "exp": now + 3600,
            "client_id": client_id,
            "scope": scope,
            "email": code_payload.get("email"),
        }
        access_token = _jwt_sign(access_payload, key)

        refresh_payload = {
            "typ": "refresh",
            "iat": now,
            "exp": now + 30 * 24 * 3600,
            "client_id": client_id,
            "scope": scope,
            "email": code_payload.get("email"),
        }
        refresh_token = _jwt_sign(refresh_payload, key)

        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": refresh_token,
                "scope": scope,
            }
        )

    if grant_type == "refresh_token":
        refresh_token = (form.get("refresh_token") or [""])[0]
        try:
            refresh_payload = _jwt_verify(refresh_token, key)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        if refresh_payload.get("typ") != "refresh":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        if refresh_payload.get("client_id") != client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        scope = refresh_payload.get("scope", "mcp")
        access_payload = {
            "typ": "access",
            "iat": now,
            "exp": now + 3600,
            "client_id": client_id,
            "scope": scope,
            "email": refresh_payload.get("email"),
        }
        access_token = _jwt_sign(access_payload, key)

        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": refresh_token,
                "scope": scope,
            }
        )

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@app.middleware("http")
async def oauth_protect_mcp(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return _unauthorized(request)
        token = auth.split(" ", 1)[1].strip()
        try:
            payload = _jwt_verify(token, _signing_key())
        except Exception:
            return _unauthorized(request)
        if payload.get("typ") != "access":
            return _unauthorized(request)
    return await call_next(request)

@app.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
async def mcp_redirect() -> RedirectResponse:
    return RedirectResponse(url="/mcp/", status_code=307)


app.mount("/mcp", mcp_asgi)
