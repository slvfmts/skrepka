"""#44: replying to a thread must not cross a Google Docs tab boundary.

Drive addresses a reply by document/comment id only.  Its comment ``anchor``
does not carry a tab id (measured in M19), so ``--tab`` is a safety assertion:
skrepka has to prove the thread belongs to that tab before it posts anything.
"""

import io
import json
import sys
import zipfile
import copy

import pytest


_DEFAULT = object()


class _Result:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


def _paragraph(text, start=1):
    end = start + len(text) + 1
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "elements": [{
                "startIndex": start,
                "endIndex": end,
                "textRun": {"content": text + "\n"},
            }],
        },
    }


def _tab(tab_id, title, text, children=()):
    tail = len(text) + 2
    tab = {
        "tabProperties": {"tabId": tab_id, "title": title},
        "documentTab": {"body": {"content": [
            _paragraph(text), _paragraph("", start=tail),
        ]}},
    }
    if children:
        tab["childTabs"] = list(children)
    return tab


def _multi_tab_doc(root_text="Текст черновика", child_text="Текст договора"):
    child = _tab("t-child", "Договор", child_text)
    return {
        "documentId": "doc1",
        "revisionId": "R0",
        "tabs": [_tab("t-root", "Черновик", root_text, [child])],
    }


def _docx(root_text, child_text, anchored_in, record_author="Редактор"):
    """The export is the evidence: unlike quotedFileContent, its marker has
    an exact position in the proven root/child segments."""
    wordml = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def head(title):
        return (f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>'
                f'<w:r><w:t>{title}</w:t></w:r></w:p>')

    def para(text, here):
        start = '<w:commentRangeStart w:id="7"/>' if here else ""
        end = ('<w:commentRangeEnd w:id="7"/>'
               '<w:r><w:commentReference w:id="7"/></w:r>' if here else "")
        return f'<w:p>{start}<w:r><w:t>{text}</w:t></w:r>{end}</w:p>'

    body = (head("Черновик") + para(root_text, anchored_in == "t-root")
            + "<w:p/>" + head("Договор")
            + para(child_text, anchored_in == "t-child") + "<w:p/>")
    document = (f'<?xml version="1.0"?><w:document xmlns:w="{wordml}">'
                f"<w:body>{body}</w:body></w:document>")
    comments = (f'<?xml version="1.0"?><w:comments xmlns:w="{wordml}">'
                f'<w:comment w:id="7" w:author="{record_author}" '
                'w:date="2026-08-25T10:00:00Z"><w:p><w:r>'
                '<w:t>Уточните формулировку</w:t></w:r></w:p></w:comment>'
                '</w:comments>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/comments.xml", comments)
    return out.getvalue()


def _comment(comment_id="c1", quote="Текст договора", author="Редактор",
             anchor="kix.thread-anchor"):
    return {
        "id": comment_id,
        "content": "Уточните формулировку",
        "author": {"displayName": author},
        "createdTime": "2026-08-25T10:00:00Z",
        # `anchor` is deliberately present: M19 proved that its kix id still
        # says nothing about the tab, so code must not treat it as evidence.
        "anchor": anchor,
        "quotedFileContent": {"value": quote} if quote is not None else {},
    }


class _Docs:
    def __init__(self, doc, events, final_revision=_DEFAULT):
        self.doc = doc
        self.events = events
        self.get_calls = []
        self.final_revision = final_revision
        self.canary_text = None

    def documents(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        self.events.append("document.get")
        if kwargs.get("fields") == "revisionId":
            revision = (self.doc.get("revisionId")
                        if self.final_revision is _DEFAULT
                        else self.final_revision)
            return _Result({"revisionId": revision})
        return _Result(copy.deepcopy(self.doc))

    def batchUpdate(self, documentId=None, body=None):
        del documentId
        for request in body.get("requests", []):
            insertion = request.get("insertText")
            if insertion and "skrepka-canary" in insertion.get("text", ""):
                self.canary_text = insertion["text"].lstrip("\n")
                tab_id = (insertion.get("location") or {}).get("tabId")
                for tid, doc_tab in self._tabs():
                    if tid == tab_id:
                        content = doc_tab.setdefault(
                            "body", {}).setdefault("content", [])
                        start = content[-1]["endIndex"] if content else 1
                        content.append(_paragraph(self.canary_text, start))
                        break
                self.doc["revisionId"] = "R1"
            deletion = request.get("deleteContentRange")
            if deletion and self.canary_text:
                for _tid, doc_tab in self._tabs():
                    content = (doc_tab.get("body", {}) or {}).get(
                        "content", [])
                    doc_tab["body"]["content"] = [
                        element for element in content
                        if self.canary_text not in json.dumps(
                            element, ensure_ascii=False)
                    ]
                self.canary_text = None
                self.doc["revisionId"] = "R2"
        return _Result({
            "writeControl": {"requiredRevisionId": self.doc.get(
                "revisionId")}})

    def _tabs(self):
        out = []

        def walk(tabs):
            for tab in tabs:
                props = tab.get("tabProperties", {})
                out.append((props.get("tabId"), tab["documentTab"]))
                walk(tab.get("childTabs", []))

        walk(self.doc.get("tabs", []))
        if not out:
            out.append((None, self.doc))
        return out


class _Drive:
    def __init__(self, comment, docx, events, docs, other_comments=(),
                 comment_snapshots=None, fresh_docx=None):
        self.comment = comment
        self.comments_seen = [comment, *other_comments]
        self.comment_snapshots = comment_snapshots
        self.docx = docx
        self.fresh_docx = fresh_docx
        self.docs = docs
        self.events = events
        self.comment_get_calls = []
        self.comment_list_calls = []
        self.reply_calls = []

    def comments(self):
        return self._Comments(self)

    def replies(self):
        return self._Replies(self)

    def files(self):
        return self._Files(self)

    class _Comments:
        def __init__(self, owner):
            self.owner = owner

        def get(self, **kwargs):
            self.owner.comment_get_calls.append(kwargs)
            self.owner.events.append("comment.get")
            return _Result(self.owner.comment)

        def list(self, **kwargs):
            self.owner.comment_list_calls.append(kwargs)
            self.owner.events.append("comments.list")
            snapshots = self.owner.comment_snapshots
            if snapshots:
                index = min(len(self.owner.comment_list_calls) - 1,
                            len(snapshots) - 1)
                comments = snapshots[index]
            else:
                comments = self.owner.comments_seen
            return _Result({"comments": comments})

    class _Files:
        def __init__(self, owner):
            self.owner = owner

        def export(self, **_kwargs):
            self.owner.events.append("files.export")
            data = (self.owner.fresh_docx
                    if self.owner.docs.canary_text
                    and self.owner.fresh_docx is not None
                    else self.owner.docx)
            canary = self.owner.docs.canary_text
            if not canary:
                return _Result(data)
            source = io.BytesIO(data)
            output = io.BytesIO()
            with zipfile.ZipFile(source) as before, zipfile.ZipFile(
                    output, "w") as after:
                for info in before.infolist():
                    payload = before.read(info.filename)
                    if info.filename == "word/document.xml":
                        xml = payload.decode()
                        para = (f'<w:p><w:r><w:t>{canary}</w:t></w:r>'
                                f'</w:p>')
                        xml = xml.replace("</w:body>", para + "</w:body>")
                        payload = xml.encode()
                    after.writestr(info, payload)
            return _Result(output.getvalue())

    class _Replies:
        def __init__(self, owner):
            self.owner = owner

        def create(self, **kwargs):
            self.owner.reply_calls.append(kwargs)
            self.owner.events.append("reply.create")
            return _Result({
                "id": "r1",
                "content": kwargs["body"]["content"],
                "author": {"displayName": "Я"},
                "createdTime": "2026-08-25T10:00:01Z",
            })


def _wire(engine, monkeypatch, doc, comment, docx=None, other_comments=(),
          final_revision=_DEFAULT, comment_snapshots=None, fresh_docx=None):
    events = []
    docs = _Docs(doc, events, final_revision=final_revision)
    drive = _Drive(comment, docx, events, docs,
                   other_comments=other_comments,
                   comment_snapshots=comment_snapshots,
                   fresh_docx=fresh_docx)
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_docs_service", lambda _creds: docs)
    monkeypatch.setattr(engine, "get_drive_service", lambda _creds: drive)
    return docs, drive, events


def _error(capsys):
    return json.loads(capsys.readouterr().out)["error"]


def test_reply_proves_a_child_tab_before_posting(engine, monkeypatch, capsys):
    """The target is a child and intentionally not the first/root tab."""
    repeated = "Одинаковый абзац"
    docs, drive, events = _wire(
        engine, monkeypatch,
        _multi_tab_doc(root_text=repeated, child_text=repeated),
        _comment(quote=repeated),
        _docx(repeated, repeated, anchored_in="t-child"))

    engine.reply_comment(
        "doc1", "c1", "Исправил формулировку", tab_id="t-child")

    assert docs.get_calls
    assert any(call.get("includeTabsContent") is True
               for call in docs.get_calls)
    assert drive.comment_get_calls or drive.comment_list_calls
    if drive.comment_get_calls:
        assert drive.comment_get_calls[0]["commentId"] == "c1"
        fields = drive.comment_get_calls[0]["fields"]
    else:
        fields = drive.comment_list_calls[0]["fields"]
    assert "author" in fields and "createdTime" in fields
    assert "files.export" in events
    assert len(drive.reply_calls) == 1
    assert events.index("reply.create") > events.index("document.get")
    comment_read = ("comment.get" if "comment.get" in events
                    else "comments.list")
    assert events.index("reply.create") > events.index(comment_read)
    out = json.loads(capsys.readouterr().out)
    assert out["comment_id"] == "c1"
    assert out["tab_id"] == "t-child"


def test_reply_refuses_a_thread_from_another_tab(
        engine, monkeypatch, capsys):
    repeated = "Одинаковый абзац"
    _docs, drive, _events = _wire(
        engine, monkeypatch,
        _multi_tab_doc(root_text=repeated, child_text=repeated),
        _comment("c-root", repeated),
        _docx(repeated, repeated, anchored_in="t-root"))

    with pytest.raises(SystemExit):
        engine.reply_comment(
            "doc1", "c-root", "Это не наш разговор", tab_id="t-child")

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    for fact in ("c-root", "t-root", "t-child"):
        assert fact in error
    assert "not sent" in error or "не отправ" in error


def test_reply_refuses_when_tab_identity_is_ambiguous(
        engine, monkeypatch, capsys):
    """A repeated stale quote is not permission to guess a conversation."""
    repeated = "Одинаковый абзац"
    _docs, drive, _events = _wire(
        engine, monkeypatch,
        _multi_tab_doc(root_text=repeated, child_text=repeated),
        _comment("c-twin", repeated),
        # There IS a valid child marker — but its author/time witness belongs
        # to another thread. Mere proximity or a matching stale quote must not
        # let c-twin borrow that thread's tab identity.
        _docx(repeated, repeated, anchored_in="t-child",
              record_author="Заказчик"),
        other_comments=[_comment("c-other", repeated, author="Заказчик")])

    with pytest.raises(SystemExit):
        engine.reply_comment(
            "doc1", "c-twin", "Ответ", tab_id="t-child")

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    assert "c-twin" in error
    assert "t-root" in error and "t-child" in error
    assert "ambiguous" in error or "неоднознач" in error or "не доказ" in error
    assert "google docs" in error  # safe remedy when attribution is unknown


def test_reply_requires_scope_on_a_multi_tab_document(
        engine, monkeypatch, capsys):
    _docs, drive, _events = _wire(
        engine, monkeypatch, _multi_tab_doc(), _comment(),
        _docx("Текст черновика", "Текст договора", anchored_in="t-child"))

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ")

    assert drive.reply_calls == []
    error = _error(capsys)
    assert "--tab" in error
    assert "t-root" in error and "t-child" in error


def test_reply_without_tab_keeps_legacy_single_tab_behavior(
        engine, monkeypatch, capsys):
    legacy = {
        "documentId": "doc1",
        "title": "Обычный документ",
        "body": {"content": [_paragraph("Текст договора")]},
    }
    docs, drive, _events = _wire(engine, monkeypatch, legacy, _comment())

    engine.reply_comment("doc1", "c1", "Ответ")

    assert len(drive.reply_calls) == 1
    assert len(docs.get_calls) == 1  # no irrelevant final revision read
    out = json.loads(capsys.readouterr().out)
    assert out["comment_id"] == "c1"
    assert out["tab_id"] is None


def test_document_level_comment_in_multi_tab_doc_stays_replyable(
        engine, monkeypatch, capsys):
    document_comment = _comment(quote=None, anchor=None)
    _docs, drive, events = _wire(
        engine, monkeypatch, _multi_tab_doc(), document_comment,
        comment_snapshots=[[document_comment], [document_comment]])

    engine.reply_comment("doc1", "c1", "Ответ всему документу")

    assert len(drive.reply_calls) == 1
    assert "files.export" not in events
    assert json.loads(capsys.readouterr().out)["tab_id"] is None


def test_document_level_reply_ignores_unrelated_doc_revision_change(
        engine, monkeypatch, capsys):
    document_comment = _comment(quote=None, anchor=None)
    docs, drive, _events = _wire(
        engine, monkeypatch, _multi_tab_doc(), document_comment,
        final_revision="R1",
        comment_snapshots=[[document_comment], [document_comment]])

    engine.reply_comment("doc1", "c1", "Ответ всему документу")

    assert len(drive.reply_calls) == 1
    assert len(docs.get_calls) == 1
    assert json.loads(capsys.readouterr().out)["tab_id"] is None


def test_document_level_comment_rejects_a_fake_tab_assertion(
        engine, monkeypatch, capsys):
    document_comment = _comment(quote=None, anchor=None)
    _docs, drive, _events = _wire(
        engine, monkeypatch, _multi_tab_doc(), document_comment)

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ", tab_id="t-child")

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    assert "document-level" in error and "no tab" in error


@pytest.mark.parametrize("bad_ids", [("dup", "dup"), ("t-root", None)])
def test_reply_refuses_missing_or_duplicate_tab_ids(
        engine, monkeypatch, capsys, bad_ids):
    doc = _multi_tab_doc()
    doc["tabs"][0]["tabProperties"]["tabId"] = bad_ids[0]
    doc["tabs"][0]["childTabs"][0]["tabProperties"]["tabId"] = bad_ids[1]
    _docs, drive, _events = _wire(engine, monkeypatch, doc, _comment())

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ", tab_id=bad_ids[0])

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    assert ("duplicate" if bad_ids[1] else "missing") in error


def test_reply_refuses_when_doc_revision_changes_after_export(
        engine, monkeypatch, capsys):
    repeated = "Одинаковый абзац"
    _docs, drive, _events = _wire(
        engine, monkeypatch,
        _multi_tab_doc(root_text=repeated, child_text=repeated),
        _comment(quote=repeated),
        _docx(repeated, repeated, anchored_in="t-child"),
        final_revision="R9")

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ", tab_id="t-child")

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    assert "doc changed" in error or "document changed" in error
    assert "reply not sent" in error


def test_canary_rejects_a_stale_export_with_the_old_tab_marker(
        engine, monkeypatch, capsys):
    repeated = "Одинаковый абзац"
    docs, drive, _events = _wire(
        engine, monkeypatch,
        _multi_tab_doc(root_text=repeated, child_text=repeated),
        _comment(quote=repeated),
        # The first export is stale and would authorize the wrong child tab.
        _docx(repeated, repeated, anchored_in="t-child"),
        # An export containing the canary is necessarily fresh and shows the
        # marker's current root-tab home.
        fresh_docx=_docx(repeated, repeated, anchored_in="t-root"))

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ", tab_id="t-child")

    assert drive.reply_calls == []
    assert docs.canary_text is None
    error = _error(capsys).lower()
    assert "fresh export" in error
    assert "t-child" in error
    assert "reply not sent" in error


def test_anchored_multi_tab_reply_requires_an_initial_revision(
        engine, monkeypatch, capsys):
    repeated = "Одинаковый абзац"
    doc = _multi_tab_doc(root_text=repeated, child_text=repeated)
    del doc["revisionId"]
    _docs, drive, _events = _wire(
        engine, monkeypatch, doc, _comment(quote=repeated),
        _docx(repeated, repeated, anchored_in="t-child"),
        final_revision="R0")

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ", tab_id="t-child")

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    assert "revision" in error and "reply not sent" in error


def test_reply_refuses_when_full_comment_snapshot_changes(
        engine, monkeypatch, capsys):
    repeated = "Одинаковый абзац"
    before = _comment(quote=repeated)
    after = dict(before, content="Комментарий отредактирован параллельно")
    _docs, drive, _events = _wire(
        engine, monkeypatch,
        _multi_tab_doc(root_text=repeated, child_text=repeated), before,
        _docx(repeated, repeated, anchored_in="t-child"),
        comment_snapshots=[[before], [after]])

    with pytest.raises(SystemExit):
        engine.reply_comment("doc1", "c1", "Ответ", tab_id="t-child")

    assert drive.reply_calls == []
    error = _error(capsys).lower()
    assert "comment c1 changed" in error and "fresh thread" in error


def test_cli_threads_tab_scope_into_reply(engine, monkeypatch, capsys):
    captured = {}

    def _reply(file_id, comment_id, text, resolve=False, yes=False,
               tab_id=None):
        captured.update(file_id=file_id, comment_id=comment_id, text=text,
                        resolve=resolve, yes=yes, tab_id=tab_id)

    monkeypatch.setattr(engine, "reply_comment", _reply)
    monkeypatch.setattr(
        sys, "argv",
        ["skrepka", "reply", "doc1", "c1", "Ответ", "--tab", "t-child"],
    )

    engine.main()

    assert captured == {
        "file_id": "doc1",
        "comment_id": "c1",
        "text": "Ответ",
        "resolve": False,
        "yes": False,
        "tab_id": "t-child",
    }
    capsys.readouterr()


def test_resolve_helper_preserves_tab_scope(engine, monkeypatch):
    captured = {}

    def _reply(file_id, comment_id, text, resolve=False, yes=False,
               tab_id=None):
        captured.update(file_id=file_id, comment_id=comment_id, text=text,
                        resolve=resolve, yes=yes, tab_id=tab_id)

    monkeypatch.setattr(engine, "reply_comment", _reply)
    engine.resolve_comment("doc1", "c1", text="Готово", yes=True,
                           tab_id="t-child")

    assert captured == {
        "file_id": "doc1",
        "comment_id": "c1",
        "text": "Готово",
        "resolve": True,
        "yes": True,
        "tab_id": "t-child",
    }


def test_cli_threads_tab_scope_into_resolve(engine, monkeypatch, capsys):
    captured = {}

    def _resolve(file_id, comment_id, text=None, yes=False, tab_id=None):
        captured.update(file_id=file_id, comment_id=comment_id, text=text,
                        yes=yes, tab_id=tab_id)

    monkeypatch.setattr(engine, "resolve_comment", _resolve)
    monkeypatch.setattr(
        sys, "argv",
        ["skrepka", "resolve", "doc1", "c1", "--text", "Готово",
         "--yes", "--tab", "t-child"],
    )

    engine.main()

    assert captured == {
        "file_id": "doc1",
        "comment_id": "c1",
        "text": "Готово",
        "yes": True,
        "tab_id": "t-child",
    }
    capsys.readouterr()
