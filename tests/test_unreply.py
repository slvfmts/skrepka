"""Safety contract for deleting one surplus reply (#18)."""

import json
import sys

import pytest


def _reply(reply_id="r-own", *, me=True, deleted=False, action=None):
    row = {
        "id": reply_id,
        "content": "Лишний ответ",
        "author": {"displayName": "Current user"},
        "createdTime": "2026-08-25T10:00:00.000Z",
    }
    if me is not ...:
        row["author"]["me"] = me
    if deleted:
        row["deleted"] = True
    if action is not None:
        row["action"] = action
    return row


def _comment(comment_id="c-target", replies=(), *, deleted=False,
             resolved=False):
    row = {
        "id": comment_id,
        "content": "Parent",
        "author": {"displayName": "Reviewer", "me": False},
        "createdTime": "2026-08-25T09:00:00.000Z",
        "replies": list(replies),
    }
    if deleted:
        row["deleted"] = True
    if resolved:
        row["resolved"] = True
    return row


class _Request:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error is not None:
            raise self.error
        return self.result


class _CommentsResource:
    def __init__(self, drive):
        self.drive = drive

    def list(self, **kwargs):
        self.drive.list_calls.append(kwargs)
        if self.drive.list_error is not None:
            return _Request(error=self.drive.list_error)
        token = kwargs.get("pageToken")
        return _Request(result=self.drive.pages[token])

    def delete(self, **_kwargs):
        raise AssertionError("comments.delete must never be called by unreply")


class _RepliesResource:
    def __init__(self, drive):
        self.drive = drive

    def delete(self, **kwargs):
        self.drive.delete_calls.append(kwargs)
        return _Request(result={}, error=self.drive.delete_error)

    def create(self, **_kwargs):
        raise AssertionError("replies.create must never be called by unreply")


class Drive:
    def __init__(self, comments, *, paginated=False, list_error=None,
                 delete_error=None):
        rows = list(comments)
        if paginated:
            midpoint = max(1, len(rows) // 2)
            self.pages = {
                None: {"comments": rows[:midpoint], "nextPageToken": "p2"},
                "p2": {"comments": rows[midpoint:]},
            }
        else:
            self.pages = {None: {"comments": rows}}
        self.list_error = list_error
        self.delete_error = delete_error
        self.list_calls = []
        self.delete_calls = []
        self._comments = _CommentsResource(self)
        self._replies = _RepliesResource(self)

    def comments(self):
        return self._comments

    def replies(self):
        return self._replies


def _wire(engine, monkeypatch, drive):
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda _creds: drive)

    def no_docs(_creds):
        raise AssertionError("unreply must not construct or call a Docs service")

    monkeypatch.setattr(engine, "get_docs_service", no_docs)


def _refused(engine, monkeypatch, capsys, comments, *, comment_id="c-target",
             reply_id="r-own", drive=None):
    drive = drive or Drive(comments)
    _wire(engine, monkeypatch, drive)
    with pytest.raises(SystemExit) as exc:
        engine.unreply_comment("doc1", comment_id, reply_id)
    assert exc.value.code == 1
    receipt = json.loads(capsys.readouterr().out)
    assert drive.delete_calls == []
    return receipt["error"], drive


def test_unreply_uses_one_paginated_include_deleted_census_then_exact_delete(
        engine, monkeypatch, capsys):
    # Put the target on page two so stopping after the first page cannot pass.
    drive = Drive([
        _comment("c-other", [_reply("r-other", me=False)]),
        _comment(replies=[_reply()]),
    ], paginated=True)
    _wire(engine, monkeypatch, drive)

    engine.unreply_comment(
        "https://docs.google.com/document/d/doc1/edit", "c-target", "r-own")

    assert [call["pageToken"] for call in drive.list_calls] == [None, "p2"]
    assert all(call["includeDeleted"] is True for call in drive.list_calls)
    assert all(call["fields"] == engine._COMMENT_FIELDS
               for call in drive.list_calls)
    assert drive.delete_calls == [{
        "fileId": "doc1",
        "commentId": "c-target",
        "replyId": "r-own",
    }]
    assert json.loads(capsys.readouterr().out) == {
        "action": "reply-deleted",
        "deleted": True,
        "doc_id": "doc1",
        "comment_id": "c-target",
        "reply_id": "r-own",
    }


def test_unreply_refuses_a_reply_owned_by_another_parent(
        engine, monkeypatch, capsys):
    error, _drive = _refused(engine, monkeypatch, capsys, [
        _comment(replies=[]),
        _comment("c-other", [_reply()]),
    ])
    assert "belongs to comment c-other" in error


@pytest.mark.parametrize("parent_state", ["deleted", "resolved"])
def test_unreply_requires_a_live_open_parent(
        engine, monkeypatch, capsys, parent_state):
    kwargs = {parent_state: True}
    error, _drive = _refused(
        engine, monkeypatch, capsys,
        [_comment(replies=[_reply()], **kwargs)])
    assert parent_state in error


def test_unreply_refuses_an_already_deleted_reply(
        engine, monkeypatch, capsys):
    error, _drive = _refused(
        engine, monkeypatch, capsys,
        [_comment(replies=[_reply(deleted=True)])])
    assert "already deleted" in error


@pytest.mark.parametrize("me", [False, ...], ids=["not-me", "missing-me"])
def test_unreply_requires_explicit_true_author_me(
        engine, monkeypatch, capsys, me):
    error, _drive = _refused(
        engine, monkeypatch, capsys,
        [_comment(replies=[_reply(me=me)])])
    assert "author.me must be true" in error


@pytest.mark.parametrize("action", ["resolve", "reopen"])
def test_unreply_never_deletes_a_thread_action(
        engine, monkeypatch, capsys, action):
    error, _drive = _refused(
        engine, monkeypatch, capsys,
        [_comment(replies=[_reply(action=action)])])
    assert "not an ordinary reply" in error


def test_unreply_refuses_missing_and_duplicate_exact_ids(
        engine, monkeypatch, capsys):
    error, _drive = _refused(
        engine, monkeypatch, capsys,
        [_comment(replies=[_reply("different")])])
    assert "was not found" in error

    error, _drive = _refused(engine, monkeypatch, capsys, [
        _comment(replies=[_reply()]),
        _comment("c-other", [_reply()]),
    ])
    assert "ambiguous" in error


@pytest.mark.parametrize("phase", ["census", "delete"])
def test_unreply_api_failures_never_claim_success(
        engine, monkeypatch, capsys, phase):
    kwargs = ({"list_error": RuntimeError("list broke")} if phase == "census"
              else {"delete_error": RuntimeError("delete broke")})
    drive = Drive([_comment(replies=[_reply()])], **kwargs)
    _wire(engine, monkeypatch, drive)

    with pytest.raises(SystemExit):
        engine.unreply_comment("doc1", "c-target", "r-own")

    error = json.loads(capsys.readouterr().out)["error"]
    assert "broke" in error
    if phase == "census":
        assert drive.delete_calls == []
    else:
        assert drive.delete_calls == [{
            "fileId": "doc1", "commentId": "c-target", "replyId": "r-own"}]
        assert "may still be present" in error


def test_unreply_cli_dispatches_all_three_exact_ids(
        engine, monkeypatch, capsys):
    seen = {}

    def unreply(file_id, comment_id, reply_id):
        seen.update(file_id=file_id, comment_id=comment_id, reply_id=reply_id)

    monkeypatch.setattr(engine, "unreply_comment", unreply)
    monkeypatch.setattr(
        sys, "argv", ["skrepka", "unreply", "doc1", "c1", "r1"])

    engine.main()

    assert seen == {"file_id": "doc1", "comment_id": "c1", "reply_id": "r1"}
    capsys.readouterr()


def test_anchor_map_remedy_names_unreply_before_ui_and_never_resolve(engine):
    remedy = engine._anchor_map_remedy(
        "comment c1 shares every (author, second) key with another thread")

    assert "skrepka unreply" in remedy
    assert remedy.index("skrepka unreply") < remedy.index("Google Docs")
    assert "resolve" not in remedy.lower()
    assert "переоткры" not in remedy.lower()
