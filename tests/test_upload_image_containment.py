"""Markdown image references must stay inside the .md file's own directory.

Markdown handed to `upload`/`update` is untrusted: it can be the verbatim
export of a document whose text third parties wrote. Before the containment
check, `![x](/etc/passwd)` made skrepka upload that file to Drive and grant it
`anyone:reader` for the duration of the insert — arbitrary local file read with
a public window, and into `--folder` when one was given.

Every refusal here must leave the reference as literal markdown (never upload
it), which is the same thing that happens for a file that does not exist.
"""

import os

import pytest


@pytest.fixture
def md_tree(tmp_path):
    """A .md next to an images dir, plus a secret file outside the tree."""
    work = tmp_path / "work"
    (work / "img").mkdir(parents=True)
    (work / "img" / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (work / "sibling.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    secret = tmp_path / "outside" / "secret.png"
    secret.parent.mkdir()
    secret.write_bytes(b"\x89PNG\r\n\x1a\nSENTINEL-OFF-TREE-FILE-MUST-NOT-UPLOAD")
    return work, secret


def test_relative_image_inside_tree_is_accepted(engine, md_tree):
    work, _ = md_tree
    assert engine._resolve_upload_image(str(work), "img/ok.png") == \
        os.path.realpath(work / "img" / "ok.png")
    assert engine._resolve_upload_image(str(work), "sibling.png") == \
        os.path.realpath(work / "sibling.png")


def test_absolute_path_is_refused(engine, md_tree):
    work, secret = md_tree
    assert engine._resolve_upload_image(str(work), str(secret)) is None
    assert engine._resolve_upload_image(str(work), "/etc/hosts") is None


def test_dotdot_traversal_is_refused(engine, md_tree):
    work, _ = md_tree
    assert engine._resolve_upload_image(str(work), "../outside/secret.png") is None
    assert engine._resolve_upload_image(str(work), "img/../../outside/secret.png") is None


def test_symlink_out_of_tree_is_refused(engine, md_tree):
    work, secret = md_tree
    link = work / "img" / "innocent.png"
    os.symlink(secret, link)
    assert link.exists()  # the old os.path.exists() check would have passed
    assert engine._resolve_upload_image(str(work), "img/innocent.png") is None


def test_non_image_extension_is_refused(engine, md_tree):
    work, _ = md_tree
    (work / "id_rsa").write_text("SENTINEL-NON-IMAGE-MUST-NOT-UPLOAD")
    assert engine._resolve_upload_image(str(work), "id_rsa") is None


def test_svg_is_refused_even_inside_the_tree(engine, md_tree):
    """SVG renderers resolve references inside the file, so an in-tree .svg is
    still an outbound-fetch and local-file-read channel. 0.9 refuses it."""
    work, _ = md_tree
    (work / "img" / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image xlink:href="file:///etc/passwd"/></svg>'
    )
    assert engine._resolve_upload_image(str(work), "img/logo.svg") is None
    assert ".svg" not in engine._UPLOAD_IMAGE_EXTS


def test_directory_and_missing_file_are_refused(engine, md_tree):
    work, _ = md_tree
    assert engine._resolve_upload_image(str(work), "img") is None
    assert engine._resolve_upload_image(str(work), "img/nope.png") is None


def test_remote_url_is_left_alone(engine, md_tree):
    work, _ = md_tree
    assert engine._resolve_upload_image(str(work), "https://evil.test/x.png") is None
    assert engine._resolve_upload_image(str(work), "file:///etc/hosts") is None


def test_prepare_md_uploads_only_contained_images(engine, md_tree):
    """End-to-end: only the in-tree image becomes a marker; the escapes stay text."""
    work, secret = md_tree
    md = work / "doc.md"
    md.write_text(
        f"# t\n\n"
        f"![ok](img/ok.png)\n\n"
        f"![abs]({secret})\n\n"
        f"![up](../outside/secret.png)\n\n"
        f"![key](id_rsa)\n"
    )
    (work / "id_rsa").write_text("SENTINEL-NON-IMAGE-MUST-NOT-UPLOAD")

    tmp_path_out, images = engine._prepare_md_for_upload(str(md))
    try:
        assert [i[2] for i in images] == ["img/ok.png"]
        assert [i[3] for i in images] == [os.path.realpath(work / "img" / "ok.png")]
        text = open(tmp_path_out).read()
        # the refused references survive verbatim, so nothing is silently dropped
        assert f"![abs]({secret})" in text
        assert "![up](../outside/secret.png)" in text
        assert "![key](id_rsa)" in text
        assert "img/ok.png" not in text  # replaced by the «IMG:…» marker
    finally:
        if tmp_path_out:
            os.unlink(tmp_path_out)
