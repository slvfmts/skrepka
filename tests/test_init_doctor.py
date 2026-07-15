"""init/doctor: credential validation, hardened secret I/O, token envelope,
scope provenance, transactional activation, durable smoke cleanup, error
taxonomy and --json sanitization (PLAN-r2-init-doctor v4)."""

import json
import os
import stat as stat_mod

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A config store rooted inside a fake HOME so the owner/parent-chain
    checks pass on a throwaway directory."""
    home = tmp_path
    os.chmod(home, 0o700)
    cfg = home / "cfg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SKREPKA_CONFIG_DIR", str(cfg))
    import skrepka.config as config
    config.ensure_config_dir()
    return config


@pytest.fixture
def setup_mod():
    import skrepka.setup as s
    return s


GOOD_CRED = {
    "installed": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def cred_bytes(mut=None):
    d = json.loads(json.dumps(GOOD_CRED))
    if mut:
        mut(d)
    return json.dumps(d).encode()


# --- validator (r1 #1, r2 #1, r3 #6) ---

def test_validator_accepts_desktop(setup_mod):
    snap = setup_mod.validate_credentials_bytes(cred_bytes())
    assert "installed" in snap


def test_validator_rejects_web(setup_mod):
    raw = json.dumps({"web": GOOD_CRED["installed"]}).encode()
    with pytest.raises(setup_mod.SetupError) as e:
        setup_mod.validate_credentials_bytes(raw)
    assert e.value.code == "web_client"


def test_validator_rejects_service_account(setup_mod):
    raw = json.dumps({"type": "service_account", "installed": {}}).encode()
    with pytest.raises(setup_mod.SetupError) as e:
        setup_mod.validate_credentials_bytes(raw)
    assert e.value.code == "service_account"


def test_validator_rejects_foreign_token_uri(setup_mod):
    raw = cred_bytes(lambda d: d["installed"].update(
        token_uri="https://evil.example.com/token"))
    with pytest.raises(setup_mod.SetupError) as e:
        setup_mod.validate_credentials_bytes(raw)
    assert e.value.code == "bad_endpoint"


def test_validator_rejects_foreign_auth_uri(setup_mod):
    raw = cred_bytes(lambda d: d["installed"].update(
        auth_uri="https://evil.example.com/auth"))
    with pytest.raises(setup_mod.SetupError) as e:
        setup_mod.validate_credentials_bytes(raw)
    assert e.value.code == "bad_endpoint"


def test_validator_rejects_non_loopback_redirect(setup_mod):
    raw = cred_bytes(lambda d: d["installed"].update(
        redirect_uris=["https://evil.example.com/cb"]))
    with pytest.raises(setup_mod.SetupError) as e:
        setup_mod.validate_credentials_bytes(raw)
    assert e.value.code == "bad_redirect"


def test_validator_rejects_empty_redirects(setup_mod):
    raw = cred_bytes(lambda d: d["installed"].update(redirect_uris=[]))
    with pytest.raises(setup_mod.SetupError):
        setup_mod.validate_credentials_bytes(raw)


def test_validator_rejects_missing_secret(setup_mod):
    def mut(d):
        del d["installed"]["client_secret"]
    with pytest.raises(setup_mod.SetupError):
        setup_mod.validate_credentials_bytes(cred_bytes(mut))


def test_validator_rejects_bad_client_id(setup_mod):
    raw = cred_bytes(lambda d: d["installed"].update(client_id="nope"))
    with pytest.raises(setup_mod.SetupError):
        setup_mod.validate_credentials_bytes(raw)


# --- config path + secret I/O (r2 #2/#7, r3 #5) ---

def test_config_dir_override_outside_home_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("SKREPKA_CONFIG_DIR", str(tmp_path / "outside"))
    import skrepka.config as config
    with pytest.raises(config.ConfigError):
        config.config_dir()


def test_secret_roundtrip_and_perms(store):
    store.write_secret_bytes("x.json", b"hello")
    assert store.read_secret_bytes("x.json") == b"hello"
    mode = os.stat(os.path.join(store.config_dir(), "x.json")).st_mode
    assert stat_mod.S_IMODE(mode) == 0o600


def test_config_dir_is_0700(store):
    mode = os.stat(store.config_dir()).st_mode
    assert stat_mod.S_IMODE(mode) == 0o700


def test_read_symlinked_secret_refused(store):
    target = os.path.join(store.config_dir(), "real")
    with open(target, "wb") as f:
        f.write(b"data")
    link = os.path.join(store.config_dir(), "link.json")
    os.symlink(target, link)
    with pytest.raises(store.ConfigError):
        store.read_secret_bytes("link.json")


def test_write_over_symlink_refused(store):
    outside = os.path.join(store.config_dir(), "outside")
    with open(outside, "wb") as f:
        f.write(b"orig")
    link = os.path.join(store.config_dir(), "t.json")
    os.symlink(outside, link)
    with pytest.raises(store.ConfigError):
        store.write_secret_bytes("t.json", b"new")
    with open(outside, "rb") as f:
        assert f.read() == b"orig"  # target not followed/overwritten


def test_group_writable_own_dir_is_tightened(store):
    # a config dir we OWN is fixed to 0700, not refused (r1 #2)
    os.chmod(store.config_dir(), 0o770)
    store.ensure_config_dir()
    assert stat_mod.S_IMODE(os.stat(store.config_dir()).st_mode) == 0o700


def test_group_writable_parent_refused(tmp_path, monkeypatch):
    home = tmp_path
    os.chmod(home, 0o700)
    parent = home / "shared"
    parent.mkdir()
    os.chmod(parent, 0o770)  # a group-writable PARENT cannot be trusted
    cfg = parent / "skrepka"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SKREPKA_CONFIG_DIR", str(cfg))
    import skrepka.config as config
    with pytest.raises(config.ConfigError):
        config.ensure_config_dir()


# --- token envelope + provenance (r3 #1) ---

def test_token_envelope_roundtrip(store):
    tok = {"refresh_token": "rt", "token": "at"}
    prov = {"client_id": "c", "refresh_token_hash":
            store.refresh_token_hash(tok), "granted_scopes": ["a"]}
    store.write_token_envelope(tok, prov)
    env = store.read_token_envelope()
    assert env["token"] == tok
    assert env["provenance"]["granted_scopes"] == ["a"]


def test_refresh_token_hash_ignores_access_token(store):
    a = store.refresh_token_hash({"refresh_token": "rt", "token": "at1"})
    b = store.refresh_token_hash({"refresh_token": "rt", "token": "at2"})
    assert a == b


# --- marker barrier + get_creds (r1 #6, r3 #3) ---

def test_get_creds_no_token_errors_to_init(store, monkeypatch, capsys):
    import skrepka._engine as engine
    called = {"flow": False}
    monkeypatch.setattr(engine, "Credentials",
                        _flag_credentials(called), raising=True)
    with pytest.raises(SystemExit):
        engine.get_creds()
    assert "skrepka init" in json.loads(capsys.readouterr().out)["error"]
    assert called["flow"] is False  # no interactive flow, no browser


def test_get_creds_fails_closed_on_marker(store, capsys):
    store.write_token_envelope({"refresh_token": "rt"},
                               {"client_id": "c"})
    store.write_marker({"state": "activate"})
    import skrepka._engine as engine
    with pytest.raises(SystemExit):
        engine.get_creds()
    assert "did not finish" in json.loads(capsys.readouterr().out)["error"]


def _flag_credentials(flag):
    class C:
        @staticmethod
        def from_authorized_user_info(info, scopes):
            flag["flow"] = True
            raise AssertionError("should not be reached without a token")
    return C


# --- scope provenance from token response (r2 #2, r3 #1) ---

def test_run_oauth_reads_granted_scope_field(setup_mod, monkeypatch):
    from skrepka._engine import SCOPES

    class FakeCreds:
        refresh_token = "rt"

        def to_json(self):
            return json.dumps({"refresh_token": "rt", "token": "at"})

    class FakeSession:
        token = {"scope": " ".join(SCOPES)}

    class FakeFlow:
        credentials = FakeCreds()
        oauth2session = FakeSession()

        def run_local_server(self, **kw):
            return self.credentials

    import google_auth_oauthlib.flow as flowmod
    monkeypatch.setattr(flowmod.InstalledAppFlow, "from_client_config",
                        classmethod(lambda cls, cfg, scopes: FakeFlow()))
    creds, tok, granted = setup_mod.run_oauth(GOOD_CRED, no_browser=True)
    assert set(granted) == set(SCOPES)


def test_run_oauth_partial_consent_refused(setup_mod, monkeypatch):
    from skrepka._engine import SCOPES

    class FakeFlow:
        credentials = type("C", (), {
            "refresh_token": "rt",
            "to_json": lambda self: json.dumps({"refresh_token": "rt"})})()
        oauth2session = type("S", (), {"token": {"scope": SCOPES[0]}})()

        def run_local_server(self, **kw):
            return self.credentials

    import google_auth_oauthlib.flow as flowmod
    monkeypatch.setattr(flowmod.InstalledAppFlow, "from_client_config",
                        classmethod(lambda cls, cfg, scopes: FakeFlow()))
    with pytest.raises(setup_mod.SetupError) as e:
        setup_mod.run_oauth(GOOD_CRED, no_browser=True)
    assert e.value.code == "partial_consent"


# --- transactional activation + recovery (r2 #6, r3 #2/#3) ---

def test_activate_swaps_and_clears_marker(store, setup_mod):
    setup_mod.activate(b'{"installed":{}}',
                       {"refresh_token": "rt"}, {"client_id": "c"})
    assert store.read_marker() is None
    assert store.read_secret_bytes(store.CREDENTIALS_NAME) == b'{"installed":{}}'
    assert store.read_token_envelope()["token"]["refresh_token"] == "rt"


def test_resolve_marker_restores_old_pair(store, setup_mod):
    store.write_secret_bytes(store.CREDENTIALS_NAME, b"OLDCRED")
    store.write_token_envelope({"refresh_token": "old"}, {"client_id": "o"})
    store.write_secret_bytes(setup_mod.RECOVERY_CRED, b"OLDCRED")
    store.write_secret_bytes(
        setup_mod.RECOVERY_TOKEN,
        store.read_secret_bytes(store.TOKEN_NAME))
    # simulate crash: half-written new pair + marker
    store.write_secret_bytes(store.CREDENTIALS_NAME, b"NEWCRED")
    store.write_marker({"state": "activate"})
    assert setup_mod._resolve_pending_marker() is True
    assert store.read_secret_bytes(store.CREDENTIALS_NAME) == b"OLDCRED"
    assert store.read_marker() is None


# --- smoke cleanup (r2 #4/#3/#5) ---

def _fake_drive(files_by_query, deletes, incomplete=False):
    class Files:
        def create(self, body=None, ignoreDefaultVisibility=None, fields=None):
            fid = "doc-" + body["properties"]["skrepka-smoke"]
            files_by_query.setdefault(
                body["properties"]["skrepka-smoke"], []).append(fid)
            return _Exec({"id": fid})

        def list(self, q=None, fields=None, pageSize=None, pageToken=None,
                 spaces=None):
            nonce = q.split("value='")[1].split("'")[0]
            ids = files_by_query.get(nonce, [])
            return _Exec({"files": [{"id": i} for i in ids],
                          "incompleteSearch": incomplete})

        def delete(self, fileId=None):
            deletes.append(fileId)
            for k in list(files_by_query):
                files_by_query[k] = [i for i in files_by_query[k]
                                     if i != fileId]
            return _Exec({})

    class Comments:
        def create(self, fileId=None, fields=None, body=None):
            return _Exec({"id": "cmt"})

    class Drive:
        def files(self):
            return Files()

        def comments(self):
            return Comments()
    return Drive()


class _Exec:
    def __init__(self, r):
        self._r = r

    def execute(self):
        return self._r


def _fake_docs():
    class Docs:
        def documents(self):
            return self

        def get(self, documentId=None):
            return _Exec({"documentId": documentId})
    return Docs()


TOK = {"refresh_token": "rt", "token": "at"}


def _journals(store):
    return [f for f in os.listdir(store.config_dir())
            if f.startswith(store.JOURNAL_PREFIX)]


def test_smoke_ok_removes_journal(store, setup_mod, monkeypatch):
    fbq, deletes = {}, []
    monkeypatch.setattr(setup_mod, "_drive",
                        lambda c: _fake_drive(fbq, deletes))
    monkeypatch.setattr(setup_mod, "_docs", lambda c: _fake_docs())
    signed_in, clean = setup_mod.run_smoke(object(), TOK)
    assert signed_in and clean
    assert deletes  # the created file was deleted (by id)
    assert not _journals(store)


def test_smoke_incomplete_search_stays_pending(store, setup_mod, monkeypatch):
    fbq, deletes = {}, []
    monkeypatch.setattr(setup_mod, "_drive",
                        lambda c: _fake_drive(fbq, deletes, incomplete=True))
    monkeypatch.setattr(setup_mod, "_docs", lambda c: _fake_docs())
    signed_in, clean = setup_mod.run_smoke(object(), TOK)
    assert signed_in and not clean  # created but not provably cleaned
    assert _journals(store)


def test_smoke_create_service_disabled_not_signed_in(store, setup_mod,
                                                     monkeypatch):
    class Drive:
        def files(self):
            return self

        def create(self, **kw):
            raise _http_error(403, "SERVICE_DISABLED")

        def list(self, **kw):
            return _Exec({"files": [], "incompleteSearch": False})

        def delete(self, fileId=None):
            return _Exec({})

        def comments(self):
            return self
    monkeypatch.setattr(setup_mod, "_drive", lambda c: Drive())
    monkeypatch.setattr(setup_mod, "_docs", lambda c: _fake_docs())
    signed_in, clean = setup_mod.run_smoke(object(), TOK)
    assert signed_in is False
    # a definitive 4xx means nothing was created — no lingering journal
    assert clean is True
    assert not _journals(store)


def test_resume_pending_smoke_cleans(store, setup_mod, monkeypatch):
    # a leftover ambiguous-create journal with an orphan findable by tag
    nonce = "abc123"
    fbq = {nonce: ["doc-abc123"]}
    deletes = []
    store.write_secret_bytes(
        store.JOURNAL_PREFIX + nonce + ".json",
        json.dumps({"nonce": nonce, "ambiguous": True,
                    "created_ts": 0, "attempts": 0}).encode())
    store.write_secret_bytes(setup_mod.TOKEN_PREFIX + nonce + ".json",
                             json.dumps({"token": {}}).encode())
    monkeypatch.setattr(setup_mod, "_journal_creds", lambda n: object())
    monkeypatch.setattr(setup_mod, "_drive",
                        lambda c: _fake_drive(fbq, deletes))
    resolved, pending = setup_mod.resume_pending_smoke()
    assert resolved == 1 and pending == 0
    assert "doc-abc123" in deletes


# --- error taxonomy (r1 #11, r2 #8) ---

def _http_error(status, reason=None):
    import httplib2
    from googleapiclient.errors import HttpError
    body = {"error": {"code": status}}
    if reason:
        body["error"]["details"] = [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "domain": "googleapis.com", "reason": reason}]
    return HttpError(resp=httplib2.Response({"status": str(status)}),
                     content=json.dumps(body).encode())


def test_taxonomy_service_disabled(setup_mod):
    code, _ = setup_mod.classify_http_error(
        _http_error(403, "SERVICE_DISABLED"))
    assert code == "api_disabled"


def test_taxonomy_rate_limited(setup_mod):
    code, _ = setup_mod.classify_http_error(_http_error(429))
    assert code == "rate_limited"


def test_taxonomy_server_error(setup_mod):
    code, _ = setup_mod.classify_http_error(_http_error(503))
    assert code == "server_error"


def test_taxonomy_unknown_403_is_forbidden_not_disabled(setup_mod):
    code, _ = setup_mod.classify_http_error(_http_error(403))
    assert code == "forbidden"


def test_taxonomy_never_returns_raw_text(setup_mod):
    err = _http_error(400, "someWeirdReason")
    code, msg = setup_mod.classify_http_error(err)
    assert "someWeirdReason" not in msg


# --- doctor --json sanitization (r2 #8, r3 #4) ---

def test_doctor_json_no_paths_or_secrets(store, setup_mod, monkeypatch, capsys):
    # secret-laden environment; doctor --json must not echo any of it
    secret_path = str(store.config_dir())
    store.write_secret_bytes(store.CREDENTIALS_NAME, cred_bytes())
    store.write_token_envelope(
        {"refresh_token": "SECRET_RT", "token": "SECRET_AT"},
        {"client_id": "cid", "granted_scopes": [],
         "refresh_token_hash": "h"})

    # doctor builds creds via _active_creds (never engine.get_creds); stop it
    # before the network probes so we exercise only sanitization
    monkeypatch.setattr(setup_mod, "_active_creds", lambda: None)
    setup_mod.doctor_main(["--json"])
    out = capsys.readouterr()
    blob = out.out + out.err
    assert "SECRET_RT" not in blob and "SECRET_AT" not in blob
    assert secret_path not in blob
    parsed = json.loads(out.out)
    assert parsed["action"] == "doctor"


def test_doctor_json_bad_usage_is_safe(store, setup_mod, capsys):
    code = setup_mod.doctor_main(["--json", "--bogus", "/home/user/secret"])
    assert code == 2
    out = capsys.readouterr()
    assert "/home/user/secret" not in (out.out + out.err)
    assert json.loads(out.out)["error"] == "bad_usage"


# --- docs probe (r1 #9) ---

def test_docs_probe_404_means_reachable(store, setup_mod, monkeypatch):
    checks = []

    def add(name, status, hint=None, link=None):
        checks.append((name, status))

    class Docs:
        def documents(self):
            return self

        def get(self, documentId=None):
            raise _http_error(404)
    monkeypatch.setattr(setup_mod, "_docs", lambda c: Docs())
    setup_mod._probe_docs(object(), add)
    assert ("docs_api", "ok") in checks


def test_docs_probe_disabled(store, setup_mod, monkeypatch):
    checks = []

    def add(name, status, hint=None, link=None):
        checks.append((name, status))

    class Docs:
        def documents(self):
            return self

        def get(self, documentId=None):
            raise _http_error(403, "SERVICE_DISABLED")
    monkeypatch.setattr(setup_mod, "_docs", lambda c: Docs())
    setup_mod._probe_docs(object(), add)
    assert ("docs_api", "fail") in checks


# --- code-review-r1 fixes ---

def test_validator_rejects_userinfo_redirect(setup_mod):
    raw = cred_bytes(lambda d: d["installed"].update(
        redirect_uris=["http://127.0.0.1:80@evil.example/cb"]))
    with pytest.raises(setup_mod.SetupError):
        setup_mod.validate_credentials_bytes(raw)


def test_validator_rejects_extra_top_level(setup_mod):
    raw = cred_bytes(lambda d: d.update(extra={"x": 1}))
    with pytest.raises(setup_mod.SetupError):
        setup_mod.validate_credentials_bytes(raw)


def test_taxonomy_legacy_service_disabled_not_trusted(setup_mod):
    import httplib2
    from googleapiclient.errors import HttpError
    body = {"error": {"code": 403,
                      "errors": [{"reason": "SERVICE_DISABLED"}]}}
    err = HttpError(resp=httplib2.Response({"status": "403"}),
                    content=json.dumps(body).encode())
    code, _ = setup_mod.classify_http_error(err)
    assert code == "forbidden"  # legacy reason is NOT trusted → not disabled


def test_taxonomy_malformed_body_is_total(setup_mod):
    import httplib2
    from googleapiclient.errors import HttpError
    for content in (b"[]", b"not json", b'{"error": "str"}'):
        err = HttpError(resp=httplib2.Response({"status": "400"}),
                        content=content)
        code, _ = setup_mod.classify_http_error(err)
        assert code == "bad_request"  # no crash, safe generic


def test_persist_refresh_cas_skips_on_concurrent_reauth(store):
    import skrepka._engine as engine
    # on-disk already holds a DIFFERENT (newer) grant from a concurrent reauth
    store.write_token_envelope({"refresh_token": "NEW", "token": "at_new"},
                               {"granted_scopes": ["a"]})

    class Creds:
        granted_scopes = None

        def to_json(self):
            return json.dumps({"refresh_token": "OLD",
                               "token": "at_refreshed"})

    started = {"token": {"refresh_token": "OLD"}, "provenance": {}}
    engine._persist_refreshed_token(Creds(), started)
    assert store.read_token_envelope()["token"]["refresh_token"] == "NEW"


def test_persist_refresh_updates_when_matching(store):
    import skrepka._engine as engine
    store.write_token_envelope(
        {"refresh_token": "RT", "token": "old_at"},
        {"granted_scopes": list(engine.SCOPES)})

    class Creds:
        granted_scopes = None

        def to_json(self):
            return json.dumps({"refresh_token": "RT", "token": "fresh_at"})

    started = {"token": {"refresh_token": "RT"}, "provenance": {}}
    engine._persist_refreshed_token(Creds(), started)
    env = store.read_token_envelope()
    assert env["token"]["token"] == "fresh_at"
    # provenance rebased on the freshest on-disk copy (code-r3 #3)
    assert env["provenance"]["granted_scopes"] == list(engine.SCOPES)


def test_persist_refresh_blocks_reduced_scope(store, capsys):
    import skrepka._engine as engine
    store.write_token_envelope(
        {"refresh_token": "RT", "token": "old_at"},
        {"granted_scopes": list(engine.SCOPES)})

    class Creds:
        granted_scopes = [engine.SCOPES[0]]  # refresh returned a REDUCED grant

        def to_json(self):
            return json.dumps({"refresh_token": "RT", "token": "fresh_at"})

    started = {"token": {"refresh_token": "RT"},
               "provenance": {"granted_scopes": list(engine.SCOPES)}}
    with pytest.raises(SystemExit):
        engine._persist_refreshed_token(Creds(), started)
    assert "--reauth" in json.loads(capsys.readouterr().out)["error"]


def test_reconcile_removes_orphan_token(store, setup_mod):
    # a bound-token sidecar with no journal must be dropped (code-r3 #2)
    store.write_secret_bytes(setup_mod.TOKEN_PREFIX + "orphan.json",
                             json.dumps({"token": {}}).encode())
    setup_mod._reconcile_journal_tokens()
    assert not [f for f in os.listdir(store.config_dir())
                if f.startswith(setup_mod.TOKEN_PREFIX)]


def test_resume_drops_journal_without_token(store, setup_mod):
    # journal present, bound token already gone → resolved leftover, removed
    store.write_secret_bytes(store.JOURNAL_PREFIX + "leftover.json",
                             json.dumps({"nonce": "leftover"}).encode())
    resolved, pending = setup_mod.resume_pending_smoke()
    assert resolved == 1 and pending == 0
    assert not _journals(store)


def test_doctor_scopes_warn_on_provenance_mismatch(store, setup_mod,
                                                   monkeypatch, capsys):
    from skrepka._engine import SCOPES
    store.write_secret_bytes(store.CREDENTIALS_NAME, cred_bytes())
    store.write_token_envelope(
        {"refresh_token": "rt"},
        {"client_id": "123-abc.apps.googleusercontent.com",
         "refresh_token_hash": "WRONGHASH", "granted_scopes": list(SCOPES)})
    monkeypatch.setattr(setup_mod, "_active_creds", lambda: None)
    setup_mod.doctor_main(["--json"])
    checks = json.loads(capsys.readouterr().out)["checks"]
    scopes = [c for c in checks if c["name"] == "scopes"][0]
    assert scopes["status"] == "warn"


def test_resume_horizon_removes_stale_journal(store, setup_mod, monkeypatch):
    nonce = "ghost1"
    store.write_secret_bytes(
        store.JOURNAL_PREFIX + nonce + ".json",
        json.dumps({"nonce": nonce, "ambiguous": True, "created_ts": 0,
                    "attempts": setup_mod._RESUME_HORIZON - 1}).encode())
    store.write_secret_bytes(setup_mod.TOKEN_PREFIX + nonce + ".json",
                             json.dumps({"token": {}}).encode())
    monkeypatch.setattr(setup_mod, "_journal_creds", lambda n: object())
    monkeypatch.setattr(setup_mod, "_drive", lambda c: _fake_drive({}, []))
    resolved, pending = setup_mod.resume_pending_smoke()
    assert resolved == 1 and pending == 0  # horizon + old age → concluded gone
    assert not _journals(store)


def test_resume_keeps_journal_before_horizon(store, setup_mod, monkeypatch):
    nonce = "ghost2"
    store.write_secret_bytes(
        store.JOURNAL_PREFIX + nonce + ".json",
        json.dumps({"nonce": nonce, "ambiguous": True, "created_ts": 0,
                    "attempts": 0}).encode())
    store.write_secret_bytes(setup_mod.TOKEN_PREFIX + nonce + ".json",
                             json.dumps({"token": {}}).encode())
    monkeypatch.setattr(setup_mod, "_journal_creds", lambda n: object())
    monkeypatch.setattr(setup_mod, "_drive", lambda c: _fake_drive({}, []))
    resolved, pending = setup_mod.resume_pending_smoke()
    assert resolved == 0 and pending == 1
    data = json.loads(store.read_secret_bytes(
        store.JOURNAL_PREFIX + nonce + ".json"))
    assert data["attempts"] == 1  # incremented, not abandoned


def test_resume_young_journal_not_abandoned(store, setup_mod, monkeypatch):
    # attempts past horizon but the journal is YOUNG → keep (index may lag)
    import time as _t
    nonce = "young1"
    store.write_secret_bytes(
        store.JOURNAL_PREFIX + nonce + ".json",
        json.dumps({"nonce": nonce, "ambiguous": True,
                    "created_ts": _t.time(),  # just now
                    "attempts": setup_mod._RESUME_HORIZON + 5}).encode())
    store.write_secret_bytes(setup_mod.TOKEN_PREFIX + nonce + ".json",
                             json.dumps({"token": {}}).encode())
    monkeypatch.setattr(setup_mod, "_journal_creds", lambda n: object())
    monkeypatch.setattr(setup_mod, "_drive", lambda c: _fake_drive({}, []))
    resolved, pending = setup_mod.resume_pending_smoke()
    assert resolved == 0 and pending == 1  # min-age gate holds it


def test_activate_crash_between_writes_recovers(store, setup_mod, monkeypatch):
    # an existing working pair we must not lose
    store.write_secret_bytes(store.CREDENTIALS_NAME, b"OLDCRED")
    store.write_token_envelope({"refresh_token": "old"}, {"client_id": "o"})

    real_write_env = store.write_token_envelope

    def boom(token, prov):
        raise RuntimeError("crash mid-activation")
    monkeypatch.setattr(store, "write_token_envelope", boom)
    with pytest.raises(RuntimeError):
        setup_mod.activate(b"NEWCRED", {"refresh_token": "new"},
                           {"client_id": "n"})
    monkeypatch.setattr(store, "write_token_envelope", real_write_env)
    # marker present, credentials half-swapped — recovery must restore the old
    assert store.read_marker() is not None
    setup_mod._resolve_pending_marker()
    assert store.read_secret_bytes(store.CREDENTIALS_NAME) == b"OLDCRED"
    assert store.read_token_envelope()["token"]["refresh_token"] == "old"
    assert store.read_marker() is None


def test_config_lock_acquires_and_releases(store):
    with store.lock():
        pass
    with store.lock():  # a second acquisition must not deadlock
        pass


def test_docs_probe_400_inconclusive(store, setup_mod, monkeypatch):
    checks = []

    def add(name, status, hint=None, link=None):
        checks.append((name, status))

    class Docs:
        def documents(self):
            return self

        def get(self, documentId=None):
            raise _http_error(400)
    monkeypatch.setattr(setup_mod, "_docs", lambda c: Docs())
    setup_mod._probe_docs(object(), add)
    assert ("docs_api", "inconclusive") in checks


def test_resume_skips_fresh_journal(store, setup_mod, monkeypatch):
    # a fresh journal with an orphan file must NOT be swept — it may belong
    # to another init's in-flight smoke (delta-2)
    import time as _t
    nonce = "inflight"
    fbq = {nonce: ["doc-inflight"]}
    deletes = []
    store.write_secret_bytes(
        store.JOURNAL_PREFIX + nonce + ".json",
        json.dumps({"nonce": nonce, "ambiguous": True,
                    "created_ts": _t.time(), "attempts": 0}).encode())
    store.write_secret_bytes(setup_mod.TOKEN_PREFIX + nonce + ".json",
                             json.dumps({"token": {}}).encode())
    monkeypatch.setattr(setup_mod, "_journal_creds", lambda n: object())
    monkeypatch.setattr(setup_mod, "_drive",
                        lambda c: _fake_drive(fbq, deletes))
    resolved, pending = setup_mod.resume_pending_smoke()
    assert resolved == 0 and pending == 1
    assert deletes == []  # the in-flight file was left alone
