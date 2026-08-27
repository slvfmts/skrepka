"""Anchor accounting, export canary and protected-interval checks
(PLAN-sync-anchors v4): sync/patch structural edits on commented docs."""

import io
import json
import zipfile

import pytest

WORDML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def make_docx_full(paras, comments):
    """docx with document.xml AND comments.xml.

    paras: [(text, [(cid, start_off, end_off)])] — offsets in characters.
    comments: [(cid, author, date)].
    """
    body = []
    for text, anchors in paras:
        cuts = sorted({0, len(text)}
                      | {o for _c, s, e in anchors for o in (s, e)})
        runs = []
        for a, b in zip(cuts, cuts[1:]):
            seg = text[a:b]
            for cid, s, _e in anchors:
                if s == a:
                    runs.append(f'<w:commentRangeStart w:id="{cid}"/>')
            runs.append(f"<w:r><w:t xml:space=\"preserve\">{seg}</w:t></w:r>")
            for cid, _s, e in anchors:
                if e == b:
                    runs.append(f'<w:commentRangeEnd w:id="{cid}"/>')
                    runs.append(f'<w:commentReference w:id="{cid}"/>')
        if not text:
            for cid, s, e in anchors:
                runs.append(f'<w:commentRangeStart w:id="{cid}"/>')
                runs.append(f'<w:commentRangeEnd w:id="{cid}"/>')
        body.append(f"<w:p>{''.join(runs)}</w:p>")
    document = (f'<?xml version="1.0"?><w:document xmlns:w="{WORDML}">'
                f"<w:body>{''.join(body)}</w:body></w:document>")
    centries = "".join(
        f'<w:comment w:id="{cid}" w:author="{author}" w:date="{date}">'
        f"<w:p><w:r><w:t>c</w:t></w:r></w:p></w:comment>"
        for cid, author, date in comments)
    comments_xml = (f'<?xml version="1.0"?><w:comments xmlns:w="{WORDML}">'
                    f"{centries}</w:comments>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", document)
        if comments:
            z.writestr("word/comments.xml", comments_xml)
    return buf.getvalue()


def make_doc(texts, named_ranges=None, rev="R0"):
    """Docs API document JSON with plain NORMAL_TEXT paragraphs."""
    content, idx = [], 1
    for t in texts:
        s, e = idx, idx + len(t) + 1
        content.append({"startIndex": s, "endIndex": e, "paragraph": {
            "elements": [{"startIndex": s, "endIndex": e,
                          "textRun": {"content": t + "\n", "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        }})
        idx = e
    doc = {"documentId": "doc1", "revisionId": rev,
           "body": {"content": content}}
    if named_ranges:
        doc["namedRanges"] = named_ranges
    return doc


def semantic_batch(docs):
    """The batch that carries the edit: canary delete first, then the write.

    Since 0.17 the anchor-safe path writes by absolute index instead of
    searching the tab for a string, so a batch is recognised by shape rather
    than by the presence of `replaceAllText`.
    """
    for b in docs.batches:
        if len(b) > 1 and "deleteContentRange" in b[0]:
            return b
    raise AssertionError("no semantic batch was sent")


def written_text(batch):
    """The text the semantic batch inserts, or '' for a pure deletion."""
    return "".join(r["insertText"]["text"] for r in batch
                   if "insertText" in r)


def api_comment(cid, author, created, resolved=False, replies=()):
    return {"id": cid, "createdTime": created,
            "author": {"displayName": author},
            "quotedFileContent": {"value": "x"},
            "resolved": resolved, "content": "c",
            "replies": list(replies)}


# ---------------------------------------------------------------------------
# service stubs
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r


def http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError
    return HttpError(resp=httplib2.Response({"status": str(status)}),
                     content=b"boom")


class DocsStub:
    """Scripted Docs service: R0 doc + canary lifecycle + merged doc.

    Failure injection: insert_error/main_error raise at execute() time;
    *_applies=True mutates state anyway (applied-but-response-lost);
    fail_cleanup rejects the canary cleanup delete; post_insert_rev fakes
    a concurrent edit right after the canary insert.
    """

    def __init__(self, base_doc, merged_doc=None):
        self.base, self.merged = base_doc, merged_doc
        self.rev = base_doc["revisionId"]
        self.canary_text = None
        self.batches = []
        self.main_applied = False
        self.insert_error = None
        self.insert_error_applies = False
        self.main_error = None
        self.main_error_applies = False
        self.fail_cleanup = False
        self.post_insert_rev = None

    def documents(self):
        return self

    def get(self, documentId=None, fields=None, **kw):
        if fields == "revisionId":
            if self.post_insert_rev and self.canary_text is not None:
                return _Req({"revisionId": self.post_insert_rev})
            return _Req({"revisionId": self.rev})
        return _Req(self._current())

    def _current(self):
        doc = json.loads(json.dumps(
            self.merged if self.main_applied else self.base))
        doc["revisionId"] = self.rev
        if self.canary_text is not None and not self.main_applied:
            content = doc["body"]["content"]
            end = content[-1]["endIndex"]
            t = self.canary_text
            content.append({"startIndex": end, "endIndex": end + len(t) + 1,
                            "paragraph": {"elements": [{
                                "startIndex": end, "endIndex": end + len(t) + 1,
                                "textRun": {"content": t + "\n",
                                            "textStyle": {}}}],
                                "paragraphStyle": {
                                    "namedStyleType": "NORMAL_TEXT"}}})
        return doc

    def batchUpdate(self, documentId=None, body=None):
        reqs = body["requests"]
        self.batches.append(reqs)
        first = reqs[0]
        if (len(reqs) == 1 and "insertText" in first
                and "skrepka-canary" in first["insertText"]["text"]):
            if self.insert_error is not None:
                err, self.insert_error = self.insert_error, None
                if self.insert_error_applies:
                    self.canary_text = \
                        first["insertText"]["text"].lstrip("\n")
                    self.rev = "R1"
                return _Req(err)
            self.canary_text = first["insertText"]["text"].lstrip("\n")
            self.rev = "R1"
            return _Req({"writeControl": {"requiredRevisionId": "R1"}})
        if (len(reqs) == 1 and "deleteContentRange" in first
                and self.canary_text is not None):
            if self.fail_cleanup:
                return _Req(http_error(409))
            self.canary_text = None  # cleanup
            self.rev = "R1c"
            return _Req({"writeControl": {"requiredRevisionId": "R1c"}})
        if self.main_error is not None and self.merged is not None:
            err, self.main_error = self.main_error, None
            if self.main_error_applies:
                self.main_applied = True
                self.canary_text = None
                self.rev = "R2"
            return _Req(err)
        if any("replaceAllText" in r for r in reqs):
            self.canary_text = None
            self.rev = "R2"
            return _Req({"writeControl": {"requiredRevisionId": "R2"},
                         "replies": [
                             {"replaceAllText": {"occurrencesChanged": 1}}
                             if "replaceAllText" in r else {}
                             for r in reqs]})
        if not self.main_applied and self.merged is not None:
            self.main_applied = True
            self.canary_text = None
            self.rev = "R2"
            return _Req({"writeControl": {"requiredRevisionId": "R2"},
                         "replies": [{} for _ in reqs]})
        # index-addressed semantic batch: canary delete first, then the edit
        if (len(reqs) > 1 and "deleteContentRange" in first
                and self.canary_text is not None
                and any("insertText" in r or "deleteContentRange" in r
                        for r in reqs[1:])):
            self.canary_text = None
            self.rev = "R2"
            return _Req({"writeControl": {"requiredRevisionId": "R2"},
                         "replies": [{} for _ in reqs]})
        # style batch etc.
        self.rev = "R3"
        return _Req({"writeControl": {"requiredRevisionId": "R3"},
                     "replies": [{} for _ in reqs]})


class DriveStub:
    def __init__(self, comments, docx_builder, html=b"<p>x</p>",
                 comments_after=None, switch_after_lists=None):
        self._comments = comments
        self._docx_builder = docx_builder
        self._html = html
        self._comments_after = comments_after
        self._switch_after = switch_after_lists
        self._list_calls = 0

    def comments(self):
        return self

    def list(self, **kw):
        self._list_calls += 1
        payload = self._comments
        if (self._switch_after is not None
                and self._list_calls > self._switch_after):
            payload = self._comments_after
        return _Req({"comments": json.loads(json.dumps(payload))})

    def files(self):
        return self

    def export(self, fileId=None, mimeType=None):
        if "wordprocessingml" in mimeType:
            return _Req(self._docx_builder())
        return _Req(self._html)


def wire(engine, monkeypatch, docs, drive):
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda c: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda c: drive)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)


def make_workdir(engine, tmp_path, doc, base_md, local_md):
    md_path = tmp_path / "doc.md"
    md_path.write_text(base_md, encoding="utf-8")
    payload = engine._sidecar_payload("doc1", str(md_path), base_md, doc)
    assert payload["sync_supported"], payload["reason"]
    (tmp_path / ("doc.md" + engine.SIDECAR_SUFFIX)).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(local_md, encoding="utf-8")
    return str(md_path)


# ---------------------------------------------------------------------------
# units: accounting
# ---------------------------------------------------------------------------

def test_trunc_seconds(engine):
    assert engine._trunc_seconds("2026-07-13T17:53:08.108Z") == \
        "2026-07-13T17:53:08Z"
    assert engine._trunc_seconds("2026-07-13T17:53:08Z") == \
        "2026-07-13T17:53:08Z"
    assert engine._trunc_seconds("") == ""


def test_docx_comment_records_parses_and_requires_unique_ids(engine):
    docx = make_docx_full(
        [("Alpha", [("0", 0, 5)]), ("Bravo", [("1", 0, 5)])],
        [("0", "A", "2026-07-13T17:53:08Z"),
         ("1", "B", "2026-07-13T17:53:09Z")])
    records, problems = engine._docx_comment_records(docx)
    assert problems == []
    assert {r["docx_id"] for r in records} == {"0", "1"}
    assert records[0]["author"] == "A"


def test_docx_comment_records_no_comments_part(engine, make_docx):
    records, problems = engine._docx_comment_records(
        make_docx("<w:p><w:r><w:t>x</w:t></w:r></w:p>"))
    assert records == [] and problems == []


def test_docx_comment_records_duplicate_id_is_problem(engine):
    docx = make_docx_full(
        [("Alpha", [("0", 0, 5)])],
        [("0", "A", "d1"), ("0", "A", "d2")])
    _records, problems = engine._docx_comment_records(docx)
    assert any("duplicate w:id" in p for p in problems)


def _spans(*ids):
    return [{"docx_id": i} for i in ids]


def test_accounting_happy_with_reply(engine):
    anchored = [api_comment("c1", "A", "2026-07-13T17:53:08.108Z",
                            replies=[{"createdTime": "2026-07-13T17:54:32.202Z",
                                      "author": {"displayName": "A"}}])]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-07-13T17:53:08Z"},
               {"docx_id": "1", "author": "A",
                "date_sec": "2026-07-13T17:54:32Z"}]
    problems, metrics = engine._account_anchored_comments(
        anchored, records, _spans("0", "1"),
        universe=engine._key_owners_universe(anchored))
    assert problems == []
    assert metrics["api_threads_accounted"] == 1
    assert metrics["docx_comment_entries"] == 2
    assert metrics["anchor_spans"] == 2


def test_accounting_missing_entry_blocks(engine):
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "A", "2026-01-01T00:00:02Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0"),
        universe=engine._key_owners_universe(anchored))
    assert any("missing from the export" in p for p in problems)


def test_accounting_extra_docx_entry_blocks(engine):
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"},
               {"docx_id": "1", "author": "Z",
                "date_sec": "2026-01-01T00:00:09Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0", "1"),
        universe=engine._key_owners_universe(anchored))
    assert any("unknown to the API" in p for p in problems)


def test_accounting_reply_cannot_saturate_missing_parent(engine):
    # two API parents same (author, second); docx has one parent + one
    # reply with the same key — subset check would pass, equality must not
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"},
               {"docx_id": "1", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0", "1"),
        universe=engine._key_owners_universe(anchored))
    # Counts match. Under the witness rule the refusal comes from the other
    # side: neither thread owns a key of its own, so nothing in the export
    # identifies either of them. Same threat, different mechanism (#14).
    assert any("shares every" in p for p in problems)


def test_accounting_deleted_reply_not_expected(engine):
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z",
                            replies=[{"createdTime": "2026-01-01T00:00:02Z",
                                      "author": {"displayName": "A"},
                                      "deleted": True}])]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0"),
        universe=engine._key_owners_universe(anchored))
    assert problems == []


# ---------------------------------------------------------------------------
# r10 (#34): a thread that vanished stops holding the document hostage
# ---------------------------------------------------------------------------

def _vanished_case(engine, quote):
    """One thread missing from the export, one live thread newer than it."""
    gone = api_comment("c1", "A", "2026-01-01T00:00:01Z")
    gone["quotedFileContent"] = {"value": quote} if quote else {}
    live = api_comment("c2", "B", "2026-01-01T00:00:09Z")
    records = [{"docx_id": "0", "author": "B",
                "date_sec": "2026-01-01T00:00:09Z"}]
    anchored = [gone, live]
    return anchored, records, engine._key_owners_universe(anchored)


def test_a_vanished_thread_whose_text_is_gone_stops_blocking(engine):
    """Living case 2026-08-09: five edits refused as one because a thread had
    lost its anchor when the customer moved a block. Losing a comment without
    closing it is normal practice, and a ghost has no anchor left to protect."""
    anchored, records, universe = _vanished_case(engine, "Вариант 1")
    problems, metrics = engine._account_anchored_comments(
        anchored, records, _spans("0"), universe=universe, file_id="DOC",
        doc_tab=make_doc(["Совсем другой текст"]))
    assert problems == []
    assert [g["id"] for g in metrics["ghosts"]] == ["c1"]
    assert metrics["ghosts"][0]["link"].endswith("disco=c1")
    assert engine._fence_off_ghosts(metrics["ghosts"]) == []


def test_a_vanished_thread_whose_text_is_still_here_is_fenced(engine):
    """The two signs disagree: the export lost it, the document still holds
    the text it was attached to. Being wrong here must cost a local refusal,
    not a thread — so the place is fenced and the rest stays editable."""
    anchored, records, universe = _vanished_case(engine, "Вариант 1")
    _p, metrics = engine._account_anchored_comments(
        anchored, records, _spans("0"), universe=universe, file_id="DOC",
        doc_tab=make_doc(["до", "Вариант 1", "после"]))
    blocked = engine._fence_off_ghosts(metrics["ghosts"])
    assert [(s, e) for s, e, _l in blocked] == [(4, 13)]
    assert "пропал из выгрузки" in blocked[0][2]
    assert "disco=c1" in blocked[0][2]


def test_dates_are_compared_as_moments_not_as_strings(engine):
    """Found in review: '2026-01-01T01:00:00+05:00' is EARLIER than
    '2026-01-01T00:00:00-05:00' and lexicographically later. Whether a thread
    counts as a ghost must not depend on how Google spelled the offset."""
    assert engine._rfc3339_epoch("2026-01-01T01:00:00+05:00") < \
        engine._rfc3339_epoch("2026-01-01T00:00:00-05:00")
    # unreadable rather than assumed-UTC: guessing decides a ghost verdict
    assert engine._rfc3339_epoch("2026-01-01T00:00:00") is None
    assert engine._rfc3339_epoch("") is None

    gone = api_comment("c1", "A", "2026-01-01T01:00:00+05:00")
    gone["quotedFileContent"] = {"value": "Вариант 1"}
    live = api_comment("c2", "B", "2026-01-01T00:00:00-05:00")
    anchored = [gone, live]
    _p, metrics = engine._account_anchored_comments(
        anchored, [{"docx_id": "0", "author": "B",
                    "date_sec": "2026-01-01T00:00:00-05:00"}],
        _spans("0"), universe=engine._key_owners_universe(anchored),
        doc_tab=make_doc(["ничего похожего"]))
    assert [g["id"] for g in metrics["ghosts"]] == ["c1"]


def test_a_vanished_thread_whose_text_hid_in_a_footnote_still_blocks(engine):
    """The fence walks the body; `replaceAllText` reaches headers, footers and
    footnotes too. Old text surviving out there is a place we cannot fence,
    so the verdict is withheld (found in review)."""
    anchored, records, universe = _vanished_case(engine, "Вариант 1")
    tab = make_doc(["ничего похожего"])
    tab["footnotes"] = {"f1": {"content": [
        {"paragraph": {"elements": [
            {"textRun": {"content": "Вариант 1\n"}}]}}]}}
    problems, metrics = engine._account_anchored_comments(
        anchored, records, _spans("0"), universe=universe, doc_tab=tab)
    assert any("missing from the export" in p for p in problems)
    assert "ghosts" not in metrics


def test_the_newest_thread_missing_from_the_export_still_blocks(engine):
    """Nothing in the export was created after it, so the snapshot may simply
    have been cut off before it existed — there lag really is
    indistinguishable from a ghost, and the refusal stands."""
    gone = api_comment("c1", "A", "2026-01-01T00:00:09Z")
    live = api_comment("c2", "B", "2026-01-01T00:00:01Z")
    anchored = [gone, live]
    problems, metrics = engine._account_anchored_comments(
        anchored, [{"docx_id": "0", "author": "B",
                    "date_sec": "2026-01-01T00:00:01Z"}],
        _spans("0"), universe=engine._key_owners_universe(anchored),
        doc_tab=make_doc(["ничего похожего"]))
    assert any("missing from the export" in p for p in problems)
    assert "ghosts" not in metrics


def test_a_thread_that_never_held_text_does_not_block(engine):
    """Measured 2026-08-16: a comment created through the API never attaches
    to document text, yet Drive stores the `anchor` it was handed verbatim and
    skrepka counts anything with `anchor` as anchored. Such a thread has no
    text anchor to lose — and it used to freeze every replace in the document,
    which is what any other tool leaving comments through the API does to a
    document."""
    anchored, records, universe = _vanished_case(engine, None)
    problems, metrics = engine._account_anchored_comments(
        anchored, records, _spans("0"), universe=universe,
        doc_tab=make_doc(["что угодно"]))
    assert problems == []
    assert [g["id"] for g in metrics["ghosts"]] == ["c1"]
    # nothing to fence with, and nothing that needs fencing
    assert engine._fence_off_ghosts(metrics["ghosts"]) == []


def test_a_quoteless_thread_keeps_the_sign_while_the_export_has_records(
        engine):
    """The #46 waiver is narrow on purpose: it applies only to an export with
    NO records at all, the one shape where sign 1 can never speak. Here the
    export does carry a record, just not a newer one — so the cheap second
    opinion is still available and is still required."""
    gone = api_comment("c1", "A", "2026-01-01T00:00:09Z")
    gone["quotedFileContent"] = {}
    live = api_comment("c2", "B", "2026-01-01T00:00:01Z")
    anchored = [gone, live]
    problems, metrics = engine._account_anchored_comments(
        anchored, [{"docx_id": "0", "author": "B",
                    "date_sec": "2026-01-01T00:00:01Z"}],
        _spans("0"), universe=engine._key_owners_universe(anchored),
        doc_tab=make_doc(["что угодно"]))
    assert any("missing from the export" in p for p in problems)
    assert "ghosts" not in metrics


def test_a_document_whose_comments_are_all_api_made_is_not_frozen(engine):
    """The shape #46 is actually about: another tool left every comment
    through the Drive API, so `word/comments.xml` is absent entirely. Before,
    the freshness sign had nothing to speak with and every replace was
    refused."""
    made_by_api = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                   api_comment("c2", "A", "2026-01-01T00:00:02Z")]
    for c in made_by_api:
        c["quotedFileContent"] = {}
    problems, metrics = engine._account_anchored_comments(
        made_by_api, [], [],
        universe=engine._key_owners_universe(made_by_api),
        doc_tab=make_doc(["обычный абзац"]))
    assert problems == []
    assert sorted(g["id"] for g in metrics["ghosts"]) == ["c1", "c2"]
    assert engine._fence_off_ghosts(metrics["ghosts"]) == []


def test_a_thread_with_a_quote_still_waits_for_the_freshness_sign(engine):
    """The waiver is for quote-less threads only. A thread that HAS a quote is
    a real text anchor that vanished, and there sign 1 is the only thing
    telling a ghost from an export older than the thread."""
    gone = api_comment("c1", "A", "2026-01-01T00:00:09Z")
    gone["quotedFileContent"] = {"value": "пропавший фрагмент"}
    live = api_comment("c2", "B", "2026-01-01T00:00:01Z")
    anchored = [gone, live]
    problems, metrics = engine._account_anchored_comments(
        anchored, [{"docx_id": "0", "author": "B",
                    "date_sec": "2026-01-01T00:00:01Z"}],
        _spans("0"), universe=engine._key_owners_universe(anchored),
        doc_tab=make_doc(["что угодно"]))
    assert any("missing from the export" in p for p in problems)
    assert "ghosts" not in metrics


def test_accounting_resolved_absent_from_export_is_clean(engine):
    """Google omits resolved threads from the export entirely — measured on a
    live document 2026-07-30. The previous version of this test put the
    resolved thread in `records`, asserting the opposite, which is why the
    accounting bug shipped: one closed thread blocked every replace forever."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z",
                            resolved=True)]
    problems, metrics = engine._account_anchored_comments(
        anchored, [], [], universe=engine._key_owners_universe(anchored))
    assert problems == []
    assert metrics["api_anchored_resolved"] == 1
    assert metrics["api_anchored_live"] == 0


def test_accounting_resolved_alongside_live_thread(engine):
    """The live thread is accounted for; the resolved one is simply absent."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "A", "2026-01-01T00:00:09Z", resolved=True)]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, metrics = engine._account_anchored_comments(
        anchored, records, _spans("0"),
        universe=engine._key_owners_universe(anchored))
    assert problems == []
    assert metrics["api_anchored_live"] == 1
    assert metrics["api_anchored_resolved"] == 1


def test_accounting_excludes_resolved_on_every_path(engine):
    """r8: the exclusion is unconditional, deletion paths included. A closed
    thread is no longer a reason to refuse anything — the conversation is
    over, and freezing the document costs the person more than the anchor.
    Deletion paths warn and archive instead."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z", resolved=True)]
    problems, metrics = engine._account_anchored_comments(
        anchored, [], [], universe=engine._key_owners_universe(anchored))
    assert problems == []
    assert metrics["api_anchored_resolved"] == 1


def test_accounting_live_thread_still_blocks_when_missing(engine):
    """Regression guard: excluding resolved must not weaken ghost detection."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    problems, _ = engine._account_anchored_comments(
        anchored, [], [], universe=engine._key_owners_universe(anchored))
    assert any("missing from the export" in p for p in problems)


# ---------------------------------------------------------------------------
# r8: an anchor that lives outside document.xml (a footnote, a header)
# ---------------------------------------------------------------------------

def _census(in_body=(), in_tables=(), elsewhere=()):
    return {"in_body": frozenset(in_body), "in_tables": frozenset(in_tables),
            "elsewhere": frozenset(elsewhere)}


def test_record_with_no_marker_anywhere_is_an_anchor_outside_the_body(engine):
    """The record proves the thread is in the export (a ghost is absent from
    it entirely), and document.xml carries no marker for it — that is a
    footnote or a header anchor. Nothing written into the body can reach it,
    so it is not a problem. Before r8 one such comment froze every replace."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, metrics = engine._account_anchored_comments(
        anchored, records, [], universe=engine._key_owners_universe(anchored),
        marker_census=_census())
    assert problems == []
    assert metrics["anchors_outside_body"] == 1


def test_record_anchored_in_a_table_is_bounded_not_global(engine):
    """The marker IS in document.xml, inside a table. The accounting must
    carry that the same way the parser does — otherwise this plain problem
    freezes the document the table fence was built to keep editable."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, [], universe=engine._key_owners_universe(anchored),
        marker_census=_census(in_tables=("0",)))
    assert len(problems) == 1
    assert problems[0].in_tables == frozenset({"0"})
    tab = {"body": {"content": [
        {"startIndex": 1, "endIndex": 40, "table": {"rows": 1}}]}}
    remaining, blocked = engine._fence_off_tables(problems, tab)
    assert remaining == []
    assert [(s, e) for s, e, _ in blocked] == [(1, 40)]


def test_an_ordinary_cell_anchor_makes_no_problems_at_all(engine, make_docx):
    """r11: the parser walks cells, so a plain comment in a cell produces a
    span like any other. Both problem sources must stay silent — the record
    joins its span, the census sees nothing hidden — or the document would be
    refused for an anchor that is perfectly readable."""
    body = ('<w:tbl><w:tr><w:tc><w:p>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t>cell</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:r><w:t>обычный абзац</w:t></w:r></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    assert problems == []
    anchored = [api_comment("c1", "A", CREATED)]
    records = [{"docx_id": "3", "author": "A", "date_sec": CREATED_SEC}]
    acc, _metrics = engine._account_anchored_comments(
        anchored, records, spans,
        universe=engine._key_owners_universe(anchored), marker_census=census)
    assert acc == []


def test_hidden_cell_anchor_survives_the_whole_problem_pipeline(engine,
                                                                make_docx):
    """Both problem sources together on a marker the walk still cannot reach
    (`w:sdt` inside a cell): the parser reports the hidden marker AND the
    accounting reports the record with no span. If either stays global the
    document is frozen and the fence buys nothing — which is exactly what the
    first version of it did. Both must fence the CELL, not every table."""
    body = ('<w:tbl><w:tr><w:tc>'
            '<w:sdt><w:commentRangeStart w:id="3"/><w:r><w:t>cell</w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:sdt>'
            '<w:p><w:r><w:t>видно</w:t></w:r></w:p>'
            '</w:tc></w:tr></w:tbl>'
            '<w:p><w:r><w:t>обычный абзац</w:t></w:r></w:p>')
    spans, problems, census = engine._parse_docx_anchor_spans(make_docx(body))
    anchored = [api_comment("c1", "A", CREATED)]
    records = [{"docx_id": "3", "author": "A", "date_sec": CREATED_SEC}]
    acc, _metrics = engine._account_anchored_comments(
        anchored, records, spans,
        universe=engine._key_owners_universe(anchored), marker_census=census)
    assert acc and getattr(acc[0], "in_tables", None) == frozenset({"3"})
    tab = {"body": {"content": [
        {"startIndex": 1, "endIndex": 40, "table": {"tableRows": [
            {"startIndex": 2, "endIndex": 39, "tableCells": [
                {"startIndex": 3, "endIndex": 38, "content": [
                    {"startIndex": 4, "endIndex": 10, "paragraph": {
                        "elements": [{"startIndex": 4, "endIndex": 10,
                                      "textRun": {"content": "видно\n"}}]}}]}]}]}},
        {"startIndex": 40, "endIndex": 55, "paragraph": {"elements": []}},
    ]}}
    remaining, blocked = engine._fence_off_tables(problems + acc, tab)
    assert remaining == []
    assert [(s, e) for s, e, _ in blocked] == [(3, 38), (3, 38)]


def test_one_cell_fenced_twice_becomes_one_interval(engine):
    """The parser and the accounting report the same hidden marker
    separately, so the same cell arrives twice. Every duplicate is walked
    again by `_blocked_hits` for every operation and counted again in the
    receipt — one interval per range."""
    same_twice = [(3, 18, "одна причина"), (3, 18, "одна причина"),
                  (40, 55, "другая ячейка")]
    assert engine._dedupe_blocked(same_twice) == [
        (3, 18, "одна причина"), (40, 55, "другая ячейка")]


def test_two_different_reasons_on_one_cell_are_both_named(engine):
    """Two distinct comments in one cell are two threads the person may need
    to look at. Collapsing them to one interval is right; hiding the second
    reason is not."""
    out = engine._dedupe_blocked(
        [(3, 18, "комментарий A"), (3, 18, "комментарий B")])
    assert len(out) == 1
    assert out[0][:2] == (3, 18)
    assert "комментарий A" in out[0][2] and "ещё причин здесь: 1" in out[0][2]


def test_record_whose_marker_hides_elsewhere_stays_global(engine):
    """A container that is not a table bounds nothing, so the refusal stays
    document-wide."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, [], universe=engine._key_owners_universe(anchored),
        marker_census=_census(elsewhere=("0",)))
    assert any("has 0 anchor spans" in p for p in problems)


def test_without_a_census_a_missing_span_is_still_a_refusal(engine):
    """No census (older callers, tests): nothing is assumed, fail closed."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, [], universe=engine._key_owners_universe(anchored))
    assert any("has 0 anchor spans" in p for p in problems)


# ---------------------------------------------------------------------------
# r8 / #20: which thread does an export record belong to
# ---------------------------------------------------------------------------

def test_attribution_names_the_thread_behind_each_record(engine):
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "B", "2026-01-01T00:00:05Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"},
               {"docx_id": "1", "author": "B",
                "date_sec": "2026-01-01T00:00:05Z"}]
    attribution = engine._attribute_records_to_threads(
        anchored, records, engine._key_owners_universe(anchored))
    assert attribution == {"0": "c1", "1": "c2"}


def test_attribution_covers_every_anchor_of_a_multi_anchor_thread(engine):
    """One thread on three fragments exports three records with the same
    second — all three are its anchors."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": str(i), "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"} for i in range(3)]
    attribution = engine._attribute_records_to_threads(
        anchored, records, engine._key_owners_universe(anchored))
    assert attribution == {"0": "c1", "1": "c1", "2": "c1"}


def test_attribution_stays_silent_on_a_shared_key(engine):
    """Two threads created in the same second by the same author: no witness,
    nothing may be attributed — and the accounting refuses on them anyway."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    attribution = engine._attribute_records_to_threads(
        anchored, records, engine._key_owners_universe(anchored))
    assert attribution == {}


# ---------------------------------------------------------------------------
# issue #12: one census feeds both the accounting and the fingerprint
# ---------------------------------------------------------------------------

def _reply(rid, author, created, action="", deleted=False):
    return {"id": rid, "createdTime": created,
            "author": {"displayName": author},
            "action": action, "deleted": deleted}


def test_census_fingerprint_comes_from_the_same_read(engine):
    """The accounting data and fp1 must be one snapshot, not two reads.
    Two reads left a gap no check covered: comments could move between the
    census the accounting trusted and the fp1/fp2 sandwich."""
    drive = DriveStub([api_comment("c1", "A", CREATED)], lambda: b"")
    live, anchored, fp, _uni = engine._census_comments(drive, "doc1")
    assert drive._list_calls == 1
    assert [c["id"] for c in live] == ["c1"]
    assert [c["id"] for c in anchored] == ["c1"]
    # fp2 is taken through a different entry point — same shape, comparable
    assert fp == engine._comments_fingerprint(drive, "doc1")


def test_census_asks_for_the_fields_the_fingerprint_needs(engine):
    """Guards the request itself: trimming `fields` or flipping includeDeleted
    would silently weaken the fingerprint while every other test still passes,
    because the stubs hand back whatever the fixtures declare."""
    seen = {}

    class Recording(DriveStub):
        def list(self, **kw):
            seen.update(kw)
            return super().list(**kw)

    engine._census_comments(Recording([api_comment("c1", "A", CREATED)],
                                      lambda: b""), "doc1")
    assert seen["includeDeleted"] is True
    # the reply sub-selection is asserted whole: checking for bare "deleted"
    # or "createdTime" would be satisfied by the parent comment's own fields
    assert ("replies(id,content,createdTime,author/displayName,author/me,"
            "deleted,action)") in seen["fields"]
    for f in ("resolved", "quotedFileContent", "anchor"):
        assert f in seen["fields"]


def test_fingerprint_notices_a_new_reply(engine):
    """The old fingerprint carried only (id, deleted, resolved, anchored) of
    parent threads, so a reply could appear or vanish invisibly — while the
    accounting keys count reply entries and the export emits a record per
    entry. Adding a reply must change the fingerprint."""
    before = [api_comment("c1", "A", CREATED)]
    after = [api_comment("c1", "A", CREATED,
                         replies=[_reply("r1", "B", "2026-07-14T00:00:05Z")])]
    assert (engine._fingerprint_from_census(before)
            != engine._fingerprint_from_census(after))


def test_fingerprint_notices_a_resolve_reopen_round_trip(engine):
    """Resolving and reopening in the UI appends a reply per action, so the
    thread comes back with resolved=False but two extra reply records. The
    fingerprint sees them; a parents-only fingerprint would read the round
    trip as no change at all."""
    before = [api_comment("c1", "A", CREATED)]
    after = [api_comment("c1", "A", CREATED, replies=[
        _reply("r1", "A", "2026-07-14T00:00:05Z", action="resolve"),
        _reply("r2", "A", "2026-07-14T00:00:06Z", action="reopen"),
    ])]
    assert (engine._fingerprint_from_census(before)
            != engine._fingerprint_from_census(after))


def test_census_keeps_deleted_comments_out_of_the_live_view(engine):
    """Reading with includeDeleted=True serves the fingerprint; every other
    consumer must still see only live comments, exactly as before."""
    raw = [api_comment("c1", "A", CREATED),
           dict(api_comment("c2", "A", "2026-07-14T00:00:09Z"), deleted=True)]
    drive = DriveStub(raw, lambda: b"")
    live, anchored, fp, _uni = engine._census_comments(drive, "doc1")
    assert [c["id"] for c in live] == ["c1"]
    assert [c["id"] for c in anchored] == ["c1"]
    assert any(t[1] == "c2" for t in fp)  # deleted entry still fingerprinted


def test_patch_blocks_when_only_a_reply_changes(engine, monkeypatch):
    """End to end for #12: a reply landing between the census and the write
    must abort the replace. Before the fix the fingerprint ignored replies,
    so this exact race went through unnoticed."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    docs.fail_cleanup = True
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        comments_after=[api_comment("c1", "A", CREATED, replies=[
            _reply("r1", "B", "2026-07-14T00:00:05Z")])],
        switch_after_lists=1)  # list 1 = census+fp1, list 2 = fp2
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    with pytest.raises(engine.PatchOpError) as exc:
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert exc.value.state == "not_applied"


def test_accounting_multi_anchor_excess_is_allowed(engine):
    """One thread anchored to three fragments yields three export records with
    the same key. Exact multiset equality called that a stale export and
    blocked the document."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": str(i), "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"} for i in range(3)]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0", "1", "2"),
        universe=engine._key_owners_universe(anchored))
    assert problems == []


def test_accounting_multi_anchor_excess_cannot_mask_a_ghost(engine):
    """The counterexample both reviewers found independently against the first
    draft of the 0.9.1 fix: a live two-anchor thread and a ghosted thread
    sharing one (author, second) key give api=2, docx=2. A plain `docx >= api`
    rule passes and the ghost's anchor is left unprotected.

    Kept through the switch to the witness rule (#14) on both reviewers'
    insistence: the threat is unchanged, only the refusal is. Neither thread
    has a key of its own, so neither can be identified in the export."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "A", "2026-01-01T00:00:01Z")]  # ghosted
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"},
               {"docx_id": "1", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]  # both from c1
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0", "1"),
        universe=engine._key_owners_universe(anchored))
    assert any("shares every" in p for p in problems)


# ---------------------------------------------------------------------------
# issue #14: presence proved by a witness key, not by counting entries
# ---------------------------------------------------------------------------

def test_witness_incident_colliding_replies_across_threads_passes(engine):
    """The field incident: an agent answered every thread in a loop and two
    replies landed in the same second. Parents are placed by a human seconds
    apart, so each thread still owns its parent key and stays identifiable.
    The old rule refused this and froze the whole document."""
    anchored, records, ids = [], [], []
    for i in range(6):
        rep = "2026-01-01T00:05:0{}Z".format(i // 2)  # pairs collide
        anchored.append(api_comment(
            f"c{i}", "A", f"2026-01-01T00:00:0{i}Z",
            replies=[{"createdTime": rep, "author": {"displayName": "A"}}]))
        records.append({"docx_id": f"p{i}", "author": "A",
                        "date_sec": f"2026-01-01T00:00:0{i}Z"})
        records.append({"docx_id": f"r{i}", "author": "A",
                        "date_sec": rep})
        ids += [f"p{i}", f"r{i}"]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans(*ids),
        universe=engine._key_owners_universe(anchored))
    assert problems == []


def test_witness_two_replies_same_second_into_one_thread_passes(engine):
    """The other half of the incident. Both replies belong to the SAME thread,
    so the key is still owned by one thread and remains a witness. The old
    rule counted entries and refused."""
    same = "2026-01-01T00:05:00Z"
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z", replies=[
        {"createdTime": same, "author": {"displayName": "A"}},
        {"createdTime": same, "author": {"displayName": "A"}}])]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0"),
        universe=engine._key_owners_universe(anchored))
    assert problems == []


def test_witness_subset_signature_refused(engine):
    """Codex r2 counterexample: signatures {p} and {p, q}. A damaged group of
    the second thread reads exactly as the first, so the first must not be
    certifiable — it owns no key of its own."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z"),
                api_comment("c2", "A", "2026-01-01T00:00:09Z", replies=[
                    {"createdTime": "2026-01-01T00:00:01Z",
                     "author": {"displayName": "A"}}])]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"},
               {"docx_id": "1", "author": "A",
                "date_sec": "2026-01-01T00:00:09Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0", "1"),
        universe=engine._key_owners_universe(anchored))
    assert any("c1" in p and "shares every" in p for p in problems)


def test_witness_pairwise_disjoint_but_no_unique_key_refused(engine):
    """Why the witness rule and not the weaker "no signature is a subset of
    another": {a,b}, {a,c}, {b,d} are pairwise non-nested, yet the first owns
    no key alone and damaged groups of the other two could forge it."""
    a, b, c, d = (f"2026-01-01T00:00:0{i}Z" for i in range(1, 5))

    def rep(t):
        return {"createdTime": t, "author": {"displayName": "A"}}

    anchored = [api_comment("t1", "A", a, replies=[rep(b)]),
                api_comment("t2", "A", a, replies=[rep(c)]),
                api_comment("t3", "A", b, replies=[rep(d)])]
    problems, _ = engine._account_anchored_comments(
        anchored, [], [], universe=engine._key_owners_universe(anchored))
    assert any("t1" in p and "shares every" in p for p in problems)


def test_witness_may_not_come_from_a_resolved_thread(engine):
    """codex r3: uniqueness is measured against everything the API can see.
    A leftover export record from a resolved thread must not pass for the
    witness of a live one."""
    key = "2026-01-01T00:00:01Z"
    live = api_comment("c1", "A", key)
    resolved = api_comment("c2", "A", key, resolved=True)
    records = [{"docx_id": "0", "author": "A", "date_sec": key}]
    problems, _ = engine._account_anchored_comments(
        [live], records, _spans("0"),
        universe=engine._key_owners_universe([live, resolved]))
    assert any("shares every" in p for p in problems)


def test_witness_one_of_several_present_is_enough(engine):
    """A thread may own more than one key; requiring a particular one would
    flap on partial staleness."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z", replies=[
        {"createdTime": "2026-01-01T00:00:09Z",
         "author": {"displayName": "A"}}])]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:09Z"}]  # parent's record gone
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0"),
        universe=engine._key_owners_universe(anchored))
    assert problems == []


def test_accounting_record_without_span_breaks_bijection(engine):
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, [], universe=engine._key_owners_universe(anchored))
    assert any("anchor spans in document.xml" in p for p in problems)


def test_accounting_span_without_record_is_problem(engine):
    problems, _ = engine._account_anchored_comments(
        [], [], _spans("7"), universe={})
    assert any("no comments.xml entry" in p for p in problems)


def test_accounting_entry_without_created_time(engine):
    anchored = [{"id": "c1", "author": {"displayName": "A"},
                 "quotedFileContent": {"value": "x"}, "replies": []}]
    problems, _ = engine._account_anchored_comments(
        anchored, [], [], universe=engine._key_owners_universe(anchored))
    assert any("without author/createdTime" in p for p in problems)
    # the refusal names the thread, not a bare author@second pair (#10)
    assert any("c1 «x»" in p for p in problems)


# ---------------------------------------------------------------------------
# issue #10: readable refusals + range-confined blocking
# ---------------------------------------------------------------------------

def test_comment_label_names_id_and_quote(engine):
    c = {"id": "AAABBB", "quotedFileContent": {"value": "  хранит своё\nи "
                                                        "умеет своё  "}}
    assert engine._comment_label(c) == "AAABBB «хранит своё и умеет своё»"


def test_comment_label_truncates_and_survives_a_missing_quote(engine):
    long = {"id": "c1", "quotedFileContent": {"value": "я" * 80}}
    assert engine._comment_label(long) == "c1 «" + "я" * 40 + "…»"
    assert engine._comment_label({"id": "c2"}) == "c2 (без цитаты)"


def test_missing_thread_refusal_names_the_thread(engine):
    """The old message was «'slv fmts' @ 2026-07-30T12:46:33Z» — useless on a
    doc where one person wrote everything inside one minute (#10)."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    anchored[0]["quotedFileContent"] = {"value": "хранит своё"}
    problems, _ = engine._account_anchored_comments(
        anchored, [], [], universe=engine._key_owners_universe(anchored))
    msg = next(p for p in problems if "missing from the export" in p)
    assert "c1 «хранит своё»" in msg
    assert "'A' @" not in msg


def test_scope_leaves_a_coordinateless_problem_global(engine):
    """A thread missing from the export has no span, so there is nothing to
    confine the refusal to — it must keep blocking the document."""
    problems = ["anchored comment entries missing from the export: c1 «x»"]
    global_problems, blocked = engine._scope_anchor_problems(problems, [])
    assert global_problems == problems
    assert blocked == []


def test_record_with_several_spans_stays_global(engine):
    """Tempting to confine to those spans, but the docx parser already calls
    such a document globally broken, and a w:id whose markers do not pair up
    1:1 has exactly the unreliable offsets we would be confining to."""
    anchored = [api_comment("c1", "A", "2026-01-01T00:00:01Z")]
    records = [{"docx_id": "0", "author": "A",
                "date_sec": "2026-01-01T00:00:01Z"}]
    problems, _ = engine._account_anchored_comments(
        anchored, records, _spans("0", "0"),
        universe=engine._key_owners_universe(anchored))
    p = next(p for p in problems if "anchor spans in document.xml" in p)
    assert getattr(p, "docx_ids", ()) == ()


def test_scope_confines_a_problem_that_maps_to_a_position(engine):
    p = engine._AnchorProblem("anchor span 9 has no comments.xml entry", ("9",))
    anchors = [(10, 15, "Bravo", "9"), (30, 37, "Charlie", "0")]
    global_problems, blocked = engine._scope_anchor_problems([p], anchors)
    assert global_problems == []
    assert len(blocked) == 1
    start, end, label = blocked[0]
    assert (start, end) == (10, 15)
    assert "docx id 9" in label and "Bravo" in label


def test_scope_falls_back_to_global_when_the_id_does_not_map(engine):
    """An anchor we cannot place is an anchor we cannot avoid."""
    p = engine._AnchorProblem("anchor span 9 has no comments.xml entry", ("9",))
    global_problems, blocked = engine._scope_anchor_problems(
        [p], [(30, 37, "Charlie", "0")])
    assert global_problems == [p]
    assert blocked == []


def test_patch_refuses_only_the_paragraph_holding_the_odd_anchor(
        engine, monkeypatch, capsys):
    """Bravo carries an export span with no comments.xml entry. That op is
    refused — and the refusal says so instead of blocking the document."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("9", 0, 5)]),
                             ("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    with pytest.raises(SystemExit):
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "отклонена ИМЕННО ЭТА операция" in err
    assert "docx id 9" in err
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_patch_still_applies_away_from_the_odd_anchor(engine, monkeypatch):
    """Same document, an op nowhere near the unaccounted span: it goes
    through. Before #10 this raised too — one murky thread froze everything."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("9", 0, 5)]),
                             ("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"}
    engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    main = semantic_batch(docs)
    assert "deleteContentRange" in main[0]  # canary delete first
    assert "deleteContentRange" in main[1]  # the edit, by index


def _dup_paragraph_doc(docs=None):
    """A document whose commented paragraph appears twice, byte for byte —
    the live shape of #26. The second copy carries no comment."""
    return [("Alpha", []), ("Bravo", [("0", 0, 5)]), ("Charlie", []),
            ("Bravo", [])]


def test_patch_survives_two_identical_paragraphs(engine, monkeypatch):
    """#26: the anchor matches two paragraphs, so it cannot be placed — and
    that used to disable replaces and deletes in the WHOLE document. An op
    away from the copies goes through."""
    doc = make_doc(["Alpha", "Bravo", "Charlie", "Bravo"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, _dup_paragraph_doc(), [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"}
    engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    main = semantic_batch(docs)
    assert "deleteContentRange" in main[1]  # the edit, by index


def test_patch_refuses_an_op_reaching_into_a_copy(engine, monkeypatch, capsys):
    """Операция, накрывающая живой якорь целиком, отказывает — и с 0.17
    отказ называет НАСТОЯЩУЮ причину.

    Раньше здесь срабатывала ограда двойников: якорь стоял на одной из двух
    одинаковых «Bravo», и какая из них — было неизвестно, поэтому отказ
    говорил про одинаковые копии. Теперь копия известна, ограды нет, и видна
    та причина, которая на самом деле мешает: замена накрывает якорь целиком,
    а переписать разом два абзаца скрепка не умеет."""
    doc = make_doc(["Alpha", "Bravo", "Charlie", "Bravo"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED),
         api_comment("c2", "B", "2026-07-13T18:10:00.000Z")],
        _docx_builder(docs,
                      [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                       ("Charlie", [("1", 0, 7)]), ("Bravo", [])],
                      [("0", "A", CREATED_SEC),
                       ("1", "B", "2026-07-13T18:10:00Z")]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    # a quote across the paragraph boundary IS unique, so the uniqueness check
    # lets it through and the fence is what stops it
    op = {"op": "replace_quote", "quote": "Bravo\nCharlie", "with": "Zulu"}
    with pytest.raises(SystemExit):
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    err = json.loads(capsys.readouterr().out)["error"]
    # с 0.17 отказ приходит раньше и называет причину точнее: запись идёт по
    # индексам, поэтому такая правка СЪЕЛА БЫ границу абзаца
    assert "удаляет границу абзаца" in err
    assert _no_content_mutation(docs)


def test_a_quote_op_on_a_copy_is_stopped_by_uniqueness(engine, monkeypatch,
                                                       capsys):
    """Recorded on purpose: editing a duplicated paragraph by quote never
    reaches the fence — the quote must resolve to ONE place, and any substring
    of a duplicated paragraph resolves to two. The tail of #26 consists of
    weakening exactly this rule, so whoever weakens it removes a guard that is
    doing real work here."""
    doc = make_doc(["Alpha", "Bravo", "Charlie", "Bravo"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, _dup_paragraph_doc(), [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    with pytest.raises(SystemExit):
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "non-unique (2 matches)" in err
    # and the way out it offers must be one that works on a commented doc:
    # `occurrence` targeting is refused there, so pointing at it would be a
    # dead end (#24 — a refusal has to name a path that exists)
    assert "occurrence" not in err or "comment" in err
    # "extend the quote" is the remedy everywhere EXCEPT here, and here is
    # where it gets offered. Measured on a live doc 15 August: the paragraph
    # was duplicated whole, so every longer quote stayed non-unique too. A
    # refusal that promises a remedy which provably cannot work is worse than
    # one that admits the edit is unreachable.
    assert "repeats word for word" in err
    assert "works everywhere" not in err
    assert _no_content_mutation(docs)


def test_a_fenced_range_stops_a_sync_delete(engine):
    """The sync path protects through the same list (`protected` absorbs
    `snap["blocked"]`), so a fence built for an ambiguous anchor stops the
    positional rewrite that would ghost it."""
    fence = engine._fence_off_ambiguous([{
        "docx_id": "0", "para_text": "Bravo", "start_off": 0, "end_off": 5,
        "candidates": [(7, 13), (21, 27)],
    }])[0]
    flat = [{"deleteContentRange": {"range": {"startIndex": 7,
                                              "endIndex": 13}}}]
    overlap = engine._find_protected_overlap(flat, fence)
    assert overlap and "одинаковых копий" in overlap


def test_sync_protects_every_table_once_a_cell_anchor_exists(engine):
    """`patch` is safe on a placed cell anchor because a replace must be
    unique across the whole tab — a byte-identical twin table refuses the
    operation before position matters. `sync` does no such count: it deletes
    by absolute range. So it is handed every table as protected the moment one
    cell anchor exists, which is what happened before r11 anyway and costs
    sync nothing it could do (it treats a table as indivisible)."""
    tables = [(1, 40, "таблица, в одной из ячеек которой стоит комментарий")]
    # the anchor was placed in table 1, sync deletes table 2 — without this
    # list nothing would overlap and the thread could be lost
    second_table = [{"deleteContentRange": {"range": {"startIndex": 41,
                                                      "endIndex": 80}}}]
    assert engine._find_protected_overlap(second_table, tables) is None
    both = tables + [(41, 80, "и вторая такая же")]
    assert engine._find_protected_overlap(second_table, both)


def test_a_cell_fence_stops_a_sync_rewrite_of_its_table(engine):
    """`sync` treats a table as indivisible, so any change inside one arrives
    as a delete over the WHOLE table. A cell fence sits inside that range, so
    the overlap check refuses it — the narrower fence does not let a
    table-level rewrite through."""
    cell_fence = [(12, 30, "ячейка таблицы, в которой стоит комментарий")]
    whole_table = [{"deleteContentRange": {"range": {"startIndex": 5,
                                                     "endIndex": 60}}}]
    assert engine._find_protected_overlap(whole_table, cell_fence)
    # and an edit to a paragraph outside the table still goes through
    elsewhere = [{"deleteContentRange": {"range": {"startIndex": 61,
                                                   "endIndex": 70}}}]
    assert engine._find_protected_overlap(elsewhere, cell_fence) is None


# ---------------------------------------------------------------------------
# units: named ranges + overlap
# ---------------------------------------------------------------------------

def test_named_range_intervals(engine):
    doc = make_doc(["Alpha"], named_ranges={
        "mark1": {"namedRanges": [{"ranges": [
            {"startIndex": 1, "endIndex": 4},
            {"startIndex": 10, "endIndex": 12}]}]}})
    out = engine._named_range_intervals(doc)
    assert (1, 4, "named range 'mark1'") in out
    assert (10, 12, "named range 'mark1'") in out


def test_named_range_malformed_fails_closed(engine, capsys):
    doc = make_doc(["Alpha"], named_ranges={
        "bad": {"namedRanges": [{"ranges": [{"startIndex": 5}]}]}})
    with pytest.raises(SystemExit):
        engine._named_range_intervals(doc)
    assert "malformed" in json.loads(capsys.readouterr().out)["error"]


def test_named_range_inverted_fails_closed(engine):
    doc = make_doc(["Alpha"], named_ranges={
        "bad": {"namedRanges": [{"ranges": [
            {"startIndex": 9, "endIndex": 4}]}]}})
    with pytest.raises(SystemExit):
        engine._named_range_intervals(doc)


def _del(s, e):
    return {"deleteContentRange": {"range": {"startIndex": s, "endIndex": e}}}


def test_overlap_full_coverage_refused(engine):
    msg = engine._find_protected_overlap([_del(5, 20)], [(8, 12, "anchor X")])
    assert msg and "anchor X" in msg


def test_overlap_partial_refused(engine):
    assert engine._find_protected_overlap([_del(5, 10)], [(8, 12, "a")])
    assert engine._find_protected_overlap([_del(10, 15)], [(8, 12, "a")])


def test_overlap_touching_boundaries_pass(engine):
    assert engine._find_protected_overlap([_del(5, 8)], [(8, 12, "a")]) is None
    assert engine._find_protected_overlap([_del(12, 15)], [(8, 12, "a")]) is None


def test_overlap_insert_text_ignored(engine):
    req = {"insertText": {"location": {"index": 9}, "text": "x"}}
    assert engine._find_protected_overlap([req], [(8, 12, "a")]) is None


def test_overlap_unknown_request_type_fails_closed(engine):
    msg = engine._find_protected_overlap(
        [{"replaceAllText": {"containsText": {"text": "x"}}}], [(1, 2, "a")])
    assert msg and "unexpected request type" in msg


def test_overlap_malformed_range_fails_closed(engine):
    assert "malformed" in engine._find_protected_overlap(
        [_del(9, 9)], [(1, 2, "a")])
    assert "malformed" in engine._find_protected_overlap(
        [{"deleteContentRange": {"range": {"startIndex": 1}}}], [(1, 2, "a")])


# ---------------------------------------------------------------------------
# e2e on stubs: the four-scenario matrix (codex sync-anchors r2 #4)
# ---------------------------------------------------------------------------

BASE_TEXTS = ["Alpha", "Bravo", "Charlie"]
BASE_MD = "Alpha\n\nBravo\n\nCharlie"
CREATED = "2026-07-13T17:53:08.108Z"
CREATED_SEC = "2026-07-13T17:53:08Z"


def _docx_builder(docs_stub, paras, comments, include_canary=True):
    def build():
        p = list(paras)
        if include_canary and docs_stub.canary_text is not None:
            p.append((docs_stub.canary_text, []))
        return make_docx_full(p, comments)
    return build


def _crossing_docx(docs_stub, texts, cid, start, end, comments):
    """Export builder for ONE anchor dragged across a paragraph break.

    `start` and `end` are (paragraph index, offset). `make_docx_full` keeps
    every anchor inside a single paragraph, which is exactly the shape #45 is
    not about.
    """
    def build():
        paras = list(texts)
        if docs_stub.canary_text is not None:
            paras = paras + [docs_stub.canary_text]
        body = []
        for i, t in enumerate(paras):
            if i == start[0]:
                runs = [f'<w:r><w:t xml:space="preserve">{t[:start[1]]}</w:t></w:r>',
                        f'<w:commentRangeStart w:id="{cid}"/>',
                        f'<w:r><w:t xml:space="preserve">{t[start[1]:]}</w:t></w:r>']
            elif i == end[0]:
                runs = [f'<w:r><w:t xml:space="preserve">{t[:end[1]]}</w:t></w:r>',
                        f'<w:commentRangeEnd w:id="{cid}"/>',
                        f'<w:commentReference w:id="{cid}"/>',
                        f'<w:r><w:t xml:space="preserve">{t[end[1]:]}</w:t></w:r>']
            else:
                runs = [f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r>']
            body.append(f"<w:p>{''.join(runs)}</w:p>")
        document = (f'<?xml version="1.0"?><w:document xmlns:w="{WORDML}">'
                    f"<w:body>{''.join(body)}</w:body></w:document>")
        centries = "".join(
            f'<w:comment w:id="{c}" w:author="{a}" w:date="{d}">'
            f"<w:p><w:r><w:t>c</w:t></w:r></w:p></w:comment>"
            for c, a, d in comments)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", document)
            z.writestr(
                "word/comments.xml",
                f'<?xml version="1.0"?><w:comments xmlns:w="{WORDML}">'
                f"{centries}</w:comments>")
        return buf.getvalue()
    return build


class MutatingDocsStub(DocsStub):
    """DocsStub whose document actually changes when replaceAllText lands.

    The plain stub answers every read with the same snapshot. That is enough
    for one operation and wrong for a chain, and a chain is exactly what #36
    is about: the second operation has to see what the first one wrote.
    """

    def _flat(self):
        """Body text as one string; absolute Docs index i is `flat[i - 1]`."""
        return "".join(el["paragraph"]["elements"][0]["textRun"]["content"]
                       for el in self.base["body"]["content"]
                       if "paragraph" in el)

    def _store(self, flat):
        self.base = make_doc(flat.split("\n")[:-1])

    def batchUpdate(self, documentId=None, body=None):
        reqs = body["requests"]
        result = super().batchUpdate(documentId=documentId, body=body)
        for rq in reqs:
            rat = rq.get("replaceAllText")
            if not rat:
                continue
            old, new = rat["containsText"]["text"], rat["replaceText"]
            texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
                     for el in self.base["body"]["content"]
                     if "paragraph" in el]
            self.base = make_doc([t.replace(old, new) for t in texts])
        # Index-addressed edit: skip the leading canary delete (it targets the
        # canary paragraph, which this stub never materialised) and apply the
        # rest, so the NEXT operation reads what this one wrote.
        if len(reqs) > 1 and "deleteContentRange" in reqs[0]:
            flat = self._flat()
            for rq in reqs[1:]:
                if "deleteContentRange" in rq:
                    rng = rq["deleteContentRange"]["range"]
                    flat = (flat[:rng["startIndex"] - 1]
                            + flat[rng["endIndex"] - 1:])
                elif "insertText" in rq:
                    at = rq["insertText"]["location"]["index"]
                    flat = (flat[:at - 1] + rq["insertText"]["text"]
                            + flat[at - 1:])
            self._store(flat)
        return result


def _docx_tracking(docs_stub, anchors, comments):
    """Export builder that follows the stub's CURRENT text.

    `anchors` maps paragraph index -> [(cid, start_off, end_off)]. Needed
    wherever the document changes between operations: a frozen export would
    stop matching the API paragraphs and the mapping would fail closed for a
    reason the test never meant to create.
    """
    def build():
        texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
                 for el in docs_stub._current()["body"]["content"]
                 if "paragraph" in el]
        return make_docx_full(
            [(t, anchors.get(i, [])) for i, t in enumerate(texts)], comments)
    return build


def _no_content_mutation(docs):
    """True when no batch contains a non-canary deleteContentRange/insert."""
    for reqs in docs.batches:
        for r in reqs:
            if "insertText" in r and \
                    "skrepka-canary" in r["insertText"]["text"]:
                continue
            if "deleteContentRange" in r and len(reqs) == 1:
                continue  # canary cleanup
            return False
    return True


def test_e2e_unmatched_comment_blocks_before_batch(engine, monkeypatch,
                                                   tmp_path, capsys):
    """(1) API has an anchored comment the export does not contain
    (ghost or stale export) → no main batch, canary cleaned up."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [(t, []) for t in BASE_TEXTS], []))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD,
                      "Alpha\n\nBravo edited\n\nCharlie")
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "missing from the export" in err
    assert _no_content_mutation(docs)
    assert docs.canary_text is None  # cleaned up


def test_e2e_matched_no_overlap_applies(engine, monkeypatch, tmp_path,
                                        capsys):
    """(2) anchored comment on Charlie, local edit on Bravo → batch runs,
    canary delete is the FIRST request of the atomic batch."""
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs,
                      [("Alpha", []), ("Bravo", []),
                       ("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)]),
        html=b"<p>Alpha</p><p>Bravo edited</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD,
                      "Alpha\n\nBravo edited\n\nCharlie")
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert out["replaced"] == 1
    assert out["anchor_accounting"]["canary"] == "confirmed"
    main = next(b for b in docs.batches
                if len(b) > 1 and "deleteContentRange" in b[0])
    # the canary delete leads the batch and sits at the doc end
    body_end = doc["body"]["content"][-1]["endIndex"]
    assert main[0]["deleteContentRange"]["range"]["startIndex"] == \
        body_end - 1
    assert docs.main_applied


def test_e2e_matched_overlap_blocks(engine, monkeypatch, tmp_path, capsys):
    """(3) anchored comment on Bravo, local edit on Bravo → refused
    before the batch, canary cleaned up."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs,
                      [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                       ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD,
                      "Alpha\n\nBravo edited\n\nCharlie")
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "anchor of a live comment" in err
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_e2e_named_range_overlap_blocks_without_comments(engine, monkeypatch,
                                                         tmp_path, capsys):
    """(4) comment-free doc, named range on Bravo, local delete of Bravo →
    refused before ANY write (no canary involved)."""
    doc = make_doc(BASE_TEXTS, named_ranges={
        "mark1": {"namedRanges": [{"ranges": [
            {"startIndex": 7, "endIndex": 12}]}]}})
    docs = DocsStub(doc)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, "Alpha\n\nCharlie")
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "named range 'mark1'" in err
    assert docs.batches == []  # nothing at all was written


def test_e2e_export_without_canary_blocks_and_cleans(engine, monkeypatch,
                                                     tmp_path, capsys):
    """Freshness failure: export never contains the canary → abort after
    retries, cleanup issued, no main batch."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)], include_canary=False))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD,
                      "Alpha\n\nBravo edited\n\nCharlie")
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "freshness canary" in err
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_e2e_named_range_at_doc_end_blocks_canary_insert(engine, monkeypatch,
                                                         tmp_path, capsys):
    """A named range reaching the canary insertion point refuses BEFORE
    any write."""
    doc = make_doc(BASE_TEXTS, named_ranges={
        "tail": {"namedRanges": [{"ranges": [
            {"startIndex": 13, "endIndex": 21}]}]}})
    docs = DocsStub(doc)
    drive = DriveStub([api_comment("c1", "A", CREATED)], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD,
                      "Alpha\n\nBravo edited\n\nCharlie")
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "end of the document" in err
    assert docs.batches == []


# ---------------------------------------------------------------------------
# patch integration (same helper)
# ---------------------------------------------------------------------------

def test_patch_mixed_live_and_missing_refused(engine, monkeypatch, capsys):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED),
         api_comment("c2", "B", "2026-07-13T17:59:59.100Z")],
        _docx_builder(docs, [("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    with pytest.raises(SystemExit):
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "missing from the export" in err
    assert _no_content_mutation(docs)
    assert docs.canary_text is None


def test_patch_replace_applies_with_canary_first(engine, monkeypatch):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", []),
                             ("Charlie", [("0", 0, 7)])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    main = semantic_batch(docs)
    assert "deleteContentRange" in main[0]  # canary delete first
    assert "deleteContentRange" in main[1]  # the edit, by index


def _covered_anchor_doc(engine, monkeypatch, comments=None):
    """Doc whose middle paragraph is covered by one anchor, end to end."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        comments or [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    return docs, drive


def test_patch_full_anchor_coverage_is_rewritten(engine, monkeypatch):
    """#21: the whole commented fragment is replaced, and the thread comes
    along. Used to be a refusal — the single most common thing an editor is
    asked to do on a commented paragraph."""
    docs, drive = _covered_anchor_doc(engine, monkeypatch)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "rewritten"

    main = semantic_batch(docs)
    assert "deleteContentRange" in main[0]           # canary first
    assert main[1]["insertText"] == {"location": {"index": 11},
                                     "text": "Zulu"}  # inside the anchor
    assert main[2]["replaceAllText"]["containsText"]["text"] == "BravZulu"
    assert main[2]["replaceAllText"]["replaceText"] == "Zulu"
    assert main[3]["deleteContentRange"]["range"] == {"startIndex": 11,
                                                      "endIndex": 12}


def test_a_closed_thread_no_longer_switches_the_rewrite_off(
        engine, monkeypatch, capsys):
    """#38: ONE closed thread anywhere in the file used to disable rewriting a
    commented fragment across the whole document — a live session lost the
    ability to fix a phrase because the customer closed unrelated threads at
    the other end, and the refusal asked the editor to reopen them.

    Measured 2026-08-16 (M13) on the worst shape there is — a closed thread
    whose ENTIRE anchor is the one character this batch deletes: it survived,
    still in the resolved list, conversation intact, marked by Google as
    «Исходный контент удален». It loses its attachment, not its words, which
    is what happens when a person edits the same text by hand."""
    docs, drive = _covered_anchor_doc(engine, monkeypatch, comments=[
        api_comment("c1", "A", CREATED),
        api_comment("c2", "B", "2026-07-13T18:00:00.000Z", resolved=True)])
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "rewritten"
    assert docs.canary_text is None  # cleaned up


# ---------------------------------------------------------------------------
# r8: narrowing a replace instead of refusing it
# ---------------------------------------------------------------------------

def test_common_affixes_cannot_overlap(engine):
    """«да, да, да» → «да, да»: prefix and suffix both want the same
    characters. Uncapped they sum past the string and the core comes out with
    negative length."""
    p, s = engine._common_affixes("да, да, да", "да, да")
    assert p + s <= len("да, да")


def test_points_for_units_never_splits_a_surrogate_pair(engine):
    """💡 is one code point and two UTF-16 units: a cut of one unit has to
    take the whole character, not half of it."""
    assert engine._points_for_units("💡x", 1) == 1
    assert engine._points_for_units("💡x", 2) == 1
    assert engine._points_for_units("💡x", 3) == 2


def test_pure_insertion_detects_both_sides(engine):
    assert engine._pure_insertion("фраза", "фраза и хвост") == (
        "after", " и хвост")
    assert engine._pure_insertion("фраза", "начало фраза") == (
        "before", "начало ")
    assert engine._pure_insertion("фраза", "другое") is None
    assert engine._pure_insertion("фраза", "фра") is None


def test_doomed_thread_needs_all_its_anchors_covered(engine):
    """A thread on three fragments survives a replace covering two of them
    (measured) — skrepka used to refuse on the first one."""
    anchors = [(10, 20, "one", "0"), (30, 40, "two", "1"),
               (50, 60, "three", "2")]
    attribution = {"0": "c1", "1": "c1", "2": "c1"}
    assert engine._doomed_threads(5, 45, anchors, attribution) == []
    doomed = engine._doomed_threads(5, 65, anchors, attribution)
    assert [cid for cid, _ in doomed] == ["c1"]


def test_unattributed_anchor_keeps_the_strict_rule(engine):
    """Nobody could say which thread this span belongs to, so «the last
    anchor» is unknowable and any full coverage counts."""
    anchors = [(10, 20, "one", "0")]
    doomed = engine._doomed_threads(5, 25, anchors, {})
    assert [cid for cid, _ in doomed] == [None]


def _narrow_tab(text):
    return make_doc([text])


def _one_para(engine, text):
    """Tab with one paragraph, and the (start, end) of its text.

    Indices are UTF-16 code units, like the real API — `make_doc` counts code
    points, which is the same thing only while the text stays in the BMP.
    """
    end = 1 + engine._utf16_len(text)
    tab = {"documentId": "doc1", "revisionId": "R0", "body": {"content": [
        {"startIndex": 1, "endIndex": end + 1, "paragraph": {
            "elements": [{"startIndex": 1, "endIndex": end + 1,
                          "textRun": {"content": text + "\n",
                                      "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}]}}
    return tab, 1, end


def test_narrowing_keeps_the_anchor_when_one_word_changes(engine):
    """The typical edit: a commented sentence with one word replaced. The
    naive replace covers the anchor whole; the narrowed one leaves the
    anchor's first character alive, which is all the thread needs.

    The cut is the MINIMAL one that frees the anchor — not the whole common
    affix — so the core stays long enough to still be unique."""
    text = "Здесь слишком мало примеров"
    tab, start, end = _one_para(engine, text)
    a_start = start + len("Здесь ")
    anchors = [(a_start, a_start + len("слишком мало"), "слишком мало", "0")]
    new_text = "Здесь слишком мало образцов"
    out = engine._narrow_replace(tab, text, new_text, start, end,
                                 anchors, [], {"0": "c1"})
    assert out is not None
    core, repl, start2, end2 = out
    # the applied operation still produces exactly the text that was asked for
    assert text.replace(core, repl) == new_text
    assert start2 > a_start  # the anchor's first character is out of range
    assert engine._doomed_threads(start2, end2, anchors, {"0": "c1"}) == []


def _tab_body_and_cell(engine, body_text, cell_text):
    """Tab with one body paragraph and one 1x1 table after it.

    Returns (tab, body_start, body_end, cell_interval) — the cell interval is
    what a cell fence puts into `blocked`.
    """
    b_end = 1 + engine._utf16_len(body_text)
    t_start = b_end + 1
    c_start = t_start + 2
    p_end = c_start + 1 + engine._utf16_len(cell_text) + 1
    tab = {"documentId": "doc1", "revisionId": "R0", "body": {"content": [
        {"startIndex": 1, "endIndex": b_end + 1, "paragraph": {
            "elements": [{"startIndex": 1, "endIndex": b_end + 1,
                          "textRun": {"content": body_text + "\n",
                                      "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}},
        {"startIndex": t_start, "endIndex": p_end + 1, "table": {"tableRows": [
            {"startIndex": t_start + 1, "endIndex": p_end,
             "tableCells": [
                 {"startIndex": c_start, "endIndex": p_end, "content": [
                     {"startIndex": c_start + 1, "endIndex": p_end,
                      "paragraph": {
                          "elements": [
                              {"startIndex": c_start + 1, "endIndex": p_end,
                               "textRun": {"content": cell_text + "\n",
                                           "textStyle": {}}}],
                          "paragraphStyle": {
                              "namedStyleType": "NORMAL_TEXT"}}}]}]}]}},
    ]}}
    return tab, 1, b_end, (c_start, p_end)


def test_narrowing_refuses_when_the_same_text_lives_in_a_fenced_cell(engine):
    """The target is in the body and the fenced cell holds the same words.
    `replaceAllText` acts on the whole tab and DOES reach into cells (M16b),
    so the narrowed core is not unique and the narrowing has to be dropped —
    the cell fence alone would not have stopped this."""
    tab, start, end, cell = _tab_body_and_cell(
        engine, "Здесь слишком мало примеров", "слишком мало примеров")
    a_start = start + len("Здесь ")
    anchors = [(a_start, a_start + len("слишком мало"), "слишком мало", "0")]
    out = engine._narrow_replace(
        tab, "Здесь слишком мало примеров", "Здесь слишком мало образцов",
        start, end, anchors, [(cell[0], cell[1], "ячейка")], {"0": "c1"})
    assert out is None


def test_narrowing_never_crosses_into_a_fenced_cell(engine):
    """Whatever the narrowing settles on, it must not overlap the cell fence.
    The check runs again on the narrowed range, not only on the original."""
    tab, start, end, cell = _tab_body_and_cell(
        engine, "Здесь слишком мало примеров", "текст в ячейке")
    a_start = start + len("Здесь ")
    anchors = [(a_start, a_start + len("слишком мало"), "слишком мало", "0")]
    blocked = [(cell[0], cell[1], "ячейка")]
    out = engine._narrow_replace(
        tab, "Здесь слишком мало примеров", "Здесь слишком мало образцов",
        start, end, anchors, blocked, {"0": "c1"})
    assert out is not None
    _core, _repl, start2, end2 = out
    assert engine._blocked_hits(start2, end2, blocked) == []
    assert end2 <= cell[0]


def test_a_cell_fence_refuses_an_edit_inside_that_cell(engine):
    """The other direction: the operation itself lands inside the fenced cell.
    Any overlap is refused — unlike a healthy anchor, a fence has no partial
    case, because what it protects has no known position inside it."""
    tab, _start, _end, cell = _tab_body_and_cell(
        engine, "Обычный абзац", "текст в ячейке")
    blocked = [(cell[0], cell[1], "ячейка")]
    hit = engine._find_quote_in_doctab(tab, "текст в ячейке")
    assert hit is not None
    assert engine._blocked_hits(hit[0], hit[1], blocked)


def test_narrowing_gives_up_when_the_anchor_is_inside_the_change(engine):
    """The anchor sits strictly inside the part that changes: neither border
    can move past it without eating into the change itself. Nothing can save
    the thread, so narrowing returns nothing and the caller refuses."""
    text = "Начало мало конец"
    tab, start, end = _one_para(engine, text)
    a_start = start + len("Начало м")
    anchors = [(a_start, a_start + len("ал"), "ал", "0")]
    out = engine._narrow_replace(tab, text, "Начало много конец", start, end,
                                 anchors, [], {"0": "c1"})
    assert out is None


def test_narrowing_refuses_a_core_that_is_not_unique(engine):
    """The narrowed core repeats elsewhere in the tab: replaceAllText would
    rewrite both places, so this narrowing is unusable and we fall back to
    the refusal instead of quietly changing two paragraphs."""
    tab = make_doc(["Xмало примеров", "мало примеров"])
    text, start, end = "Xмало примеров", 1, 15
    anchors = [(start, start + 1, "X", "0")]
    out = engine._narrow_replace(tab, text, "Xмало образцов", start, end,
                                 anchors, [], {"0": "c1"})
    assert out is None


def test_narrowing_counts_headers_when_checking_uniqueness(engine):
    """replaceAllText is not confined to the body. A core that is unique in
    the body but repeated in a running header must not be used — otherwise
    the mismatch only surfaces after the write, as occurrencesChanged."""
    text = "Здесь слишком мало примеров"
    tab, start, end = _one_para(engine, text)
    # NO startIndex anywhere: that is how the API really returns a header —
    # a separate index space, and the first element carries no index at all
    # (measured on a live document 2026-08-05, where the indexed walker
    # raised KeyError and took the whole replace down with it)
    tab["headers"] = {"h1": {"content": [
        {"paragraph": {"elements": [
            {"textRun": {"content": "лишком мало примеров\n"}}]}}]}}
    a_start = start + len("Здесь ")
    anchors = [(a_start, a_start + len("слишком мало"), "слишком мало", "0")]
    out = engine._narrow_replace(tab, text, "Здесь слишком мало образцов",
                                 start, end, anchors, [], {"0": "c1"})
    assert out is None


def test_narrowing_survives_a_non_bmp_char_in_the_affix(engine):
    """The cuts are counted in UTF-16 units and applied to a string of code
    points. A 💡 in the common prefix is one code point and two units, so a
    naive implementation puts start2 one unit short and the round-trip check
    is the only thing between that and a wrong write."""
    text = "💡 Здесь слишком мало примеров 💡"
    tab, start, end = _one_para(engine, text)
    a_start = start + 2 + len(" Здесь ")  # 💡 is 2 UTF-16 units
    anchors = [(a_start, a_start + len("слишком мало"), "слишком мало", "0")]
    out = engine._narrow_replace(
        tab, text, "💡 Здесь слишком мало образцов 💡", start, end,
        anchors, [], {"0": "c1"})
    assert out is not None
    core, repl, start2, end2 = out
    # the range the engine will write must be exactly where the core sits
    assert engine._find_quote_in_doctab(tab, core) == (start2, end2)
    assert text.replace(core, repl) == "💡 Здесь слишком мало образцов 💡"


def _two_run_para(bold_prefix, rest):
    """One paragraph built from two runs with DIFFERENT styles."""
    n1 = len(bold_prefix)
    doc = {"documentId": "doc1", "revisionId": "R0", "body": {"content": [
        {"startIndex": 1, "endIndex": 1 + n1 + len(rest) + 1, "paragraph": {
            "elements": [
                {"startIndex": 1, "endIndex": 1 + n1,
                 "textRun": {"content": bold_prefix,
                             "textStyle": {"bold": True}}},
                {"startIndex": 1 + n1, "endIndex": 1 + n1 + len(rest) + 1,
                 "textRun": {"content": rest + "\n", "textStyle": {}}},
            ],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}]}}
    return doc


def test_mixed_style_in_the_untouched_part_no_longer_decides(engine,
                                                             monkeypatch):
    """The bold prefix carries the anchor and is NOT what changes. Checking
    style over the whole original range refused the operation before
    narrowing was ever tried; the check belongs on the range that will
    actually be written."""
    doc = _two_run_para("Жирный якорь", " и обычный хвост примеров")
    docs = DocsStub(doc)
    para = "Жирный якорь и обычный хвост примеров"
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [(para, [("0", 0, 12)])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": para,
          "with": "Жирный якорь и обычный хвост образцов"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "narrowed"
    main = semantic_batch(docs)
    edit = next(r["deleteContentRange"]["range"] for r in main[1:]
                if "deleteContentRange" in r)
    assert edit["endIndex"] > edit["startIndex"]


def test_noop_replace_writes_nothing(engine, monkeypatch):
    """«Bravo» → «Bravo» changed nothing, so nothing may be written: the old
    path deleted the text and inserted it back, with every gate that hangs
    off a rewrite."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Bravo"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "no-op"
    assert docs.batches == []


def test_replace_that_only_extends_is_applied_as_an_insert(engine, monkeypatch):
    """«Bravo» → «Bravo2» removes no original character, so it cannot ghost
    anything: no export, no canary, just an insert. It used to be refused as
    full anchor coverage."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Bravo2"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "insert"
    assert not any("replaceAllText" in r for b in docs.batches for r in b)
    inserts = [r["insertText"] for b in docs.batches for r in b
               if "insertText" in r]
    assert inserts == [{"location": {"index": 12}, "text": "2"}]


def test_narrowed_replace_reaches_the_api_and_is_reported(engine, monkeypatch):
    """End to end: the op covers the anchor, skrepka narrows it, the write
    goes through and the receipt says the applied operation is not the one
    that was asked for."""
    doc = make_doc(["Alpha", "Здесь слишком мало примеров", "Charlie"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []),
                             ("Здесь слишком мало примеров",
                              [("0", 6, 18)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Здесь слишком мало примеров",
          "with": "Здесь слишком мало образцов"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "narrowed"
    main = semantic_batch(docs)
    # minimal cut: only what the anchor needs is rewritten
    assert written_text(main) == "лишком мало образцов"


# ---------------------------------------------------------------------------
# #21: rewriting a fully covered anchor without ghosting the thread
# ---------------------------------------------------------------------------

def _rewrite_case(engine, text="Здесь старый текст", new="Совсем иная фраза",
                  **over):
    """Everything `_rewrite_anchor_requests` needs for one paragraph whose
    anchor covers the text exactly."""
    tab, start, end = _one_para(engine, text)
    kw = {
        "doc_tab": tab, "search_text": text, "new_text": new,
        "start": start, "end": end,
        "anchors": [(start, end, text, "0")],
        "attribution": {"0": "c1"},
        "named_intervals": [],
    }
    kw.update(over)
    return kw


def test_rewrite_builds_the_three_requests(engine):
    kw = _rewrite_case(engine)
    requests, tail_len = engine._rewrite_anchor_requests(**kw)
    assert tail_len == 1
    ins, rep, dele = requests
    # insert lands strictly inside the anchor, before its last character
    assert ins["insertText"]["location"]["index"] == kw["end"] - 1
    # the match leaves that last character alone, so it never covers the whole
    assert rep["replaceAllText"]["containsText"]["text"] == \
        kw["search_text"][:-1] + kw["new_text"]
    assert rep["replaceAllText"]["replaceText"] == kw["new_text"]
    # and the leftover original character goes last
    tail_at = kw["start"] + engine._utf16_len(kw["new_text"])
    assert dele["deleteContentRange"]["range"] == {"startIndex": tail_at,
                                                   "endIndex": tail_at + 1}


def test_rewrite_counts_a_non_bmp_tail_in_utf16(engine):
    """The anchor ends with an emoji: one code point, two index units."""
    kw = _rewrite_case(engine, text="Старый текст 💡")
    requests, tail_len = engine._rewrite_anchor_requests(**kw)
    assert tail_len == 2
    assert requests[0]["insertText"]["location"]["index"] == kw["end"] - 2
    rng = requests[2]["deleteContentRange"]["range"]
    assert rng["endIndex"] - rng["startIndex"] == 2


def test_rewrite_refuses_a_single_emoji_anchor(engine):
    """«💡» is two UTF-16 units but ONE character: there is no position
    strictly inside it, and a length check in units would wave it through."""
    assert engine._rewrite_anchor_requests(**_rewrite_case(engine, text="💡")) \
        is None


def test_rewrite_refuses_a_single_character_anchor(engine):
    assert engine._rewrite_anchor_requests(**_rewrite_case(engine, text="Я")) \
        is None


def test_rewrite_refuses_an_empty_replacement(engine):
    """Nothing would be left under the anchor to keep it from collapsing."""
    assert engine._rewrite_anchor_requests(**_rewrite_case(engine, new="")) \
        is None


def test_rewrite_refuses_a_newline_in_the_replacement(engine):
    """Docs turns \\n into a paragraph break, so the inserted text would not
    be the text every position was computed for."""
    assert engine._rewrite_anchor_requests(
        **_rewrite_case(engine, new="две\nстроки")) is None


def test_rewrite_refuses_when_a_neighbour_anchor_touches_the_range(engine):
    """A neighbour ending on the tail character would meet the deletion from
    outside — the one shape measured to damage text."""
    kw = _rewrite_case(engine)
    kw["anchors"] = kw["anchors"] + [(kw["end"] - 1, kw["end"] + 3, "хвост",
                                      "9")]
    kw["attribution"] = {"0": "c1", "9": "c2"}
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_when_a_named_range_covers_the_target(engine):
    kw = _rewrite_case(engine)
    kw["named_intervals"] = [(kw["start"], kw["end"], "named range 'mark1'")]
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_when_a_table_of_contents_is_present(engine):
    kw = _rewrite_case(engine)
    kw["doc_tab"]["body"]["content"].append(
        {"startIndex": 900, "endIndex": 950, "tableOfContents": {}})
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_when_the_anchor_is_inside_a_wider_replace(engine):
    """The anchor is a phrase in the middle: the original would have to go
    from both sides, and that means deleting into the anchor from outside."""
    kw = _rewrite_case(engine)
    kw["anchors"] = [(kw["start"] + 6, kw["start"] + 11, "старый", "0")]
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_two_doomed_threads(engine):
    kw = _rewrite_case(engine)
    kw["anchors"] = kw["anchors"] + [(kw["start"] + 1, kw["start"] + 4,
                                      "дес", "9")]
    kw["attribution"] = {"0": "c1", "9": "c2"}
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_a_second_thread_on_the_same_selection(engine):
    """Two comments on the same paragraph export byte-identical ranges. If the
    other thread has a second anchor elsewhere it is not doomed, so the count
    of doomed threads stays at one — and «same range» must not be read as
    «same thread», or someone else's anchor gets rewritten silently."""
    kw = _rewrite_case(engine)
    kw["anchors"] = kw["anchors"] + [
        (kw["start"], kw["end"], kw["search_text"], "9"),   # the twin
        (900, 910, "далеко", "8"),                           # its other anchor
    ]
    kw["attribution"] = {"0": "c1", "9": "c2", "8": "c2"}
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_a_span_nobody_could_attribute(engine):
    """A span whose (author, second) collides with another thread stays
    unattributed. Here it only OVERLAPS the target, so it is not doomed and
    the count of doomed threads stays at one — it has to be refused by the
    neighbour check itself. It may be a duplicate of our own anchor or a
    whole other thread, and the geometry cannot tell."""
    kw = _rewrite_case(engine)
    kw["anchors"] = kw["anchors"] + [(kw["end"] - 1, kw["end"] + 5,
                                      "хвост", "7")]
    assert engine._doomed_threads(
        kw["start"], kw["end"], kw["anchors"], kw["attribution"]) != []
    assert len(engine._doomed_threads(
        kw["start"], kw["end"], kw["anchors"], kw["attribution"])) == 1
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_a_colliding_reply_as_two_doomed_threads(engine):
    """The other half of the same trouble: an unattributed span that IS
    covered by the target counts as a second doomed thread. Refusing is the
    honest outcome — it may be our own reply, and it may not be."""
    kw = _rewrite_case(engine)
    kw["anchors"] = kw["anchors"] + [(kw["start"], kw["end"],
                                      kw["search_text"], "7")]
    assert len(engine._doomed_threads(
        kw["start"], kw["end"], kw["anchors"], kw["attribution"])) == 2
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_survives_a_thread_with_replies(engine):
    """A thread exports one record per entry, all with the parent's range —
    a reply means two identical anchors. Counting spans instead of distinct
    ranges would switch the rewrite off for every thread anybody answered."""
    kw = _rewrite_case(engine)
    kw["anchors"] = kw["anchors"] + [(kw["start"], kw["end"],
                                      kw["search_text"], "1")]
    kw["attribution"] = {"0": "c1", "1": "c1"}
    assert engine._rewrite_anchor_requests(**kw) is not None


def test_rewrite_refuses_a_projected_string_that_is_not_unique(engine):
    """The needle exists only after the insert, so uniqueness is checked on
    the projected text — and here the same pair already sits in the document
    next door."""
    text, new = "аб", "вг"
    tab = make_doc([text, "а" + new])   # «авг» already contains «а» + «вг»
    kw = _rewrite_case(engine, text=text, new=new)
    kw["doc_tab"] = tab
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_refuses_when_the_needle_also_sits_in_a_header(engine):
    kw = _rewrite_case(engine)
    kw["doc_tab"]["headers"] = {"h1": {"content": [
        {"paragraph": {"elements": [
            {"textRun": {"content": kw["search_text"][:-1] + kw["new_text"]}}
        ]}}]}}
    assert engine._rewrite_anchor_requests(**kw) is None


def test_rewrite_response_without_one_match_is_unknown(engine):
    """occurrencesChanged here is diagnosis after the write: the insert and
    the deletion land whatever the replace matched, so anything but one means
    the document changed in a way nobody planned."""
    class Svc:
        def documents(self):
            return self

        def batchUpdate(self, **kw):
            return self

        def execute(self):
            return {"replies": [{}, {}, {"replaceAllText":
                                         {"occurrencesChanged": 0}}]}

    with pytest.raises(engine.PatchOpError) as exc:
        engine._execute_anchor_rewrite(Svc(), "doc1", None, [
            {"insertText": {"location": {"index": 1}, "text": "x"}},
            {"replaceAllText": {"containsText": {"text": "a"},
                                "replaceText": "b"}},
            {"deleteContentRange": {"range": {"startIndex": 1,
                                              "endIndex": 2}}},
        ], "R1", "quote='a'")
    assert exc.value.state == "unknown"
    assert "документ изменён" in str(exc.value)


def test_narrowing_wins_over_rewriting(engine, monkeypatch):
    """The cheap path first: when one word changes, the thread is saved by
    narrowing and the document is written once, not three times."""
    doc = make_doc(["Alpha", "Здесь слишком мало примеров", "Charlie"])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []),
                             ("Здесь слишком мало примеров", [("0", 6, 18)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", {
        "op": "replace_quote", "quote": "Здесь слишком мало примеров",
        "with": "Здесь слишком мало образцов"}, None)
    assert note["applied_as"] == "narrowed"
    main = semantic_batch(docs)
    # narrowing writes the shortened range directly; the anchor rewrite would
    # instead do its sentinel dance through `replaceAllText`
    assert not any("replaceAllText" in r for r in main)
    assert written_text(main) == "лишком мало образцов"


# ---------------------------------------------------------------------------
# r8: a pending suggestion stops blocking inserts elsewhere
# ---------------------------------------------------------------------------

def _tab_with_suggestion():
    """Two paragraphs; the FIRST carries a suggested insertion."""
    tab = make_doc(["Alpha", "Bravo"])
    first = tab["body"]["content"][0]
    first["paragraph"]["elements"][0]["textRun"]["suggestedInsertionIds"] = \
        ["sug1"]
    return tab


def test_suggestion_intervals_cover_only_the_suggested_run(engine):
    intervals = engine._suggestion_intervals(_tab_with_suggestion())
    assert [(s, e) for s, e, _ in intervals] == [(1, 7)]


def test_paragraph_level_suggestion_covers_the_paragraph(engine):
    tab = make_doc(["Alpha", "Bravo"])
    tab["body"]["content"][1]["paragraph"]["suggestedParagraphStyleChanges"] \
        = {"sug1": {}}
    intervals = engine._suggestion_intervals(tab)
    assert [(s, e) for s, e, _ in intervals] == [(7, 13)]


def test_insert_outside_a_suggestion_is_allowed(engine):
    """The whole point of #19: a suggestion in the first paragraph used to
    block an insert in the second, though an insert removes nothing."""
    tab = _tab_with_suggestion()
    engine._refuse_on_suggestion_at(tab, 10, "quote='Bravo'")  # no raise


def test_insert_inside_a_suggestion_is_refused(engine, capsys):
    tab = _tab_with_suggestion()
    with pytest.raises(SystemExit):
        engine._refuse_on_suggestion_at(tab, 3, "quote='Alpha'")
    err = json.loads(capsys.readouterr().out)["error"]
    assert "внутрь предложенной правки" in err


def test_a_replace_that_removes_nothing_is_judged_as_an_insert(engine):
    """An op is judged by what it does, not by the word in its name: «Alpha»
    → «Alpha дальше» removes nothing, so a suggestion in that same paragraph
    is the only thing that can stop it — and here it is in the other one."""
    tab = make_doc(["Alpha", "Bravo"])
    tab["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
        "suggestedInsertionIds"] = ["sug1"]
    r = engine._resolve_op(
        {"op": "replace_quote", "quote": "Bravo", "with": "Bravo дальше"},
        tab, None)
    assert engine._op_pure_insertion(
        {"op": "replace_quote", "quote": "Bravo", "with": "Bravo дальше"},
        tab, r) == ("after", " дальше")


def test_a_replace_that_removes_text_is_not_an_insert(engine):
    tab = make_doc(["Alpha", "Bravo"])
    r = engine._resolve_op(
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}, tab, None)
    assert engine._op_pure_insertion(
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"},
        tab, r) is None


def test_replace_away_from_a_suggestion_is_allowed(engine):
    """Measured 2026-08-05: the export carries a pending suggestion as a real
    tracked change, paragraph texts still match the API snapshot, and the
    anchor pipeline comes out clean. So a suggestion three paragraphs away is
    no longer a reason to refuse a replace."""
    tab = _tab_with_suggestion()
    engine._refuse_on_suggestion_range(tab, 8, 12, "quote='Bravo'")


def test_replace_touching_a_suggestion_is_refused(engine, capsys):
    """What replaceAllText does to a match overlapping suggested text is NOT
    measured — that is what stays refused."""
    tab = _tab_with_suggestion()
    with pytest.raises(SystemExit):
        engine._refuse_on_suggestion_range(tab, 3, 9, "quote='Alpha'")
    err = json.loads(capsys.readouterr().out)["error"]
    assert "задевает предложенную правку" in err


def test_sync_is_still_blocked_by_any_suggestion(engine, capsys):
    """sync reconciles the whole document against a local file, and what a
    pending suggestion does to that comparison was not measured."""
    with pytest.raises(SystemExit):
        engine._refuse_on_suggestions(_tab_with_suggestion())
    err = json.loads(capsys.readouterr().out)["error"]
    assert "pending suggestions" in err


# ---------------------------------------------------------------------------
# r8: one refused operation must not cost the person the other nine
# ---------------------------------------------------------------------------

def test_clean_doc_patch_does_not_rewrite_a_pure_insertion(engine, monkeypatch,
                                                           tmp_path, capsys):
    """No comments in the document, so the atomic index path runs. A replace
    that only extends its target must still be emitted as an insert: turning
    it into delete+insert removes text the caller never asked to remove, and
    the suggestion gate was already told this op removes nothing."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Bravo", "with": "Bravo2"},
    ]), encoding="utf-8")

    engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "patched"
    batch = docs.batches[0]
    assert not any("deleteContentRange" in rq for rq in batch)
    assert batch[0]["insertText"] == {"location": {"index": 12}, "text": "2"}


def test_op_classification_follows_the_same_target_as_resolution(engine):
    """An op carrying BOTH `range` and `quote` is accepted, and `_resolve_op`
    targets the range. Reading the quote instead would classify the op
    against text it does not touch — and a no-op decided on the wrong text
    writes nothing where a replace was due."""
    doc = make_doc(BASE_TEXTS, named_ranges={
        "mark1": {"namedRanges": [{"ranges": [
            {"startIndex": 7, "endIndex": 12}]}]}})
    op = {"op": "replace_range", "range": "mark1", "quote": "Charlie",
          "text": "Charlie"}
    r = engine._resolve_op(op, doc, None)
    assert (r["start"], r["end"]) == (7, 12)  # the range, not the quote
    # the range holds "Bravo", the new text is "Charlie" — not a no-op
    assert engine._op_is_noop(op, doc, r) is False
    assert engine._op_pure_insertion(op, doc, r) is None


def test_a_noop_does_not_veto_a_compatible_neighbour(engine, monkeypatch,
                                                     tmp_path, capsys):
    """A no-op writes nothing, so it occupies nothing. It used to hold its
    range through the overlap check and reject the insert next to it."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Bravo", "with": "Bravo"},
        {"op": "insert_after_quote", "quote": "Bravo", "text": " хвост"},
    ]), encoding="utf-8")

    engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "patched"
    inserts = [rq["insertText"] for b in docs.batches for rq in b
               if "insertText" in rq]
    assert inserts == [{"location": {"index": 12}, "text": " хвост"}]


def test_clean_doc_patch_skips_a_noop(engine, monkeypatch, tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Bravo", "with": "Bravo"},
    ]), encoding="utf-8")

    engine.patch_doc("doc1", str(ops))
    assert docs.batches == []


# ---------------------------------------------------------------------------
# r10 (#45): a comment selected across a paragraph break
# ---------------------------------------------------------------------------

CROSS_TEXTS = ["Заголовок", "Подзаголовок", "Прочий текст"]


def _crossing_case(engine, monkeypatch):
    """A doc whose only comment is dragged from «Заголовок» into
    «Подзаголовок» — what an editor produces by dragging the selection down
    without thinking."""
    docs = DocsStub(make_doc(CROSS_TEXTS))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _crossing_docx(docs, CROSS_TEXTS, "0", (0, 0), (1, len("Подзаголовок")),
                       [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    return docs


def test_a_crossing_comment_no_longer_freezes_the_document(engine, monkeypatch,
                                                           tmp_path, capsys):
    """#45: the refusal had no coordinates to confine it, so ONE comment
    selected across a break disabled every replace in the document. The
    accounting also demanded exactly one span per comments.xml record and got
    zero, which was a second global refusal on top."""
    docs = _crossing_case(engine, monkeypatch)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Прочий текст", "with": "Другой"},
    ]), encoding="utf-8")

    engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 1
    assert any("insertText" in rq for b in docs.batches for rq in b)


def test_a_replace_covering_a_crossing_anchor_is_still_refused(engine,
                                                              monkeypatch,
                                                              tmp_path,
                                                              capsys):
    """Размещение не равно разрешению: тред, чей якорь протянут через границу
    абзаца, по-прежнему защищён.

    С 0.17 отказ приходит от более ранней и более строгой гвардии. Запись
    идёт по индексам, поэтому такая правка физически удалила бы перевод
    строки: два абзаца слились бы в один, а оформление второго пропало.
    Защита самого якоря от полного покрытия никуда не делась и проверяется
    на правках внутри одного абзаца."""
    _crossing_case(engine, monkeypatch)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Заголовок\nПодзаголовок",
         "with": "Совсем другое"},
    ]), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("doc1", str(ops))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert "удаляет границу абзаца" in out["refused"][0]["error"]


def test_a_ghost_is_named_in_the_receipt_and_the_patch_goes_on(engine,
                                                               monkeypatch,
                                                               tmp_path,
                                                               capsys):
    """#34 end to end: one vanished thread used to refuse every replace in the
    document. It is now named once — not per operation — and removing it stays
    the person's decision (CONTRACT §2.2)."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    gone = api_comment("c2", "B", "2026-07-13T17:00:00.000Z")
    gone["quotedFileContent"] = {"value": "Вариант 1"}
    drive = DriveStub(
        [api_comment("c1", "A", CREATED), gone],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"},
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
    ]), encoding="utf-8")

    engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 2
    assert [g["id"] for g in out["ghost_threads"]] == ["c2"]
    assert "disco=c2" in out["ghost_threads"][0]["link"]
    assert "Убрать его можно вручную" in out["ghost_threads"][0]["note"]


# ---------------------------------------------------------------------------
# r10 (#36): the validation phase must not throw the file away either
# ---------------------------------------------------------------------------

def test_an_unresolvable_op_does_not_zero_the_clean_batch(engine, monkeypatch,
                                                          tmp_path, capsys):
    """Live session 2026-08-09: one non-unique quote refused the whole file
    and 36 correct edits were never written. Per-op refusal used to live only
    inside the apply loop; everything rejected before it took the file down."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"},
        {"op": "replace_quote", "quote": "Такого текста нет", "with": "X"},
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
    ]), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("doc1", str(ops))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "partially-patched"
    assert out["ops_applied"] == 2
    assert [r["op"] for r in out["refused"]] == [1]
    # the refusal names the operation, and a deferred op has no resolved
    # record to take that name from
    assert out["refused"][0]["source"] == "quote='Такого текста нет'"
    written = [rq["insertText"]["text"] for b in docs.batches for rq in b
               if "insertText" in rq]
    assert sorted(written) == ["Xray", "Yankee"]


def test_an_overlapping_pair_is_refused_and_the_neighbour_applies(
        engine, monkeypatch, tmp_path, capsys):
    """Both members of the pair go: applying either one moves the other's
    target, so there is no «first one wins». The op that overlaps nothing has
    no part in the ambiguity and used to be refused with them."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub([], lambda: b"")
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"},
        {"op": "replace_quote", "quote": "rav", "with": "RAV"},
        {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"},
    ]), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("doc1", str(ops))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert [r["op"] for r in out["refused"]] == [0, 1]
    assert "ops overlap" in out["refused"][0]["error"]
    written = [rq["insertText"]["text"] for b in docs.batches for rq in b
               if "insertText" in rq]
    assert written == ["Yankee"]


def test_an_op_made_unique_by_its_predecessor_goes_through(engine, monkeypatch,
                                                           tmp_path, capsys):
    """The engine applies against the live document, the validator judged a
    dead snapshot — so a chain where op N makes op N+1 unique was rejected
    before anything was written. In the live session that forced one logical
    set of edits into seven separate `patch` calls (#36)."""
    docs = MutatingDocsStub(make_doc(["Один Дубль", "Два Дубль", "Три"]))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_tracking(docs, {2: [("0", 0, 3)]}, [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Один Дубль", "with": "Один Копия"},
        {"op": "replace_quote", "quote": "Дубль", "with": "Финал"},
    ]), encoding="utf-8")

    engine.patch_doc("doc1", str(ops))
    out = json.loads(capsys.readouterr().out)
    assert out["ops_applied"] == 2
    texts = [el["paragraph"]["elements"][0]["textRun"]["content"][:-1]
             for el in docs.base["body"]["content"] if "paragraph" in el]
    assert texts == ["Один Копия", "Два Финал", "Три"]
    # and the receipt does not pretend the overlap check covered it — in the
    # operation's OWN note, not a second entry that would read as a second
    # operation
    notes = [n for n in out["op_notes"] if "deferred" in n]
    assert len(notes) == 1
    assert len(out["op_notes"]) == 1
    assert "в проверке пересечений" in notes[0]["deferred"]


def test_patch_refuses_one_op_and_applies_the_rest(engine, monkeypatch,
                                                   tmp_path, capsys):
    """The anchor sits on «Bravo», so replacing Bravo whole is refused — but
    Alpha and Charlie have nothing to do with it. Before r8 the loop broke on
    the first refusal and the other operations never ran."""
    doc = make_doc(BASE_TEXTS)
    # a table of contents keeps the rewrite path out of this document (the
    # text walkers do not read one, so the uniqueness count would miss a match
    # hiding there), so covering the anchor whole is still a refusal. A closed
    # thread used to serve that purpose and no longer does — see M13.
    doc["body"]["content"].append({"startIndex": 100, "endIndex": 101,
                                   "tableOfContents": {}})
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Alpha", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]))
    wire(engine, monkeypatch, docs, drive)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "replace_quote", "quote": "Alpha", "with": "Yankee"},
        {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"},
        {"op": "replace_quote", "quote": "Charlie", "with": "Xray"},
    ]), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        engine.patch_doc("doc1", str(ops))
    assert exc.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "partially-patched"
    assert out["ops_applied"] == 2
    assert [r["op"] for r in out["refused"]] == [1]
    assert "накрывает целиком последний якорь" in out["refused"][0]["error"]
    # nothing halted: no failed_at, the doc state is known throughout
    assert "failed_at" not in out


def test_replace_all_reply_found_by_type_not_position(engine):
    class OneShot:
        def __init__(self):
            self.body = None

        def documents(self):
            return self

        def batchUpdate(self, documentId=None, body=None):
            self.body = body
            return _Req({"replies": [{}, {"replaceAllText":
                                          {"occurrencesChanged": 1}}]})

    svc = OneShot()
    engine._execute_replace_all(
        svc, "doc1", None, "a", "b", "R1", "quote='a'",
        extra_requests_before=[_del(5, 9)])
    assert "deleteContentRange" in svc.body["requests"][0]
    # no PatchOpError raised: occurrencesChanged found in reply #2


# ---------------------------------------------------------------------------
# export html: comment artifacts must not leak into md
# ---------------------------------------------------------------------------

GOOGLE_COMMENT_HTML = (
    '<p><span>Первый абзац.</span>'
    '<sup><a href="#cmnt1" id="cmnt_ref1">[a]</a></sup>'
    '<sup><a href="#cmnt2" id="cmnt_ref2">[b]</a></sup></p>'
    '<p><span>Второй абзац с <a href="#heading=h.abc">якорем на раздел</a>'
    ' и <a href="https://example.com/#cmnt99">внешней ссылкой</a>.</span></p>'
    '<div><p><a href="#cmnt_ref1" id="cmnt1">[a]</a>'
    '<span>Текст первого коммента</span></p></div>'
    '<div><p><a href="#cmnt_ref2" id="cmnt2">[b]</a>'
    '<span>Текст второго коммента</span></p></div>')


def test_prepare_export_html_strips_comment_markers(engine):
    out = engine._prepare_export_html(GOOGLE_COMMENT_HTML)
    assert "[a]" not in out and "[b]" not in out
    assert "cmnt_ref" not in out
    assert "Текст первого коммента" not in out
    assert "Текст второго коммента" not in out
    assert "Первый абзац." in out


def test_prepare_export_html_keeps_real_links(engine):
    out = engine._prepare_export_html(GOOGLE_COMMENT_HTML)
    assert "#heading=h.abc" in out
    assert "https://example.com/#cmnt99" in out
    assert "внешней ссылкой" in out


def test_commented_doc_sidecar_is_syncable(engine, tmp_path):
    """The md rebuilt from a comment-bearing export must round-trip: the
    sidecar for a commented doc says sync_supported=True."""
    from markdownify import markdownify as md_convert
    html = engine._prepare_export_html(GOOGLE_COMMENT_HTML)
    md = md_convert(html, heading_style="ATX", strip=["style"])
    import re as _re
    md = _re.sub(r"\n{3,}", "\n\n", md).strip("\n")
    doc = make_doc(["Первый абзац.",
                    "Второй абзац с якорем на раздел и внешней ссылкой."])
    payload = engine._sidecar_payload("doc1", str(tmp_path / "d.md"), md, doc)
    assert payload["sync_supported"], payload["reason"]


# ---------------------------------------------------------------------------
# canary lifecycle under failures (codex code-r1 #1/#2/#3/#5 + test note #7)
# ---------------------------------------------------------------------------

ANCHORED_PARAS = [("Alpha", []), ("Bravo", []), ("Charlie", [("0", 0, 7)])]
ANCHORED_COMMENTS = [("0", "A", CREATED_SEC)]
LOCAL_EDIT = "Alpha\n\nBravo edited\n\nCharlie"


def _anchored_setup(engine, monkeypatch, tmp_path, docs=None, drive=None):
    doc = make_doc(BASE_TEXTS)
    docs = docs or DocsStub(doc)
    drive = drive or DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)
    return doc, docs, drive, md


def test_sync_with_a_closed_thread_applies_and_archives_the_conversation(
        engine, monkeypatch, tmp_path, capsys):
    """r8: a closed thread stops blocking sync. Google hides it from the
    export, so its anchor cannot be protected and the rewrite may unhook it —
    the words are written out first and the receipt says so, instead of the
    document being frozen forever."""
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    closed = api_comment("c2", "B", "2026-07-13T17:55:00.000Z", resolved=True)
    closed["replies"] = [{"id": "r1", "content": "и вот почему",
                          "author": {"displayName": "B"},
                          "createdTime": "2026-07-13T17:56:00.000Z"}]
    drive = DriveStub(
        [api_comment("c1", "A", CREATED), closed],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        html=b"<p>Alpha</p><p>Bravo edited</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert out["closed_threads"]["count"] == 1

    with open(out["closed_threads"]["archive"], encoding="utf-8") as f:
        archive = json.load(f)
    thread = archive["threads"][0]
    assert thread["id"] == "c2"
    assert thread["link"].endswith("?disco=c2")
    assert thread["replies"][0]["content"] == "и вот почему"


def test_closed_thread_archive_accumulates(engine, tmp_path):
    """A thread archived earlier may be gone from today's census — deleted,
    or no longer anchored. A plain overwrite would destroy the only copy of
    it that exists anywhere, which is not what an archive is."""
    md = tmp_path / "doc.md"
    md.write_text("x", encoding="utf-8")
    first = api_comment("old", "A", CREATED, resolved=True)
    engine._archive_closed_threads(str(md), "doc1", [first])
    second = api_comment("new", "B", CREATED, resolved=True)
    path = engine._archive_closed_threads(str(md), "doc1", [second])
    with open(path, encoding="utf-8") as f:
        archive = json.load(f)
    assert {t["id"] for t in archive["threads"]} == {"old", "new"}


def test_closed_thread_archive_refuses_to_clobber_an_unreadable_one(engine,
                                                                    tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("x", encoding="utf-8")
    broken = tmp_path / "doc.md.skrepka-closed-threads.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        engine._archive_closed_threads(
            str(md), "doc1",
            [api_comment("c2", "B", CREATED, resolved=True)])
    assert "единственная копия" in str(exc.value)
    assert broken.read_text(encoding="utf-8") == "{ not json"


def test_sync_stops_when_the_conversation_cannot_be_saved(engine, monkeypatch,
                                                          tmp_path, capsys):
    """The archive is what makes the known cost of this edit acceptable.
    Without it the sync could unhook a closed thread and leave no copy of
    what was said — so it does not run."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED),
         api_comment("c2", "B", "2026-07-13T17:55:00.000Z", resolved=True)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS))
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs, drive=drive)
    monkeypatch.setattr(engine, "_archive_closed_threads",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            OSError("read-only file system")))
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "не удалось сохранить разговор закрытых тредов" in err
    assert not docs.main_applied


def test_insert_lost_response_but_applied_is_cleaned(engine, monkeypatch,
                                                     tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    docs.insert_error = http_error(503)
    docs.insert_error_applies = True
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "lost response" in err and "re-run" in err
    assert docs.canary_text is None  # probed, found, cleaned
    assert _no_content_mutation(docs)


def test_insert_5xx_not_applied_is_retry_reason(engine, monkeypatch,
                                                tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    docs.insert_error = http_error(503)
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "did not land" in err
    assert docs.canary_text is None


def test_revid_moved_with_failing_cleanup_warns(engine, monkeypatch,
                                                tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    docs.post_insert_rev = "R9"  # concurrent edit after the insert
    docs.fail_cleanup = True
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "осталась служебная строка" in err
    assert docs.canary_text is not None  # honestly reported as orphaned


def test_unexpected_exception_after_insert_cleans_up(engine, monkeypatch,
                                                     tmp_path, capsys):
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "_account_anchored_comments",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "unexpectedly" in err
    assert docs.canary_text is None
    assert _no_content_mutation(docs)


def test_main_batch_lost_response_recovers_as_success(engine, monkeypatch,
                                                      tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    docs.main_error = OSError("timeout mid-flight")
    docs.main_error_applies = True
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        html=b"<p>Alpha</p><p>Bravo edited</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert docs.main_applied


def test_main_batch_lost_response_not_applied_fails_partial(
        engine, monkeypatch, tmp_path, capsys):
    """Response lost, canary gone by collaborator hand, batch NOT applied:
    positional verify must fail-partial, not report success."""
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    docs.main_error = OSError("timeout mid-flight")
    docs.main_error_applies = False

    class TrickyDocs(DocsStub):
        pass
    # simulate the canary disappearing without the batch applying:
    # after the transport error, hide the canary from probes
    orig_current = docs._current

    def current_without_canary():
        saved, docs.canary_text = docs.canary_text, None
        try:
            return orig_current()
        finally:
            docs.canary_text = saved
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS))
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)

    real_batch = docs.batchUpdate

    def batch_then_hide(documentId=None, body=None):
        r = real_batch(documentId=documentId, body=body)
        if docs.main_error is None and not docs.main_applied:
            docs._current = current_without_canary
        return r
    docs.batchUpdate = batch_then_hide
    with pytest.raises(SystemExit) as exc:
        engine.sync_doc("doc1", md)
    assert exc.value.code == 3  # sync-partial-failure, not success
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "sync-partial-failure"


def test_patch_fp2_change_with_failing_cleanup_raises(engine, monkeypatch,
                                                      capsys):
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    docs.fail_cleanup = True
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        comments_after=[api_comment("c1", "A", CREATED),
                        api_comment("c9", "Z", "2026-07-14T00:00:00.5Z")],
        # one census now yields both the accounting data and fp1 (#12),
        # so the second list IS fp2 and it sees a new comment
        switch_after_lists=1)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Zulu"}
    with pytest.raises(engine.PatchOpError) as exc:
        engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert "осталась служебная строка" in str(exc.value)
    assert exc.value.state == "not_applied"


# ---------------------------------------------------------------------------
# named-range contract + gate scope (codex code-r1 #6)
# ---------------------------------------------------------------------------

def test_named_range_none_entry_fails_closed(engine):
    doc = make_doc(["Alpha"], named_ranges={"mark": None})
    with pytest.raises(SystemExit):
        engine._named_range_intervals(doc)


def test_named_range_empty_list_fails_closed(engine):
    doc = make_doc(["Alpha"], named_ranges={"mark": {"namedRanges": []}})
    with pytest.raises(SystemExit):
        engine._named_range_intervals(doc)


def test_named_range_no_segments_fails_closed(engine):
    doc = make_doc(["Alpha"], named_ranges={
        "mark": {"namedRanges": [{"ranges": []}]}})
    with pytest.raises(SystemExit):
        engine._named_range_intervals(doc)


def test_insert_only_sync_ignores_malformed_named_range(engine, monkeypatch,
                                                        tmp_path, capsys):
    """The named-range gate guards removals only: an insert-only sync on a
    doc with a malformed mark must proceed."""
    doc = make_doc(BASE_TEXTS, named_ranges={"mark": None})
    merged = make_doc(["Alpha", "Bravo", "New para", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    drive = DriveStub(
        [], lambda: b"",
        html=b"<p>Alpha</p><p>Bravo</p><p>New para</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD,
                      "Alpha\n\nBravo\n\nNew para\n\nCharlie")
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert out["inserted"] == 1


# ---------------------------------------------------------------------------
# comment-tail html: shared containers (codex code-r1 #4)
# ---------------------------------------------------------------------------

SHARED_DIV_HTML = (
    '<div>'
    '<p><span>Настоящий контент документа.</span></p>'
    '<p><a href="#cmnt_ref1" id="cmnt1">[a]</a><span>Коммент</span></p>'
    '</div>')


def test_shared_div_keeps_real_content(engine):
    out = engine._prepare_export_html(SHARED_DIV_HTML)
    assert "Настоящий контент документа." in out
    assert "Коммент" not in out
    assert "cmnt_ref" not in out


def test_pure_comment_div_fully_removed(engine):
    html = ('<p><span>Текст.</span></p>'
            '<div><p><a href="#cmnt_ref1" id="cmnt1">[a]</a>'
            '<span>К1</span></p>'
            '<p><a href="#cmnt_ref2" id="cmnt2">[b]</a>'
            '<span>К2</span></p></div>')
    out = engine._prepare_export_html(html)
    assert "Текст." in out
    assert "К1" not in out and "К2" not in out
    assert "<div>" not in out


def test_accounting_span_without_id_no_crash(engine):
    # regression: sorted({None, "7"}) used to TypeError (codex code-r1 #3)
    records = [{"docx_id": "7", "author": "A", "date_sec": "d"}]
    problems, _ = engine._account_anchored_comments(
        [], records, [{"docx_id": None}], universe={})
    assert any("without a w:id" in p for p in problems)


# ---------------------------------------------------------------------------
# final-batch HttpError branches + recovery skips styles (codex code-r2)
# ---------------------------------------------------------------------------

def test_recovery_skips_style_pass(engine, monkeypatch, tmp_path, capsys):
    """Lost response recovery must NOT run the style batch — a concurrent
    style-only edit between the batch and the re-read would be lost."""
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    docs.main_error = OSError("timeout mid-flight")
    docs.main_error_applies = True
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        html=b"<p>Alpha</p><p>Bravo edited</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert out["recovered_after_lost_response"] is True
    assert out["styled_blocks"] == 0
    assert out["style_pass_skipped"] >= 1
    # no updateParagraphStyle/updateTextStyle batch was sent
    for reqs in docs.batches:
        assert not any("updateParagraphStyle" in r or "updateTextStyle" in r
                       for r in reqs)


def test_final_batch_4xx_is_deterministic_not_applied(engine, monkeypatch,
                                                      tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    docs.main_error = http_error(409)
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs)
    with pytest.raises(SystemExit) as exc:
        engine.sync_doc("doc1", md)
    assert exc.value.code == 1  # plain error, not partial-failure
    err = json.loads(capsys.readouterr().out)["error"]
    assert "nothing applied — atomic" in err
    assert docs.canary_text is None  # cleaned by _canary_note


def test_final_batch_503_not_applied_proven_by_canary(engine, monkeypatch,
                                                      tmp_path, capsys):
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    docs.main_error = http_error(503)  # applies=False: canary stays
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs)
    with pytest.raises(SystemExit) as exc:
        engine.sync_doc("doc1", md)
    assert exc.value.code == 1
    err = json.loads(capsys.readouterr().out)["error"]
    assert "canary intact" in err
    assert docs.canary_text is None  # cleaned after the proof


def test_final_batch_503_applied_recovers(engine, monkeypatch, tmp_path,
                                          capsys):
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    docs.main_error = http_error(503)
    docs.main_error_applies = True
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        html=b"<p>Alpha</p><p>Bravo edited</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert out["recovered_after_lost_response"] is True


def test_odd_insert_response_still_cleans_canary(engine, monkeypatch,
                                                 tmp_path, capsys):
    """A structurally unexpected SUCCESS response to the canary insert
    (codex code-r2 #2) must go through cleanup, not raw AttributeError."""
    doc = make_doc(BASE_TEXTS)
    docs = DocsStub(doc)
    real_batch = docs.batchUpdate

    def odd_insert(documentId=None, body=None):
        r = real_batch(documentId=documentId, body=body)
        first = body["requests"][0]
        if ("insertText" in first
                and "skrepka-canary" in first["insertText"]["text"]):
            return _Req({"writeControl": "not-a-dict"})
        return r
    docs.batchUpdate = odd_insert
    _, docs, _drive, md = _anchored_setup(engine, monkeypatch, tmp_path,
                                          docs=docs)
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    err = json.loads(capsys.readouterr().out)["error"]
    assert "unexpectedly" in err
    assert docs.canary_text is None  # cleaned despite the odd response


def test_odd_final_batch_response_recovers_positionally(engine, monkeypatch,
                                                        tmp_path, capsys):
    """A structurally odd SUCCESS response to the final batch (codex
    code-r3 #1) must fall into positional recovery, not AttributeError."""
    doc = make_doc(BASE_TEXTS)
    merged = make_doc(["Alpha", "Bravo edited", "Charlie"], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    real_batch = docs.batchUpdate

    def odd_main(documentId=None, body=None):
        r = real_batch(documentId=documentId, body=body)
        if docs.main_applied:
            return _Req({"writeControl": "not-a-dict"})
        return r
    docs.batchUpdate = odd_main
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, ANCHORED_PARAS, ANCHORED_COMMENTS),
        html=b"<p>Alpha</p><p>Bravo edited</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc, BASE_MD, LOCAL_EDIT)
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    assert out["recovered_after_lost_response"] is True
    assert out["styled_blocks"] == 0


def _make_doc_styled(specs, rev="R0"):
    """Like make_doc, but each paragraph carries its own named style — so a
    heading and a body paragraph can hold the SAME text."""
    content, idx = [], 1
    for text, style in specs:
        s, e = idx, idx + len(text) + 1
        content.append({"startIndex": s, "endIndex": e, "paragraph": {
            "elements": [{"startIndex": s, "endIndex": e,
                          "textRun": {"content": text + "\n",
                                      "textStyle": {}}}],
            "paragraphStyle": {"namedStyleType": style},
        }})
        idx = e
    return {"documentId": "doc1", "revisionId": rev,
            "body": {"content": content}}


def test_sync_refuses_to_rewrite_a_twin_of_an_anchored_paragraph(
        engine, monkeypatch, tmp_path, capsys):
    """Зеркальная половина на пути sync: правится СВОБОДНЫЙ двойник.

    Заголовок и абзац могут нести один текст. Раньше карта якорей видела
    текст дважды, не знала, на какой копии комментарий, и огораживала обе —
    правка заголовка отказывала, хотя комментарий был на абзаце. Теперь копия
    доказана местом, и свободный двойник переписывается. Отказ на самой
    прокомментированной копии проверяет следующий тест."""
    doc = _make_doc_styled([("Bravo", "HEADING_1"), ("Bravo", "NORMAL_TEXT"),
                            ("Charlie", "NORMAL_TEXT")])
    merged = _make_doc_styled([("Bravo edited", "HEADING_1"),
                               ("Bravo", "NORMAL_TEXT"),
                               ("Charlie", "NORMAL_TEXT")], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        # якорь на копии-АБЗАЦЕ; правка ниже трогает заголовок, и теперь это
        # разные адреса, а не два кандидата
        _docx_builder(docs, [("Bravo", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]),
        html=b"<h1>Bravo edited</h1><p>Bravo</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc,
                      "# Bravo\n\nBravo\n\nCharlie",
                      "# Bravo edited\n\nBravo\n\nCharlie")
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"


def test_sync_refuses_to_rewrite_the_anchored_copy_itself(
        engine, monkeypatch, tmp_path, capsys):
    """И зеркало: правка САМОЙ прокомментированной копии по-прежнему отказывает.

    Позиционная перезапись — `deleteContentRange` — делает якорь призраком,
    когда накрывает его целиком (C1, подтверждено замером M24-0). Знание о
    том, какая копия прокомментирована, эту опасность не отменяет, оно лишь
    сужает её до одной копии вместо обеих."""
    doc = _make_doc_styled([("Bravo", "HEADING_1"), ("Bravo", "NORMAL_TEXT"),
                            ("Charlie", "NORMAL_TEXT")])
    docs = DocsStub(doc)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Bravo", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]),
        html=b"<h1>Bravo</h1><p>Bravo</p><p>Charlie</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc,
                      "# Bravo\n\nBravo\n\nCharlie",
                      "# Bravo\n\nBravo edited\n\nCharlie")
    with pytest.raises(SystemExit):
        engine.sync_doc("doc1", md)
    assert _no_content_mutation(docs)


def test_sync_applies_away_from_the_copies_and_counts_them_honestly(
        engine, monkeypatch, tmp_path, capsys):
    """Расписка про двойников. С 0.17 копия, на которой стоит якорь,
    доказывается местом в выгрузке, поэтому неразмещённых якорей больше нет:
    и `ambiguous_anchors`, и ограда пусты, а документ сливается целиком.

    Счётчик остаётся в контракте и проверяется на документе, где сторонам
    верить нельзя, — тестом ниже."""
    doc = _make_doc_styled([("Bravo", "HEADING_1"), ("Bravo", "NORMAL_TEXT"),
                            ("Charlie", "NORMAL_TEXT")])
    merged = _make_doc_styled([("Bravo", "HEADING_1"), ("Bravo", "NORMAL_TEXT"),
                               ("Charlie edited", "NORMAL_TEXT")], rev="R2")
    docs = DocsStub(doc, merged_doc=merged)
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _docx_builder(docs, [("Bravo", []), ("Bravo", [("0", 0, 5)]),
                             ("Charlie", [])],
                      [("0", "A", CREATED_SEC)]),
        html=b"<h1>Bravo</h1><p>Bravo</p><p>Charlie edited</p>")
    wire(engine, monkeypatch, docs, drive)
    md = make_workdir(engine, tmp_path, doc,
                      "# Bravo\n\nBravo\n\nCharlie",
                      "# Bravo\n\nBravo\n\nCharlie edited")
    engine.sync_doc("doc1", md)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "synced"
    accounting = out["anchor_accounting"]
    assert accounting["ambiguous_anchors"] == 0
    assert accounting["blocked_anchors"] == 0


# ---------------------------------------------------------------------------
# r10 (#29): «стоял здесь» is a claim, and on repeated paragraphs a false one
# ---------------------------------------------------------------------------

def test_a_closed_thread_on_a_repeated_paragraph_is_not_claimed_to_be_here(
        engine):
    """`_locate_comment_in_tab` returns EVERY occurrence by contract, so a
    thread standing in the clean copy used to be reported as hit when the
    other copy was edited — and the person went to check a thread the edit
    never touched."""
    tab = make_doc(["Одинаковый абзац", "прочее", "Одинаковый абзац"])
    closed = [api_comment("c1", "A", CREATED, resolved=True)]
    closed[0]["quotedFileContent"] = {"value": "Одинаковый абзац"}
    # the edit covers the FIRST copy only
    suspects = engine._closed_threads_in_edited_ranges(tab, closed, [(1, 17)])
    assert suspects == {"c1": 2}


def test_a_closed_thread_matching_once_is_named_plainly(engine):
    tab = make_doc(["Единственный абзац", "прочее"])
    closed = [api_comment("c1", "A", CREATED, resolved=True)]
    closed[0]["quotedFileContent"] = {"value": "Единственный абзац"}
    assert engine._closed_threads_in_edited_ranges(
        tab, closed, [(1, 19)]) == {"c1": 1}


def test_the_archive_says_one_of_n_places_not_here(engine, tmp_path):
    tab = make_doc(["Одинаковый абзац", "прочее", "Одинаковый абзац"])
    closed = [api_comment("c1", "A", CREATED, resolved=True)]
    closed[0]["quotedFileContent"] = {"value": "Одинаковый абзац"}
    md = tmp_path / "doc.md"
    md.write_text("x", encoding="utf-8")
    path = engine._archive_closed_threads(str(md), "DOC", closed,
                                          doc_tab=tab, edited=[(1, 17)])
    entry = json.loads(open(path, encoding="utf-8").read())["threads"][0]
    assert entry["probably_in_an_edited_paragraph"] is True
    assert "встречается в документе 2 раз" in entry["where"]


def test_the_rewrite_refuses_a_tail_it_cannot_get_in_front_of(engine):
    """The batch inserts the new text immediately BEFORE the last character,
    and Google refuses an insert landing inside a grapheme cluster — measured
    2026-08-18 on a stressed vowel (#47). So the refusal is about the shape of
    the text, not about closed threads: it holds either way, and it holds for
    the composite shapes built the same way."""
    for tail, head in (("\u0301", "письмо"),          # буква со знаком ударения
                       ("\ufe0f", "телефон\u260e"),    # variation selector
                       ("\U0001f466", "семья\U0001f468\u200d")):  # склейка через ZWJ
        for closed in (True, False):
            kw = _rewrite_case(engine, text=head + tail,
                               new="Совсем иная фраза", closed_present=closed)
            assert engine._rewrite_anchor_requests(**kw) is None, (tail, closed)


def test_an_emoji_tail_no_longer_blocks_the_rewrite(engine):
    """M13 measured the worst case — a closed thread whose whole anchor is the
    single character this batch deletes — on a plain BMP letter, and 0.12 kept
    a guard because a surrogate pair was not covered. Measured 2026-08-18 on a
    live document: the batch runs to completion on a phrase ending in an emoji
    whose last character is covered by a closed thread, and the thread is still
    there afterwards. The guard is gone, so an emoji at the end of a commented
    phrase works whether or not the document has closed threads."""
    for closed in (True, False):
        kw = _rewrite_case(engine, text="Здесь старый текст\U0001f4a1",
                           new="Совсем иная фраза", closed_present=closed)
        assert engine._rewrite_anchor_requests(**kw) is not None, closed
    assert engine._rewrite_anchor_requests(**_rewrite_case(engine)) is not None


def test_the_refusal_names_the_real_reason_the_rewrite_did_not_fire(engine):
    """`_rewrite_anchor_requests` answers None to half a dozen questions, and
    the refusal used to give the same advice for all of them — «leave part of
    the original anchor text alone», which is sound for exactly one and
    misleading for the rest (#24 all over again)."""
    kw = _rewrite_case(engine)
    base = dict(doc_tab=kw["doc_tab"], search_text=kw["search_text"],
                new_text=kw["new_text"], start=kw["start"], end=kw["end"],
                anchors=kw["anchors"], attribution=kw["attribution"],
                named_intervals=[])

    toc = dict(base)
    toc["doc_tab"] = {"body": {"content": list(
        kw["doc_tab"]["body"]["content"]) + [{"tableOfContents": {}}]}}
    assert "оглавление" in engine._why_no_rewrite(**toc)

    named = dict(base)
    named["named_intervals"] = [(kw["start"], kw["end"], "метка mark1")]
    assert "метка mark1" in engine._why_no_rewrite(**named)

    crossing = dict(base)
    crossing["search_text"] = "первый\nвторой"
    assert "несколько абзацев" in engine._why_no_rewrite(**crossing)

    neighbour = dict(base)
    neighbour["anchors"] = list(kw["anchors"]) + [
        (kw["start"], kw["end"], "x", "9")]
    assert "ещё один комментарий" in engine._why_no_rewrite(**neighbour)

    # nothing structural in the way: the original sentence, which is the one
    # that is true then
    assert "ИСХОДНЫЙ символ якоря" in engine._why_no_rewrite(**base)


# ---------------------------------------------------------------------------
# r13: мягкий перенос через весь путь patch (#27, #40)
# ---------------------------------------------------------------------------

def _softbreak_export(docs_stub, paras, comments):
    """Сборщик выгрузки, пишущий `\\v` настоящим `w:br`.

    `make_docx_full` кладёт текст в `w:t`, а XML управляющих символов в
    содержимом не допускает вовсе — мягкий перенос там представим только
    тегом, ровно как его пишет Google (M20-1).
    """
    def build():
        p = list(paras)
        if docs_stub.canary_text is not None:
            p.append((docs_stub.canary_text, None))
        body = []
        for text, cid in p:
            runs = []
            if cid is not None:
                runs.append(f'<w:commentRangeStart w:id="{cid}"/>')
            for i, piece in enumerate(text.split("\v")):
                if i:
                    runs.append("<w:r><w:br/></w:r>")
                runs.append(
                    f'<w:r><w:t xml:space="preserve">{piece}</w:t></w:r>')
            if cid is not None:
                runs.append(f'<w:commentRangeEnd w:id="{cid}"/>')
                runs.append(f'<w:commentReference w:id="{cid}"/>')
            body.append(f"<w:p>{''.join(runs)}</w:p>")
        document = (f'<?xml version="1.0"?><w:document xmlns:w="{WORDML}">'
                    f"<w:body>{''.join(body)}</w:body></w:document>")
        centries = "".join(
            f'<w:comment w:id="{cid}" w:author="{author}" w:date="{date}">'
            f"<w:p><w:r><w:t>c</w:t></w:r></w:p></w:comment>"
            for cid, author, date in comments)
        comments_xml = (f'<?xml version="1.0"?><w:comments xmlns:w="{WORDML}">'
                        f"{centries}</w:comments>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", document)
            z.writestr("word/comments.xml", comments_xml)
        return buf.getvalue()
    return build


PREVIEW = "Пальто и ботинки\vОсенний ассортимент"


def test_patch_works_beside_a_commented_soft_break(engine, monkeypatch):
    """Живой инцидент 20 августа целиком: комментарий висит на абзаце превью,
    где стоит shift+enter, а правка нужна в СОСЕДНЕМ абзаце. До 0.15 таких
    замен отказывали все до единой — 50 абзацев, правки в 30, обхода нет."""
    docs = DocsStub(make_doc(["Alpha", PREVIEW, "Charlie"]),
                    merged_doc=make_doc(["Zulu", PREVIEW, "Charlie"]))
    drive = DriveStub(
        [api_comment("c1", "A", CREATED)],
        _softbreak_export(docs, [("Alpha", None), (PREVIEW, "0"),
                                 ("Charlie", None)],
                          [("0", "A", CREATED_SEC)]))
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    op = {"op": "replace_quote", "quote": "Alpha", "with": "Zulu"}
    engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    main = semantic_batch(docs)
    assert written_text(main) == "Zulu"


def test_patch_writes_a_soft_break_into_a_commented_paragraph(engine,
                                                              monkeypatch):
    """#40 через весь путь: перезапись прокомментированного фрагмента,
    которая ДОБАВЛЯЕТ мягкий перенос. Раньше запрет управляющих символов
    отправлял оператора ставить его руками через Docs API — мимо канарейки,
    отпечатка и всех проверок, — и там он резал пробел внутри якоря."""
    docs, drive = _covered_anchor_doc(engine, monkeypatch)
    op = {"op": "replace_quote", "quote": "Bravo", "with": "Заголовок\vНиз"}
    note = engine._apply_op_anchor_safe(docs, drive, "doc1", op, None)
    assert note["applied_as"] == "rewritten"
    main = semantic_batch(docs)
    assert main[1]["insertText"]["text"] == "Заголовок\vНиз"
    assert main[2]["replaceAllText"]["replaceText"] == "Заголовок\vНиз"


def _archived_replies(engine, tmp_path, first, second):
    md = tmp_path / "doc.md"
    md.write_text("x", encoding="utf-8")
    engine._archive_closed_threads(str(md), "doc1", [first])
    path = engine._archive_closed_threads(str(md), "doc1", [second])
    with open(path, encoding="utf-8") as f:
        return json.load(f)["threads"][0]["replies"]


def _archive_reply(author, created, content, deleted=False):
    return {"author": {"displayName": author}, "createdTime": created,
            "content": content, "deleted": deleted}


def test_closed_thread_archive_keeps_a_reply_deleted_between_runs(
        engine, tmp_path):
    """#28: the old thread entry is the deleted reply's only remaining copy.

    The fresh thread must update the conversation, not replace it whole.
    """
    first = api_comment("c1", "A", CREATED, resolved=True, replies=[
        _archive_reply("B", "2026-08-01T00:00:01Z", "первый"),
        _archive_reply("C", "2026-08-01T00:00:02Z", "второй"),
    ])
    second = api_comment("c1", "A", CREATED, resolved=True, replies=[
        _archive_reply("B", "2026-08-01T00:00:01Z", "первый",
                       deleted=True),
        _archive_reply("C", "2026-08-01T00:00:02Z", "второй"),
    ])

    replies = _archived_replies(engine, tmp_path, first, second)

    assert [r["content"] for r in replies] == ["первый", "второй"]


def test_closed_thread_archive_updates_a_known_reply_and_appends_a_new_one(
        engine, tmp_path):
    first = api_comment("c1", "A", CREATED, resolved=True, replies=[
        _archive_reply("B", "2026-08-01T00:00:01Z", "старое"),
    ])
    second = api_comment("c1", "A", CREATED, resolved=True, replies=[
        _archive_reply("B", "2026-08-01T00:00:01Z", "уточнённое"),
        _archive_reply("C", "2026-08-01T00:00:02Z", "новое"),
    ])

    replies = _archived_replies(engine, tmp_path, first, second)

    assert [(r["author"], r["created"], r["content"]) for r in replies] == [
        ("B", "2026-08-01T00:00:01Z", "уточнённое"),
        ("C", "2026-08-01T00:00:02Z", "новое"),
    ]


def test_closed_thread_reply_identity_needs_both_author_and_created(
        engine, tmp_path):
    old_specs = [
        ("A", "2026-08-01T00:00:01Z", "a1"),
        ("A", "2026-08-01T00:00:02Z", "a2"),
        ("B", "2026-08-01T00:00:01Z", "b1"),
    ]
    first = api_comment("c1", "A", CREATED, resolved=True, replies=[
        _archive_reply(author, created, content)
        for author, created, content in old_specs
    ])
    second = api_comment("c1", "A", CREATED, resolved=True, replies=[
        _archive_reply(author, created, content + "-fresh")
        for author, created, content in old_specs
    ])

    replies = _archived_replies(engine, tmp_path, first, second)

    assert [(r["author"], r["created"], r["content"]) for r in replies] == [
        (author, created, content + "-fresh")
        for author, created, content in old_specs
    ]


def test_closed_thread_archive_refuses_malformed_old_replies(
        engine, tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("x", encoding="utf-8")
    path = tmp_path / "doc.md.skrepka-closed-threads.json"
    original = json.dumps({
        "doc_id": "doc1",
        "threads": [{"id": "c1", "replies": "not-a-list"}],
    })
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        engine._archive_closed_threads(
            str(md), "doc1",
            [api_comment("c1", "A", CREATED, resolved=True)])

    assert "единственная копия" in str(exc.value)
    assert path.read_text(encoding="utf-8") == original
