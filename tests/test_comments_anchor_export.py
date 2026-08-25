"""Issue #37: comments exposes honest, read-only DOCX anchor evidence."""

import io
import json
import zipfile


WORDML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class _Result:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _docx(records):
    rows = "".join(
        f'<w:comment w:id="{i}" w:author="{author}" w:date="{created}"/>'
        for i, author, created in records)
    xml = (f'<?xml version="1.0"?><w:comments xmlns:w="{WORDML}">'
           f"{rows}</w:comments>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/comments.xml", xml)
    return buf.getvalue()


def _comment(cid, author, created, quote=None, *, resolved=False):
    row = {
        "id": cid,
        "content": cid,
        "author": {"displayName": author, "me": True},
        "createdTime": created,
        "resolved": resolved,
        "replies": [],
    }
    if quote is not None:
        row["quotedFileContent"] = {"value": quote}
    return row


def _doc(text):
    end = 1 + len(text)
    return {"revisionId": "R1", "body": {"content": [{
        "startIndex": 1,
        "endIndex": end,
        "paragraph": {"elements": [{
            "startIndex": 1,
            "endIndex": end,
            "textRun": {"content": text},
        }]},
    }]}}


class _Services:
    def __init__(self, comments, doc, export):
        self.comment_rows = comments
        self.doc = doc
        self.export_payload = export
        self.comment_calls = []
        self.get_calls = []
        self.export_calls = []
        self.batch_calls = []
        self.write_calls = []

    def comments(self):
        return self

    def list(self, **kwargs):
        self.comment_calls.append(kwargs)
        return _Result({"comments": self.comment_rows})

    def documents(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Result(self.doc)

    def files(self):
        return self

    def export(self, **kwargs):
        self.export_calls.append(kwargs)
        return _Result(self.export_payload)

    def batchUpdate(self, **kwargs):  # pragma: no cover - safety assertion
        self.batch_calls.append(kwargs)
        raise AssertionError("comments must not write a freshness canary")

    def replies(self):  # pragma: no cover - only a hidden write can use it
        return self

    def create(self, **kwargs):  # pragma: no cover - safety assertion
        self.write_calls.append(("create", kwargs))
        raise AssertionError("comments must not create comments or replies")

    def update(self, **kwargs):  # pragma: no cover - safety assertion
        self.write_calls.append(("update", kwargs))
        raise AssertionError("comments must not update comments or replies")

    def delete(self, **kwargs):  # pragma: no cover - safety assertion
        self.write_calls.append(("delete", kwargs))
        raise AssertionError("comments must not delete comments or replies")

    def copy(self, **kwargs):  # pragma: no cover - safety assertion
        self.write_calls.append(("copy", kwargs))
        raise AssertionError("comments must not copy the document")


class _SequencedServices(_Services):
    def __init__(self, comment_snapshots, docs, export):
        super().__init__(comment_snapshots[0], docs[0], export)
        self.comment_snapshots = comment_snapshots
        self.docs = docs

    def list(self, **kwargs):
        self.comment_calls.append(kwargs)
        index = min(len(self.comment_calls) - 1,
                    len(self.comment_snapshots) - 1)
        return _Result({"comments": self.comment_snapshots[index]})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        index = min(len(self.get_calls) - 1, len(self.docs) - 1)
        return _Result(self.docs[index])


class _PagedServices(_Services):
    def __init__(self, pages, doc, export):
        super().__init__([], doc, export)
        self.pages = pages

    def list(self, **kwargs):
        self.comment_calls.append(kwargs)
        page_token = kwargs.get("pageToken")
        if page_token == "p2":
            return _Result({"comments": self.pages[1]})
        return _Result({"comments": self.pages[0], "nextPageToken": "p2"})


def _wire(engine, monkeypatch, comments, doc, export):
    services = _Services(comments, doc, export)
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda _creds: services)
    monkeypatch.setattr(engine, "get_docs_service", lambda _creds: services)
    return services


def _wire_services(engine, monkeypatch, services):
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda _creds: services)
    monkeypatch.setattr(engine, "get_docs_service", lambda _creds: services)
    return services


def test_comments_distinguishes_export_record_ghost_and_unknown(
        engine, monkeypatch, capsys, tmp_path):
    comments = [
        _comment("present", "A", "2026-01-01T00:00:01Z", "live text"),
        _comment("ghost", "B", "2026-01-01T00:00:02Z", "deleted text"),
        _comment("still-there", "D", "2026-01-01T00:00:02Z", "still here"),
        _comment("later", "C", "2026-01-01T00:00:03Z", "later text"),
        _comment("newest", "N", "2026-01-01T00:00:06Z", "newest deleted"),
        _comment("shared-1", "S", "2026-01-01T00:00:05Z", "shared one"),
        _comment("shared-2", "S", "2026-01-01T00:00:05Z", "shared two"),
        _comment("document", "U", "2026-01-01T00:00:07Z"),
        _comment("resolved", "R", "2026-01-01T00:00:08Z", "old",
                 resolved=True),
    ]
    export = _docx([
        ("0", "A", "2026-01-01T00:00:01Z"),
        ("1", "C", "2026-01-01T00:00:03Z"),
        ("2", "S", "2026-01-01T00:00:05Z"),
    ])
    services = _wire(
        engine, monkeypatch, comments,
        _doc("live text\nstill here\nlater text\nshared one\nshared two\n"),
        export)
    target = tmp_path / "comments.json"

    engine.list_comments("doc1", output=str(target))

    rows = {row["id"]: row for row in json.loads(target.read_text())}
    receipt = json.loads(capsys.readouterr().out)
    assert rows["present"]["anchor_export"] == {
        "status": "record_present",
        "reason": "unique_thread_record_found_in_export",
        "record_count": 1,
        "export_freshness": "unproven",
    }
    assert rows["ghost"]["anchor_export"] == {
        "status": "ghost",
        "reason": ("record_missing_after_newer_export_record_and_"
                   "quote_absent_from_document"),
        "export_freshness": "unproven",
    }
    assert rows["still-there"]["anchor_export"]["status"] == "unknown"
    assert rows["still-there"]["anchor_export"]["reason"] == (
        "record_missing_but_quote_still_present")
    assert rows["newest"]["anchor_export"]["reason"] == (
        "record_missing_export_freshness_unproven")
    assert rows["shared-1"]["anchor_export"]["reason"] == (
        "shared_or_missing_export_identity")
    assert rows["shared-2"]["anchor_export"]["reason"] == (
        "shared_or_missing_export_identity")
    assert rows["document"]["anchor_export"]["status"] == "not_applicable"
    assert rows["resolved"]["anchor_export"]["reason"] == (
        "resolved_threads_omitted_from_export")

    assert receipt["anchor_record_present"] == 2
    assert receipt["anchor_ghost"] == 1
    assert receipt["anchor_unknown"] == 4
    assert len(services.export_calls) == 1
    assert services.batch_calls == []
    assert services.write_calls == []
    assert services.comment_calls[0]["includeDeleted"] is True
    assert "deleted" in services.comment_calls[0]["fields"]
    assert services.comment_calls[0]["fields"].count("author/me") == 2


def test_stale_record_is_never_described_as_a_live_anchor(
        engine, monkeypatch, capsys):
    comment = _comment(
        "c1", "A", "2026-01-01T00:00:01Z", "text since deleted")
    _wire(engine, monkeypatch, [comment], _doc("current text\n"), _docx([
        ("0", "A", "2026-01-01T00:00:01Z"),
    ]))

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["anchor_export"]["status"] == "record_present"
    assert row["anchor_export"]["export_freshness"] == "unproven"
    assert "present" not in row["anchor_export"] or (
        row["anchor_export"].get("present") is not True)


def test_export_failure_keeps_comments_and_marks_anchor_unknown(
        engine, monkeypatch, capsys):
    comment = _comment("c1", "A", "2026-01-01T00:00:01Z", "text")
    services = _wire(
        engine, monkeypatch, [comment], _doc("text\n"),
        RuntimeError("export offline"))

    engine.list_comments("doc1")

    captured = capsys.readouterr()
    row = json.loads(captured.out)[0]
    assert row["anchor_export"] == {
        "status": "unknown",
        "reason": "document_export_unavailable",
        "export_freshness": "unproven",
    }
    assert "export offline" in captured.err
    assert services.batch_calls == []


def test_shared_reply_record_cannot_turn_missing_unique_witness_into_ghost(
        engine):
    comment = _comment(
        "c1", "A", "2026-01-01T00:00:01Z", "deleted text")
    comment["replies"] = [{
        "id": "r1",
        "author": {"displayName": "S"},
        "createdTime": "2026-01-01T00:00:02Z",
    }]
    neighbour = _comment(
        "c2", "S", "2026-01-01T00:00:02Z", "other")
    universe = engine._key_owners_universe([comment, neighbour])

    status = engine._comment_anchor_export_status(
        comment,
        records=[{"docx_id": "0", "author": "S",
                  "date_sec": "2026-01-01T00:00:02Z"}],
        universe=universe,
        tabs=[(None, "", _doc("current text\n"))],
        file_id="doc1",
    )

    assert status["status"] == "unknown"
    assert status["reason"] == "ambiguous_record_may_belong_to_thread"


def test_reopened_thread_requires_export_newer_than_reopen(
        engine, monkeypatch, capsys):
    target = _comment(
        "c1", "A", "2026-01-01T00:00:01Z", "old anchored text")
    target["replies"] = [
        {"id": "resolve", "author": {"displayName": "A", "me": True},
         "createdTime": "2026-01-01T00:00:04Z", "action": "resolve"},
        {"id": "reopen", "author": {"displayName": "A", "me": True},
         "createdTime": "2026-01-01T00:00:10Z", "action": "reopen"},
    ]
    later_only_before_reopen = _comment(
        "c2", "B", "2026-01-01T00:00:05Z", "other")
    services = _wire(
        engine, monkeypatch, [target, later_only_before_reopen],
        _doc("current text\n"),
        _docx([("0", "B", "2026-01-01T00:00:05Z")]))

    engine.list_comments("doc1")

    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["c1"]["anchor_export"] == {
        "status": "unknown",
        "reason": "record_missing_export_freshness_unproven",
        "export_freshness": "unproven",
    }
    assert all("action" not in reply for reply in rows["c1"]["replies"])
    assert services.batch_calls == []
    assert services.write_calls == []


def test_persistent_comment_race_makes_both_attributions_unknown(
        engine, monkeypatch, capsys):
    target = _comment("c1", "A", "2026-01-01T00:00:01Z", "quote")
    snapshots = [
        [target],
        [target, _comment("c2", "B", "2026-01-01T00:00:02Z")],
        [target, _comment("c2", "B", "2026-01-01T00:00:02Z"),
         _comment("c3", "C", "2026-01-01T00:00:03Z")],
    ]
    services = _wire_services(
        engine, monkeypatch,
        _SequencedServices(snapshots, [_doc("quote\n")] * 4, _docx([])))

    engine.list_comments("doc1")

    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["c1"]["tab_attribution"]["reason"] == (
        "snapshot_changed_during_read")
    assert rows["c1"]["anchor_export"]["reason"] == (
        "snapshot_changed_during_read")
    assert len(services.comment_calls) == 3
    assert len(services.get_calls) == 4
    assert len(services.export_calls) == 2


def test_persistent_document_revision_race_is_not_mixed_with_export(
        engine, monkeypatch, capsys):
    target = _comment("c1", "A", "2026-01-01T00:00:01Z", "quote")
    docs = []
    for revision in ("R1", "R2", "R2", "R3"):
        snapshot = _doc("quote\n")
        snapshot["revisionId"] = revision
        docs.append(snapshot)
    services = _wire_services(
        engine, monkeypatch,
        _SequencedServices([[target]] * 3, docs, _docx([])))

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["tab_attribution"]["status"] == "unknown"
    assert row["tab_attribution"]["reason"] == (
        "snapshot_changed_during_read")
    assert row["anchor_export"]["reason"] == (
        "snapshot_changed_during_read")
    assert len(services.export_calls) == 2


def test_stable_view_only_docs_without_revision_keep_exact_evidence(
        engine, monkeypatch, capsys):
    target = _comment("c1", "A", "2026-01-01T00:00:01Z", "quote")
    view_only_doc = {"tabs": [{
        "tabProperties": {"tabId": "view-tab", "title": "View only"},
        "documentTab": {"body": _doc("quote\n")["body"]},
    }]}
    services = _wire(
        engine, monkeypatch, [target], view_only_doc,
        _docx([("0", "A", "2026-01-01T00:00:01Z")]))

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["tab_id"] == "view-tab"
    assert row["tab_attribution"]["status"] == "exact"
    assert row["tab_attribution"]["candidates"][0][
        "quote_occurrences"] == 1
    assert row["anchor_export"]["status"] == "record_present"
    assert len(services.get_calls) == 2
    assert len(services.export_calls) == 1


def test_view_only_content_hash_race_is_unknown_after_retry(
        engine, monkeypatch, capsys):
    target = _comment("c1", "A", "2026-01-01T00:00:01Z", "quote")
    docs = []
    for text in ("quote one\n", "quote two\n",
                 "quote three\n", "quote four\n"):
        snapshot = _doc(text)
        snapshot.pop("revisionId")
        docs.append(snapshot)
    services = _wire_services(
        engine, monkeypatch,
        _SequencedServices([[target]] * 3, docs, _docx([])))

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["tab_attribution"]["reason"] == (
        "snapshot_changed_during_read")
    assert row["anchor_export"]["reason"] == (
        "snapshot_changed_during_read")
    assert len(services.get_calls) == 4
    assert len(services.export_calls) == 2


def test_revision_present_on_only_one_boundary_is_unknown(
        engine, monkeypatch, capsys):
    target = _comment("c1", "A", "2026-01-01T00:00:01Z", "quote")
    with_revision = _doc("quote\n")
    without_revision = _doc("quote\n")
    without_revision.pop("revisionId")
    services = _wire_services(
        engine, monkeypatch,
        _SequencedServices(
            [[target]] * 3,
            [with_revision, without_revision,
             with_revision, without_revision],
            _docx([])))

    engine.list_comments("doc1")

    row = json.loads(capsys.readouterr().out)[0]
    assert row["tab_attribution"]["reason"] == (
        "document_revision_unavailable")
    assert row["anchor_export"]["reason"] == (
        "document_revision_unavailable")
    assert len(services.export_calls) == 2


def test_malformed_export_identity_cannot_support_absence_verdict(
        engine, monkeypatch, capsys):
    target = _comment(
        "c1", "A", "2026-01-01T00:00:01Z", "deleted quote")
    _wire(
        engine, monkeypatch, [target], _doc("current text\n"),
        _docx([("0", "", ""),
               ("1", "B", "2026-01-01T00:00:03Z")]))

    engine.list_comments("doc1")

    captured = capsys.readouterr()
    row = json.loads(captured.out)[0]
    assert row["anchor_export"] == {
        "status": "unknown",
        "reason": "export_comment_identity_unreadable",
        "export_freshness": "unproven",
    }
    assert "no readable author/date identity" in captured.err


def test_paginated_second_page_collisions_and_later_records_are_counted(
        engine, monkeypatch, capsys):
    shared = _comment(
        "shared", "A", "2026-01-01T00:00:01Z", "deleted shared")
    ghost = _comment(
        "ghost", "C", "2026-01-01T00:00:02Z", "deleted unique")
    collision = _comment(
        "collision", "A", "2026-01-01T00:00:01Z", "other")
    later = _comment(
        "later", "D", "2026-01-01T00:00:03Z", "later")
    services = _wire_services(
        engine, monkeypatch,
        _PagedServices(
            [[shared, ghost], [collision, later]], _doc("current text\n"),
            _docx([("0", "D", "2026-01-01T00:00:03Z")])))

    engine.list_comments("doc1")

    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["shared"]["anchor_export"]["reason"] == (
        "shared_or_missing_export_identity")
    assert rows["ghost"]["anchor_export"]["status"] == "ghost"
    assert [call.get("pageToken") for call in services.comment_calls] == [
        None, "p2", None, "p2"]


def test_quote_in_nested_child_tab_prevents_ghost_and_gets_exact_tab(
        engine, monkeypatch, capsys):
    target = _comment(
        "c1", "A", "2026-01-01T00:00:01Z", "child-only quote")
    later = _comment(
        "c2", "B", "2026-01-01T00:00:02Z", "later")
    child_body = _doc("child-only quote\n")["body"]
    root_body = _doc("root text\n")["body"]
    doc = {
        "revisionId": "R1",
        "tabs": [{
            "tabProperties": {"tabId": "root", "title": "Root"},
            "documentTab": {"body": root_body},
            "childTabs": [{
                "tabProperties": {"tabId": "child", "title": "Child"},
                "documentTab": {"body": child_body},
            }],
        }],
    }
    _wire(
        engine, monkeypatch, [target, later], doc,
        _docx([("0", "B", "2026-01-01T00:00:02Z")]))

    engine.list_comments("doc1")

    row = {row["id"]: row for row in json.loads(capsys.readouterr().out)}["c1"]
    assert row["tab_id"] == "child"
    assert row["tab_attribution"]["status"] == "exact"
    assert row["anchor_export"]["status"] == "unknown"
    assert row["anchor_export"]["reason"] == (
        "record_missing_but_quote_still_present")


def test_comments_evidence_path_has_no_hidden_writes(
        engine, monkeypatch, capsys):
    comment = _comment("c1", "A", "2026-01-01T00:00:01Z", "quote")
    services = _wire(
        engine, monkeypatch, [comment], _doc("quote\n"),
        _docx([("0", "A", "2026-01-01T00:00:01Z")]))

    engine.list_comments("doc1")
    capsys.readouterr()

    assert services.batch_calls == []
    assert services.write_calls == []
    assert len(services.comment_calls) == 2
    assert len(services.get_calls) == 2
    assert len(services.export_calls) == 1
