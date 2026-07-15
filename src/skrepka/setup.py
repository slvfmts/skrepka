"""`skrepka init` / `skrepka doctor` — Google OAuth setup wizard + diagnostics.

Interactive OAuth lives HERE and only here (a single human-run entry point);
normal commands never open a browser. Security-critical: see
PLAN-r2-init-doctor.md and the threat model in config.py. Diagnostics go to
stderr; exactly one JSON object goes to stdout. `--json` builds output from an
allowlist only — it never serializes an exception, response body, path, or ID.
"""

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import time
import uuid
import warnings

from skrepka import config

# Canonical Google OAuth endpoints — exact full URLs, no host wildcards.
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
SMOKE_PROPERTY_KEY = "skrepka-smoke"
_MAX_CRED_BYTES = 64 * 1024


class SetupError(Exception):
    """User-actionable setup failure. `code` is an allowlisted slug; the
    message is human text for the TTY narrative (never emitted in --json)."""

    def __init__(self, code, message, hint=None, link=None):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.link = link


# ---------------------------------------------------------------------------
# credentials.json validation → immutable snapshot (r1 #1, r2 #1, r3 #6)
# ---------------------------------------------------------------------------

def _is_loopback_redirect(u):
    """Strict parse (code-r1 #9): scheme http, no userinfo, host is exactly a
    loopback address. Prefix matching would accept
    `http://127.0.0.1:80@evil.example/cb`."""
    if not isinstance(u, str) or not u:
        return False
    from urllib.parse import urlparse
    try:
        p = urlparse(u)
    except ValueError:
        return False
    if p.scheme != "http" or p.username or p.password:
        return False
    return p.hostname in ("127.0.0.1", "::1", "localhost")


def _nonempty_str(v):
    return isinstance(v, str) and bool(v)


def validate_credentials_bytes(raw):
    """Parse and strictly validate a Desktop OAuth client JSON. Returns the
    immutable snapshot dict ({"installed": {...}}) that MUST be both persisted
    and handed to the flow — never reopen the path (r2 #1). All checks happen
    before any network call."""
    if len(raw) > _MAX_CRED_BYTES:
        raise SetupError("bad_credentials",
                         "credentials file is implausibly large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise SetupError("bad_credentials",
                         "credentials file is not valid JSON")
    if not isinstance(data, dict):
        raise SetupError("bad_credentials",
                         "credentials file has an unexpected shape")
    if data.get("type") == "service_account":
        raise SetupError(
            "service_account",
            "this is a service-account key, not a user OAuth client; "
            "skrepka acts as YOU — create an OAuth Desktop client instead")
    if "web" in data and "installed" not in data:
        raise SetupError(
            "web_client",
            "this is a Web OAuth client; the loopback flow needs a Desktop "
            "app client — recreate it with Application type = Desktop app")
    if "installed" not in data:
        raise SetupError(
            "not_desktop",
            "not a Desktop OAuth client (no `installed` section) — create an "
            "OAuth client with Application type = Desktop app")
    if "web" in data:
        raise SetupError("bad_credentials",
                         "credentials file mixes web and installed clients")
    # a genuine Desktop client JSON is exactly {"installed": {...}} — refuse
    # unexpected extra top-level keys rather than silently ignoring them
    extra = set(data) - {"installed"}
    if extra:
        raise SetupError("bad_credentials",
                         "credentials file has unexpected extra sections")
    conf = data["installed"]
    if not isinstance(conf, dict):
        raise SetupError("bad_credentials", "malformed `installed` section")
    for field in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not _nonempty_str(conf.get(field)):
            raise SetupError("bad_credentials",
                             f"credentials file is missing `{field}`")
    if conf["auth_uri"] != GOOGLE_AUTH_URI:
        raise SetupError(
            "bad_endpoint",
            "the credentials file points auth_uri somewhere other than "
            "Google — refusing (this could redirect your sign-in)")
    if conf["token_uri"] != GOOGLE_TOKEN_URI:
        raise SetupError(
            "bad_endpoint",
            "the credentials file points token_uri somewhere other than "
            "Google — refusing (this could exfiltrate your authorization)")
    if not conf["client_id"].endswith(".apps.googleusercontent.com"):
        raise SetupError("bad_credentials",
                         "client_id is not a Google OAuth client id")
    redirects = conf.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects:
        raise SetupError("bad_credentials",
                         "credentials file has no redirect_uris")
    for u in redirects:
        if not _is_loopback_redirect(u):
            raise SetupError(
                "bad_redirect",
                "a redirect URI is not a loopback address — a Desktop client "
                "must redirect only to 127.0.0.1/localhost")
    # return exactly the parsed object; caller persists json.dumps(data)
    return data


# ---------------------------------------------------------------------------
# error taxonomy — pure, never touches exception text (r1 #11, r2 #8)
# ---------------------------------------------------------------------------

_HTTP_REASON_CODES = {
    "SERVICE_DISABLED": ("api_disabled",
                         "a required Google API is not enabled for this "
                         "project"),
    "accessNotConfigured": ("api_disabled",
                            "a required Google API is not enabled"),
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": ("insufficient_scope",
                                        "the granted scopes are insufficient"),
    "insufficientPermissions": ("insufficient_scope",
                                "insufficient permissions/scopes"),
    "ADMIN_POLICY_ENFORCED": ("admin_policy",
                              "a Workspace admin policy blocks this"),
    "admin_policy_enforced": ("admin_policy",
                              "a Workspace admin policy blocks this"),
    "orgInternal": ("org_internal",
                    "the resource is restricted to another organization"),
}


def classify_http_error(exc):
    """Map a googleapiclient HttpError to an allowlisted (code, message).
    Parses only structured fields; never returns raw server text."""
    status = None
    resp = getattr(exc, "resp", None)
    if resp is not None:
        try:
            status = int(getattr(resp, "status", None))
        except (TypeError, ValueError):
            status = None
    reason, source = _extract_reason(exc)
    # only a STRUCTURED google.rpc ErrorInfo reason is trusted for the
    # disabled/scope/policy mappings; a legacy free-form reason falls through
    # to status-based classification (code-r1 #11)
    if source == "structured" and reason in _HTTP_REASON_CODES:
        return _HTTP_REASON_CODES[reason]
    if status == 429:
        return ("rate_limited", "rate-limited by Google; try again shortly")
    if status is not None and status >= 500:
        return ("server_error", "Google returned a temporary server error")
    if status == 401:
        return ("unauthenticated",
                "not authenticated — run `skrepka init --reauth`")
    if status == 403:
        return ("forbidden", "access was denied for this request")
    if status == 404:
        return ("not_found", "the target was not found")
    if status == 400:
        return ("bad_request", "the request was rejected as invalid")
    return ("api_error", "an unexpected API error occurred")


def _extract_reason(exc):
    """Pull a reason from an HttpError body. Returns (reason, source) where
    source is "structured" (google.rpc ErrorInfo with domain googleapis.com —
    trusted) or "legacy" (free-form errors[].reason — untrusted). Total over
    malformed bodies: any non-dict shape yields (None, None) (code-r1 #11)."""
    content = getattr(exc, "content", None)
    if content is None:
        return (None, None)
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        body = json.loads(content)
    except (ValueError, AttributeError):
        return (None, None)
    if not isinstance(body, dict):
        return (None, None)
    err = body.get("error")
    if not isinstance(err, dict):
        return (None, None)
    details = err.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            atype = detail.get("@type")
            if (isinstance(atype, str) and atype.endswith("ErrorInfo")
                    and detail.get("domain") == "googleapis.com"):
                r = detail.get("reason")
                if isinstance(r, str):
                    return (r, "structured")
    errors = err.get("errors")
    if isinstance(errors, list):
        for e in errors:
            if isinstance(e, dict) and isinstance(e.get("reason"), str):
                return (e["reason"], "legacy")
    return (None, None)


def classify_exception(exc):
    """Top-level safe classifier for any exception (r2 #8). Never serializes
    the exception text. The HttpError import is guarded so the classifier
    cannot itself fail when googleapiclient is missing (code-r2 #6)."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        HttpError = ()
    if isinstance(exc, SetupError):
        return (exc.code, str(exc))
    if HttpError and isinstance(exc, HttpError):
        return classify_http_error(exc)
    name = type(exc).__name__
    if name in ("RefreshError",):
        return ("refresh_failed",
                "could not refresh access — run `skrepka init --reauth`")
    if name in ("TransportError", "ConnectionError", "TimeoutError",
                "ServerNotFoundError"):
        return ("network_error", "could not reach Google (network problem)")
    module = (getattr(type(exc), "__module__", "") or "").lower()
    if "oauth" in module or "oauthlib" in module:
        return ("oauth_error", "the OAuth flow did not complete")
    return ("internal_error", "an unexpected error occurred")


# ---------------------------------------------------------------------------
# safe output (r2 #8): one JSON object to stdout; --json = allowlist only
# ---------------------------------------------------------------------------

def emit_json(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _check_entry(name, status, hint=None, link=None):
    e = {"name": name, "status": status}
    if hint:
        e["hint"] = hint
    if link:
        e["link"] = link
    return e


# ---------------------------------------------------------------------------
# OAuth + scope provenance (r2 #2, r3 #1)
# ---------------------------------------------------------------------------

def run_oauth(snapshot, no_browser, reauth=False, timeout_seconds=300):
    """Run the loopback flow from the validated snapshot (no reopen).
    Returns (creds, token_dict, granted_scopes). Forces `prompt=consent` on
    reauth so Google re-issues a refresh token (code-r1 #8), requests offline
    access, and bounds the wait so a closed browser cannot hang forever. The
    library prints the auth URL to stdout — redirect that to stderr to protect
    the one-JSON contract (r2 #9)."""
    from skrepka._engine import SCOPES
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(snapshot, SCOPES)
    prompt = "consent" if reauth else "select_account consent"
    with contextlib.redirect_stdout(sys.stderr):
        flow.run_local_server(host="127.0.0.1", port=0,
                              open_browser=not no_browser,
                              timeout_seconds=timeout_seconds,
                              access_type="offline", prompt=prompt,
                              authorization_prompt_message=(
                                  "Sign in to Google in the browser window "
                                  "(or open the URL below):\n{url}"))
    creds = flow.credentials
    token_dict = json.loads(creds.to_json())
    # exact granted scopes come only from the server's token response
    granted = None
    try:
        granted = flow.oauth2session.token.get("scope")
    except Exception:
        granted = None
    if isinstance(granted, str):
        granted_scopes = granted.split()
    elif isinstance(granted, (list, tuple)):
        granted_scopes = list(granted)
    else:
        # omitted scope ⇒ unchanged from requested (OAuth semantics)
        granted_scopes = list(SCOPES)
    missing = [s for s in SCOPES if s not in granted_scopes]
    if missing:
        raise SetupError(
            "partial_consent",
            "you did not grant all required access; skrepka needs both the "
            "Drive and Docs scopes — run init again and leave both checked")
    if not creds.refresh_token:
        raise SetupError(
            "no_refresh_token",
            "Google did not return a refresh token; revoke the app's prior "
            "access and run `skrepka init --reauth` so it prompts fresh")
    return creds, token_dict, granted_scopes


def _provenance(snapshot, token_dict, granted_scopes):
    return {
        "client_id": snapshot["installed"]["client_id"],
        "refresh_token_hash": config.refresh_token_hash(token_dict),
        "granted_scopes": granted_scopes,
    }


# ---------------------------------------------------------------------------
# transactional activation (r2 #6, r3 #2/#3)
# ---------------------------------------------------------------------------

RECOVERY_CRED = "recovery.credentials.json"
RECOVERY_TOKEN = "recovery.token.json"


def _resolve_pending_marker():
    """If a write-ahead marker is present, a prior activation crashed mid-swap.
    Restore the previous working pair from the recovery bundle if we have one
    (both files — activate writes both BEFORE the marker), else discard the
    half-written new pair. The restore is idempotent and the marker is the
    COMMIT point: it is cleared BEFORE the recovery files are removed, so a
    crash in the middle re-runs the same restore rather than deleting the
    already-restored active pair (code-r1 #4). Returns True if resolved."""
    # take the SAME lock activate() holds, so a rollback cannot interleave
    # with a forward activation (code-r3 #1)
    with config.lock():
        marker = config.read_marker()
        if marker is None:
            return False
        rec_cred = config.read_secret_bytes(RECOVERY_CRED)
        rec_tok = config.read_secret_bytes(RECOVERY_TOKEN)
        if rec_cred is not None and rec_tok is not None:
            config.write_secret_bytes(config.CREDENTIALS_NAME, rec_cred)
            config.write_secret_bytes(config.TOKEN_NAME, rec_tok)
        else:
            config.remove_secret(config.CREDENTIALS_NAME)
            config.remove_secret(config.TOKEN_NAME)
        config.clear_marker()  # commit; recovery files are inert leftovers
        config.remove_secret(RECOVERY_CRED)
        config.remove_secret(RECOVERY_TOKEN)
        return True


def _remove_stray_recovery():
    """With no marker present, any lingering recovery files are inert leftovers
    from a crash after commit — remove them under the lock so we never delete
    the recovery bundle of an in-flight activation (code-r1 #4 / r3 #1)."""
    with config.lock():
        if config.read_marker() is None:
            config.remove_secret(RECOVERY_CRED)
            config.remove_secret(RECOVERY_TOKEN)


def _active_creds():
    try:
        env = config.read_token_envelope()
    except config.ConfigError:
        return None
    if env is None:
        return None
    try:
        return _creds_from_token(env.get("token") or {})
    except Exception:  # noqa: BLE001
        return None


def _creds_from_token(token_dict):
    from skrepka._engine import SCOPES
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_info(token_dict, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def activate(snapshot_bytes, token_dict, provenance):
    """Atomically swap in a new credentials/token pair, bracketed by a
    write-ahead marker so a crash mid-swap is recoverable and every reader
    fails closed until it completes (r3 #3). Holds the config lock so it does
    not interleave with a concurrent refresh CAS (code-r2 #1)."""
    with config.lock():
        old_cred = config.read_secret_bytes(config.CREDENTIALS_NAME)
        old_tok = config.read_secret_bytes(config.TOKEN_NAME)
        if old_cred is not None and old_tok is not None:
            config.write_secret_bytes(RECOVERY_CRED, old_cred)
            config.write_secret_bytes(RECOVERY_TOKEN, old_tok)
        config.write_marker({"state": "activate"})
        config.write_secret_bytes(config.CREDENTIALS_NAME, snapshot_bytes)
        config.write_token_envelope(token_dict, provenance)
        config.clear_marker()
        config.remove_secret(RECOVERY_CRED)
        config.remove_secret(RECOVERY_TOKEN)


# ---------------------------------------------------------------------------
# smoke test with a durable cleanup journal (r2 #4/#3/#5)
# ---------------------------------------------------------------------------

TOKEN_PREFIX = "smktok-"  # per-journal creating token; NOT "smoke-" (no clash)


def _journal_name(nonce):
    return config.JOURNAL_PREFIX + nonce + ".json"


def _journal_token_name(nonce):
    return TOKEN_PREFIX + nonce + ".json"


def _list_journals():
    d = config.config_dir()
    out = []
    try:
        for fn in os.listdir(d):
            if (fn.startswith(config.JOURNAL_PREFIX) and fn.endswith(".json")
                    and not fn.startswith(TOKEN_PREFIX)):
                raw = config.read_secret_bytes(fn)
                if raw:
                    try:
                        out.append((fn, json.loads(raw)))
                    except ValueError:
                        out.append((fn, {"state": "unknown"}))
    except FileNotFoundError:
        pass
    return out


def _update_journal(nonce, **fields):
    data = _read_journal(nonce)
    data.update(fields)
    config.write_secret_bytes(_journal_name(nonce), json.dumps(data).encode())


def _remove_journal_all(nonce):
    config.remove_secret(_journal_name(nonce))
    config.remove_secret(_journal_token_name(nonce))


def _journal_creds(nonce):
    """Rebuild the creds that CREATED this journal's file, so cleanup works
    even after reauth switched to a different account (code-r2 #3)."""
    raw = config.read_secret_bytes(_journal_token_name(nonce))
    if not raw:
        return None
    try:
        return _creds_from_token(json.loads(raw).get("token") or {})
    except Exception:  # noqa: BLE001
        return None


def _reconcile_journal_tokens():
    """Remove bound-token sidecars that have no journal (code-r3 #2): a crash
    between writing the token and the journal (nothing was created yet), or
    between removing a resolved journal and its token (file already deleted).
    Both are safe to drop. Holds the lock so it cannot race the paired
    token+journal write in run_smoke and delete a live token (delta #2)."""
    with config.lock():
        d = config.config_dir()
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            return
        journ = {n[len(config.JOURNAL_PREFIX):-len(".json")] for n in names
                 if n.startswith(config.JOURNAL_PREFIX) and n.endswith(".json")
                 and not n.startswith(TOKEN_PREFIX)}
        for n in names:
            if n.startswith(TOKEN_PREFIX) and n.endswith(".json"):
                nonce = n[len(TOKEN_PREFIX):-len(".json")]
                if nonce not in journ:
                    config.remove_secret(n)


def _drive(creds):
    from skrepka._engine import get_drive_service
    return get_drive_service(creds)


def _docs(creds):
    from skrepka._engine import get_docs_service
    return get_docs_service(creds)


def _status_of(exc):
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return 0
    if isinstance(exc, HttpError):
        try:
            return int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _sweep_by_nonce(creds, nonce):
    """One list→delete→confirm pass. Returns (provably_clean, found_any):
    provably_clean is True only when a confirming search is COMPLETE
    (incompleteSearch == false) and returns nothing — an empty result from a
    lagging index is NOT proof (r2 #4). found_any records whether any tagged
    file was ever seen (needed to decide the ambiguous-create case)."""
    drive = _drive(creds)
    q = f"properties has {{ key='{SMOKE_PROPERTY_KEY}' and value='{nonce}' }}"

    def scan():
        incomplete, ids = False, []
        token = None
        while True:
            resp = drive.files().list(
                q=q, spaces="drive",
                fields="nextPageToken,incompleteSearch,files(id)",
                pageSize=100, pageToken=token).execute()
            incomplete = incomplete or bool(resp.get("incompleteSearch"))
            ids += [f["id"] for f in resp.get("files", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return incomplete, ids

    inc1, ids1 = scan()
    for fid in ids1:
        try:
            drive.files().delete(fileId=fid).execute()
        except Exception as e:  # noqa: BLE001
            if _status_of(e) != 404:
                raise
    inc2, ids2 = scan()
    provably_clean = (not inc1) and (not inc2) and not ids2
    return provably_clean, bool(ids1)


def _read_journal(nonce):
    raw = config.read_secret_bytes(_journal_name(nonce))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


_RESUME_HORIZON = 3       # separate clean sweeps before concluding "no orphan"
_RESUME_MIN_AGE = 600     # seconds a journal must age (index-lag window)
_RESUME_SKIP_FRESH = 120  # a journal younger than this may belong to an
#                           in-flight smoke — never sweep its file (delta-2)


def _attempt_cleanup(creds, nonce, data):
    """One cleanup attempt for a journal. Returns "resolved" or "pending".
    A known created_id is deleted BY ID first (index-lag-proof) and only a
    confirmed delete + provably-clean sweep resolves it (code-r2 #2). An
    ambiguous create with no id needs the tag sweep to actually find-and-remove
    it, or the resume horizon (attempts AND minimum age) to expire before
    concluding the create never landed (code-r2 #4)."""
    drive = _drive(creds)
    created_id = (data or {}).get("created_id")
    if created_id:
        delete_ok = False
        try:
            drive.files().delete(fileId=created_id).execute()
            delete_ok = True
        except Exception as e:  # noqa: BLE001
            if _status_of(e) == 404:
                delete_ok = True
        try:
            provably_clean, _found = _sweep_by_nonce(creds, nonce)
        except Exception:  # noqa: BLE001
            return "pending"
        if delete_ok and provably_clean:
            _remove_journal_all(nonce)
            return "resolved"
        return "pending"
    if not (data or {}).get("ambiguous"):
        # create definitively failed: nothing was ever created
        _remove_journal_all(nonce)
        return "resolved"
    try:
        provably_clean, found = _sweep_by_nonce(creds, nonce)
    except Exception:  # noqa: BLE001
        return "pending"
    if provably_clean and found:
        _remove_journal_all(nonce)
        return "resolved"
    if provably_clean:
        attempts = int((data or {}).get("attempts", 0)) + 1
        age = time.time() - float((data or {}).get("created_ts", 0) or 0)
        if attempts >= _RESUME_HORIZON and age >= _RESUME_MIN_AGE:
            _remove_journal_all(nonce)
            return "resolved"
        _update_journal(nonce, attempts=attempts)
        return "pending"
    return "pending"


def run_smoke(creds, token_dict):
    """Create → read → comment, then clean up. Returns (signed_in,
    cleanup_complete). Binds `token_dict` to the journal BEFORE creating so a
    crash mid-smoke (or a later reauth to a different account) can still find
    and delete the test file (code-r2 #3). Never raises."""
    nonce = uuid.uuid4().hex
    # write the bound token and the journal as ONE unit under the lock so a
    # concurrent reconcile cannot delete the token in between (delta #2)
    with config.lock():
        config.write_secret_bytes(
            _journal_token_name(nonce),
            json.dumps({"token": token_dict}).encode())
        _update_journal(nonce, state="create_attempted_ambiguous",
                        attempts=0, created_ts=time.time(), ambiguous=True)
    drive = _drive(creds)
    created_id, signed_in = None, False
    try:
        created = drive.files().create(
            body={"name": f"skrepka setup check {nonce[:8]}",
                  "mimeType": "application/vnd.google-apps.document",
                  "properties": {SMOKE_PROPERTY_KEY: nonce}},
            ignoreDefaultVisibility=True, fields="id").execute()
        created_id = created.get("id")
        _update_journal(nonce, created_id=created_id, ambiguous=False)
        _docs(creds).documents().get(documentId=created_id).execute()
        drive.comments().create(
            fileId=created_id, fields="id",
            body={"content": "skrepka setup check — safe to ignore"}).execute()
        signed_in = True
    except Exception as e:  # noqa: BLE001 — classified by the caller
        if created_id is None:
            # only a lost/5xx/429/408 create response is truly ambiguous; a
            # definitive 4xx means the file was not created (no orphan)
            st = _status_of(e)
            ambiguous = st == 0 or st in (408, 429) or st >= 500
            _update_journal(nonce, ambiguous=ambiguous)
    status = _attempt_cleanup(creds, nonce, _read_journal(nonce))
    return signed_in, status == "resolved"


def resume_pending_smoke():
    """Retry every unfinished cleanup journal using ITS OWN bound creds, so a
    later init on a different account still cleans a prior account's orphan
    (code-r2 #3). Returns (resolved, pending)."""
    _reconcile_journal_tokens()
    resolved, pending = 0, 0
    for jname, data in _list_journals():
        nonce = jname[len(config.JOURNAL_PREFIX):-len(".json")]
        # distinguish "no bound token FILE" from "token present but creds could
        # not be built right now" (a transient refresh/parse error): only the
        # former is a resolved leftover — removal is journal-first, so a
        # surviving journal with no token means its file was already deleted
        # (delta #2). A transient failure must stay pending, never dropped.
        if config.read_secret_bytes(_journal_token_name(nonce)) is None:
            _remove_journal_all(nonce)
            resolved += 1
            continue
        # a young journal may belong to another init's smoke that is still
        # using its file — never sweep it, or we would delete a live test file
        # out from under that process (delta-2). A genuinely crashed journal
        # ages past this window well before the next interactive init.
        age = time.time() - float((data or {}).get("created_ts", 0) or 0)
        if age < _RESUME_SKIP_FRESH:
            pending += 1
            continue
        creds = _journal_creds(nonce)
        if creds is None:
            pending += 1  # token present but unusable now — retry later
            continue
        try:
            status = _attempt_cleanup(creds, nonce, data)
        except Exception:  # noqa: BLE001
            pending += 1
            continue
        if status == "resolved":
            resolved += 1
        else:
            pending += 1
    return resolved, pending


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

_CONSOLE = "https://console.cloud.google.com"


def _print_console_guide(no_browser):
    steps = [
        ("Create or select a Google Cloud project",
         f"{_CONSOLE}/projectcreate"),
        ("Enable the Google Docs API",
         f"{_CONSOLE}/apis/library/docs.googleapis.com"),
        ("Enable the Google Drive API",
         f"{_CONSOLE}/apis/library/drive.googleapis.com"),
        ("Set up the OAuth consent screen (pick your new project at the top "
         "first). Two tabs in the left menu:\n"
         "        • Audience: User type = External (the only choice on a "
         "personal Gmail account). Leave Publishing status = In production "
         "(then no Test users are needed and the sign-in won't expire); if "
         "it shows Testing, either add your own address under Test users or "
         "click Back to production.\n"
         "        • Data Access → Add or remove scopes → paste BOTH of these "
         "into \"Manually add scopes\", then Add to table and Update:\n"
         "            https://www.googleapis.com/auth/drive\n"
         "            https://www.googleapis.com/auth/documents",
         f"{_CONSOLE}/auth/overview"),
        ("Clients tab → Create client → Application type = Desktop app → "
         "download the JSON", f"{_CONSOLE}/auth/clients"),
    ]
    print("\nFirst-time Google setup (about 15–30 minutes; skrepka cannot do "
          "these steps for you — that would need project-management access it "
          "deliberately never requests):", file=sys.stderr)
    for i, (text, url) in enumerate(steps, 1):
        print(f"  {i}. {text}\n     {url}", file=sys.stderr)
    print("\nHeads up: in Testing mode Google issues a sign-in that expires "
          "after 7 days — keep the app In production (the usual default) to "
          "avoid that. Either way the browser will warn the app is "
          "\"unverified\"; that is expected for your own client — click "
          "Advanced → Go to … to continue.\n", file=sys.stderr)
    if not no_browser:
        import webbrowser
        for _text, url in steps:
            try:
                webbrowser.open(url)
                break  # open only the first page; the rest are linked above
            except Exception:
                pass


def cmd_init(argv):
    p = _SafeArgParser(prog="skrepka init")
    # not required: recovery/already_configured paths must run WITHOUT it
    # (code-r2 #5); we demand it only right before the OAuth step
    p.add_argument("--credentials",
                   help="path to the downloaded OAuth Desktop client JSON")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--reauth", action="store_true")
    try:
        args = p.parse_args(argv)
    except _ArgError:
        print("Invalid usage. See `skrepka init --help`.", file=sys.stderr)
        emit_json({"action": "init", "status": "bad_usage"})
        return 2

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print("`skrepka init` is interactive and must run in a terminal. "
              "A human runs the Google sign-in; agents cannot.", file=sys.stderr)
        emit_json({"action": "init", "status": "needs_terminal"})
        return 2
    try:
        config.ensure_config_dir()
        _resolve_pending_marker()
        _remove_stray_recovery()
        # retry any leftover cleanup journals BEFORE the early return, so a
        # prior crash's orphan is not stranded by `already_configured` (r1 #3);
        # each journal carries its own creds, so no active grant is needed
        _resolved, carried_pending = resume_pending_smoke()
        if carried_pending:
            print(f"Note: {carried_pending} earlier setup test file(s) could "
                  f"not be confirmed deleted yet. Re-run `skrepka init` after "
                  f"a couple of minutes to finish cleaning them up.",
                  file=sys.stderr)
        if config.detect_legacy():
            print("Note: found an older gdocs-uploader config. skrepka uses a "
                  "new location; this init sets it up fresh here.",
                  file=sys.stderr)
        existing = config.read_token_envelope()
        if existing is not None and not args.reauth:
            print("You already appear to be signed in. Re-run with --reauth "
                  "to replace the current sign-in.", file=sys.stderr)
            # a carried-over pending cleanup must not read as a clean state
            # (code-r2 delta-3 P2)
            status = "cleanup_pending" if carried_pending else "configured"
            emit_json({"action": "init",
                       "status": ("already_configured" if status
                                  == "configured" else "cleanup_pending")})
            return 2 if carried_pending else 0

        if not args.credentials:
            # first-time users have no JSON yet — the whole point of running
            # init is to be shown HOW to create it. Print the guide and tell
            # them to re-run with the downloaded file (the guide must be
            # reachable before you have credentials, not after).
            _print_console_guide(args.no_browser)
            print("\nOnce you've downloaded the Desktop-client JSON, run:\n"
                  "  skrepka init --credentials /path/to/client_secret.json",
                  file=sys.stderr)
            emit_json({"action": "init", "status": "needs_credentials"})
            return 2

        raw = _read_credentials_file(args.credentials)
        snapshot = validate_credentials_bytes(raw)
        snapshot_bytes = json.dumps(snapshot, ensure_ascii=False).encode()

        # credentials supplied ⇒ the user already went through the console
        # guide — go straight to sign-in
        print("Signing in with Google — a browser window will open…",
              file=sys.stderr)
        creds, token_dict, granted = run_oauth(
            snapshot, args.no_browser, reauth=args.reauth)
        provenance = _provenance(snapshot, token_dict, granted)

        # Smoke-test on the NEW creds (bound to a durable journal that carries
        # its own token). Only activate — destroying the old working pair —
        # AFTER the sign-in ops succeed (code-r1 #2). A failed cleanup keeps
        # the journal so a later init still deletes the test file (code-r2 #3).
        signed_in, cleanup_complete = run_smoke(creds, token_dict)
        if not signed_in:
            raise SetupError(
                "smoke_failed",
                "signed in, but a basic create/read/comment check did not "
                "complete — the APIs may not be fully enabled yet; see "
                "`skrepka doctor` and try again")
        activate(snapshot_bytes, token_dict, provenance)
        # a test file carried over from an earlier crashed init (skipped as too
        # fresh to sweep) must keep the status honest — not a clean "ready"
        # (code-r2 delta-3 P2)
        status = ("ready" if (cleanup_complete and not carried_pending)
                  else "cleanup_pending")
    except SetupError as e:
        _narrate_error(e.code, str(e), e.hint, e.link)
        emit_json({"action": "init", "status": "error", "code": e.code})
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        emit_json({"action": "init", "status": "cancelled"})
        return 2
    except Exception as e:  # noqa: BLE001 — safe classification, no raw text
        code, msg = classify_exception(e)
        _narrate_error(code, msg, None, None)
        emit_json({"action": "init", "status": "error", "code": code})
        return 2

    capabilities = ["drive.create", "docs.read", "drive.comment",
                    "drive.delete"]
    if status == "cleanup_pending":
        print("Setup works, but a test file could not be confirmed deleted "
              "yet (Google's search index can lag). Re-run `skrepka init` "
              "later to finish cleanup.", file=sys.stderr)
        emit_json({"action": "init", "status": "cleanup_pending",
                   "capabilities": capabilities})
        return 2
    print("\nAll set. Subsequent runs need no setup.", file=sys.stderr)
    emit_json({"action": "init", "status": "ready",
               "capabilities": capabilities})
    return 0


def _read_credentials_file(path):
    """Read the user-supplied credentials once, via O_NOFOLLOW, verifying it
    is a regular file we own."""
    import stat as _stat
    try:
        fd = os.open(os.path.expanduser(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise SetupError("bad_credentials",
                         f"cannot read credentials file: {e.strerror}")
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise SetupError("bad_credentials",
                             "credentials path is not a regular file")
        if st.st_uid != os.getuid():
            raise SetupError("bad_credentials",
                             "credentials file is not owned by you")
        if st.st_size > _MAX_CRED_BYTES:
            raise SetupError("bad_credentials",
                             "credentials file is implausibly large")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _narrate_error(code, msg, hint, link):
    print(f"\nError: {msg}", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)
    if link:
        print(f"  {link}", file=sys.stderr)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _collect_doctor_checks(argv):
    """Run all diagnostics and return the checks list. NEVER emits output.
    Raises _ArgError on bad usage (incl. --help while --json, since add_help is
    disabled there) so the caller can render a safe message (r3 #4)."""
    json_mode = "--json" in argv
    p = _SafeArgParser(prog="skrepka doctor", add_help=not json_mode)
    p.add_argument("--json", action="store_true")
    p.parse_args(argv)
    checks = []

    def add(name, status, hint=None, link=None):
        checks.append(_check_entry(name, status, hint, link))

    try:
        config.ensure_config_dir()
        add("config_dir", "ok")
    except config.ConfigError:
        add("config_dir", "fail", "config directory could not be secured")
        return checks

    if config.read_marker() is not None:
        add("pending_init", "warn",
            "a previous `skrepka init` did not finish — run `skrepka init`")
    if _list_journals():
        add("pending_cleanup", "warn",
            "a setup test file may not be deleted — run `skrepka init` to "
            "finish cleanup")

    cred = config.read_secret_bytes(config.CREDENTIALS_NAME)
    if cred is None:
        add("credentials", "fail", "no credentials — run `skrepka init`")
        return checks
    try:
        validate_credentials_bytes(cred)
        add("credentials", "ok")
    except SetupError as e:
        add("credentials", "fail", e.code if json_mode else str(e))
        return checks

    env = config.read_token_envelope()
    if env is None:
        add("token", "fail", "not signed in — run `skrepka init`")
        return checks
    add("token", "ok")

    from skrepka._engine import SCOPES
    prov = env.get("provenance") or {}
    # trust the recorded scopes only if provenance is bound to THIS token and
    # this credentials client (code-r1 #10)
    bound = (prov.get("refresh_token_hash")
             == config.refresh_token_hash(env.get("token") or {}))
    try:
        cred_client_id = json.loads(cred)["installed"]["client_id"]
    except Exception:  # noqa: BLE001
        cred_client_id = None
    client_ok = (cred_client_id is None
                 or prov.get("client_id") == cred_client_id)
    granted = prov.get("granted_scopes") or []
    if not bound or not client_ok:
        add("scopes", "warn",
            "the recorded scope grant does not match the stored sign-in — "
            "run `skrepka init --reauth`")
    elif all(s in granted for s in SCOPES):
        add("scopes", "ok")
    else:
        add("scopes", "warn",
            "the recorded grant is missing a required scope — "
            "run `skrepka init --reauth`")

    # build creds locally — NOT via the engine's get_creds, which prints to
    # stdout and would break the one-JSON/--json contract (code-r1 #1)
    creds = _active_creds()
    if creds is None:
        add("auth", "fail", "sign-in is not usable — run `skrepka init "
            "--reauth`")
        return checks
    add("auth", "ok")

    _probe_drive(creds, add)
    _probe_docs(creds, add)
    return checks


def _probe_drive(creds, add):
    try:
        _drive(creds).about().get(fields="kind").execute()
        add("drive_api", "ok")
    except Exception as e:  # noqa: BLE001
        code, _msg = classify_exception(e)
        if code == "api_disabled":
            add("drive_api", "fail",
                "Drive API is not enabled — enable it and retry",
                "https://console.cloud.google.com/apis/library/"
                "drive.googleapis.com")
        else:
            add("drive_api", "fail" if code != "rate_limited" else "warn",
                code)


def _probe_docs(creds, add):
    # syntactically valid, almost-certainly-absent id (Doc-id-shaped, so we
    # get 404 not 400): 404 ⇒ reachable, SERVICE_DISABLED ⇒ not enabled,
    # anything else ⇒ inconclusive (r1 #9)
    import secrets
    bogus = "1" + secrets.token_urlsafe(32)[:42]
    try:
        _docs(creds).documents().get(documentId=bogus).execute()
        add("docs_api", "ok")
    except Exception as e:  # noqa: BLE001
        code, _msg = classify_exception(e)
        status = _status_of(e)
        if status == 404:
            add("docs_api", "ok")
        elif code == "api_disabled":
            add("docs_api", "fail",
                "Docs API is not enabled — enable it and retry",
                "https://console.cloud.google.com/apis/library/"
                "docs.googleapis.com")
        else:
            add("docs_api", "inconclusive", code)


def _safe_code(e):
    try:
        if isinstance(e, Exception):
            return classify_exception(e)[0]
    except Exception:  # noqa: BLE001
        pass
    return "internal_error"


# ---------------------------------------------------------------------------
# argparse that never leaks the offending token/path (r2 #8, r3 #4)
# ---------------------------------------------------------------------------

class _ArgError(Exception):
    """Raised instead of argparse's default stderr-print-and-exit, so the
    caller renders a safe message with no offending argument echoed."""


class _SafeArgParser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D401
        raise _ArgError()


# ---------------------------------------------------------------------------
# entry points (called from cli.py before the heavy engine argparse)
# ---------------------------------------------------------------------------

def init_main(argv):
    return cmd_init(argv)


def doctor_main(argv):
    """In --json mode, capture BOTH stdout and stderr (plus warnings/logging)
    so nothing but our final allowlisted JSON escapes on either stream, and
    emit that single object to the real stdout afterwards (r3 #4)."""
    json_mode = "--json" in argv
    if json_mode:
        real_out = sys.stdout
        logging.disable(logging.CRITICAL)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sink = io.StringIO()
                with contextlib.redirect_stdout(sink), \
                        contextlib.redirect_stderr(sink):
                    try:
                        checks = _collect_doctor_checks(argv)
                        ok = all(c["status"] == "ok" for c in checks)
                        payload = {"action": "doctor", "ok": ok,
                                   "checks": checks}
                        code = 0 if ok else 2
                    except (_ArgError, SystemExit):
                        payload = {"action": "doctor", "ok": False,
                                   "error": "bad_usage"}
                        code = 2
                    except BaseException as e:  # noqa: BLE001
                        payload = {"action": "doctor", "ok": False,
                                   "error": _safe_code(e)}
                        code = 1
        finally:
            logging.disable(logging.NOTSET)
        real_out.write(json.dumps(payload, ensure_ascii=False) + "\n")
        real_out.flush()
        return code

    # human mode: narrate to stderr, one summary JSON to stdout
    try:
        checks = _collect_doctor_checks(argv)
    except _ArgError:
        print("Invalid usage. See `skrepka doctor --help`.", file=sys.stderr)
        return 2
    except SystemExit:
        raise  # argparse --help already printed
    except BaseException as e:  # noqa: BLE001
        print("doctor hit an unexpected problem.", file=sys.stderr)
        emit_json({"action": "doctor", "ok": False, "error": _safe_code(e)})
        return 1
    for c in checks:
        line = f"  [{c['status']}] {c['name']}"
        if c.get("hint"):
            line += f" — {c['hint']}"
        print(line, file=sys.stderr)
        if c.get("link"):
            print(f"        {c['link']}", file=sys.stderr)
    ok = all(c["status"] == "ok" for c in checks)
    emit_json({"action": "doctor", "ok": ok,
               "checks": [{"name": c["name"], "status": c["status"]}
                          for c in checks]})
    return 0 if ok else 2
