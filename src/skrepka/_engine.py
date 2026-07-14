#!/usr/bin/env python3
"""Google Docs toolkit: upload, download, update, comments, suggestions."""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

CONFIG_DIR = os.path.expanduser("~/.config/gdocs-uploader")
CREDENTIALS_FILE = os.path.join(CONFIG_DIR, "credentials.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token.json")
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

# Light indigo background for :::highlight blocks (#EEF2FF)
HIGHLIGHT_COLOR = {"red": 0.933, "green": 0.945, "blue": 1.0}
HIGHLIGHT_PADDING = {"magnitude": 6, "unit": "PT"}

# --- Characterization gates (empirical Docs API behavior; see docs/FINDINGS.md) ---
# Docs with anchored UI comments: whether a given operation class is verified
# to keep comment anchors alive AND text intact.
#   None  = unverified -> fail closed (operation class is blocked on such docs)
#   True  = verified safe in live UI check
#   False = verified UNSAFE (block stays permanently)
# C1: replaceAllText whose match fully covers a comment anchor keeps it visible.
# VERIFIED 2026-07-13: FALSE — full coverage ghosts the comment (partial
# overlap keeps it alive, but anchor positions are unknowable via the GA API,
# so replaces on anchored-comment docs stay blocked until export-based anchor
# mapping ships — see FINDINGS.md "docx anchor spans").
C1_FULL_ANCHOR_REPLACE_SAFE = False
# C5: insertText at/near a comment anchor boundary keeps anchor and text intact.
# VERIFIED 2026-07-13: TRUE — inserts at both anchor boundaries left the
# anchor alive and the text intact (live UI comment + export detector).
C5_INSERT_NEAR_ANCHOR_SAFE = True

# textStyle fields compared for the mixed-style preflight (C2: replaceAllText
# flattens replacement to the style of the first matched char, so a match
# spanning differently-styled runs must be refused).
_STYLE_FIELDS = (
    "bold", "italic", "underline", "strikethrough", "smallCaps",
    "baselineOffset", "fontSize", "weightedFontFamily",
    "foregroundColor", "backgroundColor", "link",
)


class PatchOpError(Exception):
    """Per-op failure inside the anchor-safe patch loop.

    Raised instead of exiting so the caller can always emit an honest
    partial-application report (codex code review #2). `state` describes
    what is known about the document after the failure:
    "not_applied" — the write definitely did not land;
    "unknown"     — the write (or part of it) may have landed.
    """

    def __init__(self, msg, state="not_applied"):
        super().__init__(msg)
        self.state = state


_RAISE_ERRORS = False  # per-op mode: _error raises PatchOpError instead of exiting


def _error(msg):
    """Print JSON error to stdout and exit (or raise in per-op mode)."""
    if _RAISE_ERRORS:
        raise PatchOpError(msg)
    print(json.dumps({"error": msg}))
    sys.exit(1)


def _warn(msg):
    """Print JSON warning to stderr (non-fatal)."""
    print(json.dumps({"warning": msg}), file=sys.stderr)


def get_creds():
    """Authenticate and return credentials."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                _error(f"credentials not found at {CREDENTIALS_FILE}")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(creds.to_json())
    return creds


def get_drive_service(creds):
    return build("drive", "v3", credentials=creds)


def get_docs_service(creds):
    return build("docs", "v1", credentials=creds)


def _extract_text_runs(content):
    """Walk document content and yield (start_index, end_index, text) for each text run."""
    for element in content:
        if "paragraph" in element:
            for elem in element["paragraph"].get("elements", []):
                tr = elem.get("textRun")
                if tr:
                    yield elem["startIndex"], elem["endIndex"], tr["content"]
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from _extract_text_runs(cell.get("content", []))


# ---------------------------------------------------------------------------
# Safe read / tab handling / text search
# ---------------------------------------------------------------------------

def _safe_get_doc(docs_service, doc_id):
    """Read a document with tabs, in the only view whose indices are valid
    for a subsequent batchUpdate.

    Per Google docs, only SUGGESTIONS_INLINE (the physical document state)
    yields indices that match batchUpdate's coordinate space. The previous
    use of PREVIEW_WITHOUT_SUGGESTIONS was itself an index-drift bug when
    pending suggestions existed (codex review r1 #1).
    """
    return docs_service.documents().get(
        documentId=doc_id,
        includeTabsContent=True,
        suggestionsViewMode="SUGGESTIONS_INLINE",
    ).execute()


def _scan_suggestions(node):
    """Deep-scan a Document resource for any pending suggestion markers.

    Returns the first `suggested*` key found with a non-empty value
    (covers insertions, deletions, textStyle/paragraphStyle/bullet changes,
    structural suggestions), or None. v1 policy: any pending suggestion in
    the doc blocks patch/sync entirely — no interval math around suggestions.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("suggested") and v:
                return k
            found = _scan_suggestions(v)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _scan_suggestions(item)
            if found:
                return found
    return None


def _census_comments(drive_service, file_id):
    """List ALL comments (incl. resolved) with pagination; fail closed.

    Returns (all_comments, anchored_comments). A comment counts as anchored
    when it carries quotedFileContent or an anchor field. quotedFileContent
    is a stale snapshot — it is NEVER used to locate anchors, only to decide
    whether the doc contains anchored comments at all.
    """
    out = []
    page_token = None
    try:
        while True:
            resp = drive_service.comments().list(
                fileId=file_id,
                fields="nextPageToken,comments(id,content,author/displayName,"
                       "quotedFileContent,resolved,deleted,anchor)",
                includeDeleted=False,
                pageSize=100,
                pageToken=page_token,
            ).execute()
            out.extend(resp.get("comments", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        _error(
            f"cannot list comments (fail closed, no writes performed): "
            f"{e.reason if hasattr(e, 'reason') else e}"
        )
    anchored = [
        c for c in out
        if not c.get("deleted") and (c.get("quotedFileContent") or c.get("anchor"))
    ]
    return out, anchored


def _extract_runs_full(content):
    """Like _extract_text_runs but yields the full textRun dict as well."""
    for element in content:
        if "paragraph" in element:
            for elem in element["paragraph"].get("elements", []):
                tr = elem.get("textRun")
                if tr:
                    yield elem["startIndex"], elem["endIndex"], tr
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from _extract_runs_full(cell.get("content", []))


def _extract_exact_text_range(doc_tab, start, end):
    """Return the text covering EXACTLY [start, end), or None when the range
    is not contiguously covered by text runs.

    A gap (inline object, structural element, table boundary) inside the
    range makes naive concatenation dangerous: the glued string could match
    unrelated text elsewhere and replaceAllText would rewrite that instead
    (codex code review #1). Fail closed on any gap.
    """
    body = doc_tab.get("body", {}) or {}
    pos = start
    parts = []
    for s, e, tr in _extract_runs_full(body.get("content", [])):
        if e <= start or s >= end:
            continue
        if s > pos:
            return None  # gap inside the range
        text = tr.get("content", "")
        prefix_units = max(0, pos - s)
        take_units = min(e, end) - max(s, pos)
        acc, skipped = [], 0
        for ch in text:
            w = 2 if ord(ch) > 0xFFFF else 1
            if skipped < prefix_units:
                skipped += w
                continue
            if take_units <= 0:
                break
            acc.append(ch)
            take_units -= w
        parts.append("".join(acc))
        pos = min(e, end)
        if pos >= end:
            break
    if pos != end:
        return None
    return "".join(parts)


def _match_style_signature(doc_tab, start, end):
    """Return (uniform: bool, fields_that_differ: list) for text in [start, end).

    Compares _STYLE_FIELDS across every textRun intersecting the range.
    Suggested-insertion runs are ignored (callers refuse suggestions anyway).
    """
    body = doc_tab.get("body", {}) or {}
    sigs = []
    for s, e, tr in _extract_runs_full(body.get("content", [])):
        if s < end and e > start and not tr.get("suggestedInsertionIds"):
            ts = tr.get("textStyle", {}) or {}
            sigs.append({f: json.dumps(ts.get(f), sort_keys=True) for f in _STYLE_FIELDS})
    if len(sigs) <= 1:
        return True, []
    first = sigs[0]
    differing = sorted({
        f for sig in sigs[1:] for f in _STYLE_FIELDS if sig[f] != first[f]
    })
    return not differing, differing


def _collect_tabs(doc):
    """Return a flat list of (tab_id, title, documentTab) for all tabs (including nested).

    Falls back to legacy single-tab docs by returning a synthetic entry with
    tab_id=None and documentTab=doc (so body/namedRanges lookups still work).
    """
    tabs = doc.get("tabs") or []
    out = []

    def _walk(tab_list):
        for t in tab_list:
            props = t.get("tabProperties", {}) or {}
            tab_id = props.get("tabId")
            title = props.get("title", "")
            doc_tab = t.get("documentTab", {}) or {}
            out.append((tab_id, title, doc_tab))
            # Tabs can nest
            child = t.get("childTabs")
            if child:
                _walk(child)

    if tabs:
        _walk(tabs)
    else:
        # Legacy: no tabs field at all. Synthesize one.
        out.append((None, doc.get("title", ""), doc))
    return out


def _select_tab(doc, tab_id=None):
    """Pick the target tab. Error out if ambiguous.

    Returns (tab_id, documentTab_dict). documentTab_dict has .body, .namedRanges, etc.
    """
    tabs = _collect_tabs(doc)
    if tab_id is not None:
        for tid, _title, dt in tabs:
            if tid == tab_id:
                return tid, dt
        available = ", ".join(
            f"{tid}:{title!r}" for tid, title, _ in tabs if tid
        ) or "(none)"
        _error(f"tab not found: {tab_id}. Available tabs: {available}")
    if len(tabs) == 1:
        return tabs[0][0], tabs[0][2]
    listing = "; ".join(
        f"{tid}={title!r}" for tid, title, _ in tabs
    )
    _error(
        f"document has {len(tabs)} tabs; pass --tab <tabId>. Tabs: {listing}"
    )


def _extract_text_from_doctab(doc_tab):
    """Yield (start, end, text) tuples from a documentTab's body."""
    body = doc_tab.get("body", {}) or {}
    yield from _extract_text_runs(body.get("content", []))


def _find_quote_in_doctab(doc_tab, quote, occurrence=1):
    """Find the Nth (1-based) occurrence of `quote` within a tab's text.

    Returns (start_index, end_index) absolute indices in the Docs coordinate
    system, or None if not found. Matches are computed against a concatenated
    text buffer keyed by real indices — so the quote may span multiple text
    runs but not structural boundaries (paragraph breaks are represented by
    '\\n' in textRun content, which is fine).
    """
    runs = list(_extract_text_from_doctab(doc_tab))
    if not runs:
        return None

    # Build a flat buffer and index map: position-in-buffer -> doc index.
    # Google Docs indices are UTF-16 code units. Non-BMP chars (e.g. 💡)
    # are 1 Python code point but 2 UTF-16 units (surrogate pair).
    # We track the doc index offset per code point to stay aligned.
    # A '\x00' sentinel is inserted wherever consecutive runs are not
    # contiguous in the index space (table cell boundaries, structural
    # elements) so a quote can never falsely match across them.
    buf_parts = []
    index_map = []  # parallel array: index_map[i] = doc index for buf char i
    last_end = None
    for start, end, text in runs:
        if last_end is not None and start != last_end:
            buf_parts.append("\x00")
            index_map.append(-1)
        last_end = end
        doc_offset = 0
        for ch in text:
            buf_parts.append(ch)
            index_map.append(start + doc_offset)
            # Advance by UTF-16 code unit count for this char
            doc_offset += 2 if ord(ch) > 0xFFFF else 1
    buf = "".join(buf_parts)

    if not quote or "\x00" in quote:
        return None  # NUL is the internal boundary sentinel — never matchable

    found_count = 0
    pos = 0
    while True:
        idx = buf.find(quote, pos)
        if idx == -1:
            return None
        found_count += 1
        if found_count == occurrence:
            start_doc = index_map[idx]
            last_char = quote[-1]
            last_char_utf16_len = 2 if ord(last_char) > 0xFFFF else 1
            end_doc = index_map[idx + len(quote) - 1] + last_char_utf16_len
            return start_doc, end_doc
        pos = idx + 1


def _count_quote_occurrences(doc_tab, quote):
    runs = list(_extract_text_from_doctab(doc_tab))
    parts, last_end = [], None
    for start, end, text in runs:
        if last_end is not None and start != last_end:
            parts.append("\x00")  # same cross-boundary sentinel as _find_quote
        last_end = end
        parts.append(text)
    buf = "".join(parts)
    if not quote or "\x00" in quote:
        return 0  # NUL is the internal boundary sentinel — never matchable
    count = 0
    pos = 0
    while True:
        idx = buf.find(quote, pos)
        if idx == -1:
            return count
        count += 1
        pos = idx + 1


def _write_control(revision_id):
    """Build writeControl block for batchUpdate with required revision pinning."""
    return {"requiredRevisionId": revision_id}


def post_process_highlights(docs_service, doc_id):
    """Find :::highlight / ::: markers and apply background shading.

    Only marker lines that contain NOTHING but the marker are deleted
    (exact-line semantics — a marker sharing a paragraph with other text is
    skipped with a warning instead of destroying the neighbors). Writes are
    pinned to the read revision.
    """
    try:
        doc = docs_service.documents().get(
            documentId=doc_id, suggestionsViewMode="SUGGESTIONS_INLINE"
        ).execute()
    except HttpError as e:
        _warn(f"Could not read doc for highlight processing: {e}")
        return

    revision_id = doc.get("revisionId")
    content = doc.get("body", {}).get("content", [])

    # Scan PARAGRAPHS for :::highlight and ::: markers (pure marker lines
    # only; aggregation over the paragraph's runs makes split-run markers
    # work — codex code review #6)
    marker_runs = []
    para_texts = {}
    for element in content:
        para = element.get("paragraph")
        if not para:
            continue
        elements = para.get("elements", [])
        # A paragraph is a deletable marker line ONLY if every element is a
        # plain text run — an inline image/equation/rich link alongside the
        # marker must never be deleted with it (codex code review r2 #3).
        text_only = all("textRun" in e for e in elements)
        text = "".join(
            e.get("textRun", {}).get("content", "") for e in elements)
        start_idx, end_idx = element["startIndex"], element["endIndex"]
        para_texts[(start_idx, end_idx)] = text
        stripped = text.strip()
        if stripped == ":::highlight" and text_only:
            marker_runs.append((start_idx, end_idx, "open"))
        elif ":::highlight" in text:
            _warn(f"skipping :::highlight marker not on a pure text line: {stripped[:60]!r}")
        elif stripped == ":::":
            if text_only:
                marker_runs.append((start_idx, end_idx, "close"))
            else:
                # Unsafe closer: warn AND poison pairing so an earlier open
                # cannot pair past it to a later pure ::: (which would
                # highlight an unintended span).
                _warn(f"skipping ::: close marker not on a pure text line "
                      f"at index {start_idx}")
                marker_runs.append((start_idx, end_idx, "unsafe_close"))

    # Pair open/close markers
    highlight_blocks = []
    i = 0
    while i < len(marker_runs):
        if marker_runs[i][2] == "open":
            for j in range(i + 1, len(marker_runs)):
                if marker_runs[j][2] == "unsafe_close":
                    # Poisoned closer: do NOT pair past it — leave this
                    # open marker (and the unsafe closer) untouched.
                    _warn("open :::highlight left unpaired due to an "
                          "unsafe ::: closer — markers left in the doc")
                    i = j + 1
                    break
                if marker_runs[j][2] == "close":
                    highlight_blocks.append({
                        "open_start": marker_runs[i][0],
                        "open_end": marker_runs[i][1],
                        "close_start": marker_runs[j][0],
                        "close_end": marker_runs[j][1],
                        "content_start": marker_runs[i][1],
                        "content_end": marker_runs[j][0],
                    })
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1

    if not highlight_blocks:
        return

    # Resolve actual text for close markers to check trailing newline
    close_texts = para_texts

    # Build batch requests (reverse order to preserve indices)
    requests = []
    for block in sorted(highlight_blocks, key=lambda b: b["open_start"], reverse=True):
        # 1. Delete closing marker (highest index first)
        # Preserve trailing \n to avoid merging with next paragraph
        close_key = (block["close_start"], block["close_end"])
        close_text = close_texts.get(close_key, "")
        close_end = block["close_end"]
        if close_text.endswith("\n"):
            close_end -= 1  # keep the newline
        if block["close_start"] < close_end:
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": block["close_start"],
                        "endIndex": close_end,
                    }
                }
            })
        # 2. Apply shading + 6pt padding to content
        if block["content_start"] < block["content_end"]:
            requests.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": block["content_start"],
                        "endIndex": block["content_end"],
                    },
                    "paragraphStyle": {
                        "shading": {
                            "backgroundColor": {
                                "color": {"rgbColor": HIGHLIGHT_COLOR}
                            }
                        },
                        "borderLeft": {"padding": HIGHLIGHT_PADDING, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID", "color": {"color": {"rgbColor": HIGHLIGHT_COLOR}}},
                        "borderRight": {"padding": HIGHLIGHT_PADDING, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID", "color": {"color": {"rgbColor": HIGHLIGHT_COLOR}}},
                        "borderTop": {"padding": HIGHLIGHT_PADDING, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID", "color": {"color": {"rgbColor": HIGHLIGHT_COLOR}}},
                        "borderBottom": {"padding": HIGHLIGHT_PADDING, "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID", "color": {"color": {"rgbColor": HIGHLIGHT_COLOR}}},
                    },
                    "fields": "shading.backgroundColor,borderLeft,borderRight,borderTop,borderBottom",
                }
            })
        # 3. Delete opening marker (lowest index last)
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": block["open_start"],
                    "endIndex": block["open_end"],
                }
            }
        })

    try:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests,
                  "writeControl": _write_control(revision_id)},
        ).execute()
    except HttpError as e:
        _warn(f"Highlight post-processing failed: {e}")


def _svg_to_png(svg_path):
    """Convert SVG to PNG using cairosvg (no extra margins)."""
    try:
        import cairosvg
    except ImportError:
        # Fallback to qlmanage if cairosvg is not installed
        tmp_dir = tempfile.mkdtemp()
        subprocess.run(
            ["qlmanage", "-t", "-s", "1600", "-o", tmp_dir, svg_path],
            capture_output=True,
        )
        png_path = os.path.join(tmp_dir, os.path.basename(svg_path) + ".png")
        return png_path if os.path.exists(png_path) else None

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    cairosvg.svg2png(url=svg_path, write_to=tmp.name, output_width=1600)
    if os.path.getsize(tmp.name) > 0:
        return tmp.name
    return None


def _upload_image_to_drive(drive_service, image_path, folder_id=None):
    """Upload image to Drive, make it publicly readable (temporarily).

    Returns (uri, file_id, permission_id). If the public permission cannot
    be created, the just-created file is deleted before re-raising so no
    orphan is left behind (codex code review #4).
    """
    file_metadata = {"name": os.path.basename(image_path)}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    media = MediaFileUpload(image_path, mimetype="image/png", resumable=True)
    img_file = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id",
        supportsAllDrives=True,
    ).execute()
    file_id = img_file["id"]

    try:
        perm = drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
            fields="id",
        ).execute()
    except HttpError:
        try:
            drive_service.files().delete(
                fileId=file_id, supportsAllDrives=True).execute()
        except HttpError as e2:
            _warn(f"orphan staging file {file_id} could not be deleted: {e2}")
        raise

    return (f"https://drive.google.com/uc?id={file_id}", file_id, perm.get("id"))


def _cleanup_staging_image(drive_service, file_id, permission_id):
    """Revoke the public ACL, then delete the staging file. Never raises —
    each step catches broad Exception so a transport error in the revoke
    cannot prevent the delete attempt (codex code review r2)."""
    if permission_id:
        try:
            drive_service.permissions().delete(
                fileId=file_id, permissionId=permission_id,
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            _warn(f"staging ACL revoke failed for {file_id}: {e}")
    try:
        drive_service.files().delete(
            fileId=file_id, supportsAllDrives=True).execute()
    except Exception as e:
        _warn(f"staging image cleanup failed for {file_id}: {e} "
              f"— delete it manually (it may be publicly readable)")


def post_process_images(docs_service, drive_service, doc_id, images, folder_id=None):
    """Find «IMG:…» markers in doc and replace with actual images.

    images: list of (marker_text, alt, rel_path, full_path) from _prepare_md_for_upload.
    Markers carry a uuid suffix (unique by construction); the EXACT marker
    substring is deleted via UTF-16 coordinates — never the whole run.
    Staging files are deleted from Drive in a finally path on every outcome
    (C6-positive: Docs stores its own copy of the image at insert time).
    """
    if not images:
        return

    for marker, alt, rel_path, full_path in reversed(images):
        # Re-read doc for fresh indices + revision pinning
        doc = docs_service.documents().get(
            documentId=doc_id, suggestionsViewMode="SUGGESTIONS_INLINE"
        ).execute()
        revision_id = doc.get("revisionId")
        synthetic_tab = {"body": doc.get("body", {})}
        found = _find_quote_in_doctab(synthetic_tab, marker)
        if not found:
            _warn(f"Could not find image marker '{marker}' in doc")
            continue
        target_idx, target_end = found

        # Marker must be unique (uuid guarantees it, but verify — fail closed)
        if _count_quote_occurrences(synthetic_tab, marker) != 1:
            _warn(f"image marker '{marker}' is not unique in doc — skipped")
            continue

        # Convert SVG → PNG if needed
        tmp_png = None
        if full_path.endswith(".svg"):
            png_path = _svg_to_png(full_path)
            if not png_path:
                _warn(f"SVG to PNG conversion failed for {full_path}")
                continue
            tmp_png = png_path
        else:
            png_path = full_path

        staged_id, staged_perm = None, None
        try:
            try:
                image_uri, staged_id, staged_perm = _upload_image_to_drive(
                    drive_service, png_path, folder_id)
            except HttpError as e:
                _warn(f"Image upload failed for {full_path}: {e}")
                continue

            requests = [
                {"deleteContentRange": {"range": {
                    "startIndex": target_idx, "endIndex": target_end}}},
                {"insertInlineImage": {
                    "location": {"index": target_idx},
                    "uri": image_uri,
                    "objectSize": {"width": {"magnitude": 468, "unit": "PT"}},
                }},
            ]
            try:
                docs_service.documents().batchUpdate(
                    documentId=doc_id,
                    body={"requests": requests,
                          "writeControl": _write_control(revision_id)},
                ).execute()
            except HttpError as e:
                _warn(f"Image insertion failed for {alt}: {e}")
        finally:
            if staged_id:
                _cleanup_staging_image(drive_service, staged_id, staged_perm)
            if tmp_png:
                try:
                    os.unlink(tmp_png)
                    parent = os.path.dirname(tmp_png)
                    if os.path.basename(parent).startswith("tmp"):
                        # qlmanage fallback creates its own temp dir
                        os.rmdir(parent)
                except OSError:
                    pass


def _prepare_md_for_upload(md_path):
    """Replace ![alt](path) with text markers and return (temp_path, images_list)."""
    with open(md_path, "r") as f:
        md_text = f.read()

    md_dir = os.path.dirname(os.path.abspath(md_path))
    images = []

    def replace_image(m):
        alt, path = m.group(1), m.group(2)
        full_path = os.path.join(md_dir, path)
        if os.path.exists(full_path):
            # full-uuid suffix guarantees marker uniqueness in the doc, so
            # the exact-substring deletion in post_process_images is
            # unambiguous; regenerate on (astronomically unlikely) collision
            while True:
                marker = f"«IMG:{uuid.uuid4().hex}»"
                if marker not in md_text:
                    break
            images.append((marker, alt, path, full_path))
            return marker
        return m.group(0)  # keep original if file not found

    new_text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace_image, md_text)

    if images:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write(new_text)
        tmp.close()
        return tmp.name, images
    return md_path, []


def upload_md(file_path, folder_id=None, title=None, no_highlights=False):
    """Upload a markdown file as a Google Doc."""
    if not os.path.exists(file_path):
        _error(f"file not found: {file_path}")

    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    # Prepare: replace image refs with text markers
    upload_path, images = _prepare_md_for_upload(file_path)

    doc_title = title or os.path.splitext(os.path.basename(file_path))[0]

    file_metadata = {
        "name": doc_title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if folder_id:
        file_metadata["parents"] = [folder_id]

    try:
        media = MediaFileUpload(upload_path, mimetype="text/markdown", resumable=True)
        file = drive_service.files().create(
            body=file_metadata, media_body=media, fields="id,name,webViewLink",
            supportsAllDrives=True
        ).execute()
    except HttpError as e:
        _error(f"upload failed: {e.reason if hasattr(e, 'reason') else e}")
    finally:
        if upload_path != file_path:
            os.unlink(upload_path)

    doc_id = file["id"]
    try:
        docs_service = get_docs_service(creds)
    except Exception as e:
        _warn(f"Docs API init failed: {e}")
        docs_service = None

    # Post-process images (before highlights, as highlights shift indices)
    if docs_service and images:
        try:
            post_process_images(docs_service, drive_service, doc_id, images, folder_id)
        except Exception as e:
            _warn(f"image processing skipped: {e}")

    # Post-process highlights
    if not no_highlights and docs_service:
        try:
            post_process_highlights(docs_service, doc_id)
        except Exception as e:
            _warn(f"highlight post-processing skipped: {e}")

    print(json.dumps({
        "id": file["id"],
        "name": file["name"],
        "url": file["webViewLink"],
    }))


def list_comments(file_id):
    """List comments on a Google Doc."""
    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    comments = []
    page_token = None
    try:
        while True:
            results = drive_service.comments().list(
                fileId=file_id,
                fields="nextPageToken,comments(id,content,author/displayName,quotedFileContent,resolved,replies(content,author/displayName))",
                includeDeleted=False,
                pageSize=100,
                pageToken=page_token,
            ).execute()
            comments.extend(results.get("comments", []))
            page_token = results.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        _error(f"failed to fetch comments: {e.reason if hasattr(e, 'reason') else e}")

    print(json.dumps(comments, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Patch (iterative structural edits with comment-conflict preflight)
# ---------------------------------------------------------------------------

def _resolve_named_range(doc_tab, name):
    """Return (start, end) of a named range by name, or None.

    Google represents named ranges as a dict: name -> {namedRanges: [...]}.
    Each entry can have multiple ranges; we take the first one's full span
    (the API splits a named range into sub-ranges when text styling changes
    inside it, but the union is what we want).
    """
    nrs_by_name = (doc_tab.get("namedRanges") or {})
    entry = nrs_by_name.get(name)
    if not entry:
        return None
    ranges = []
    for nr in entry.get("namedRanges", []):
        for r in nr.get("ranges", []):
            start = r.get("startIndex")
            end = r.get("endIndex")
            if start is not None and end is not None:
                ranges.append((start, end))
    if not ranges:
        return None
    # Check for fragmentation: if ranges aren't contiguous, error out.
    # A named range can fragment when edits split it; the gaps contain
    # real document content that we must not accidentally delete.
    sorted_ranges = sorted(ranges)
    for i in range(len(sorted_ranges) - 1):
        _, prev_end = sorted_ranges[i]
        next_start, _ = sorted_ranges[i + 1]
        if prev_end < next_start:
            _error(
                f"named range '{name}' is fragmented ({len(sorted_ranges)} "
                f"non-contiguous sub-ranges). Re-create it with /gdocs-mark."
            )
    return sorted_ranges[0][0], sorted_ranges[-1][1]


def _list_named_range_names(doc_tab):
    return sorted((doc_tab.get("namedRanges") or {}).keys())


def _resolve_op(op, doc_tab, tab_id):
    """Resolve one op dict to an internal record with absolute indices.

    Op shapes supported:
      {"op": "replace_range",   "range": "<name>", "text": "..."}
      {"op": "replace_quote",   "quote": "...", "with": "...", "occurrence": N?}
      {"op": "insert_before_range", "range": "<name>", "text": "..."}
      {"op": "insert_after_range",  "range": "<name>", "text": "..."}
      {"op": "insert_before_quote", "quote": "...", "text": "...", "occurrence": N?}
      {"op": "insert_after_quote",  "quote": "...", "text": "...", "occurrence": N?}

    Returns a dict:
      {"op": ..., "start": int, "end": int, "text": str, "kind": "replace"|"insert",
       "affect_start": int, "affect_end": int, "source": "..."}
    """
    kind_name = op.get("op")
    if not kind_name:
        _error(f"op missing 'op' field: {op}")

    # Resolve target
    if "range" in op:
        found = _resolve_named_range(doc_tab, op["range"])
        if not found:
            available = _list_named_range_names(doc_tab)
            _error(
                f"named range not found: {op['range']!r}. "
                f"Available: {available or '(none)'}"
            )
        t_start, t_end = found
        source = f"range={op['range']!r}"
    elif "quote" in op:
        quote = op["quote"]
        if "\x00" in quote:
            _error(f"quote must not contain NUL characters: {op}")
        occurrence = int(op.get("occurrence", 1))
        total = _count_quote_occurrences(doc_tab, quote)
        if total == 0:
            _error(f"quote not found: {quote!r}")
        if total > 1 and "occurrence" not in op:
            _error(
                f"quote is non-unique ({total} matches): {quote!r}. "
                f"Add 'occurrence': N to disambiguate."
            )
        if occurrence > total:
            _error(
                f"occurrence {occurrence} out of range (only {total} matches): {quote!r}"
            )
        found = _find_quote_in_doctab(doc_tab, quote, occurrence=occurrence)
        if not found:
            _error(f"quote not found: {quote!r}")
        t_start, t_end = found
        source = f"quote={quote!r} (#{occurrence}/{total})"
    else:
        _error(f"op must have 'range' or 'quote': {op}")

    # Build the normalized record
    if kind_name in ("replace_range", "replace_quote"):
        text = op.get("text") if "text" in op else op.get("with")
        if text is None:
            _error(f"replace op missing 'text' (or 'with'): {op}")
        return {
            "op": kind_name,
            "start": t_start,
            "end": t_end,
            "text": text,
            "kind": "replace",
            "affect_start": t_start,
            "affect_end": t_end,
            "source": source,
            "tab_id": tab_id,
        }
    elif kind_name in ("insert_before_range", "insert_before_quote"):
        text = op.get("text", "")
        return {
            "op": kind_name,
            "start": t_start,
            "end": t_start,
            "text": text,
            "kind": "insert",
            "affect_start": t_start,
            "affect_end": t_start,
            "source": source,
            "tab_id": tab_id,
        }
    elif kind_name in ("insert_after_range", "insert_after_quote"):
        text = op.get("text", "")
        return {
            "op": kind_name,
            "start": t_end,
            "end": t_end,
            "text": text,
            "kind": "insert",
            "affect_start": t_end,
            "affect_end": t_end,
            "source": source,
            "tab_id": tab_id,
        }
    else:
        _error(f"unknown op: {kind_name!r}")


def _locate_comment_in_tab(doc_tab, comment):
    """Best-effort: find a comment's quotedFileContent position in this tab.

    Returns a list of (start, end) tuples — one per occurrence of the quoted
    text. When the quote is non-unique, ALL matches are returned so the caller
    can treat any of them as a potential conflict (conservative approach).
    Returns empty list for unanchored/document-level comments.
    """
    qfc = (comment.get("quotedFileContent") or {}).get("value") or ""
    if not qfc:
        return []
    total = _count_quote_occurrences(doc_tab, qfc)
    if total == 0:
        return []
    results = []
    for occ in range(1, total + 1):
        found = _find_quote_in_doctab(doc_tab, qfc, occurrence=occ)
        if found:
            results.append(found)
    return results


def _ranges_overlap(a_start, a_end, b_start, b_end):
    """Check whether two op ranges conflict for batchUpdate purposes.

    For two non-zero-width ranges: standard half-open overlap.
    For a zero-width (insert) touching a non-zero-width boundary: also
    conflict, because in batchUpdate the insert and delete share the same
    index space and execution order can corrupt text.
    """
    if a_start == a_end and b_start == b_end:
        # Two inserts at the same index: order of execution would swap the
        # inserted texts — treat as a conflict (codex code review #10).
        return a_start == b_start
    if a_start == a_end:
        # Zero-width (insert): conflicts if point is inside OR at boundary
        return b_start <= a_start <= b_end and b_start < b_end
    if b_start == b_end:
        return a_start <= b_start <= a_end and a_start < a_end
    return a_start < b_end and b_start < a_end


# ---------------------------------------------------------------------------
# W8: export-based anchor mapping (PLAN.md addendum v3.1)
# ---------------------------------------------------------------------------

_WORDML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _slice_utf16(s, start_units, end_units):
    """Slice a python string by UTF-16 unit offsets."""
    out, pos = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if pos >= end_units:
            break
        if pos >= start_units:
            out.append(ch)
        pos += w
    return "".join(out)


def _parse_docx_anchor_spans(docx_bytes):
    """Parse word/document.xml and extract comment anchor spans.

    Returns (spans, problems):
      spans: [{"docx_id", "para_index", "para_text", "start_off", "end_off",
               "anchor_text", "has_objects"}] — offsets are UTF-16 units
              within the paragraph text
      problems: reasons the mapping is unusable (unpaired ranges,
              cross-paragraph spans, inline objects in anchor paragraphs,
              malformed XML). Any problem ⇒ caller fails closed.
    Strict contract per codex W8-r1 #2: linear parse, exact text, no
    normalization; w:t / w:tab / w:br / w:cr accounted for in UTF-16.
    """
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    spans, problems = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml_bytes = z.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        return [], [f"malformed docx export: {e}"]

    w = _WORDML_NS
    seen_starts, seen_ends = {}, {}
    cross_para_open = {}

    # Global census of ALL range markers anywhere in the document. After the
    # body-paragraph walk, the processed multiset must equal this one — any
    # marker hidden in an unsupported container (w:sdt, w:tbl, w:fldSimple,
    # mc:AlternateContent, tracked changes, ...) becomes a problem instead
    # of being silently ignored (codex W8-r1 P0#1).
    global_starts, global_ends = {}, {}
    for el in root.iter(f"{w}commentRangeStart"):
        cid = el.get(f"{w}id")
        global_starts[cid] = global_starts.get(cid, 0) + 1
    for el in root.iter(f"{w}commentRangeEnd"):
        cid = el.get(f"{w}id")
        global_ends[cid] = global_ends.get(cid, 0) + 1

    body = root.find(f"{w}body")
    if body is None:
        return [], ["malformed docx export: no w:body"]
    # Only DIRECT body paragraphs are processed — API-side matching also
    # uses only top-level paragraphs, so text-alone matches can never cross
    # structural domains (codex W8-r1 P1).
    body_paras = [ch for ch in body if ch.tag == f"{w}p"]

    for p_index, para in enumerate(body_paras):
        state = {"off": 0, "parts": [], "has_objects": False,
                 "has_unknown": [], "local_open": {}}

        # Whitelist walker: any element NOT explicitly known marks the
        # paragraph unclean. An anchor in an unclean paragraph is a problem
        # — truncated para_text could exact-match a DIFFERENT API paragraph
        # and produce a confidently wrong anchor range (codex W8-r2 P0).
        _BENIGN = {f"{w}pPr", f"{w}proofErr", f"{w}bookmarkStart",
                   f"{w}bookmarkEnd", f"{w}commentReference", f"{w}rPr"}
        _RUN_TEXT = {f"{w}t", f"{w}tab", f"{w}br", f"{w}cr"}
        _RUN_OBJ = {f"{w}drawing", f"{w}object", f"{w}pict"}

        def walk(node, state=state, p_index=p_index):
            for child in node:
                tag = child.tag
                if tag == f"{w}commentRangeStart":
                    cid = child.get(f"{w}id")
                    seen_starts[cid] = seen_starts.get(cid, 0) + 1
                    state["local_open"][cid] = state["off"]
                    cross_para_open[cid] = p_index
                elif tag == f"{w}commentRangeEnd":
                    cid = child.get(f"{w}id")
                    seen_ends[cid] = seen_ends.get(cid, 0) + 1
                    if cid in state["local_open"]:
                        spans.append({
                            "docx_id": cid, "para_index": p_index,
                            "start_off": state["local_open"].pop(cid),
                            "end_off": state["off"],
                        })
                        cross_para_open.pop(cid, None)
                    elif cid in cross_para_open:
                        problems.append(
                            f"comment range {cid} crosses paragraphs")
                        cross_para_open.pop(cid, None)
                    else:
                        problems.append(
                            f"commentRangeEnd {cid} without start")
                elif tag == f"{w}r":
                    for rc in child:
                        if rc.tag == f"{w}t":
                            text = rc.text or ""
                            state["parts"].append(text)
                            state["off"] += _utf16_len(text)
                        elif rc.tag in _RUN_TEXT:
                            state["parts"].append(
                                "\t" if rc.tag == f"{w}tab" else "\n")
                            state["off"] += 1
                        elif rc.tag in _RUN_OBJ:
                            state["has_objects"] = True
                        elif rc.tag in _BENIGN:
                            pass
                        else:
                            state["has_unknown"].append(rc.tag)
                elif tag in (f"{w}hyperlink", f"{w}smartTag"):
                    # visible-text containers: recurse
                    walk(child)
                elif tag in _BENIGN:
                    pass
                else:
                    # w:ins, w:del, w:sdt, w:fldSimple, mc:AlternateContent,
                    # anything else: unknown visible-text impact
                    state["has_unknown"].append(tag)

        walk(para)
        para_text = "".join(state["parts"])
        for sp in spans:
            if sp["para_index"] == p_index and "para_text" not in sp:
                sp["para_text"] = para_text
                sp["anchor_text"] = _slice_utf16(
                    para_text, sp["start_off"], sp["end_off"])
                sp["has_objects"] = state["has_objects"]
                if state["has_unknown"]:
                    problems.append(
                        f"anchor {sp['docx_id']} sits in a paragraph with "
                        f"unsupported elements ({sorted(set(state['has_unknown']))[:3]}) "
                        f"— text/offsets unreliable")

    if seen_starts != global_starts or seen_ends != global_ends:
        hidden = (set(global_starts) | set(global_ends)) - \
                 (set(seen_starts) & set(seen_ends))
        problems.append(
            f"comment range markers outside plain body paragraphs "
            f"(tables/containers/tracked changes): "
            f"{sorted(hidden) or 'count mismatch'} — mapping unusable")
    for cid, n in seen_starts.items():
        if n != 1 or seen_ends.get(cid, 0) != 1:
            problems.append(
                f"comment range {cid}: {n} starts / "
                f"{seen_ends.get(cid, 0)} ends (need exactly 1/1)")
    for s in spans:
        if s.get("has_objects"):
            problems.append(
                f"anchor {s['docx_id']} sits in a paragraph with inline "
                f"objects — offsets unreliable")
        if not s.get("anchor_text"):
            problems.append(f"anchor {s['docx_id']} is empty")
    return spans, problems


def _map_anchors_to_doc(doc_tab, spans):
    """Map docx anchor spans to absolute doc index ranges.

    Paragraph matching is by EXACT text equality (no normalization).
    Returns (ranges, problems): ranges = [(start, end, anchor_text,
    docx_id)]. Ambiguous/missing paragraph ⇒ problem ⇒ fail closed.
    """
    body = doc_tab.get("body", {}) or {}
    paras = []
    for el in body.get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        if any("textRun" not in e for e in para.get("elements", [])):
            paras.append((el["startIndex"], None))
            continue
        text = "".join(e["textRun"].get("content", "")
                       for e in para.get("elements", []))
        if text.endswith("\n"):
            text = text[:-1]
        paras.append((el["startIndex"], text))

    ranges, problems = [], []
    for s in spans:
        matches = [start for start, text in paras
                   if text is not None and text == s.get("para_text")]
        if len(matches) != 1:
            problems.append(
                f"anchor {s['docx_id']} paragraph matched {len(matches)} "
                f"times in the doc (need exactly 1): "
                f"{(s.get('para_text') or '')[:50]!r}")
            continue
        base = matches[0]
        ranges.append((base + s["start_off"], base + s["end_off"],
                       s.get("anchor_text", ""), s["docx_id"]))
    return ranges, problems


def _comments_fingerprint(drive_service, file_id):
    """Set of (id, deleted, resolved, anchored) for ALL comments, paginated.
    Raises on any error — callers treat that as fail-stop."""
    out = set()
    page_token = None
    while True:
        resp = drive_service.comments().list(
            fileId=file_id,
            fields="nextPageToken,comments(id,deleted,resolved,"
                   "quotedFileContent,anchor)",
            includeDeleted=True, pageSize=100, pageToken=page_token,
        ).execute()
        for c in resp.get("comments", []):
            out.add((c.get("id"), bool(c.get("deleted")),
                     bool(c.get("resolved")),
                     bool(c.get("quotedFileContent") or c.get("anchor"))))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


def _refuse_on_suggestions(doc_tab):
    """v1 policy: any pending suggestion in the TARGET tab blocks structural
    writes entirely (scoped per codex code review #9 — a suggestion in a
    sibling tab does not block edits to this one)."""
    marker = _scan_suggestions(doc_tab)
    if marker:
        _error(
            f"target tab has pending suggestions ({marker}); accept/reject "
            f"them in the Google Docs UI first — structural edits are blocked "
            f"while suggestions exist (fail-closed policy, see FINDINGS.md)"
        )


def _informational_replies(drive_service, file_id, doc_tab, resolved, comments):
    """Best-effort: post an informational reply on comments whose stale
    quotedFileContent still matches text intersecting an applied op.

    Purely informational — NEVER used for safety decisions (the snapshot
    can be stale; see codex r1 #2).
    """
    unresolved = [c for c in comments if not c.get("resolved")]
    notes = []
    seen = set()
    for r in resolved:
        for c in unresolved:
            cid = c.get("id")
            if cid in seen:
                continue
            for cs, ce in _locate_comment_in_tab(doc_tab, c):
                if _ranges_overlap(r["affect_start"], r["affect_end"], cs, ce):
                    seen.add(cid)
                    quoted = (c.get("quotedFileContent") or {}).get("value", "")
                    note = (
                        f"↻ Текст рядом с этим комментарием обновлён через "
                        f"/gdocs-patch (было: «{quoted[:200]}»). Тред сохранён."
                    )
                    try:
                        drive_service.replies().create(
                            fileId=file_id, commentId=cid,
                            body={"content": note}, fields="id",
                        ).execute()
                        notes.append({"comment_id": cid, "reply_posted": True})
                    except HttpError as e:
                        notes.append({"comment_id": cid, "reply_posted": False,
                                      "error": str(e)})
                    break
    return notes


def _check_ops_overlap(resolved):
    sorted_for_check = sorted(resolved, key=lambda r: (r["affect_start"], r["affect_end"]))
    for i in range(len(sorted_for_check) - 1):
        a = sorted_for_check[i]
        b = sorted_for_check[i + 1]
        if _ranges_overlap(a["affect_start"], a["affect_end"],
                           b["affect_start"], b["affect_end"]):
            _error(
                f"ops overlap: {a['source']} and {b['source']} — "
                f"split into separate patches"
            )


def _resolve_replace_target(op, doc_tab, r):
    """Shared replace-target checks on a given snapshot: exact text,
    uniqueness, round-trip, style uniformity. Returns search_text."""
    if "quote" in op:
        search_text = op["quote"]
        if "occurrence" in op and int(op["occurrence"]) != 1:
            _error(
                f"'occurrence' targeting is not supported on docs with "
                f"anchored comments ({r['source']}); provide a longer "
                f"quote that is unique in the tab"
            )
    else:
        search_text = _extract_exact_text_range(doc_tab, r["start"], r["end"])
        if search_text is None:
            _error(
                f"named range {r['source']} is not contiguously covered "
                f"by text runs (contains inline objects or structural "
                f"elements) — anchor-safe replace refused (fail closed)"
            )
        if not search_text:
            _error(f"named range {r['source']} resolved to empty text")

    total = _count_quote_occurrences(doc_tab, search_text)
    if total != 1:
        _error(
            f"replace target must be unique in tab for the anchor-safe "
            f"path, found {total} matches ({r['source']}); provide a "
            f"longer unique quote"
        )
    # Round-trip: the unique match must be exactly the resolved range.
    found = _find_quote_in_doctab(doc_tab, search_text)
    if found != (r["start"], r["end"]):
        _error(
            f"resolved text for {r['source']} matches a different "
            f"location ({found} vs ({r['start']}, {r['end']})) — refused"
        )
    uniform, differing = _match_style_signature(doc_tab, r["start"], r["end"])
    if not uniform:
        _error(
            f"replace target spans mixed text styles ({', '.join(differing)}) "
            f"— replaceAllText would flatten them (confirmed, C2). Split the "
            f"replacement into uniformly-styled pieces or edit in the UI. "
            f"({r['source']})"
        )
    return search_text


def _execute_replace_all(docs_service, file_id, tid, search_text, new_text,
                         revision_id, source):
    req = {"replaceAllText": {
        "containsText": {"text": search_text, "matchCase": True},
        "replaceText": new_text,
    }}
    if tid:
        req["replaceAllText"]["tabsCriteria"] = {"tabIds": [tid]}
    result = docs_service.documents().batchUpdate(
        documentId=file_id,
        body={"requests": [req], "writeControl": _write_control(revision_id)},
    ).execute()
    occ = (result.get("replies") or [{}])[0].get(
        "replaceAllText", {}).get("occurrencesChanged", 0)
    if occ != 1:
        # occ == 0: nothing changed; occ > 1: the doc HAS been modified in
        # multiple places — the report must not invite a blind retry.
        raise PatchOpError(
            f"replaceAllText changed {occ} occurrences (expected exactly "
            f"1) for {source} — stopping; verify the doc state",
            state="not_applied" if occ == 0 else "unknown",
        )


def _apply_op_anchor_safe(docs_service, drive_service, file_id, op, tab_id):
    """Apply ONE op on a commented doc.

    Inserts: fresh read, re-resolve, pinned batch (C5 verified safe).
    Replaces: W8 export-based anchor mapping sandwich —
      fingerprint → read(R) → export docx → read(R'); R≠R' ⇒ retry
      → mapping + target checks + coverage on the R' snapshot
      → fingerprint recheck → batchUpdate pinned to R'.
    A replacement fully covering a live anchor is refused (C1 verified:
    it would ghost the comment). Partial overlap is safe — the anchor
    shrinks to the surviving original characters.
    """
    kind_name = op.get("op", "")
    if not kind_name.startswith("replace"):
        # ---- insert path ----
        doc = _safe_get_doc(docs_service, file_id)
        tid, doc_tab = _select_tab(doc, tab_id=tab_id)
        _refuse_on_suggestions(doc_tab)
        r = _resolve_op(op, doc_tab, tid)
        if not r["text"]:
            return
        loc = {"index": r["start"]}
        if tid:
            loc["tabId"] = tid
        docs_service.documents().batchUpdate(
            documentId=file_id,
            body={"requests": [{"insertText": {"location": loc,
                                               "text": r["text"]}}],
                  "writeControl": _write_control(doc.get("revisionId"))},
        ).execute()
        return

    # ---- replace path (W8 sandwich) ----
    last_reason = "unknown"
    for attempt in range(3):
        # Everything before the final batch is read-only: an HTTP failure
        # here means the op was definitely NOT applied (codex W8-r1 P2#5).
        try:
            fp1 = _comments_fingerprint(drive_service, file_id)
            doc1 = docs_service.documents().get(
                documentId=file_id, fields="revisionId").execute()
            docx_bytes = drive_service.files().export(
                fileId=file_id,
                mimeType="application/vnd.openxmlformats-officedocument"
                         ".wordprocessingml.document").execute()
            doc = _safe_get_doc(docs_service, file_id)
        except Exception as e:
            raise PatchOpError(
                f"W8 preflight read failed: "
                f"{e.reason if hasattr(e, 'reason') else e}",
                state="not_applied")
        revision_id = doc.get("revisionId")
        if revision_id != doc1.get("revisionId"):
            last_reason = "doc changed during export"
            continue
        if len(_collect_tabs(doc)) > 1:
            # DOCX ranges carry no tab identity (addendum p.6)
            _error("multi-tab document: anchor-mapped replaces are not "
                   "supported — edit in the UI")
        tid, doc_tab = _select_tab(doc, tab_id=tab_id)
        _refuse_on_suggestions(doc_tab)
        r = _resolve_op(op, doc_tab, tid)
        search_text = _resolve_replace_target(op, doc_tab, r)

        spans, problems = _parse_docx_anchor_spans(docx_bytes)
        anchors, mproblems = _map_anchors_to_doc(doc_tab, spans)
        problems += mproblems
        if problems:
            _error(
                f"anchor mapping failed (fail closed, {r['source']}): "
                + "; ".join(problems[:3])
            )
        live_anchored = any(anch and not deleted
                            for (_id, deleted, _res, anch) in fp1)
        if live_anchored and not anchors:
            _error(
                f"doc has anchored comments in the API but the docx export "
                f"contains no anchor ranges — cannot prove they are ghosts "
                f"(C10 pending); replace blocked (fail closed). Use insert "
                f"ops or edit in the UI."
            )
        for as_, ae, atext, aid in anchors:
            if r["start"] <= as_ and ae <= r["end"]:
                _error(
                    f"replacement fully covers the anchor of a live comment "
                    f"(docx id {aid}, anchored text «{atext[:60]}») — it "
                    f"would ghost the comment (C1). Split into two replaces "
                    f"so at least one ORIGINAL anchor character survives "
                    f"(repeating the same text in the replacement does NOT "
                    f"help). ({r['source']})"
                )
        try:
            fp2 = _comments_fingerprint(drive_service, file_id)
        except Exception as e:
            raise PatchOpError(
                f"W8 final census failed: "
                f"{e.reason if hasattr(e, 'reason') else e}",
                state="not_applied")
        if fp2 != fp1:
            last_reason = "comments changed during mapping"
            continue
        # no other network calls between the final census and the write
        _execute_replace_all(docs_service, file_id, tid, search_text,
                             r["text"], revision_id, r["source"])
        return
    _error(
        f"anchor-mapped replace kept failing preflight after 3 attempts "
        f"({last_reason}) — the doc is being edited concurrently; retry later"
    )


def patch_doc(file_id, ops_path, tab_id=None):
    """Apply structural patch operations to a Google Doc.

    Strategy is selected at DOCUMENT level (codex r1 #2 — anchor positions
    are unknowable via API, so no per-op anchor localization):
      - doc has NO anchored comments -> precise index path, one atomic batch;
      - doc HAS anchored comments   -> per-op pinned batches; replaces via
        replaceAllText, inserts via insertText; each op class is gated by
        its phase-0 characterization result (fail closed while unverified).
    """
    file_id = _extract_doc_id(file_id)

    if not os.path.exists(ops_path):
        _error(f"ops file not found: {ops_path}")
    try:
        with open(ops_path, "r") as f:
            ops = json.load(f)
    except Exception as e:
        _error(f"cannot parse ops json: {e}")
    if not isinstance(ops, list) or not ops:
        _error("ops file must contain a non-empty JSON array of operations")

    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
        docs_service = get_docs_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    try:
        doc = _safe_get_doc(docs_service, file_id)
    except HttpError as e:
        _error(f"cannot read doc: {e.reason if hasattr(e, 'reason') else e}")

    revision_id = doc.get("revisionId")
    tid, doc_tab = _select_tab(doc, tab_id=tab_id)
    _refuse_on_suggestions(doc_tab)

    # Resolve every op against the current snapshot (validates targets early
    # for both paths) and reject ambiguous batches.
    resolved = [_resolve_op(op, doc_tab, tid) for op in ops]
    _check_ops_overlap(resolved)

    all_comments, anchored = _census_comments(drive_service, file_id)

    if not anchored:
        # ---- clean-doc path: single atomic index-based batch ----
        # Second census immediately before the destructive batch narrows the
        # race window (a comment added in between would change the strategy).
        _, anchored2 = _census_comments(drive_service, file_id)
        if anchored2:
            _error(
                "an anchored comment appeared while preparing the patch; "
                "re-run — the doc now requires the anchor-safe strategy"
            )
        ordered = sorted(resolved, key=lambda r: r["affect_start"], reverse=True)
        requests = []
        for r in ordered:
            if r["kind"] == "replace":
                if r["end"] > r["start"]:
                    del_range = {"startIndex": r["start"], "endIndex": r["end"]}
                    if r["tab_id"]:
                        del_range["tabId"] = r["tab_id"]
                    requests.append({"deleteContentRange": {"range": del_range}})
                if r["text"]:
                    loc = {"index": r["start"]}
                    if r["tab_id"]:
                        loc["tabId"] = r["tab_id"]
                    requests.append({"insertText": {"location": loc, "text": r["text"]}})
            elif r["kind"] == "insert" and r["text"]:
                loc = {"index": r["start"]}
                if r["tab_id"]:
                    loc["tabId"] = r["tab_id"]
                requests.append({"insertText": {"location": loc, "text": r["text"]}})
        if requests:
            try:
                docs_service.documents().batchUpdate(
                    documentId=file_id,
                    body={"requests": requests,
                          "writeControl": _write_control(revision_id)},
                ).execute()
            except HttpError as e:
                reason = e.reason if hasattr(e, "reason") else str(e)
                _error(
                    f"batchUpdate failed (possibly revision conflict): {reason}. "
                    f"Re-read the doc and retry."
                )
        print(json.dumps({
            "action": "patched",
            "strategy": "index-atomic",
            "doc_id": file_id,
            "tab_id": tid,
            "ops_applied": len(resolved),
            "revision_id_before": revision_id,
        }, ensure_ascii=False))
        return

    # ---- commented-doc path: per-op pinned batches ----
    # Replaces are protected per-op by W8 export-based anchor mapping
    # (full-anchor coverage refused; partial overlap allowed). Inserts are
    # gated by C5.
    has_insert = any(r["kind"] == "insert" for r in resolved)
    if has_insert and C5_INSERT_NEAR_ANCHOR_SAFE is not True:
        state = "unverified" if C5_INSERT_NEAR_ANCHOR_SAFE is None else "verified UNSAFE"
        _error(
            f"doc has {len(anchored)} anchored comment(s); insert ops are "
            f"blocked (C5 insert-near-anchor behavior is {state} — fail "
            f"closed). Run the C5 live UI verification and set "
            f"C5_INSERT_NEAR_ANCHOR_SAFE, or edit in the UI."
        )

    global _RAISE_ERRORS
    applied, failed_at, failure, app_state = [], None, None, None
    for i, op in enumerate(ops):
        try:
            _RAISE_ERRORS = True  # nested preflight errors must not exit
            try:
                _apply_op_anchor_safe(docs_service, drive_service,
                                      file_id, op, tab_id)
            finally:
                _RAISE_ERRORS = False
            applied.append(resolved[i]["source"])
        except PatchOpError as e:
            failed_at, failure, app_state = i, str(e), e.state
            break
        except HttpError as e:
            failed_at = i
            failure = e.reason if hasattr(e, "reason") else str(e)
            # 5xx/transport after send: the write may or may not have landed
            status = getattr(getattr(e, "resp", None), "status", None)
            app_state = "unknown" if (status is None or status >= 500) else "not_applied"
            break
        except Exception as e:  # network timeouts etc. — state unknown
            failed_at, failure, app_state = i, str(e), "unknown"
            break

    # Best-effort phase: must NEVER suppress the patch report below.
    try:
        notes = _informational_replies(
            drive_service, file_id, doc_tab,
            resolved[:len(applied)], all_comments)
    except Exception as e:
        notes = [{"error": f"informational replies failed: {e}"}]

    result = {
        "action": "patched" if failed_at is None else "partially-patched",
        "strategy": "anchor-safe-per-op",
        "doc_id": file_id,
        "tab_id": tid,
        "ops_applied": len(applied),
        "applied": applied,
        "comment_notes": notes,
    }
    if failed_at is not None:
        result["failed_at"] = failed_at
        result["error"] = failure
        result["failed_op_state"] = app_state
        result["remaining"] = [r["source"] for r in resolved[failed_at:]]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(3)
    print(json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Named ranges (mark)
# ---------------------------------------------------------------------------

def mark_range(file_id, name, quote, tab_id=None, occurrence=1):
    """Create a named range around a text fragment.

    Named ranges are the only reliable machine-owned anchoring mechanism:
    their indices are kept consistent by Google's OT across collaborator edits.
    Use them as stable targets for subsequent `patch` operations.
    """
    file_id = _extract_doc_id(file_id)
    try:
        creds = get_creds()
        docs_service = get_docs_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    try:
        doc = _safe_get_doc(docs_service, file_id)
    except HttpError as e:
        _error(f"cannot read doc: {e.reason if hasattr(e, 'reason') else e}")

    revision_id = doc.get("revisionId")
    tid, doc_tab = _select_tab(doc, tab_id=tab_id)

    total = _count_quote_occurrences(doc_tab, quote)
    if total == 0:
        _error(f"quote not found in tab {tid or '(default)'}: {quote!r}")
    if occurrence > total:
        _error(
            f"occurrence {occurrence} out of range: only {total} matches for {quote!r}"
        )
    if total > 1 and occurrence == 1:
        _error(
            f"quote is non-unique ({total} matches). "
            f"Pass --occurrence N to disambiguate."
        )

    found = _find_quote_in_doctab(doc_tab, quote, occurrence=occurrence)
    if not found:
        _error(f"quote not found in tab {tid or '(default)'}: {quote!r}")
    start_idx, end_idx = found

    # Build createNamedRange request. For multi-tab docs, range must carry tabId.
    range_obj = {"startIndex": start_idx, "endIndex": end_idx}
    if tid:
        range_obj["tabId"] = tid

    requests = [{
        "createNamedRange": {
            "name": name,
            "range": range_obj,
        }
    }]

    try:
        result = docs_service.documents().batchUpdate(
            documentId=file_id,
            body={
                "requests": requests,
                "writeControl": _write_control(revision_id),
            },
        ).execute()
    except HttpError as e:
        _error(f"mark failed: {e.reason if hasattr(e, 'reason') else e}")

    replies = result.get("replies", [])
    nr_reply = replies[0].get("createNamedRange", {}) if replies else {}

    print(json.dumps({
        "named_range_id": nr_reply.get("namedRangeId"),
        "name": name,
        "tab_id": tid,
        "start": start_idx,
        "end": end_idx,
        "quote": quote,
        "occurrence": occurrence,
        "total_matches": total,
    }, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Reply / resolve / create comment
# ---------------------------------------------------------------------------

def reply_comment(file_id, comment_id, text, resolve=False):
    """Post a reply to an existing comment. If resolve=True, also resolves the thread."""
    file_id = _extract_doc_id(file_id)
    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    body = {"content": text}
    if resolve:
        body["action"] = "resolve"

    try:
        reply = drive_service.replies().create(
            fileId=file_id,
            commentId=comment_id,
            body=body,
            fields="id,content,author/displayName,action,createdTime",
        ).execute()
    except HttpError as e:
        _error(f"reply failed: {e.reason if hasattr(e, 'reason') else e}")

    print(json.dumps({
        "id": reply.get("id"),
        "comment_id": comment_id,
        "content": reply.get("content"),
        "author": reply.get("author", {}).get("displayName"),
        "action": reply.get("action"),
        "resolved": resolve,
    }, ensure_ascii=False))


def resolve_comment(file_id, comment_id, text=None):
    """Resolve a comment by posting a reply with action=resolve.

    Per Drive API, 'resolved' is read-only; the only way to resolve is via
    replies.create with action: 'resolve'. Works for unanchored comments too.
    """
    reply_comment(file_id, comment_id, text or "Resolved.", resolve=True)


def create_comment(file_id, text):
    """Create a document-level (unanchored) comment.

    We deliberately do NOT support API-created anchored comments: per Google,
    anchors are immutable and API-created anchored comments may display as
    unanchored in Workspace editors anyway.
    """
    file_id = _extract_doc_id(file_id)
    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    try:
        comment = drive_service.comments().create(
            fileId=file_id,
            body={"content": text},
            fields="id,content,author/displayName,createdTime",
        ).execute()
    except HttpError as e:
        _error(f"comment failed: {e.reason if hasattr(e, 'reason') else e}")

    print(json.dumps({
        "id": comment.get("id"),
        "content": comment.get("content"),
        "author": comment.get("author", {}).get("displayName"),
    }, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

EXPORT_MIMETYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "html": "text/html",
    "txt": "text/plain",
    "md": "text/html",  # export as HTML, then convert
}


def _extract_doc_id(id_or_url):
    """Extract document/file ID from a Google Docs/Drive URL or return as-is."""
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", id_or_url)
    if m:
        return m.group(1)
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", id_or_url)
    if m:
        return m.group(1)
    return id_or_url


def _prepare_export_html(html):
    """Normalize Google's HTML export before markdownify.

    - font-weight:700 / bold spans -> <b>, font-style:italic -> <i>
      (Google exports styles as inline CSS which markdownify ignores;
      without this, regenerated md loses bold/italic and a subsequent
      sync would strip those styles from the doc)
    - unwrap https://www.google.com/url?q=<real> redirect links
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, parse_qs
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["style", "script"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # unwrap (possibly nested) redirect wrappers; parse_qs already
        # percent-decodes, so no extra unquote
        for _ in range(5):
            if not href.startswith("https://www.google.com/url"):
                break
            real = parse_qs(urlparse(href).query).get("q", [None])[0]
            if not real:
                break
            href = real
        a["href"] = href
    for span in soup.find_all("span"):
        style = span.get("style", "")
        wrappers = []
        if re.search(r"font-weight\s*:\s*(bold|[7-9]00)", style):
            wrappers.append("b")
        if re.search(r"font-style\s*:\s*italic", style):
            wrappers.append("i")
        for w in wrappers:
            new_tag = soup.new_tag(w)
            for child in list(span.contents):
                new_tag.append(child.extract())
            span.append(new_tag)
    out = str(soup)
    return re.sub(r'@import\s+url\([^)]*\)\s*;?', '', out)


def _download_images_from_html(html, images_dir, creds):
    """Download images referenced in HTML, save locally, rewrite src to local paths."""
    from bs4 import BeautifulSoup
    import requests as req

    soup = BeautifulSoup(html, "html.parser")
    os.makedirs(images_dir, exist_ok=True)
    count = 0
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        count += 1
        ext = ".png"
        fname = f"image_{count:03d}{ext}"
        local_path = os.path.join(images_dir, fname)
        try:
            headers = {}
            if "googleusercontent.com" in src or "google.com" in src:
                headers["Authorization"] = f"Bearer {creds.token}"
            resp = req.get(src, headers=headers, timeout=30)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
                fname = f"image_{count:03d}{ext}"
                local_path = os.path.join(images_dir, fname)
            with open(local_path, "wb") as f:
                f.write(resp.content)
            img["src"] = os.path.join(os.path.basename(images_dir), fname)
        except Exception as e:
            _warn(f"Failed to download image {src[:80]}: {e}")
            img["src"] = src
    return str(soup), count


def download_doc(file_id, fmt="md", output=None, images_dir=None):
    """Download a Google Doc in the specified format."""
    file_id = _extract_doc_id(file_id)

    if fmt not in EXPORT_MIMETYPES:
        _error(f"unsupported format: {fmt}. Supported: {', '.join(EXPORT_MIMETYPES)}")

    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    # Get file metadata for default filename
    try:
        meta = drive_service.files().get(
            fileId=file_id, fields="name,mimeType", supportsAllDrives=True
        ).execute()
    except HttpError as e:
        _error(f"cannot access file: {e.reason if hasattr(e, 'reason') else e}")

    doc_name = meta["name"]
    file_ext = fmt if fmt != "md" else "md"

    if not output:
        safe_name = re.sub(r'[^\w\s-]', '', doc_name).strip().replace(' ', '_')
        output = f"{safe_name}.{file_ext}"

    export_mime = EXPORT_MIMETYPES[fmt]

    if fmt == "md":
        from markdownify import markdownify as md_convert

        # remove any stale sidecar FIRST: if this download half-fails, an
        # old sidecar next to a new md must not look valid (codex sync-r1 #10)
        stale = output + SIDECAR_SUFFIX
        if os.path.exists(stale):
            os.unlink(stale)

        # export + doc read under a revision sandwich so the sidecar
        # describes exactly the exported content (codex sync-r1 #5)
        docs_service = get_docs_service(creds)
        try:
            data, doc_snapshot = _export_html_snapshot(
                drive_service, docs_service, file_id)
        except (HttpError, RuntimeError) as e:
            _error(f"export failed: {getattr(e, 'reason', e)}")

        html = data.decode("utf-8") if isinstance(data, bytes) else data
        html = _prepare_export_html(html)

        img_count = 0
        if images_dir is None:
            images_dir = os.path.splitext(output)[0] + "_images"
        html, img_count = _download_images_from_html(html, images_dir, creds)
        text = md_convert(html, heading_style="ATX", strip=["style"])
        # Clean up excessive blank lines and leading whitespace artifacts;
        # normalize NBSP that Google's HTML export injects for plain spaces
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.lstrip('\n').replace(" ", " ")
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
        result = {"file": output, "format": "md", "title": doc_name, "images": img_count}
        if img_count:
            result["images_dir"] = images_dir

        # Sidecar (merge base for `sync`) — best-effort, never fails download
        try:
            sidecar_path = _build_sidecar(creds, file_id, output, text,
                                          doc=doc_snapshot)
            result["sidecar"] = sidecar_path
        except Exception as e:
            _warn(f"sidecar not written (sync will be unavailable): {e}")
    else:
        try:
            data = drive_service.files().export(
                fileId=file_id, mimeType=export_mime
            ).execute()
        except HttpError as e:
            _error(f"export failed: {e.reason if hasattr(e, 'reason') else e}")
        content = data if isinstance(data, bytes) else data.encode("utf-8")
        with open(output, "wb") as f:
            f.write(content)
        result = {"file": output, "format": fmt, "title": doc_name}

    print(json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

def _extract_full_text(body):
    """Extract plain text from a Google Docs body."""
    parts = []
    for el in body.get("content", []):
        if "paragraph" in el:
            for elem in el["paragraph"].get("elements", []):
                tr = elem.get("textRun")
                if tr:
                    parts.append(tr["content"])
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    parts.append(_extract_full_text_inner(cell.get("content", [])))
            parts.append("\n")
    return "".join(parts)


def _extract_full_text_inner(content):
    parts = []
    for el in content:
        if "paragraph" in el:
            for elem in el["paragraph"].get("elements", []):
                tr = elem.get("textRun")
                if tr:
                    parts.append(tr["content"])
    return "".join(parts)


def list_suggestions(file_id):
    """Compare original vs accepted suggestions, output as structured diff."""
    file_id = _extract_doc_id(file_id)

    try:
        creds = get_creds()
        docs_service = get_docs_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    try:
        doc_original = docs_service.documents().get(
            documentId=file_id,
            suggestionsViewMode="PREVIEW_WITHOUT_SUGGESTIONS"
        ).execute()
        doc_accepted = docs_service.documents().get(
            documentId=file_id,
            suggestionsViewMode="PREVIEW_SUGGESTIONS_ACCEPTED"
        ).execute()
    except HttpError as e:
        _error(f"failed to read document: {e.reason if hasattr(e, 'reason') else e}")

    title = doc_original.get("title", "")
    original_text = _extract_full_text(doc_original.get("body", {}))
    accepted_text = _extract_full_text(doc_accepted.get("body", {}))

    if original_text == accepted_text:
        print(json.dumps({
            "title": title,
            "has_suggestions": False,
            "changes": [],
            "diff": "",
        }, ensure_ascii=False))
        return

    orig_lines = original_text.splitlines(keepends=True)
    acc_lines = accepted_text.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        orig_lines, acc_lines,
        fromfile="original", tofile="with_suggestions", n=2
    ))

    # Build structured changes
    changes = []
    current = None
    for line in diff_lines:
        if line.startswith("@@"):
            if current:
                changes.append(current)
            current = {"deleted": [], "inserted": [], "context": line.strip()}
        elif current is not None:
            if line.startswith("-") and not line.startswith("---"):
                current["deleted"].append(line[1:].rstrip("\n"))
            elif line.startswith("+") and not line.startswith("+++"):
                current["inserted"].append(line[1:].rstrip("\n"))
    if current:
        changes.append(current)

    print(json.dumps({
        "title": title,
        "has_suggestions": True,
        "change_count": len(changes),
        "changes": changes,
        "diff": "".join(diff_lines),
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# sync: three-way merge of local markdown into a Google Doc (PLAN.md W4/W5)
# ---------------------------------------------------------------------------

SIDECAR_SCHEMA_VERSION = 2
SIDECAR_SUFFIX = ".gdocs-base.json"

# Heading namedStyleType <-> markdown level
_HEADING_STYLES = {f"HEADING_{i}": i for i in range(1, 7)}


def _para_kind(paragraph):
    """Classify a paragraph: 'p', 'h1'..'h6', 'li' (any list item)."""
    if paragraph.get("bullet") is not None:
        return "li"
    style = (paragraph.get("paragraphStyle") or {}).get("namedStyleType", "")
    lvl = _HEADING_STYLES.get(style)
    return f"h{lvl}" if lvl else "p"


def _run_marks(text_style):
    """Extract the comparable style marks of a text run."""
    ts = text_style or {}
    marks = []
    if ts.get("bold"):
        marks.append("bold")
    if ts.get("italic"):
        marks.append("italic")
    link = (ts.get("link") or {}).get("url")
    if link:
        marks.append(f"link:{link}")
    return tuple(sorted(marks))


def _norm_ws(s):
    """NBSP -> space. Google's HTML export nbsp-ifies some plain spaces, so
    comparisons across the doc/md boundary must treat them as equal. Both
    are 1 UTF-16 unit, so lengths are unaffected. Known limitation: an
    intentional space<->nbsp edit is invisible to sync, and typographic
    nbsp inside a locally-REWRITTEN paragraph is not preserved."""
    return s.replace(" ", " ")


def _marks_signature(spans):
    """Canonical string form of [(text, marks), ...] with empty-mark runs
    merged, so run fragmentation differences don't create false diffs."""
    merged = []
    for text, marks in spans:
        if merged and merged[-1][1] == marks:
            merged[-1][0] += text
        else:
            merged.append([text, marks])
    return json.dumps(
        [[_norm_ws(t), list(m)] for t, m in merged if t], ensure_ascii=False)


def _doc_elements(doc_tab):
    """Extract the comparable element sequence from a documentTab.

    Returns a list of dicts:
      {"type": "p"|"h1".."h6"|"li", "text": str (no trailing \\n),
       "sig": marks signature, "start": int, "end": int}
      {"type": "opaque", "kind": "table"|..., "hash": str,
       "start": int, "end": int}
    Empty paragraphs are skipped (invisible glue — sync never touches them).
    """
    out = []
    body = doc_tab.get("body", {}) or {}
    for el in body.get("content", []):
        if "paragraph" in el:
            para = el["paragraph"]
            parts, spans, has_nontext = [], [], False
            for e in para.get("elements", []):
                tr = e.get("textRun")
                if tr is None:
                    has_nontext = True
                    continue
                if tr.get("suggestedInsertionIds"):
                    continue
                parts.append(tr.get("content", ""))
                spans.append((tr.get("content", ""),
                              _run_marks(tr.get("textStyle"))))
            text = "".join(parts)
            if text.endswith("\n"):
                text = text[:-1]
                if spans:
                    last_t, last_m = spans[-1]
                    spans[-1] = (last_t[:-1] if last_t.endswith("\n") else last_t,
                                 last_m)
            nesting = ((para.get("bullet") or {}).get("nestingLevel") or 0)
            if has_nontext or "\v" in text or "\n" in text or nesting > 0:
                # inline objects, internal line breaks, nested bullets:
                # structurally out of the v1 md subset — opaque atom, so the
                # md side (which also opaque-ifies these) pairs positionally
                # (codex sync-r2 #7)
                out.append({"type": "opaque", "kind": "complex-paragraph",
                            "hash": _opaque_hash(el),
                            "start": el["startIndex"], "end": el["endIndex"]})
                continue
            if not text.strip():
                continue
            out.append({"type": _para_kind(para), "text": text,
                        "sig": _marks_signature(spans),
                        # full-state fingerprint: the ENTIRE paragraph element
                        # (all textStyle fields, paragraphStyle, bullet) with
                        # indices stripped — remote-change detection must see
                        # underline/color/font edits too (codex sync-r2 #2)
                        "doc_fp": _opaque_hash(el),
                        "start": el["startIndex"], "end": el["endIndex"]})
        elif "sectionBreak" in el:
            continue
        else:
            kind = next(iter(k for k in el if k not in
                             ("startIndex", "endIndex")), "unknown")
            out.append({"type": "opaque", "kind": kind,
                        "hash": _opaque_hash(el),
                        "start": el.get("startIndex", 0),
                        "end": el.get("endIndex", 0)})
    return out


def _sha256_str(s):
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _strip_indices(node):
    """Deep-copy a Document element without startIndex/endIndex keys, so
    opaque-element hashes are stable when text elsewhere shifts indices."""
    if isinstance(node, dict):
        return {k: _strip_indices(v) for k, v in node.items()
                if k not in ("startIndex", "endIndex")}
    if isinstance(node, list):
        return [_strip_indices(x) for x in node]
    return node


def _opaque_hash(el):
    return _sha256_str(json.dumps(_strip_indices(el), sort_keys=True))


def _write_journal(md_path, journal):
    """Atomically write the sync recovery journal; returns its path."""
    path = md_path + ".gdocs-sync-journal.json"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def _md_inline_spans(inline_token):
    """Convert a markdown-it inline token into (plain_text, spans).

    spans: [(text, marks_tuple)] in document order. Supported marks: bold,
    italic, link:<url>. Returns (None, unsupported_reason) when the inline
    content uses something outside the v1 subset.
    """
    text_parts, spans = [], []
    active = []  # stack of marks

    def marks_now():
        flat = []
        for m in active:
            flat.append(m)
        return tuple(sorted(flat))

    for tok in inline_token.children or []:
        t = tok.type
        if t == "text":
            text_parts.append(tok.content)
            spans.append((tok.content, marks_now()))
        elif t == "softbreak":
            # a \n inside one element would become a SECOND Docs paragraph
            # on insertText — structurally unsupported (codex sync-r1 #9)
            return None, "soft line break inside a paragraph — use separate paragraphs"
        elif t == "strong_open":
            active.append("bold")
        elif t == "strong_close":
            active.remove("bold")
        elif t == "em_open":
            active.append("italic")
        elif t == "em_close":
            active.remove("italic")
        elif t == "link_open":
            active.append(f"link:{tok.attrs.get('href', '')}")
        elif t == "link_close":
            active = [m for m in active if not m.startswith("link:")]
        elif t == "code_inline":
            # v1: treat inline code as plain text (Docs has no code style
            # in our subset) — keep the text, drop the mark
            text_parts.append(tok.content)
            spans.append((tok.content, marks_now()))
        else:
            return None, f"unsupported inline markdown: {t}"
    return ("".join(text_parts), spans)


def _md_elements(md_text):
    """Parse markdown into the same element shape as _doc_elements.

    Returns (elements, errors). Elements:
      {"type": "p"|"hN"|"li", "text", "sig", "raw"}
      {"type": "opaque-md", "raw": verbatim block}
    `raw` is the verbatim source block (for base-vs-local change detection).
    """
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark")
    tokens = md.parse(md_text)
    lines = md_text.split("\n")
    out, errors = [], []

    def raw_slice(tok):
        if tok.map:
            return "\n".join(lines[tok.map[0]:tok.map[1]]).strip("\n")
        return ""

    i = 0
    list_depth = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t in ("bullet_list_open", "ordered_list_open"):
            list_depth += 1
            i += 1
        elif t in ("bullet_list_close", "ordered_list_close"):
            list_depth -= 1
            i += 1
        elif t in ("list_item_open", "list_item_close"):
            i += 1
        elif t == "heading_open":
            level = int(tok.tag[1])
            inline = tokens[i + 1]
            parsed = _md_inline_spans(inline)
            if parsed[0] is None:
                errors.append(f"{parsed[1]} in heading: {inline.content[:40]!r}")
                out.append({"type": "opaque-md", "raw": raw_slice(tok)})
            else:
                text, spans = parsed
                out.append({"type": f"h{level}", "text": text,
                            "sig": _marks_signature(spans),
                            "raw": raw_slice(tok)})
            i += 3
        elif t == "paragraph_open":
            inline = tokens[i + 1]
            parsed = _md_inline_spans(inline)
            kind = "li" if list_depth > 0 else "p"
            if list_depth > 1:
                # nested lists are structurally unsupported in v1: treat as
                # opaque so a change to them refuses instead of flattening
                errors.append("nested list item (unsupported in v1)")
                out.append({"type": "opaque-md", "raw": raw_slice(tok)})
            elif parsed[0] is None:
                errors.append(f"{parsed[1]} in block: {inline.content[:40]!r}")
                out.append({"type": "opaque-md", "raw": raw_slice(tok)})
            else:
                text, spans = parsed
                out.append({"type": kind, "text": text,
                            "sig": _marks_signature(spans),
                            "raw": raw_slice(tok)})
            i += 3
        elif t in ("fence", "code_block", "html_block", "table_open", "hr",
                   "blockquote_open"):
            out.append({"type": "opaque-md", "raw": raw_slice(tok)})
            # skip to matching close for container blocks
            if t.endswith("_open"):
                close = t.replace("_open", "_close")
                depth = 0
                while i < len(tokens):
                    if tokens[i].type == t:
                        depth += 1
                    elif tokens[i].type == close:
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
            i += 1
        else:
            i += 1
    return out, errors


def _element_key(el):
    """Comparable identity of an element for sequence alignment."""
    if el["type"].startswith("opaque"):
        return ("opaque", el.get("hash") or _sha256_str(el.get("raw", "")))
    return (el["type"], el["text"], el["sig"])


def _write_sidecar(md_output_path, payload):
    path = md_output_path + SIDECAR_SUFFIX
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def _export_html_snapshot(drive_service, docs_service, file_id):
    """Export HTML and read the doc under a revision sandwich.

    Returns (html_bytes, doc) where doc's revisionId is proven equal before
    and after the export (retry up to 3, then error) — so the HTML and the
    Document resource describe the same revision (codex sync-r1 #5).
    """
    for _ in range(3):
        r1 = docs_service.documents().get(
            documentId=file_id, fields="revisionId").execute()["revisionId"]
        data = drive_service.files().export(
            fileId=file_id, mimeType="text/html").execute()
        doc = _safe_get_doc(docs_service, file_id)
        if doc.get("revisionId") == r1:
            return data, doc
    raise RuntimeError("doc is being edited concurrently (export snapshot "
                       "unstable after 3 attempts) — retry later")


def _build_sidecar(creds, file_id, md_output_path, md_text, doc=None):
    """Build + write the merge-base sidecar next to a downloaded markdown.

    `doc` must be the SAME snapshot the markdown was generated from
    (revision-sandwiched by the caller); reading independently here would
    race (codex sync-r1 #5).
    """
    if doc is None:
        doc = _safe_get_doc(get_docs_service(creds), file_id)
    payload = _sidecar_payload(file_id, md_output_path, md_text, doc)
    return _write_sidecar(md_output_path, payload)


def _sidecar_payload(file_id, md_output_path, md_text, doc):
    """Build the sidecar payload from a held doc snapshot.

    Verifies element-by-element that the doc-derived sequence and the
    md-derived sequence agree (type + text). On any disagreement the payload
    carries sync_supported=False with the reason — sync then refuses with a
    clear message (fail closed at download time).
    """
    tabs = _collect_tabs(doc)
    payload = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "doc_id": file_id,
        "revision_id": doc.get("revisionId"),
        "tab_count": len(tabs),
        "tab_id": tabs[0][0] if tabs else None,
        "md_path": os.path.abspath(md_output_path),
        "md_sha256": _sha256_str(md_text),
        "sync_supported": True,
        "reason": None,
        "elements": [],
    }
    if len(tabs) > 1:
        payload["sync_supported"] = False
        payload["reason"] = "multi-tab document (sync v1 is single-tab only)"
        return payload

    doc_els = _doc_elements(tabs[0][2])
    md_els, md_errors = _md_elements(md_text)

    # Verify correspondence: same length, same (type, text) pairwise.
    # Opaque elements pair with opaque-md blocks positionally.
    mismatch = None
    if len(doc_els) != len(md_els):
        mismatch = (f"element count differs: doc={len(doc_els)} "
                    f"md={len(md_els)}")
    else:
        for k, (d, m) in enumerate(zip(doc_els, md_els)):
            d_opaque = d["type"] == "opaque"
            m_opaque = m["type"] == "opaque-md"
            if d_opaque != m_opaque:
                mismatch = f"element {k}: doc {d['type']} vs md {m['type']}"
                break
            if d_opaque:
                continue
            if d["type"] != m["type"] or _norm_ws(d["text"]) != _norm_ws(m["text"]):
                mismatch = (f"element {k} differs: doc ({d['type']!r}, "
                            f"{d['text'][:40]!r}) vs md ({m['type']!r}, "
                            f"{m['text'][:40]!r})")
                break

    if mismatch:
        payload["sync_supported"] = False
        payload["reason"] = mismatch
    # Store BOTH representations per element: doc side (text/type/sig) and
    # the raw md block (base for local-change detection).
    elements = []
    for k, d in enumerate(doc_els):
        entry = {k2: d[k2] for k2 in d if k2 not in ("start", "end")}
        if not mismatch:
            m = md_els[k]
            entry["raw_md"] = m.get("raw", "")
            # md-derived signature: the base for LOCAL style-change detection
            # (local md sig is comparable to md_sig, not to the doc sig)
            entry["md_sig"] = m.get("sig")
            # style_verified: doc-derived and md-derived style signatures
            # agree, so style-only diffs against this element are meaningful.
            # When they disagree (an unconvertible style), sync must fail
            # closed on local style changes to it — applying would strip
            # doc styling, ignoring would silently drop the user's edit.
            entry["style_verified"] = (
                d["type"] != "opaque"
                and m.get("sig") == d.get("sig"))
        elements.append(entry)
    payload["elements"] = elements
    if md_errors:
        payload["md_notes"] = md_errors
    return payload


def _diff_status(base_keys, other_keys):
    """Three-way building block: align base against one side.

    Returns (status, inserts, mapping):
      status:  base index -> "equal" | "changed" | "deleted"
      inserts: gap g (insert BEFORE base index g; g == len(base) = at end)
               -> list of other-side indices
      mapping: base index -> other index (for equal/changed pairs)
    """
    sm = difflib.SequenceMatcher(a=base_keys, b=other_keys, autojunk=False)
    status, inserts, mapping = {}, {}, {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for off in range(i2 - i1):
                status[i1 + off] = "equal"
                mapping[i1 + off] = j1 + off
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for off in range(n):
                status[i1 + off] = "changed"
                mapping[i1 + off] = j1 + off
            for k in range(i1 + n, i2):
                status[k] = "deleted"
            if (j2 - j1) > n:
                inserts.setdefault(i2, []).extend(range(j1 + n, j2))
        elif tag == "delete":
            for k in range(i1, i2):
                status[k] = "deleted"
        elif tag == "insert":
            inserts.setdefault(i1, []).extend(range(j1, j2))
    return status, inserts, mapping


def _utf16_len(s):
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


_KIND_TO_NAMED_STYLE = {**{f"h{i}": f"HEADING_{i}" for i in range(1, 7)},
                        "p": "NORMAL_TEXT", "li": "NORMAL_TEXT"}


def _style_requests_for_block(el, start_index):
    """Build style requests for a block whose PLAIN text sits at start_index.

    Returns a list of batchUpdate requests: namedStyleType, bullets,
    bold/italic/link spans (narrow field masks).
    """
    reqs = []
    text = el["text"]
    end_index = start_index + _utf16_len(text)
    para_range = {"startIndex": start_index, "endIndex": end_index + 1}
    reqs.append({"updateParagraphStyle": {
        "range": para_range,
        "paragraphStyle": {"namedStyleType": _KIND_TO_NAMED_STYLE[el["type"]]},
        "fields": "namedStyleType",
    }})
    if el["type"] == "li":
        reqs.append({"createParagraphBullets": {
            "range": para_range,
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
        }})
    else:
        reqs.append({"deleteParagraphBullets": {"range": para_range}})
    # Reset inline styles over the whole block, then apply spans
    if text:
        reqs.append({"updateTextStyle": {
            "range": {"startIndex": start_index, "endIndex": end_index},
            "textStyle": {"bold": False, "italic": False, "link": None},
            "fields": "bold,italic,link",
        }})
    offset = start_index
    for span_text, marks in json.loads(el["sig"]):
        span_len = _utf16_len(span_text)
        if marks:
            ts, fields = {}, []
            for m in marks:
                if m == "bold":
                    ts["bold"] = True
                    fields.append("bold")
                elif m == "italic":
                    ts["italic"] = True
                    fields.append("italic")
                elif m.startswith("link:"):
                    ts["link"] = {"url": m[5:]}
                    fields.append("link")
            if fields:
                reqs.append({"updateTextStyle": {
                    "range": {"startIndex": offset,
                              "endIndex": offset + span_len},
                    "textStyle": ts,
                    "fields": ",".join(sorted(set(fields))),
                }})
        offset += span_len
    return reqs


def sync_doc(file_id, md_path, tab_id=None):
    """Three-way merge of an edited local markdown back into a Google Doc.

    Only base->local changes are applied. Anything changed on both sides —
    text, structural type, or STYLE — is a conflict and aborts the whole
    sync with a report (no partial application of a conflicted plan).
    Safety contract: PLAN.md W5 + codex sync-r1 fixes #1-#10.
    """
    file_id = _extract_doc_id(file_id)
    if not os.path.exists(md_path):
        _error(f"file not found: {md_path}")
    sidecar_path = md_path + SIDECAR_SUFFIX
    if not os.path.exists(sidecar_path):
        _error(
            f"no sidecar found ({sidecar_path}). sync requires a merge base "
            f"written by `download --format md`. Re-download the doc, apply "
            f"your edits to the fresh copy, then sync."
        )
    with open(sidecar_path, "r", encoding="utf-8") as f:
        base = json.load(f)
    # identity validation (codex sync-r1 #10)
    for field in ("schema_version", "doc_id", "revision_id", "tab_count",
                  "elements", "md_path", "md_sha256"):  # schema v2 adds doc_fp
        if field not in base:
            _error(f"sidecar is missing field {field!r} — re-download the doc")
    if base["schema_version"] != SIDECAR_SCHEMA_VERSION:
        _error(f"sidecar schema {base['schema_version']} unsupported — "
               f"re-download the doc to get a fresh merge base")
    if base["doc_id"] != file_id:
        _error(f"sidecar belongs to doc {base['doc_id']}, not {file_id}")
    if not base.get("sync_supported"):
        _error(f"sync unsupported for this doc: {base.get('reason')}")
    if os.path.abspath(md_path) != base["md_path"]:
        _warn(
            f"sidecar was written for {base['md_path']}; syncing "
            f"{os.path.abspath(md_path)} — make sure it is the same "
            f"document's markdown"
        )

    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    local_md_sha = _sha256_str(md_text)

    local_els, md_errors = _md_elements(md_text)
    base_els = base["elements"]

    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
        docs_service = get_docs_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    doc = _safe_get_doc(docs_service, file_id)
    tabs = _collect_tabs(doc)
    if len(tabs) > 1:
        _error("multi-tab document: sync v1 is single-tab only")
    tid, doc_tab = tabs[0][0], tabs[0][2]
    _refuse_on_suggestions(doc_tab)
    remote_els = _doc_elements(doc_tab)

    # --- alignment keys ---
    # Alignment is by (type, normalized text). REMOTE change detection
    # additionally includes the style signature so a collaborator's
    # style-only edit registers as a remote change (codex sync-r1 #1).
    def k_base_local(el):
        if el["type"] == "opaque":
            return ("opaque", _norm_ws(el.get("raw_md", "")))
        return (el["type"], _norm_ws(el["text"]))

    def k_local(el):
        if el["type"] == "opaque-md":
            return ("opaque", _norm_ws(el.get("raw", "")))
        return (el["type"], _norm_ws(el["text"]))

    def k_base_remote(el):
        # STABLE identity only — style state must never enter alignment
        # keys, or a remote style edit destroys the paragraph's identity
        # and derails the whole diff (codex sync-r2 #1)
        if el["type"] == "opaque":
            return ("opaque", el.get("hash", ""))
        return (el["type"], _norm_ws(el["text"]))

    base_lkeys = [k_base_local(e) for e in base_els]
    local_keys = [k_local(e) for e in local_els]
    base_rkeys = [k_base_remote(e) for e in base_els]
    remote_keys = [k_base_remote(e) for e in remote_els]

    l_status, l_inserts, l_map = _diff_status(base_lkeys, local_keys)
    r_status, r_inserts, r_map = _diff_status(base_rkeys, remote_keys)

    # Post-alignment fingerprint pass: a remotely aligned-equal paragraph
    # whose FULL Docs state changed (any style property) is a remote change
    # (codex sync-r2 #1+#2).
    for i in list(r_status):
        if r_status[i] == "equal" and base_els[i].get("type") != "opaque":
            if base_els[i].get("doc_fp") != remote_els[r_map[i]].get("doc_fp"):
                r_status[i] = "changed"

    # --- duplicate-ambiguity guard on BOTH sides (codex sync-r1 #6) ---
    from collections import Counter

    def _dup_guard(bkeys, okeys, status, inserts, side):
        # duplicates WITHIN a sequence are ambiguous; the same key appearing
        # once in base and once in the other side is just an unchanged
        # paragraph
        dups = ({k for k, n in Counter(bkeys).items() if n > 1}
                | {k for k, n in Counter(okeys).items() if n > 1})
        if not dups:
            return
        for i, k in enumerate(bkeys):
            if k in dups and status.get(i, "equal") != "equal":
                _error(
                    f"duplicate paragraphs are involved in {side} changes "
                    f"(base element {i}) — alignment would be ambiguous; "
                    f"edit in the UI"
                )
        for _g, idxs in inserts.items():
            for j in idxs:
                if okeys[j] in dups:
                    _error(
                        f"a {side}-inserted paragraph duplicates an existing "
                        f"one — alignment would be ambiguous; edit in the UI"
                    )

    _dup_guard(base_lkeys, local_keys, l_status, l_inserts, "local")
    _dup_guard(base_rkeys, remote_keys, r_status, r_inserts, "remote")

    # --- plan + conflict matrix (one pass) ---
    conflicts, unsupported = [], []
    replaced, deleted, inserted, style_only = [], [], [], []
    for i, bel in enumerate(base_els):
        ls = l_status.get(i, "equal")
        rs = r_status.get(i, "equal")
        if ls != "equal" and rs != "equal":
            conflicts.append({
                "base_index": i,
                "base_text": (bel.get("text") or bel.get("kind", ""))[:80],
                "local": ls, "remote": rs,
            })
            continue
        if bel["type"] == "opaque":
            if ls != "equal":
                unsupported.append(
                    f"opaque block changed locally (element {i}, "
                    f"{bel.get('kind', 'md')}) — edit tables/complex blocks "
                    f"in the UI")
            continue
        if ls == "deleted":
            deleted.append(i)
        elif ls == "changed":
            lel = local_els[l_map[i]]
            if lel["type"] == "opaque-md":
                unsupported.append(
                    f"paragraph {i} was replaced with an unsupported md "
                    f"construct locally: {lel.get('raw', '')[:60]!r}")
            else:
                replaced.append((i, lel))
        else:
            # text-equal locally: check for a local STYLE change against the
            # md-derived base signature (codex sync-r1 #1)
            lel = local_els[l_map[i]] if i in l_map else None
            eff_md_sig = bel.get("md_sig", bel.get("sig"))
            if lel and lel.get("sig") is not None and lel["sig"] != eff_md_sig:
                if rs != "equal":
                    conflicts.append({
                        "base_index": i,
                        "base_text": (bel.get("text") or "")[:80],
                        "local": "style", "remote": rs,
                    })
                elif not bel.get("style_verified"):
                    unsupported.append(
                        f"local style change on element {i} cannot be "
                        f"applied safely (doc/md style correspondence was "
                        f"not verified at download) — edit styling in the UI")
                else:
                    style_only.append((i, lel))
    for g, idxs in l_inserts.items():
        if g in r_inserts:
            conflicts.append({"gap": g, "local": "insert", "remote": "insert",
                              "note": "both sides inserted at the same boundary"})
            continue
        for j in idxs:
            lel = local_els[j]
            if lel["type"] == "opaque-md":
                unsupported.append(
                    f"inserted block is an unsupported md construct: "
                    f"{lel.get('raw', '')[:60]!r}")
            else:
                inserted.append((g, lel))
    if conflicts:
        print(json.dumps({
            "error": "sync conflicts — nothing was applied",
            "conflicts": conflicts,
            "hint": "re-download the doc, re-apply your edits, then sync",
        }, ensure_ascii=False, indent=2))
        sys.exit(2)
    if unsupported:
        print(json.dumps({
            "error": "unsupported constructs in changed zones — nothing "
                     "was applied",
            "details": unsupported,
            "md_notes": md_errors,
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    if not (replaced or deleted or inserted or style_only):
        print(json.dumps({
            "action": "synced", "noop": True, "advanced": False,
            "note": "no local changes to apply",
        }, ensure_ascii=False))
        return

    # --- gates: commented docs (W8 mapping not yet integrated into sync) ---
    all_comments, anchored = _census_comments(drive_service, file_id)
    if anchored and (replaced or deleted):
        _error(
            f"doc has {len(anchored)} anchored comment(s); paragraph "
            f"replaces/deletes via sync are not yet anchor-mapped — use "
            f"/gdocs-patch (it has export-based anchor protection) or the "
            f"UI. Inserts and style-only changes are allowed."
        )

    # --- map plan to remote positions ---
    def remote_el(i):
        return remote_els[r_map[i]]

    body_content = (doc_tab.get("body", {}) or {}).get("content", [])
    body_end = body_content[-1]["endIndex"] if body_content else 2

    action = {}
    for i, lel in replaced:
        action[r_map[i]] = ("replace", lel)
    for i in deleted:
        action[r_map[i]] = ("delete",)
    so_remote = {r_map[i]: lel for i, lel in style_only}

    insert_before, end_inserts = {}, []
    by_gap = {}
    for g, lel in inserted:
        by_gap.setdefault(g, []).append(lel)
    for g in sorted(by_gap):
        target = None
        for k in range(g, len(base_els)):
            if r_status.get(k, "equal") != "deleted" and k in r_map:
                target = r_map[k]
                break
        if target is None:
            end_inserts.extend(by_gap[g])
        else:
            insert_before.setdefault(target, []).extend(by_gap[g])

    # --- expected merged sequence + style slots (codex sync-r1 #2) ---
    expected, style_slots = [], []

    def _entry_para(text):
        return ("para", _norm_ws(text))

    for j, rel in enumerate(remote_els):
        for lel in insert_before.get(j, []):
            style_slots.append((len(expected), lel))
            expected.append(_entry_para(lel["text"]))
        a = action.get(j)
        if a and a[0] == "delete":
            continue
        if a and a[0] == "replace":
            style_slots.append((len(expected), a[1]))
            expected.append(_entry_para(a[1]["text"]))
        elif rel["type"] == "opaque":
            expected.append(("opaque", rel.get("hash", "")))
        else:
            if j in so_remote:
                style_slots.append((len(expected), so_remote[j]))
            expected.append(_entry_para(rel["text"]))
    for lel in end_inserts:
        style_slots.append((len(expected), lel))
        expected.append(_entry_para(lel["text"]))

    # --- text requests ---
    text_requests = []
    for i, lel in replaced:
        rel = remote_el(i)
        t_start = rel["start"]
        t_end = t_start + _utf16_len(rel["text"])
        text_requests.append((t_start, [
            {"deleteContentRange": {"range": {"startIndex": t_start,
                                              "endIndex": t_end}}},
            {"insertText": {"location": {"index": t_start},
                            "text": lel["text"]}},
        ]))
    # deletes: coalesce contiguous ranges; apply the final-newline
    # adjustment ONCE per trailing run (codex sync-r1 #7)
    del_ranges = sorted((remote_el(i)["start"], remote_el(i)["end"])
                        for i in deleted)
    merged = []
    for s, e in del_ranges:
        if merged and merged[-1][1] == s:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    for s, e in merged:
        if e >= body_end:
            if s <= 1:
                s2, e2 = 1, body_end - 1  # empty the doc, keep final newline
            else:
                s2, e2 = s - 1, e - 1
        else:
            s2, e2 = s, e
        if s2 < e2:
            text_requests.append((s2, [
                {"deleteContentRange": {"range": {"startIndex": s2,
                                                  "endIndex": e2}}},
            ]))
    for j, lels in insert_before.items():
        pos = remote_els[j]["start"]
        joined = "".join(lel["text"] + "\n" for lel in lels)
        text_requests.append((pos, [
            {"insertText": {"location": {"index": pos}, "text": joined}},
        ]))
    if end_inserts:
        joined = "\n" + "".join(lel["text"] + "\n" for lel in end_inserts)[:-1]
        text_requests.append((body_end - 1, [
            {"insertText": {"location": {"index": body_end - 1},
                            "text": joined}},
        ]))

    # --- journal skeleton (codex sync-r1 #8) ---
    revision_id = doc.get("revisionId")
    journal = {
        "doc_id": file_id,
        "revision_before": revision_id,
        "plan": {
            "replaced": [{"base_index": i, "text": lel["text"][:80]}
                         for i, lel in replaced],
            "deleted": [{"base_index": i,
                         "text": (base_els[i].get("text") or "")[:80]}
                        for i in deleted],
            "inserted": [{"gap": g, "text": lel["text"][:80]}
                         for g, lel in inserted],
            "style_only": [{"base_index": i} for i, _ in style_only],
        },
        "phases": [],
    }

    def _fail_partial(reason, extra=None):
        journal["phases"].append({"phase": "failed", "reason": reason,
                                  **(extra or {})})
        jp = None
        try:
            jp = _write_journal(md_path, journal)
        except Exception as je:
            reason += f" (journal write also failed: {je})"
        print(json.dumps({
            "action": "sync-partial-failure",
            "error": reason,
            "journal": jp,
            "advanced": False,
            "note": "local md and sidecar NOT advanced; verify the doc, "
                    "then re-download before the next sync",
        }, ensure_ascii=False, indent=2))
        sys.exit(3)

    # --- apply text changes (single atomic batch) ---
    text_requests.sort(key=lambda pair: pair[0], reverse=True)
    flat = [req for _, reqs in text_requests for req in reqs]
    rev_after_text = revision_id
    if flat:
        try:
            resp = docs_service.documents().batchUpdate(
                documentId=file_id,
                body={"requests": flat,
                      "writeControl": _write_control(revision_id)},
            ).execute()
        except HttpError as e:
            # atomic: nothing applied — plain error, no journal needed
            _error(
                f"sync text batch failed (nothing applied — atomic): "
                f"{e.reason if hasattr(e, 'reason') else e}"
            )
        except Exception as e:
            # transport failure after send: outcome ambiguous — journal it
            journal["phases"].append({"phase": "text-batch",
                                      "status": "unknown", "error": str(e)})
            _fail_partial(f"text batch outcome unknown (transport): {e}")
        rev_after_text = (resp.get("writeControl") or {}).get(
            "requiredRevisionId") or rev_after_text
        journal["phases"].append({"phase": "text-batch", "status": "ok",
                                  "requests": len(flat)})

    # --- positional verification against the expected merged sequence ---
    try:
        doc2 = _safe_get_doc(docs_service, file_id)
    except Exception as e:
        _fail_partial(f"cannot re-read doc after text batch: {e}")
    rev2 = doc2.get("revisionId")
    journal["revision_after_text"] = rev2
    if rev2 != rev_after_text:
        # a collaborator edit landed between our text batch and this read;
        # a style-only edit would pass text verification but our style
        # batch would then overwrite it (codex sync-r2 #3) — fail closed
        _fail_partial(
            f"doc revision moved right after the text batch "
            f"({rev_after_text} -> {rev2}) — concurrent edit; styles not "
            f"applied")
    _tid2, doc_tab2 = _select_tab(doc2, tab_id=None)
    fresh_els = _doc_elements(doc_tab2)

    def _entry_actual(el):
        if el["type"] == "opaque":
            return ("opaque", el.get("hash", ""))
        return ("para", _norm_ws(el["text"]))

    actual = [_entry_actual(e) for e in fresh_els]
    if actual != expected:
        diverge = next((k for k, (a, b) in enumerate(zip(actual, expected))
                        if a != b), min(len(actual), len(expected)))
        _fail_partial(
            f"post-apply verification failed: doc diverges from the "
            f"expected merged sequence at element {diverge} "
            f"(len {len(actual)} vs {len(expected)}) — a concurrent edit "
            f"landed mid-sync",
            {"diverge_at": diverge})

    # --- style pass: positions taken positionally from fresh_els ---
    styled = 0
    style_reqs = []
    for k, lel in style_slots:
        style_reqs.extend(
            _style_requests_for_block(lel, fresh_els[k]["start"]))
        styled += 1
    if style_reqs:
        try:
            docs_service.documents().batchUpdate(
                documentId=file_id,
                body={"requests": style_reqs,
                      "writeControl": _write_control(rev2)},
            ).execute()
        except Exception as e:
            _fail_partial(f"style batch failed: {e}")
        journal["phases"].append({"phase": "style-batch", "status": "ok",
                                  "blocks": styled})

    # --- lifecycle: advance md + sidecar to the merged remote state ---
    advanced, advance_error, recovery = False, None, None
    md_advanced = False
    try:
        from markdownify import markdownify as md_convert
        html, doc3 = _export_html_snapshot(drive_service, docs_service,
                                           file_id)
        html = _prepare_export_html(
            html.decode("utf-8") if isinstance(html, bytes) else html)
        new_md = md_convert(html, heading_style="ATX", strip=["style"])
        new_md = re.sub(r'\n{3,}', '\n\n', new_md).lstrip('\n')
        new_md = new_md.replace(" ", " ")

        payload = _sidecar_payload(file_id, md_path, new_md, doc3)
        # crash-safe: build BOTH temps first; only then check the local file
        # hash IMMEDIATELY before the first rename (codex sync-r2 #4) and
        # commit md -> sidecar, journaling each subphase (codex sync-r2 #6)
        tmp_md = md_path + ".tmp"
        tmp_sc = sidecar_path + ".tmp"
        with open(tmp_md, "w", encoding="utf-8") as f:
            f.write(new_md)
        with open(tmp_sc, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        with open(md_path, "r", encoding="utf-8") as f:
            current_md = f.read()
        if _sha256_str(current_md) != local_md_sha:
            recovery = md_path + ".merged.md"
            os.replace(tmp_md, recovery)
            os.unlink(tmp_sc)
            journal["phases"].append({
                "phase": "advance", "status": "skipped-local-file-edited",
                "recovery": recovery})
            _write_journal(md_path, journal)
        else:
            os.replace(tmp_md, md_path)
            md_advanced = True
            journal["phases"].append({"phase": "advance-md", "status": "ok"})
            os.replace(tmp_sc, sidecar_path)
            journal["phases"].append({"phase": "advance-sidecar",
                                      "status": "ok"})
            advanced = True
    except Exception as e:
        advance_error = str(e)
        journal["phases"].append({"phase": "advance", "status": "failed",
                                  "error": advance_error,
                                  "md_advanced": md_advanced})
        try:
            _write_journal(md_path, journal)
        except Exception:
            pass

    result = {
        "action": "synced",
        "doc_id": file_id,
        "replaced": len(replaced),
        "inserted": len(inserted),
        "deleted": len(deleted),
        "style_only": len(style_only),
        "styled_blocks": styled,
        "comments_on_doc": len(all_comments),
        "advanced": advanced,
    }
    if recovery:
        result["note"] = (
            f"local md was edited during sync — NOT overwritten; merged "
            f"doc state saved to {recovery}; re-download before next sync")
    if advance_error:
        if md_advanced:
            result["md_advanced"] = True
            result["sidecar_advanced"] = False
            result["advance_error"] = (
                f"{advance_error} — local md WAS advanced but the sidecar "
                f"was NOT (stale base!); re-download before the next sync")
        else:
            result["advance_error"] = (
                f"{advance_error} — local md/sidecar NOT advanced; "
                f"re-download before the next sync")
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Update existing document
# ---------------------------------------------------------------------------

def update_doc(file_id, file_path, title=None, no_highlights=False,
               acknowledge_loss=False):
    """Update an existing Google Doc with new markdown content.

    DESTRUCTIVE: drive.files().update with media replaces the whole document.
    Confirmed behavior: ALL comments become invisible ghosts in the UI (alive
    in the API only) and named ranges are destroyed. Blocked when the doc has
    any comment or named range unless --acknowledge-loss is passed, in which
    case a backup copy is made first (note: per C0, the backup does NOT
    preserve comments either — only text and styles).
    """
    file_id = _extract_doc_id(file_id)

    if not os.path.exists(file_path):
        _error(f"file not found: {file_path}")

    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
        docs_service = get_docs_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    # Get current file metadata (folder for images)
    try:
        meta = drive_service.files().get(
            fileId=file_id, fields="name,parents", supportsAllDrives=True
        ).execute()
    except HttpError as e:
        _error(f"cannot access file: {e.reason if hasattr(e, 'reason') else e}")

    folder_id = (meta.get("parents") or [None])[0]

    # ---- destructiveness guard (fail closed) ----
    all_comments, anchored = _census_comments(drive_service, file_id)
    named_ranges = []
    try:
        cur_doc = _safe_get_doc(docs_service, file_id)
        for _tid, _title, dt in _collect_tabs(cur_doc):
            named_ranges.extend((dt.get("namedRanges") or {}).keys())
    except HttpError as e:
        _error(
            f"cannot read doc to check named ranges (fail closed): "
            f"{e.reason if hasattr(e, 'reason') else e}"
        )

    backup_info = None
    if all_comments or named_ranges:
        if not acknowledge_loss:
            print(json.dumps({
                "error": "destructive update blocked",
                "reason": (
                    "full replace via drive.files().update makes ALL comments "
                    "invisible ghosts in the UI and destroys named ranges. "
                    "Use `patch` for iterative edits. To proceed anyway, rerun "
                    "with --acknowledge-loss (a backup copy will be created "
                    "first, but per C0 it will NOT contain the comments)."
                ),
                "comments": len(all_comments),
                "anchored_comments": len(anchored),
                "named_ranges": sorted(named_ranges),
            }, ensure_ascii=False, indent=2))
            sys.exit(2)
        # Acknowledged: back up first, verify placement, report id BEFORE
        # destruction (codex code review #8: pin and verify parents).
        backup_body = {"name": f"{meta.get('name', 'doc')}.pre-update-backup-"
                               f"{time.strftime('%Y%m%d-%H%M%S')}"}
        if folder_id:
            backup_body["parents"] = [folder_id]
        try:
            backup = drive_service.files().copy(
                fileId=file_id,
                body=backup_body,
                fields="id,name,parents",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            _error(
                f"backup copy failed — update aborted: "
                f"{e.reason if hasattr(e, 'reason') else e}"
            )
        if not backup.get("id"):
            _error("backup copy returned no id — update aborted")
        if folder_id and folder_id not in (backup.get("parents") or []):
            _error(
                f"backup {backup['id']} landed outside the doc's folder "
                f"(parents={backup.get('parents')}) — update aborted"
            )
        backup_info = {"id": backup["id"], "name": backup.get("name"),
                       "parents": backup.get("parents"),
                       "preserves_comments": False}
        _warn(f"backup created before destructive update: {backup['id']} "
              f"(text/styles only — comments are NOT in the backup)")

    # Prepare markdown: replace image refs with markers
    upload_path, images = _prepare_md_for_upload(file_path)

    try:
        # Update content via Drive API (re-upload as markdown)
        media = MediaFileUpload(upload_path, mimetype="text/markdown", resumable=True)
        update_meta = {}
        if title:
            update_meta["name"] = title
        drive_service.files().update(
            fileId=file_id,
            body=update_meta if update_meta else {},
            media_body=media,
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        _error(f"update failed: {e.reason if hasattr(e, 'reason') else e}")
    finally:
        if upload_path != file_path:
            os.unlink(upload_path)

    # Post-process images
    if images:
        try:
            post_process_images(docs_service, drive_service, file_id, images, folder_id)
        except Exception as e:
            _warn(f"image processing skipped: {e}")

    # Post-process highlights
    if not no_highlights:
        try:
            post_process_highlights(docs_service, file_id)
        except Exception as e:
            _warn(f"highlight post-processing skipped: {e}")

    # Get updated metadata
    try:
        updated = drive_service.files().get(
            fileId=file_id, fields="id,name,webViewLink", supportsAllDrives=True
        ).execute()
    except HttpError:
        updated = {"id": file_id, "name": title or meta.get("name"), "webViewLink": ""}

    out = {
        "id": updated["id"],
        "name": updated.get("name", ""),
        "url": updated.get("webViewLink", ""),
        "action": "updated",
    }
    if backup_info:
        out["backup"] = backup_info
    print(json.dumps(out, ensure_ascii=False))


def _extract_folder_id(val):
    """Extract a Drive folder ID from a URL or return the raw value."""
    if not val:
        return val
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", val)
    if m:
        return m.group(1)
    m = re.search(r"[?&]folder=([a-zA-Z0-9_-]+)", val)
    if m:
        return m.group(1)
    return val.strip()


def upload_file(file_paths, folder_id=None, title=None):
    """Upload arbitrary file(s) to Drive as-is (no Google Doc conversion)."""
    import mimetypes
    EXTRA_MIME = {
        ".7z": "application/x-7z-compressed",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".doc": "application/msword",
        ".xls": "application/vnd.ms-excel",
    }
    if isinstance(file_paths, str):
        file_paths = [file_paths]
    folder_id = _extract_folder_id(folder_id) if folder_id else None

    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    results = []
    for fp in file_paths:
        if not os.path.exists(fp):
            _warn(f"file not found, skipped: {fp}")
            continue
        ext = os.path.splitext(fp)[1].lower()
        mime = EXTRA_MIME.get(ext) or mimetypes.guess_type(fp)[0] or "application/octet-stream"
        name = title if (title and len(file_paths) == 1) else os.path.basename(fp)
        meta = {"name": name}
        if folder_id:
            meta["parents"] = [folder_id]
        try:
            media = MediaFileUpload(fp, mimetype=mime, resumable=True)
            f = drive_service.files().create(
                body=meta, media_body=media, fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()
            results.append({"id": f["id"], "name": f["name"], "url": f.get("webViewLink")})
        except HttpError as e:
            _warn(f"upload failed for {fp}: {e.reason if hasattr(e, 'reason') else e}")
    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Google Docs toolkit for Claude Code")
    sub = parser.add_subparsers(dest="command")

    # upload command
    up = sub.add_parser("upload", help="Upload .md file as Google Doc")
    up.add_argument("file", help="Path to .md file")
    up.add_argument("--folder", help="Google Drive folder ID")
    up.add_argument("--title", help="Document title (default: filename without extension)")
    up.add_argument("--no-highlights", action="store_true", help="Skip highlight post-processing")

    # comments command
    cm = sub.add_parser("comments", help="List comments on a Google Doc")
    cm.add_argument("file_id", help="Google Doc file ID or URL")

    # reply command
    rp = sub.add_parser("reply", help="Post a reply to a comment")
    rp.add_argument("file_id", help="Google Doc file ID or URL")
    rp.add_argument("comment_id", help="Comment ID to reply to")
    rp.add_argument("text", help="Reply text")
    rp.add_argument("--resolve", action="store_true",
                    help="Also mark the thread as resolved")

    # resolve command
    rs = sub.add_parser("resolve", help="Resolve a comment thread")
    rs.add_argument("file_id", help="Google Doc file ID or URL")
    rs.add_argument("comment_id", help="Comment ID to resolve")
    rs.add_argument("--text", default=None,
                    help="Optional note (default: 'Resolved.')")

    # comment command (document-level, unanchored)
    cc = sub.add_parser("comment", help="Create a document-level comment (no anchor)")
    cc.add_argument("file_id", help="Google Doc file ID or URL")
    cc.add_argument("text", help="Comment text")

    # patch command (structural edits with comment preflight)
    pt = sub.add_parser("patch", help="Apply structural edits to a Google Doc")
    pt.add_argument("file_id", help="Google Doc file ID or URL")
    pt.add_argument("ops", help="Path to ops.json with list of operations")
    pt.add_argument("--tab", dest="tab_id", default=None,
                    help="Tab ID (required if doc has multiple tabs)")

    # mark command (named ranges)
    mk = sub.add_parser("mark", help="Create a named range around a text fragment")
    mk.add_argument("file_id", help="Google Doc file ID or URL")
    mk.add_argument("name", help="Named range identifier")
    mk.add_argument("--quote", required=True, help="Text fragment to wrap")
    mk.add_argument("--tab", dest="tab_id", default=None,
                    help="Tab ID (required if doc has multiple tabs)")
    mk.add_argument("--occurrence", type=int, default=1,
                    help="1-based match index if quote is non-unique (default: 1)")

    # download command
    dl = sub.add_parser("download", help="Download a Google Doc")
    dl.add_argument("file_id", help="Google Doc file ID or URL")
    dl.add_argument("--format", default="md", choices=list(EXPORT_MIMETYPES.keys()),
                    help="Export format (default: md)")
    dl.add_argument("--output", help="Output file path (default: auto from title)")
    dl.add_argument("--images-dir", help="Directory for images (md format only)")

    # suggestions command
    sg = sub.add_parser("suggestions", help="List suggestions on a Google Doc")
    sg.add_argument("file_id", help="Google Doc file ID or URL")

    # sync command (three-way merge)
    sy = sub.add_parser("sync", help="Three-way merge local .md into a Google Doc")
    sy.add_argument("file_id", help="Google Doc file ID or URL")
    sy.add_argument("file", help="Path to edited .md (sidecar must sit next to it)")

    # update command
    upd = sub.add_parser("update", help="Update an existing Google Doc")
    upd.add_argument("file_id", help="Google Doc file ID or URL")
    upd.add_argument("file", help="Path to .md file with new content")
    upd.add_argument("--title", help="New document title")
    upd.add_argument("--no-highlights", action="store_true",
                     help="Skip highlight post-processing")
    upd.add_argument("--acknowledge-loss", action="store_true",
                     help="Proceed despite comments/named ranges being destroyed "
                          "(a text-only backup copy is created first)")

    # upload-file command (raw upload, any file type, no conversion)
    uf = sub.add_parser("upload-file", help="Upload file(s) as-is (no Google Doc conversion)")
    uf.add_argument("file", nargs="+", help="Path(s) to file(s)")
    uf.add_argument("--folder", help="Google Drive folder ID or URL")
    uf.add_argument("--title", help="Name override (only when uploading a single file)")

    args = parser.parse_args()
    if args.command == "upload":
        upload_md(args.file, folder_id=args.folder, title=args.title,
                  no_highlights=args.no_highlights)
    elif args.command == "upload-file":
        upload_file(args.file, folder_id=args.folder, title=args.title)
    elif args.command == "comments":
        list_comments(_extract_doc_id(args.file_id))
    elif args.command == "reply":
        reply_comment(args.file_id, args.comment_id, args.text, resolve=args.resolve)
    elif args.command == "resolve":
        resolve_comment(args.file_id, args.comment_id, text=args.text)
    elif args.command == "comment":
        create_comment(args.file_id, args.text)
    elif args.command == "mark":
        mark_range(args.file_id, args.name, args.quote,
                   tab_id=args.tab_id, occurrence=args.occurrence)
    elif args.command == "patch":
        patch_doc(args.file_id, args.ops, tab_id=args.tab_id)
    elif args.command == "download":
        download_doc(args.file_id, fmt=args.format, output=args.output,
                     images_dir=args.images_dir)
    elif args.command == "suggestions":
        list_suggestions(args.file_id)
    elif args.command == "sync":
        sync_doc(args.file_id, args.file)
    elif args.command == "update":
        update_doc(args.file_id, args.file, title=args.title,
                   no_highlights=args.no_highlights,
                   acknowledge_loss=args.acknowledge_loss)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
