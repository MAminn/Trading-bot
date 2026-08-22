"""The connected user's Binance credentials, fetched from the app.

Why this exists
---------------
LIVE trading must sign with the CLIENT's keys — the ones they entered on the
website — not with whatever pair happens to sit in this host's `.env`. Before
this module, `main.py` read BINANCE_LIVE_API_KEY/SECRET from the environment,
so a real client could connect their account on the website and still have
every order placed against the server operator's wallet.

The keys are stored encrypted in Supabase and decrypted by the app, in the
app's trusted context, using the same AES-256-GCM mechanism that wrote them.
This module is the executor's side of that exchange.

Handling rules, all of them load-bearing
----------------------------------------
* Nothing here is ever logged. Not the key, not the secret, not a prefix of
  either. `__repr__` is overridden so a credentials object cannot leak through
  an f-string, a traceback frame or a debugger dump.
* A missing row is NOT an error. It is a state — `missing_user_binance_keys` —
  that the caller turns into a refusal to trade plus telemetry saying so. An
  exception here would be retried and eventually crash the process, which tells
  the operator far less than a heartbeat that names the reason.
* A transport failure IS an error, and a distinct one. "The app is unreachable"
  and "the user has not connected keys" must never collapse into one another:
  the first is transient and the second requires a human to act.
"""

import logging

import requests

log = logging.getLogger("executor.credentials")

REQUEST_TIMEOUT_SECONDS = 10
CREDENTIALS_PATH = "/api/public/engine/credentials"

# The blocked_reason reported when the user has connected no keys. Matches the
# error string the app's credentials endpoint returns, and is asserted against
# it in the tests so the two cannot drift.
MISSING_KEYS_REASON = "missing_user_binance_keys"
# A row exists but the app could not decrypt it. Distinct from "missing" on
# purpose: it means an operator problem (wrong encryption secret, legacy row),
# not a user who has yet to connect.
UNDECRYPTABLE_REASON = "user_binance_keys_undecryptable"
# The endpoint is not deployed or not configured with its own token.
NOT_CONFIGURED_REASON = "credentials_endpoint_not_configured"


class CredentialsUnavailable(Exception):
    """The app could not be asked. Transient — retry, do not conclude anything.

    Deliberately NOT raised for a missing or unreadable key row: those are
    answers, and answers are returned, not thrown.
    """


class UserCredentials:
    """One user's Binance key pair.

    Compared by value so the caller can detect a rotation, and opaque in every
    string form so it cannot be logged by accident.
    """

    __slots__ = ("api_key", "api_secret", "last4")

    def __init__(self, api_key: str, api_secret: str, last4: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.last4 = last4

    def __eq__(self, other) -> bool:
        if not isinstance(other, UserCredentials):
            return NotImplemented
        return (
            self.api_key == other.api_key and self.api_secret == other.api_secret
        )

    def __hash__(self):
        return hash((self.api_key, self.api_secret))

    def __repr__(self) -> str:
        # Never the key, never the secret. last4 is public — it is already shown
        # in the website UI — and it is the one detail that makes a log useful.
        return f"<UserCredentials last4={self.last4!r}>"

    __str__ = __repr__


class CredentialsResult:
    """Either a credential pair or the reason there isn't one."""

    __slots__ = ("credentials", "blocked_reason")

    def __init__(self, credentials=None, blocked_reason=None):
        self.credentials = credentials
        self.blocked_reason = blocked_reason

    @property
    def present(self) -> bool:
        return self.credentials is not None

    def __repr__(self) -> str:
        if self.present:
            return f"<CredentialsResult present last4={self.credentials.last4!r}>"
        return f"<CredentialsResult blocked={self.blocked_reason!r}>"

    __str__ = __repr__


class UserCredentialsClient:
    """Fetches the engine user's Binance keys from the app.

    One instance per process. The session carries the credentials token, which
    is separate from the ENGINE_SERVICE_TOKEN used for signals and telemetry —
    key material is not served behind the token that a dozen other routes
    already accept.
    """

    def __init__(self, app_api_base: str, credentials_token: str, user_id: str):
        self._url = f"{app_api_base.rstrip('/')}{CREDENTIALS_PATH}"
        self._user_id = user_id
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {credentials_token}"}
        )

    def fetch(self) -> CredentialsResult:
        """Ask the app for the user's keys.

        Returns a CredentialsResult; raises CredentialsUnavailable only when the
        question could not be put to the app at all.
        """
        try:
            resp = self._session.get(
                self._url,
                params={"user_id": self._user_id},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            raise CredentialsUnavailable(
                f"credentials fetch failed: {exc}"
            ) from exc

        if resp.status_code == 404:
            return CredentialsResult(blocked_reason=MISSING_KEYS_REASON)
        if resp.status_code == 409:
            return CredentialsResult(blocked_reason=UNDECRYPTABLE_REASON)
        if resp.status_code == 503:
            return CredentialsResult(blocked_reason=NOT_CONFIGURED_REASON)
        if resp.status_code in (401, 403):
            # A wrong token is an operator error, not a user state. Raising
            # keeps it in the failure counter where it will be noticed, rather
            # than presenting as "this client has not connected keys".
            raise CredentialsUnavailable(
                f"credentials endpoint rejected this executor (HTTP "
                f"{resp.status_code}) — check ENGINE_CREDENTIALS_TOKEN"
            )
        if not 200 <= resp.status_code < 300:
            raise CredentialsUnavailable(
                f"credentials endpoint returned HTTP {resp.status_code}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            # resp.text is NOT included: on this endpoint the body is the
            # secret.
            raise CredentialsUnavailable(
                "credentials endpoint returned a non-JSON body"
            ) from exc

        api_key = (body.get("api_key") or "").strip()
        api_secret = (body.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            # A 200 with an empty pair is a broken response, but the safe
            # reading is still "no usable keys" — fail closed, do not raise.
            log.error(
                "credentials endpoint returned a 200 with no usable key pair "
                "— treating as no connected keys"
            )
            return CredentialsResult(blocked_reason=MISSING_KEYS_REASON)

        last4 = str(body.get("api_key_last4") or api_key[-4:])
        return CredentialsResult(credentials=UserCredentials(api_key, api_secret, last4))
