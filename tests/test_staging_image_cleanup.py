"""A staging image must never survive a failure once its id is known.

`_upload_image_to_drive` creates a Drive file and then grants it
`anyone:reader` for the duration of the insert. Before this guard, cleanup ran
only for `HttpError`: a transport error after the ACL was actually created, a
malformed reply, or a KeyboardInterrupt all returned without ever handing the
caller a `staged_id`, so the outer `finally` had nothing to clean and the file
stayed in Drive, publicly readable.

The guarantee starts once `files.create` yields an id. A lost `files.create`
reply leaves a file whose id nobody knows — that one is reported, not silently
swallowed.
"""

import pytest
from googleapiclient.errors import HttpError


class _Req:
    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises

    def execute(self, *a, **kw):
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeDrive:
    """Records what was called so the test can assert the file was deleted."""

    def __init__(self, create_result, perm_outcome):
        self.create_result = create_result
        self.perm_outcome = perm_outcome
        self.deleted = []
        self.revoked = []

    def files(self):
        return self

    def permissions(self):
        return self

    def create(self, **kw):
        if "body" in kw and kw.get("body", {}).get("type") == "anyone":
            if isinstance(self.perm_outcome, BaseException):
                return _Req(raises=self.perm_outcome)
            return _Req(result=self.perm_outcome)
        return _Req(result=self.create_result)

    def delete(self, fileId=None, **kw):
        if kw.get("permissionId"):
            self.revoked.append(fileId)
        else:
            self.deleted.append(fileId)
        return _Req(result={})


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(p)


def _http_error():
    class _R:
        status, reason = 500, "boom"
    return HttpError(_R(), b"{}")


def test_transport_error_after_acl_deletes_the_file(engine, img):
    """A non-HttpError failure used to bypass cleanup entirely."""
    drive = _FakeDrive({"id": "F1"}, ConnectionResetError("lost"))
    with pytest.raises(ConnectionResetError):
        engine._upload_image_to_drive(drive, img)
    assert drive.deleted == ["F1"]


def test_keyboard_interrupt_deletes_the_file(engine, img):
    """Ctrl-C must not strand a world-readable file."""
    drive = _FakeDrive({"id": "F2"}, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        engine._upload_image_to_drive(drive, img)
    assert drive.deleted == ["F2"]


def test_http_error_still_deletes_the_file(engine, img):
    drive = _FakeDrive({"id": "F3"}, _http_error())
    with pytest.raises(HttpError):
        engine._upload_image_to_drive(drive, img)
    assert drive.deleted == ["F3"]


def test_permission_reply_without_id_deletes_the_file(engine, img):
    """The ACL may exist despite the malformed reply, so the file must go."""
    drive = _FakeDrive({"id": "F4"}, {})
    with pytest.raises(engine.PatchOpError):
        engine._upload_image_to_drive(drive, img)
    assert drive.deleted == ["F4"]


def test_create_reply_without_id_is_reported_not_swallowed(engine, img):
    """Nothing can clean up a file whose id we never learned — say so."""
    drive = _FakeDrive({}, {"id": "P"})
    with pytest.raises(engine.PatchOpError) as e:
        engine._upload_image_to_drive(drive, img)
    assert "no file id" in str(e.value)
    assert drive.deleted == []


def test_success_returns_ids_and_keeps_the_file(engine, img):
    drive = _FakeDrive({"id": "F6"}, {"id": "P6"})
    uri, fid, pid = engine._upload_image_to_drive(drive, img)
    assert (fid, pid) == ("F6", "P6")
    assert "F6" in uri
    assert drive.deleted == []
