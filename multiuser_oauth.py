from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import psycopg
from cryptography.fernet import Fernet
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.rows import dict_row


class MultiUserYouTubeOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        *,
        database_url: str,
        google_client_id: str,
        google_redirect_uri: str,
        google_scopes: list[str],
        issuer_url: str,
        resource_url: str,
        encryption_secret: str | None,
    ) -> None:
        self.database_url = database_url
        self.google_client_id = google_client_id
        self.google_redirect_uri = google_redirect_uri
        self.google_scopes = list(google_scopes)
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url
        self.encryption_secret = encryption_secret
        self._schema_ready = False

    def _connect(self):
        if not self.database_url:
            raise RuntimeError("Missing DATABASE_URL.")
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        statements = [
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                client_id TEXT PRIMARY KEY,
                metadata JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_pending (
                state TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                redirect_uri_explicit BOOLEAN NOT NULL,
                code_challenge TEXT NOT NULL,
                scopes JSONB NOT NULL,
                resource TEXT,
                client_state TEXT,
                expires_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                redirect_uri_explicit BOOLEAN NOT NULL,
                code_challenge TEXT NOT NULL,
                scopes JSONB NOT NULL,
                resource TEXT,
                expires_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_access_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                scopes JSONB NOT NULL,
                resource TEXT,
                expires_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                scopes JSONB NOT NULL,
                resource TEXT,
                expires_at BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS youtube_connections (
                subject TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                channel_title TEXT,
                refresh_token_encrypted TEXT NOT NULL,
                granted_scopes JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
        self._schema_ready = True

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _fernet(self) -> Fernet:
        if not self.encryption_secret:
            raise RuntimeError("Missing MCP_TOKEN_ENCRYPTION_KEY.")
        key = base64.urlsafe_b64encode(
            hashlib.sha256(self.encryption_secret.encode("utf-8")).digest()
        )
        return Fernet(key)

    def encrypt_refresh_token(self, token: str) -> str:
        return self._fernet().encrypt(token.encode("utf-8")).decode("utf-8")

    def decrypt_refresh_token(self, token: str) -> str:
        return self._fernet().decrypt(token.encode("utf-8")).decode("utf-8")

    def google_authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.google_client_id,
                "redirect_uri": self.google_redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.google_scopes),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state,
            }
        )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + query

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata FROM mcp_oauth_clients WHERE client_id = %s",
                    (client_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return OAuthClientInformationFull.model_validate(metadata)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._ensure_schema()
        if not client_info.client_id:
            raise RuntimeError("OAuth client registration did not provide client_id.")
        payload = client_info.model_dump(mode="json")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_oauth_clients (client_id, metadata)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (client_id)
                    DO UPDATE SET metadata = EXCLUDED.metadata
                    """,
                    (client_info.client_id, json.dumps(payload)),
                )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        self._ensure_schema()
        if not client.client_id:
            raise RuntimeError("OAuth client has no client_id.")
        state = secrets.token_urlsafe(32)
        scopes = params.scopes or ["youtube"]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mcp_oauth_pending WHERE expires_at < %s", (int(time.time()),))
                cur.execute(
                    """
                    INSERT INTO mcp_oauth_pending (
                        state, client_id, redirect_uri, redirect_uri_explicit,
                        code_challenge, scopes, resource, client_state, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    """,
                    (
                        state,
                        client.client_id,
                        str(params.redirect_uri),
                        bool(params.redirect_uri_provided_explicitly),
                        params.code_challenge,
                        json.dumps(scopes),
                        params.resource,
                        params.state,
                        int(time.time()) + 600,
                    ),
                )
        return self.google_authorization_url(state)

    def complete_google_authorization(
        self,
        *,
        state: str,
        subject: str,
        channel_id: str,
        channel_title: str | None,
        google_refresh_token: str,
        granted_scopes: list[str],
    ) -> str:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM mcp_oauth_pending WHERE state = %s",
                    (state,),
                )
                pending = cur.fetchone()
                if not pending or int(pending["expires_at"]) < int(time.time()):
                    raise RuntimeError("OAuth request expired or state is invalid.")

                encrypted = self.encrypt_refresh_token(google_refresh_token)
                cur.execute(
                    """
                    INSERT INTO youtube_connections (
                        subject, channel_id, channel_title,
                        refresh_token_encrypted, granted_scopes, updated_at
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                    ON CONFLICT (subject) DO UPDATE SET
                        channel_id = EXCLUDED.channel_id,
                        channel_title = EXCLUDED.channel_title,
                        refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                        granted_scopes = EXCLUDED.granted_scopes,
                        updated_at = NOW()
                    """,
                    (
                        subject,
                        channel_id,
                        channel_title,
                        encrypted,
                        json.dumps(granted_scopes),
                    ),
                )

                auth_code = secrets.token_urlsafe(32)
                cur.execute(
                    """
                    INSERT INTO mcp_oauth_codes (
                        code, client_id, subject, redirect_uri,
                        redirect_uri_explicit, code_challenge,
                        scopes, resource, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        auth_code,
                        pending["client_id"],
                        subject,
                        pending["redirect_uri"],
                        pending["redirect_uri_explicit"],
                        pending["code_challenge"],
                        json.dumps(pending["scopes"]),
                        pending["resource"],
                        int(time.time()) + 300,
                    ),
                )
                cur.execute("DELETE FROM mcp_oauth_pending WHERE state = %s", (state,))

        return construct_redirect_uri(
            pending["redirect_uri"],
            code=auth_code,
            state=pending["client_state"],
        )

    def get_google_refresh_token(self, subject: str) -> str:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT refresh_token_encrypted
                    FROM youtube_connections
                    WHERE subject = %s
                    """,
                    (subject,),
                )
                row = cur.fetchone()
        if not row:
            raise RuntimeError("No YouTube connection found for this authenticated user.")
        return self.decrypt_refresh_token(row["refresh_token_encrypted"])

    def get_connection_status(self, subject: str) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT channel_id, channel_title, granted_scopes, updated_at
                    FROM youtube_connections
                    WHERE subject = %s
                    """,
                    (subject,),
                )
                row = cur.fetchone()
        if not row:
            return {"connected": False}
        scopes = row["granted_scopes"]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        return {
            "connected": True,
            "channel_id": row["channel_id"],
            "channel_title": row["channel_title"],
            "granted_scopes": scopes,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        self._ensure_schema()
        if not client.client_id:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM mcp_oauth_codes WHERE code = %s AND client_id = %s",
                    (authorization_code, client.client_id),
                )
                row = cur.fetchone()
        if not row or int(row["expires_at"]) < int(time.time()):
            return None
        scopes = row["scopes"]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        return AuthorizationCode(
            code=row["code"],
            client_id=row["client_id"],
            subject=row["subject"],
            scopes=scopes,
            expires_at=float(row["expires_at"]),
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
        )

    def _issue_token_pair(
        self,
        *,
        client_id: str,
        subject: str,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        access_plain = secrets.token_urlsafe(32)
        refresh_plain = secrets.token_urlsafe(48)
        access_expires = int(time.time()) + 3600
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mcp_oauth_access_tokens
                    (token_hash, client_id, subject, scopes, resource, expires_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        self._hash_token(access_plain),
                        client_id,
                        subject,
                        json.dumps(scopes),
                        resource,
                        access_expires,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO mcp_oauth_refresh_tokens
                    (token_hash, client_id, subject, scopes, resource, expires_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s, NULL)
                    """,
                    (
                        self._hash_token(refresh_plain),
                        client_id,
                        subject,
                        json.dumps(scopes),
                        resource,
                    ),
                )
        return OAuthToken(
            access_token=access_plain,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
            refresh_token=refresh_plain,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        if not authorization_code.subject:
            raise TokenError("invalid_grant", "Authorization code has no subject.")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mcp_oauth_codes WHERE code = %s",
                    (authorization_code.code,),
                )
        return self._issue_token_pair(
            client_id=authorization_code.client_id,
            subject=authorization_code.subject,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        self._ensure_schema()
        token_hash = self._hash_token(token)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM mcp_oauth_access_tokens WHERE token_hash = %s",
                    (token_hash,),
                )
                row = cur.fetchone()
                if row and int(row["expires_at"]) < int(time.time()):
                    cur.execute(
                        "DELETE FROM mcp_oauth_access_tokens WHERE token_hash = %s",
                        (token_hash,),
                    )
                    return None
        if not row:
            return None
        scopes = row["scopes"]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            subject=row["subject"],
            scopes=scopes,
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        self._ensure_schema()
        if not client.client_id:
            return None
        token_hash = self._hash_token(refresh_token)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM mcp_oauth_refresh_tokens
                    WHERE token_hash = %s AND client_id = %s
                    """,
                    (token_hash, client.client_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        scopes = row["scopes"]
        if isinstance(scopes, str):
            scopes = json.loads(scopes)
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            subject=row["subject"],
            scopes=scopes,
            expires_at=row["expires_at"],
            resource=row["resource"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested_scopes = scopes or refresh_token.scopes
        if not set(requested_scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope", "Requested scopes exceed the original grant.")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mcp_oauth_refresh_tokens WHERE token_hash = %s",
                    (self._hash_token(refresh_token.token),),
                )
        if not refresh_token.subject:
            raise TokenError("invalid_grant", "Refresh token has no subject.")
        return self._issue_token_pair(
            client_id=refresh_token.client_id,
            subject=refresh_token.subject,
            scopes=requested_scopes,
            resource=refresh_token.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._ensure_schema()
        token_hash = self._hash_token(token.token)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mcp_oauth_access_tokens WHERE token_hash = %s",
                    (token_hash,),
                )
                cur.execute(
                    "DELETE FROM mcp_oauth_refresh_tokens WHERE token_hash = %s",
                    (token_hash,),
                )
