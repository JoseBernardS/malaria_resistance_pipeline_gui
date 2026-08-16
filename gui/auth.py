"""Authentication seam for the (future) cloud pipeline service.

Cloud execution needs a bearer token on every request, scoped to an org. That
identity has to live *somewhere* the UI and the cloud client can both read, so
this module owns it: a ``Session`` object that holds the current sign-in state
and an ``AuthClient`` stub marking where the real login/refresh calls will go.

``AuthClient`` calls the real login/refresh/logout routes via ``cloud_client``
when a server URL is configured (``PF_CLOUD_API_URL``); when it is unset the
methods raise ``NotImplementedError`` so the login dialog shows a "no server
configured" notice and the whole app stays usable offline.

Design notes:

- The access token is held **in memory only** and never written to the SQLite
  DB. Only the long-lived ``refresh_token`` is persisted, and only to the OS
  keychain (Keychain on macOS, Credential Locker on Windows, Secret Service on
  Linux) via :mod:`keyring`. On next launch ``Session.restore`` reads it and
  exchanges it for a fresh access token, so sign-in survives a restart without
  ever storing a bearer token on disk.
- Every keyring call is best-effort: a locked, missing, or headless backend
  degrades to in-memory-only sign-in and never raises into the UI.
- ``Session`` is a ``QObject`` so widgets can react to sign-in/out live via the
  ``changed`` signal, the same pattern the theme toggle uses to repaint chrome.
"""

import json
import time

from PyQt5.QtCore import QObject, pyqtSignal

from . import cloud_client

try:
    import keyring
    import keyring.errors
except Exception:                       # pragma: no cover - keyring optional
    keyring = None

# Keychain coordinates for the persisted credential. ``_KEYRING_SERVICE`` groups
# the app's secrets; ``_KEYRING_KEY`` is the single account entry holding the
# refresh token (plus the email/org needed to re-hydrate the account row).
_KEYRING_SERVICE = "PfDrugResistanceSurveillance"
_KEYRING_KEY = "cloud-session"


def _keyring_save(payload):
    """Best-effort write of the session ``payload`` dict to the OS keychain."""
    if keyring is None:
        return
    try:
        keyring.set_password(
            _KEYRING_SERVICE, _KEYRING_KEY, json.dumps(payload))
    except keyring.errors.KeyringError:
        pass


def _keyring_load():
    """Best-effort read of the persisted session dict, or ``None``."""
    if keyring is None:
        return None
    try:
        blob = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    except keyring.errors.KeyringError:
        return None
    if not blob:
        return None
    try:
        return json.loads(blob)
    except ValueError:
        return None


def _keyring_clear():
    """Best-effort delete of the persisted session (sign-out / stale token)."""
    if keyring is None:
        return
    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_KEY)
    except keyring.errors.KeyringError:
        pass


class AuthError(Exception):
    """A sign-in / authorization failure.

    Carries an optional ``error_code`` so the UI can branch on the stable codes
    the cloud API will return (e.g. ``INVALID_CREDENTIALS``) once the real
    client is wired, rather than string-matching messages.
    """

    def __init__(self, message, error_code=None):
        super().__init__(message)
        self.error_code = error_code


class Session(QObject):
    """In-memory sign-in state for cloud execution.

    Holds the bearer token plus the identity it belongs to (email/org) and its
    expiry. Emits ``changed`` on every sign-in and sign-out so the account row
    and any cloud-gated controls repaint without polling.
    """

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._email = None
        self._org = None
        self._token = None
        self._refresh_token = None
        self._expires_at = None      # unix ts, or None for "no known expiry"

    # -- state queries ---------------------------------------------------
    def is_expired(self):
        """True once a known expiry has passed (unknown expiry never expires)."""
        return self._expires_at is not None and time.time() >= self._expires_at

    def is_authenticated(self):
        """True while we hold a non-expired token."""
        return bool(self._token) and not self.is_expired()

    def raw_token(self):
        """The bare access token (no ``Bearer`` prefix), or ``None``."""
        return self._token if self.is_authenticated() else None

    def bearer(self):
        """``"Bearer <token>"`` for the Authorization header, or ``None``."""
        if not self.is_authenticated():
            return None
        return "Bearer %s" % self._token

    @property
    def email(self):
        return self._email

    @property
    def org(self):
        return self._org

    def label(self):
        """Short display string for the account row (email, falling back to org)."""
        return self._email or self._org or ""

    # -- mutations -------------------------------------------------------
    def sign_in(self, email, token, org=None, expires_at=None,
                refresh_token=None):
        """Record a successful sign-in, persist it, and notify listeners.

        The ``refresh_token`` (when present) is written to the OS keychain so
        the next launch can re-authenticate via :meth:`restore`; the access
        token itself is never persisted. If no refresh token is available there
        is nothing durable to keep, so any stale keychain entry is cleared.
        """
        self._email = email
        self._org = org
        self._token = token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        if refresh_token:
            _keyring_save({"email": email, "org": org,
                           "refresh_token": refresh_token})
        else:
            _keyring_clear()
        self.changed.emit()

    def sign_out(self):
        """Clear all identity, drop the persisted token, and notify listeners."""
        self._email = None
        self._org = None
        self._token = None
        self._refresh_token = None
        self._expires_at = None
        _keyring_clear()
        self.changed.emit()

    def restore(self, auth_client=None):
        """Re-authenticate at launch from the keychain-stored refresh token.

        Reads the persisted session, exchanges its refresh token for a fresh
        access token via ``auth_client`` (a default :class:`AuthClient` if not
        injected), and signs in on success. A missing/expired/revoked token or
        an unconfigured server yields a clean signed-out state (the stale entry
        is dropped). Returns ``True`` iff the session was restored.
        """
        saved = _keyring_load()
        if not saved or not saved.get("refresh_token"):
            return False
        client = auth_client or AuthClient()
        try:
            tokens = client.refresh(saved["refresh_token"])
        except (AuthError, NotImplementedError, Exception):
            # Bad/expired refresh token, or no server configured: don't keep a
            # credential we can't use. Stay signed out and usable offline.
            _keyring_clear()
            return False
        access = (tokens or {}).get("access_token")
        if not access:
            _keyring_clear()
            return False
        self.sign_in(
            email=saved.get("email"),
            token=access,
            org=saved.get("org"),
            expires_at=None,
            refresh_token=tokens.get("refresh_token") or saved["refresh_token"])
        return True


class AuthClient:
    """Network client for cloud authentication.

    Talks to the login router (``POST /login/access-token`` form auth) and
    ``GET /users/me``. When no cloud endpoint is configured
    (``PF_CLOUD_API_URL`` unset) it raises ``NotImplementedError`` so the login
    dialog shows its "not enabled yet" notice instead of a network error — the
    app stays fully usable offline.
    """

    def login(self, email, password):
        """Exchange credentials for a session dict.

        Returns ``{"token", "refresh_token", "org", "expires_at"}``. Raises
        ``NotImplementedError`` if unconfigured, or ``AuthError`` on a rejected
        sign-in (bubbling the API's ``detail``/``error_code``).
        """
        if not cloud_client.is_configured():
            raise NotImplementedError
        try:
            tokens = cloud_client.login(email, password)
        except cloud_client.CloudApiError as e:
            raise AuthError(e.detail or "Sign-in failed",
                            error_code=e.error_code)
        access = tokens.get("access_token")
        if not access:
            raise AuthError("Sign-in returned no access token")
        org = None
        try:
            profile = cloud_client.me(access)
            org = profile.get("organization_id")
        except cloud_client.CloudApiError:
            profile = {}
        return {
            "token": access,
            "refresh_token": tokens.get("refresh_token"),
            "org": org,
            # Access-token lifetime isn't exposed to the client; leave expiry
            # unknown and let a 401 drive re-auth rather than guessing.
            "expires_at": None,
        }

    def refresh(self, refresh_token):
        """Rotate tokens via the refresh endpoint."""
        if not cloud_client.is_configured():
            raise NotImplementedError
        try:
            return cloud_client.refresh(refresh_token)
        except cloud_client.CloudApiError as e:
            raise AuthError(e.detail or "Token refresh failed",
                            error_code=e.error_code)

    def logout(self, token):
        """Best-effort server-side sign-out (no-op if unconfigured)."""
        # The login router exposes /logout; failures here are non-fatal since
        # the client already drops the token locally.
        if not cloud_client.is_configured():
            return None
        try:
            return cloud_client._request("POST", "/logout", token=token)
        except cloud_client.CloudApiError:
            return None
