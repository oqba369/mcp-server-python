import os
import hmac
import html

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


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

MCP_API_TOKEN = os.environ.get("MCP_API_TOKEN")

RENDER_EXTERNAL_HOSTNAME = os.environ.get(
    "RENDER_EXTERNAL_HOSTNAME"
)

YOUTUBE_CLIENT_ID = os.environ.get(
    "YOUTUBE_CLIENT_ID"
)

YOUTUBE_CLIENT_SECRET = os.environ.get(
    "YOUTUBE_CLIENT_SECRET"
)

YOUTUBE_REDIRECT_URI = os.environ.get(
    "YOUTUBE_REDIRECT_URI"
)

YOUTUBE_REFRESH_TOKEN = os.environ.get(
    "YOUTUBE_REFRESH_TOKEN"
)


# ============================================================
# YOUTUBE OAUTH SCOPES
# ============================================================

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
]


# ============================================================
# MCP SERVER
# ============================================================

mcp = FastMCP(
    "youtube-mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(
            RENDER_EXTERNAL_HOSTNAME
        ),
        allowed_hosts=(
            [RENDER_EXTERNAL_HOSTNAME]
            if RENDER_EXTERNAL_HOSTNAME
            else []
        ),
    ),
)


# ============================================================
# GOOGLE OAUTH CONFIG
# ============================================================

def _oauth_config() -> dict:

    if not YOUTUBE_CLIENT_ID:
        raise RuntimeError(
            "Missing YOUTUBE_CLIENT_ID."
        )

    if not YOUTUBE_CLIENT_SECRET:
        raise RuntimeError(
            "Missing YOUTUBE_CLIENT_SECRET."
        )

    if not YOUTUBE_REDIRECT_URI:
        raise RuntimeError(
            "Missing YOUTUBE_REDIRECT_URI."
        )

    return {
        "web": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": (
                "https://accounts.google.com/o/oauth2/auth"
            ),
            "token_uri": (
                "https://oauth2.googleapis.com/token"
            ),
            "redirect_uris": [
                YOUTUBE_REDIRECT_URI
            ],
        }
    }


# ============================================================
# CREATE GOOGLE OAUTH FLOW
# ============================================================

def _oauth_flow(
    state: str | None = None,
) -> Flow:

    flow = Flow.from_client_config(
        _oauth_config(),
        scopes=YOUTUBE_SCOPES,
        state=state,

        # IMPORTANT:
        # Disable automatic PKCE because otherwise
        # the callback requires the original verifier.
        autogenerate_code_verifier=False,
    )

    flow.redirect_uri = (
        YOUTUBE_REDIRECT_URI
    )

    return flow


# ============================================================
# YOUTUBE CREDENTIALS
# ============================================================

def _youtube_credentials() -> Credentials:

    if not YOUTUBE_REFRESH_TOKEN:
        raise RuntimeError(
            "YouTube has not been authorized yet. "
            "Open /oauth/start first."
        )

    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=YOUTUBE_SCOPES,
    )

    credentials.refresh(
        GoogleAuthRequest()
    )

    return credentials


# ============================================================
# MCP TOOL: HELLO
# ============================================================

@mcp.tool()
def hello(name: str) -> str:
    """
    Simple MCP connectivity test.
    """

    return f"Hello, {name}!"


# ============================================================
# MCP TOOL: YOUTUBE AUTH STATUS
# ============================================================

@mcp.tool()
def youtube_auth_status() -> dict:
    """
    Check whether YouTube OAuth is configured
    and authorized.
    """

    return {
        "client_configured": bool(
            YOUTUBE_CLIENT_ID
            and YOUTUBE_CLIENT_SECRET
            and YOUTUBE_REDIRECT_URI
        ),
        "authorized": bool(
            YOUTUBE_REFRESH_TOKEN
        ),
        "scope": YOUTUBE_SCOPES,
    }


# ============================================================
# MCP TOOL: MY YOUTUBE CHANNEL
# ============================================================

@mcp.tool()
def youtube_my_channel() -> dict:
    """
    Return information about the authenticated
    YouTube channel.
    """

    youtube = build(
        "youtube",
        "v3",
        credentials=_youtube_credentials(),
        cache_discovery=False,
    )

    result = (
        youtube.channels()
        .list(
            part=(
                "snippet,"
                "statistics,"
                "contentDetails"
            ),
            mine=True,
        )
        .execute()
    )

    items = result.get(
        "items",
        [],
    )

    if not items:
        return {
            "found": False,
            "message": (
                "No YouTube channel was found "
                "for this Google account."
            ),
        }

    channel = items[0]

    snippet = channel.get(
        "snippet",
        {},
    )

    statistics = channel.get(
        "statistics",
        {},
    )

    playlists = (
        channel
        .get(
            "contentDetails",
            {},
        )
        .get(
            "relatedPlaylists",
            {},
        )
    )

    return {
        "found": True,
        "id": channel.get("id"),
        "title": snippet.get(
            "title"
        ),
        "description": snippet.get(
            "description"
        ),
        "custom_url": snippet.get(
            "customUrl"
        ),
        "published_at": snippet.get(
            "publishedAt"
        ),
        "statistics": statistics,
        "uploads_playlist_id": (
            playlists.get("uploads")
        ),
    }


# ============================================================
# HEALTH ROUTE
# ============================================================

@mcp.custom_route(
    "/health",
    methods=["GET"],
)
async def health(
    request: Request,
) -> Response:

    return JSONResponse(
        {
            "status": "ok",
            "service": "youtube-mcp",

            "youtube_client_configured": bool(
                YOUTUBE_CLIENT_ID
                and YOUTUBE_CLIENT_SECRET
                and YOUTUBE_REDIRECT_URI
            ),

            "youtube_authorized": bool(
                YOUTUBE_REFRESH_TOKEN
            ),
        }
    )


# ============================================================
# OAUTH START
# ============================================================

@mcp.custom_route(
    "/oauth/start",
    methods=["GET"],
)
async def oauth_start(
    request: Request,
) -> Response:

    try:
        flow = _oauth_flow()

    except Exception as exc:

        return JSONResponse(
            {
                "error": str(exc)
            },
            status_code=500,
        )

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",

            include_granted_scopes=(
                "true"
            ),

            # Forces Google to issue a refresh
            # token during authorization.
            prompt="consent",
        )
    )

    response = RedirectResponse(
        authorization_url,
        status_code=302,
    )

    # Save OAuth state temporarily
    # for CSRF protection.
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
# OAUTH CALLBACK
# ============================================================

@mcp.custom_route(
    "/oauth/callback",
    methods=["GET"],
)
async def oauth_callback(
    request: Request,
) -> Response:

    # --------------------------------------------------------
    # HANDLE GOOGLE ERROR
    # --------------------------------------------------------

    oauth_error = (
        request.query_params.get(
            "error"
        )
    )

    if oauth_error:

        return HTMLResponse(
            (
                "<h2>Authorization failed</h2>"
                f"<p>{html.escape(oauth_error)}</p>"
            ),
            status_code=400,
        )


    # --------------------------------------------------------
    # VERIFY STATE
    # --------------------------------------------------------

    state = (
        request.query_params.get(
            "state"
        )
    )

    expected_state = (
        request.cookies.get(
            "youtube_oauth_state"
        )
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
            """
            <h2>Invalid OAuth state.</h2>
            <p>
            Please start authorization again from:
            <br>
            <a href="/oauth/start">
            /oauth/start
            </a>
            </p>
            """,
            status_code=400,
        )


    # --------------------------------------------------------
    # GET AUTHORIZATION CODE
    # --------------------------------------------------------

    code = (
        request.query_params.get(
            "code"
        )
    )

    if not code:

        return HTMLResponse(
            """
            <h2>
            Missing authorization code.
            </h2>
            """,
            status_code=400,
        )


    # --------------------------------------------------------
    # EXCHANGE CODE FOR TOKENS
    # --------------------------------------------------------

    try:

        flow = _oauth_flow(
            state=state
        )

        flow.fetch_token(
            code=code
        )

        refresh_token = (
            flow.credentials
            .refresh_token
        )

    except Exception as exc:

        return HTMLResponse(
            (
                "<h2>"
                "Token exchange failed."
                "</h2>"
                "<pre>"
                f"{html.escape(str(exc))}"
                "</pre>"
            ),
            status_code=500,
        )


    # --------------------------------------------------------
    # VERIFY REFRESH TOKEN
    # --------------------------------------------------------

    if not refresh_token:

        return HTMLResponse(
            """
            <h2>
            No refresh token returned.
            </h2>

            <p>
            Start the authorization process
            again:
            </p>

            <a href="/oauth/start">
            /oauth/start
            </a>
            """,
            status_code=500,
        )


    # --------------------------------------------------------
    # DISPLAY TOKEN FOR USER TO SAVE IN RENDER
    # --------------------------------------------------------

    safe_token = html.escape(
        refresh_token
    )

    response = HTMLResponse(
        """
        <!DOCTYPE html>
        <html>

        <head>
            <title>
                YouTube MCP Authorization
            </title>
        </head>

        <body>

        <h2>
        YouTube authorization succeeded ✅
        </h2>

        <p>
        Copy the token below and save it
        in Render as:
        </p>

        <h3>
        YOUTUBE_REFRESH_TOKEN
        </h3>

        <textarea
            style="
                width:95%;
                height:140px;
                font-family:monospace;
            "
        >"""
        + safe_token
        + """</textarea>

        <p>
        <strong>
        Keep this token secret.
        </strong>
        </p>

        <p>
        Do NOT paste it into ChatGPT,
        GitHub, or anywhere public.
        </p>

        <p>
        Render:
        <br>
        Environment
        →
        Add Environment Variable
        </p>

        <p>
        Name:
        <code>
        YOUTUBE_REFRESH_TOKEN
        </code>
        </p>

        <p>
        Then save and redeploy.
        </p>

        </body>
        </html>
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
# MCP BEARER AUTHENTICATION
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

        # Routes that must remain publicly accessible
        # for browser-based OAuth.
        public_paths = {
            "/health",
            "/oauth/start",
            "/oauth/callback",
        }

        if (
            scope["type"] != "http"
            or scope["path"]
            in public_paths
        ):

            await self.app(
                scope,
                receive,
                send,
            )

            return


        # ----------------------------------------------------
        # CHECK BEARER TOKEN
        # ----------------------------------------------------

        headers = dict(
            scope.get(
                "headers",
                [],
            )
        )

        auth = (
            headers.get(
                b"authorization",
                b"",
            )
            .decode()
        )

        expected = (
            f"Bearer {MCP_API_TOKEN}"
        )

        if MCP_API_TOKEN and hmac.compare_digest(
            auth,
            expected,
        ):

            await self.app(
                scope,
                receive,
                send,
            )

            return


        # ----------------------------------------------------
        # UNAUTHORIZED
        # ----------------------------------------------------

        response = JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": (
                        "Unauthorized"
                    ),
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


# ============================================================
# CREATE ASGI APP
# ============================================================

def create_app():

    app = (
        mcp.streamable_http_app()
    )

    if MCP_API_TOKEN:

        app.add_middleware(
            BearerAuthMiddleware
        )

    return app


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    import uvicorn

    if not MCP_API_TOKEN:

        print(
            "WARNING: MCP_API_TOKEN "
            "is not set. "
            "The MCP endpoint is "
            "running without authentication."
        )

    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=port,
    )
