"""OpenID Connect — signing members in through their organization's identity provider.

The authorization-code flow with PKCE, as a confidential client:

1. `discover` reads the provider's `/.well-known/openid-configuration`, and refuses one
   whose `issuer` is not the one configured (a document served for someone else);
2. `authorize_url` is where the browser goes, carrying `state` (this sign-in), `nonce`
   (bound into the ID token) and the S256 PKCE challenge;
3. `exchange` trades the code the browser brings back for tokens at the token endpoint,
   and verifies the ID token: its signature against the provider's published keys
   (fetched here; PyJWT only does the maths), its algorithm (asymmetric only — `none`
   and HMAC are refused), `aud`, `iss`, `exp` and `iat`. What the claims *mean* —
   nonce, verified email — is checked by `domain.members.check_id_claims`.

Providers are reached over HTTPS; plain HTTP is accepted only for a provider on this
machine (a development identity provider, or the tests' fake). Discovery documents and
key sets are cached briefly, and the key set is fetched again once when a token names a
key it does not have, which is how a provider's key rotation looks from here.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt

_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"})
_CACHE_S = 300.0
_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


class OIDCError(Exception):
    """The provider could not be used, or its answer could not be trusted. Shown."""


@dataclass(frozen=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    auth_methods: tuple[str, ...]


def pkce_pair() -> tuple[str, str]:
    """`(verifier, challenge)` for S256 PKCE."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _check_url(url: str, what: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in _LOCAL):
        return url
    raise OIDCError(f"the identity provider's {what} must be an https:// address")


class OIDCClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._providers: dict[str, tuple[float, Provider]] = {}
        self._keys: dict[str, tuple[float, dict[str, Any]]] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0, transport=self._transport, follow_redirects=False)

    async def discover(self, issuer: str) -> Provider:
        issuer = _check_url(issuer.rstrip("/"), "issuer")
        cached = self._providers.get(issuer)
        if cached and time.monotonic() - cached[0] < _CACHE_S:
            return cached[1]
        try:
            async with self._http() as http:
                got = await http.get(issuer + "/.well-known/openid-configuration")
            got.raise_for_status()
            doc = got.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError(f"could not read the identity provider's configuration: {exc}") from exc
        if not isinstance(doc, dict) or str(doc.get("issuer", "")).rstrip("/") != issuer:
            raise OIDCError("the identity provider's configuration names a different issuer")
        try:
            provider = Provider(
                issuer=str(doc["issuer"]),
                authorization_endpoint=_check_url(
                    str(doc["authorization_endpoint"]), "sign-in page"
                ),
                token_endpoint=_check_url(str(doc["token_endpoint"]), "token endpoint"),
                jwks_uri=_check_url(str(doc["jwks_uri"]), "key set"),
                auth_methods=tuple(
                    doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
                ),
            )
        except KeyError as exc:
            raise OIDCError(f"the identity provider's configuration has no {exc}") from exc
        self._providers[issuer] = (time.monotonic(), provider)
        return provider

    def authorize_url(
        self,
        provider: Provider,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        nonce: str,
        challenge: str,
        login_hint: str = "",
    ) -> str:
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            query["login_hint"] = login_hint
        joiner = "&" if "?" in provider.authorization_endpoint else "?"
        return provider.authorization_endpoint + joiner + urlencode(query)

    async def exchange(
        self,
        provider: Provider,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> dict[str, Any]:
        """The ID token's claims, verified — or `OIDCError`."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        # One client authentication method, never two (RFC 6749 §2.3): Basic unless the
        # provider says it only takes the secret in the form.
        extra: dict[str, Any] = {}
        if "client_secret_basic" in provider.auth_methods:
            extra["auth"] = httpx.BasicAuth(client_id, client_secret)
        else:
            form |= {"client_id": client_id, "client_secret": client_secret}
        try:
            async with self._http() as http:
                got = await http.post(
                    provider.token_endpoint,
                    data=form,
                    headers={"accept": "application/json"},
                    **extra,
                )
            body = got.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError(f"the identity provider did not answer: {exc}") from exc
        if got.status_code != 200 or not isinstance(body, dict):
            said = body if isinstance(body, dict) else {}
            error = said.get("error_description") or said.get("error") or got.status_code
            raise OIDCError(f"the identity provider refused the sign-in: {error}")
        token = body.get("id_token")
        if not isinstance(token, str):
            raise OIDCError("the identity provider sent no ID token")
        return await self.verify(provider, token, client_id=client_id)

    async def verify(self, provider: Provider, token: str, *, client_id: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OIDCError("the ID token is not a JWT") from exc
        alg = header.get("alg")
        if alg not in _ALGORITHMS:
            raise OIDCError(f"the ID token is signed with {alg!r}, which is not accepted")
        key = await self._key(provider, header.get("kid"), alg)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=client_id,
                issuer=provider.issuer,
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OIDCError(f"the ID token did not verify: {exc}") from exc
        return dict(claims)

    async def _key(self, provider: Provider, kid: object, alg: str) -> Any:
        for fresh in (False, True):
            keys = await self._key_set(provider, fresh=fresh)
            for jwk in keys.get("keys", []):
                if not isinstance(jwk, dict) or jwk.get("use", "sig") != "sig":
                    continue
                if kid is not None and jwk.get("kid") != kid:
                    continue
                try:
                    return jwt.PyJWK(jwk, algorithm=alg).key
                except jwt.PyJWTError:
                    continue
        raise OIDCError("the ID token was signed with a key the identity provider doesn't publish")

    async def _key_set(self, provider: Provider, *, fresh: bool) -> dict[str, Any]:
        cached = self._keys.get(provider.jwks_uri)
        if cached and not fresh and time.monotonic() - cached[0] < _CACHE_S:
            return cached[1]
        try:
            async with self._http() as http:
                got = await http.get(provider.jwks_uri)
            got.raise_for_status()
            keys = got.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError(f"could not read the identity provider's keys: {exc}") from exc
        if not isinstance(keys, dict):
            raise OIDCError("the identity provider's key set is not a JSON object")
        self._keys[provider.jwks_uri] = (time.monotonic(), keys)
        return keys
