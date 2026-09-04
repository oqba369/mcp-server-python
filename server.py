from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    Response,
    RedirectResponse,
    HTMLResponse,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build

import hmac
import html
import os


MCP_API_TOKEN = os.environ.get("MCP_API_TOKEN")
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REDIRECT_URI = os.environ.get("YOUTUBE_REDIRECT_URI")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
]


mcp = FastMCP(
    "youtube-mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(RENDER_EXTERNAL_HOSTNAME),
        allowed_hosts=[RENDER_EXTERNAL_HOSTNAME]
        if RENDER_EXTERNAL_HOSTNAME
        else [],
    ),
)


def _oauth_config() -> dict:
    if not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET or not YOUTUBE_REDIRECT_URI:
        raise RuntimeError(
            "Missing YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, "
            "or YOUTUBE_REDIRECT_URI."
        )

    return {
        "web": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [YOUTUBE_REDIRECT_URI],
        }
    }


def _oauth_flow(state: str | None = None) -> Flow:
    flow = Flow.from_client_config(
        _oauth_config(),
        scopes=YOUTUBE_SCOPES,
        state=state,
    )

    flow.redirect_uri = YOUTUBE_REDIRECT_URI
    return flow


def _youtube_credentials() -> Credentials:
    if not YOUTUBE_REFRESH_TOKEN:
        raise RuntimeError(
            "YouTube is not authorized yet. "
            "Open /oauth/start and authorize your account."
        )

    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=YOUTUBE_SCOPES,
    )

    credentials.refresh(GoogleAuthRequest())

    return credentials


# ============================================================
# MCP TOOLS
# ============================================================

@mcp.tool()
def hello(name: str) -> str:
    """Say hello to someone."""
    return f"Hello, {name}!"


@mcp.tool()
def youtube_auth_status() -> dict:
    """Check whether YouTube OAuth is configured."""

    return {
        "client_configured": bool(
            YOUTUBE_CLIENT_ID
            and YOUTUBE_CLIENT_SECRET
            and YOUTUBE_REDIRECT_URI
        ),
        "authorized": bool(YOUTUBE_REFRESH_TOKEN),
        "scope": YOUTUBE_SCOPES,
    }


@mcp.tool()
def youtube_my_channel() -> dict:
    """Return information about the authenticated YouTube channel."""

    youtube = build(
        "youtube",
        "v3",
        credentials=_youtube_credentials(),
        cache_discovery=False,
    )

    result = (
        youtube.channels()
        .list(
            part="snippet,statistics,contentDetails",
            mine=True,
        )
        .execute()
    )

    items = result.get("items", [])

    if not items:
        return {
            "found": False,
            "message": "No YouTube channel found.",
        }

    channel = items[0]

    snippet = channel.get("snippet", {})
    statistics = channel.get("statistics", {})
    playlists = (
        channel
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
    )

    return {
        "found": True,
        "id": channel.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "custom_url": snippet.get("customUrl"),
        "published_at": snippet.get("publishedAt"),
        "statistics": statistics,
        "uploads_playlist_id": playlists.get("uploads"),
    }


# ============================================================
# HEALTH
# ============================================================

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "service": "youtube-mcp",
            "youtube_client_configured": bool(
                YOUTUBE_CLIENT_ID
                and YOUTUBE_CLIENT_SECRET
                and YOUTUBE_REDIRECT_URI
            ),
            "youtube_authorized": bool(YOUTUBE_REFRESH_TOKEN),
        }
    )


# ============================================================
# YOUTUBE OAUTH START
# ============================================================

@mcp.custom_route("/oauth/start", methods=["GET"])
async def oauth_start(request: Request) -> Response:

    try:
        flow = _oauth_flow()

    except RuntimeError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=500,
        )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    response = RedirectResponse(
        authorization_url,
        status_code=302,
    )

    response.set_cookie(
        "youtube_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
    )

    return response


# ============================================================
# YOUTUBE OAUTH CALLBACK
# ============================================================

@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> Response:

    oauth_error = request.query_params.get("error")

    if oauth_error:
        return HTMLResponse(
            f"<h2>Authorization failed</h2>"
            f"<p>{html.escape(oauth_error)}</p>",
            status_code=400,
        )

    state = request.query_params.get("state")

    expected_state = request.cookies.get(
        "youtube_oauth_state"
    )

    if (
        not state
        or not expected_state
        or not hmac.compare_digest(
            state,
            expected_state,
        )
    ):
        return HTMLResponse(
            "<h2>Invalid OAuth state.</h2>"
            "<p>Start again from /oauth/start.</p>",
            status_code=400,
        )

    code = request.query_params.get("code")

    if not code:
        return HTMLResponse(
            "<h2>Missing authorization code.</h2>",
            status_code=400,
        )

    try:
        flow = _oauth_flow(state=state)

        flow.fetch_token(
            code=code
        )

        refresh_token = (
            flow.credentials.refresh_token
        )

    except Exception as exc:
        return HTMLResponse(
            "<h2>Token exchange failed.</h2>"
            f"<pre>{html.escape(str(exc))}</pre>",
            status_code=500,
        )

    if not refresh_token:
        return HTMLResponse(
            "<h2>No refresh token returned.</h2>"
            "<p>Open /oauth/start and authorize again.</p>",
            status_code=500,
        )

    safe_token = html.escape(
        refresh_token
    )

    response = HTMLResponse(
        """
        <h2>YouTube authorization succeeded ✅</h2>

        <p>
        Copy the value below into Render as an
        environment variable named:
        </p>

        <h3>YOUTUBE_REFRESH_TOKEN</h3>

        <textarea style="width:95%;height:120px;">
        """
        + safe_token
        + """
        </textarea>

        <p>
        <b>
        Keep this token secret.
        Do not paste it into ChatGPT or GitHub.
        </b>
        </p>

        <p>
        After saving it in Render,
        redeploy the service.
        </p>
        """
    )

    response.delete_cookie(
        "youtube_oauth_state"
    )

    response.headers[
        "Cache-Control"
    ] = "no-store"

    return response


# ============================================================
# MCP AUTH
# ============================================================

class BearerAuthMiddleware:

    def __init__(
        self,
        app: ASGIApp,
    ) -> None:

        self.app = app


    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ):

        public_paths = {
            "/health",
            "/oauth/start",
            "/oauth/callback",
        }

        if (
            scope["type"] != "http"
            or scope["path"] in public_paths
        ):

            await self.app(
                scope,
                receive,
                send,
            )

            return

        headers = dict(
            scope.get(
                "headers",
                [],
            )
        )

        auth = headers.get(
            b"authorization",
            b"",
        ).decode()

        if hmac.compare_digest(
            auth,
            f"Bearer {MCP_API_TOKEN}",
        ):

            await self.app(
                scope,
                receive,
                send,
            )

            return

        response = JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": "Unauthorized",
                },
                "id": None,
            },
            status_code=401,
        )

        await response(
            scope,
            receive,
            send,
        )


def create_app():

    app = mcp.streamable_http_app()

    if MCP_API_TOKEN:
        app.add_middleware(
            BearerAuthMiddleware
        )

    return app


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    import uvicorn

    if not MCP_API_TOKEN:
        print(
            "WARNING: MCP_API_TOKEN is not set. "
            "The MCP endpoint is unauthenticated."
        )

    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=port,
    )
