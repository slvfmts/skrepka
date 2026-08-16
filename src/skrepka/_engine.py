#!/usr/bin/env python3
"""Google Docs toolkit: upload, download, update, comments, suggestions."""

import argparse
import datetime
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
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from skrepka import config, safeio

SCOPES = [
    # `drive` alone authorizes every call skrepka makes. Docs documents.get and
    # documents.batchUpdate list `drive` as an accepted scope; the Drive
    # comments/replies/permissions endpoints REQUIRE a Drive scope and do NOT
    # accept the narrower `documents` scope at all. So `documents` grants
    # nothing skrepka uses — least privilege keeps this to a single scope
    # (verified against Google docs + cross-model review, 2026-07-20). Do not
    # re-add `documents`. `drive.file` is not an option either: it only reaches
    # app-created or user-picked files, never arbitrary docs opened by ID/URL.
    "https://www.googleapis.com/auth/drive",
]

# Light indigo background for :::highlight blocks (#EEF2FF)
HIGHLIGHT_COLOR = {"red": 0.933, "green": 0.945, "blue": 1.0}
HIGHLIGHT_PADDING = {"magnitude": 6, "unit": "PT"}

# --- Characterization gates (phase 0, docs/FINDINGS.md) ---
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


def _isatty(stream):
    """True only for a stream that is definitely a terminal. A missing or
    broken stdin/stderr (pythonw, a closed fd, a detached service) must read
    as 'no terminal' rather than blow up inside a gate."""
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _require_consent(operation, confirmed, remedy):
    """Cooperative gate for operations that are the person's decision, not the
    agent's. The explicit flag IS the confirmation, the same way --yes works
    for revoke/forget in privacy.py.

    It used to demand a TTY. Measured (#17): a subprocess launched by an agent
    has no TTY on stdin/stderr and no /dev/tty at all, so the demand made the
    legitimate human path unreachable and left one workaround — a person
    pasting a command an agent composed. A terminal existing was never
    evidence that a human consented.

    This is NOT a security boundary: an agent can pass the flag. What forbids
    it is the agent contract (agents/CONTRACT.md), and an agent holding the
    OAuth token could bypass this CLI entirely anyway. `remedy` says what to
    do instead, and is addressed to the agent.
    """
    if confirmed:
        return
    if _isatty(sys.stdin) and _isatty(sys.stderr):
        try:
            print(f"Confirm '{operation}'? [y/N]: ",
                  end="", file=sys.stderr, flush=True)
            answer = sys.stdin.readline()
        except (OSError, ValueError):
            answer = ""  # unreadable terminal is not a yes
        if answer.strip().lower() in ("y", "yes"):
            return
        _error(f"'{operation}' was not confirmed — nothing done")
    else:
        # Order matters, measured in an acceptance run: when the human-facing
        # "rerun with --yes" came last, an agent with no contract in context
        # read it as its own instruction and passed the flag. The prohibition
        # goes first and the flag is never the closing sentence.
        _error(
            f"'{operation}' is the person's decision, not the agent's. "
            f"Being asked for it is not permission to pass --yes: that flag "
            f"is the non-interactive path for a person running skrepka from "
            f"their own shell, and an agent acting for a person is not that "
            f"person. If you are that person, rerun with --yes. {remedy}")


def _emit_json(payload, output=None, summary=None):
    """Print payload as JSON, or (with output=PATH) write it to a file and
    print a short receipt instead. Protects agents whose tool output gets
    truncated from acting on a cut-off list (see agents/CONTRACT.md)."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not output:
        print(text)
        return
    # hardened write: refuse a symlinked target or any symlinked parent
    # component, atomic replace (safeio, r3 #9) — an --output path the agent
    # was handed must never overwrite an unrelated file through a symlink
    written_path = safeio.atomic_write(output, text + "\n")
    receipt = {"written": written_path,
               "bytes": len(text.encode("utf-8")) + 1}
    if summary:
        receipt.update(summary)
    print(json.dumps(receipt, ensure_ascii=False))


def get_creds():
    """Load stored credentials, refreshing when possible.

    Interactive OAuth lives ONLY in `skrepka init` (a single human-run entry
    point) — normal commands never open a browser or block on consent
    (plan R2 r1 #6). A present write-ahead commit marker means a prior
    init/reauth crashed mid-activation, so every reader fails closed here
    (r3 #3). The token is stored inside an envelope alongside its scope
    provenance; a refresh rewrites the envelope atomically, preserving
    provenance (r3 #1).
    """
    try:
        config.require_no_pending_init()
        env = config.read_token_envelope()
    except config.PendingInitError as e:
        _error(str(e))
    except config.ConfigError as e:
        _error(str(e))
    if env is None:
        hint = ""
        if config.detect_legacy():
            hint = (" (found an old gdocs-uploader config; skrepka uses a new "
                    "location — run init to set it up here)")
        _error("not signed in — run `skrepka init` first" + hint)

    creds = Credentials.from_authorized_user_info(env["token"], SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                _error(f"could not refresh access — run `skrepka init "
                       f"--reauth`: {e}")
            _persist_refreshed_token(creds, env)
        else:
            _error("stored credentials are not usable — run `skrepka init "
                   "--reauth`")
    return creds


def _persist_refreshed_token(creds, env):
    """Rewrite the token envelope after a refresh, keeping provenance bound
    to the refresh token (r3 #1).

    Compare-and-swap guard (code-r1 #5): a concurrent `init --reauth` may have
    replaced the stored grant while we were refreshing; only persist if the
    on-disk refresh token still matches the one we started from, otherwise our
    freshly minted access token is for a superseded grant — drop it.
    """
    token_dict = json.loads(creds.to_json())
    reduced = False
    # serialize the compare-and-swap against activation so a concurrent reauth
    # cannot interleave between the re-read and the write (code-r2 #1)
    with config.lock():
        try:
            config.require_no_pending_init()
            current = config.read_token_envelope()
        except config.ConfigError:
            return  # a reauth is in flight (marker) or the store changed
        if current is None:
            return  # the grant was removed while we refreshed — do not revive
        started = config.refresh_token_hash(env.get("token") or {})
        on_disk = config.refresh_token_hash(current.get("token") or {})
        if started != on_disk:
            return  # superseded by another process's reauth — keep theirs
        # base provenance on the FRESHEST on-disk copy, not our stale env, so
        # a concurrent refresh cannot roll back a newer attestation (r3 #3)
        prov = dict(current.get("provenance") or {})
        # re-verify granted scopes if the refresh response carried them; an
        # explicitly EMPTY list must overwrite the old set (code-r1 #7/r2 #9)
        granted = getattr(creds, "granted_scopes", None)
        if granted is not None:
            prov["granted_scopes"] = list(granted)
        prov["refresh_token_hash"] = config.refresh_token_hash(token_dict)
        final_scopes = prov.get("granted_scopes")
        # a refresh that returned a REDUCED grant must NEVER be persisted
        # (code-r4 #1): if we wrote it, the next invocation would load the
        # fresh access token as `valid` and return it from get_creds without
        # re-checking scopes — silently accepting a reduced grant. Skip the
        # write and leave the old (now-expired) token on disk so every run
        # re-refreshes and fails closed here until the user reauths (code-r3 #4).
        if final_scopes is not None and not all(s in final_scopes for s in SCOPES):
            reduced = True
        else:
            try:
                config.write_token_envelope(token_dict, prov)
            except config.ConfigError as e:
                _error(str(e))
    if reduced:
        _error("this sign-in no longer grants all the access skrepka needs — "
               "run `skrepka init --reauth`")


def get_drive_service(creds):
    return build("drive", "v3", credentials=creds)


def get_docs_service(creds):
    return build("docs", "v1", credentials=creds)


def _plain_text(content):
    """Concatenate the text of a content list, ignoring indices entirely.

    Headers, footers and footnotes are a SEPARATE index space, and the API
    omits `startIndex` on their first element — so the indexed walker below
    raises on them (measured on a live document 2026-08-05). Anything that
    only needs the words, not the coordinates, uses this.
    """
    out = []
    for element in content:
        if "paragraph" in element:
            for elem in element["paragraph"].get("elements", []):
                tr = elem.get("textRun")
                if tr:
                    out.append(tr.get("content", ""))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    out.append(_plain_text(cell.get("content", [])))
    return "".join(out)


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


_COMMENT_FIELDS = (
    # `content` on replies costs nothing extra here and is what makes the
    # archive of a closed thread a real record of the conversation (r8).
    "nextPageToken,comments(id,content,author/displayName,createdTime,"
    "quotedFileContent,resolved,deleted,anchor,"
    "replies(id,content,createdTime,author/displayName,deleted,action))"
)


def _list_comments_raw(drive_service, file_id):
    """Paginated comments.list carrying every field the accounting AND the
    freshness fingerprint need, deleted entries included. Raises on API
    errors — callers decide between fail-closed and fail-stop."""
    out = []
    page_token = None
    while True:
        resp = drive_service.comments().list(
            fileId=file_id,
            fields=_COMMENT_FIELDS,
            includeDeleted=True,
            pageSize=100,
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("comments", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


def _fingerprint_from_census(raw_comments):
    """Fingerprint a comment-state snapshot, threads AND replies.

    Replies are in deliberately (issue #12). Two reasons: the accounting
    keys count reply entries, so a reply appearing or being deleted shifts
    the export records the accounting is matched against; and resolving or
    reopening a thread in the UI appends a reply of its own, which makes a
    resolve→reopen round trip visible instead of cancelling out.
    """
    out = set()
    for c in raw_comments:
        out.add((
            "c", c.get("id"), bool(c.get("deleted")), bool(c.get("resolved")),
            (c.get("author") or {}).get("displayName"), c.get("createdTime"),
            bool(c.get("quotedFileContent") or c.get("anchor")),
        ))
        for r in (c.get("replies") or []):
            out.add((
                "r", r.get("id"), bool(r.get("deleted")), c.get("id"),
                (r.get("author") or {}).get("displayName"),
                r.get("createdTime"), r.get("action") or "",
            ))
    return out


def _key_owners_universe(raw_comments):
    """Map each accounting key to the set of comment ids that can produce it.

    Built over EVERY entry the API can see — deleted replies and resolved
    threads included — because that is the set of things whose leftover record
    could turn up in an export. A key owned by exactly one id is a witness for
    that thread: no other thread can put a record with that key into
    comments.xml (issue #14).
    """
    out = {}
    for c in raw_comments:
        cid = c.get("id")
        for entry in [c] + list(c.get("replies") or []):
            author = (entry.get("author") or {}).get("displayName")
            created = entry.get("createdTime")
            if author and created:
                out.setdefault((author, _trunc_seconds(created)),
                               set()).add(cid)
    return out


def _census_comments(drive_service, file_id):
    """List ALL comments (incl. resolved) with pagination; fail closed.

    Returns (all_comments, anchored_comments, fingerprint, key_universe). A comment counts
    as anchored when it carries quotedFileContent or an anchor field.
    quotedFileContent is a stale snapshot — it is NEVER used to locate
    anchors, only to decide whether the doc contains anchored comments at all.

    The fingerprint comes from THIS read, not a second one (issue #12).
    Accounting used to run on the census while freshness was fingerprinted by
    a separate call, so the state the accounting trusted was never the state
    the fp1/fp2 sandwich proved stable — comments could move in the gap
    between the two reads and no check would see it.
    """
    try:
        raw = _list_comments_raw(drive_service, file_id)
    except HttpError as e:
        _error(
            f"cannot list comments (fail closed, no writes performed): "
            f"{e.reason if hasattr(e, 'reason') else e}"
        )
    # deleted entries serve the fingerprint only; every other consumer sees
    # the same live-comment view it saw before
    out = [c for c in raw if not c.get("deleted")]
    anchored = [
        c for c in out
        if c.get("quotedFileContent") or c.get("anchor")
    ]
    return (out, anchored, _fingerprint_from_census(raw),
            _key_owners_universe(raw))


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


def _text_buffer(doc_tab):
    """Flat text of a tab's body plus a map position-in-buffer -> doc index.

    Google Docs indices are UTF-16 code units. Non-BMP characters (💡) are one
    Python code point but two units, so the index advances per character, not
    per position in the string.

    A '\\x00' sentinel marks every place where consecutive runs are NOT
    contiguous in the index space — table cell boundaries, structural
    elements — so a quote can never falsely match across one. Its entry in
    the map is -1, which is not a position anything may be resolved to.

    One builder for everyone who searches this text: matching, counting and
    the projected-uniqueness check must agree byte for byte, or the check
    stops describing what the write will do.
    """
    buf_parts, index_map, last_end = [], [], None
    for start, end, text in _extract_text_from_doctab(doc_tab):
        if last_end is not None and start != last_end:
            buf_parts.append("\x00")
            index_map.append(-1)
        last_end = end
        doc_offset = 0
        for ch in text:
            buf_parts.append(ch)
            index_map.append(start + doc_offset)
            doc_offset += 2 if ord(ch) > 0xFFFF else 1
    return "".join(buf_parts), index_map


def _find_quote_in_doctab(doc_tab, quote, occurrence=1):
    """Find the Nth (1-based) occurrence of `quote` within a tab's text.

    Returns (start_index, end_index) absolute indices in the Docs coordinate
    system, or None if not found. Matches are computed against a concatenated
    text buffer keyed by real indices — so the quote may span multiple text
    runs but not structural boundaries (paragraph breaks are represented by
    '\\n' in textRun content, which is fine).
    """
    buf, index_map = _text_buffer(doc_tab)
    if not buf:
        return None

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


def _count_in_buffer(buf, quote):
    """Occurrences of `quote` in a text buffer, overlapping matches included."""
    if not quote or "\x00" in quote:
        return 0  # NUL is the internal boundary sentinel — never matchable
    count, pos = 0, 0
    while True:
        idx = buf.find(quote, pos)
        if idx == -1:
            return count
        count += 1
        pos = idx + 1


def _count_quote_occurrences(doc_tab, quote):
    buf, _index_map = _text_buffer(doc_tab)
    return _count_in_buffer(buf, quote)


def _count_text_outside_body(doc_tab, text):
    """Occurrences of `text` in the tab's headers, footers and footnotes.

    `replaceAllText` is not confined to the body: it replaces everywhere in
    the document. The uniqueness precondition therefore has to look outside
    the body too, or a search string that is unique in the body but repeated
    in a running header would change two places, and skrepka would only learn
    that from `occurrencesChanged` — after the write, with the outcome
    unknown. Long quotes made this unlikely; narrowing (r8) makes short
    search strings ordinary, so the check moved here.
    """
    if not text or "\x00" in text:
        return 0
    total = 0
    for key in ("headers", "footers", "footnotes"):
        for container in (doc_tab.get(key) or {}).values():
            if not isinstance(container, dict):
                continue
            total += _plain_text(container.get("content", [])).count(text)
    return total


def _replace_all_match_count(doc_tab, text):
    """How many matches `replaceAllText` would find in this tab."""
    return (_count_quote_occurrences(doc_tab, text)
            + _count_text_outside_body(doc_tab, text))


def _write_control(revision_id):
    """Build writeControl block for batchUpdate with required revision pinning.

    Fails closed on a missing revision id: the google client drops None
    values from request bodies, so an unvalidated None would silently turn
    a pinned write into an UNPINNED one (codex delta-review hardening).
    """
    if not revision_id:
        _error("internal: batchUpdate without a revision id — refusing an "
               "unpinned write (doc read likely returned no revisionId)")
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
    # A malformed create response leaves a file in Drive whose id we never
    # learn — nothing can clean that up, so say so plainly instead of dying on
    # a KeyError. The guarantee below starts once we hold an id.
    file_id = (img_file or {}).get("id")
    if not file_id:
        raise PatchOpError(
            "drive.files.create returned no file id — a staging image may "
            "have been created and cannot be cleaned up automatically; "
            "check your Drive for a stray upload")

    # From here the file EXISTS in Drive and is about to become publicly
    # readable, so every exit path must remove it. `BaseException` is
    # deliberate: a KeyboardInterrupt between granting the ACL and returning
    # the tuple would otherwise leave a world-readable file whose id the
    # caller never receives (its `staged_id` stays None). Cleanup is
    # best-effort and never swallows the original exception.
    try:
        perm = drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
            fields="id",
        ).execute()
        perm_id = (perm or {}).get("id")
        if not perm_id:
            # the ACL may well exist despite the malformed reply — treat it as
            # a failure so the file is deleted rather than left public
            raise PatchOpError(
                "drive.permissions.create returned no permission id")
        return (f"https://drive.google.com/uc?id={file_id}", file_id, perm_id)
    except BaseException:
        # deleting the file drops any ACL with it, so an unknown permission id
        # is not a problem here. `_cleanup_staging_image` only catches
        # `Exception`, so a KeyboardInterrupt raised *inside* cleanup would
        # otherwise replace the original failure and hide why we got here.
        try:
            _cleanup_staging_image(drive_service, file_id, None)
        except BaseException as ce:
            _warn(f"staging image {file_id} may still exist and be publicly "
                  f"readable — cleanup itself failed: {ce}")
        raise


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

        # Re-check at use time: _resolve_upload_image vetted this path while
        # reading the markdown, and the file could have been swapped for a
        # symlink out of the tree since (TOCTOU). full_path is already a
        # realpath, so any new link in the chain shows up as a mismatch.
        if os.path.realpath(full_path) != full_path or not os.path.isfile(full_path):
            _warn(f"image {rel_path} changed on disk since it was read — skipped")
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
            except (HttpError, PatchOpError) as e:
                # PatchOpError here means a malformed Drive reply (no file id /
                # no permission id); the staging file was already cleaned up
                # where its id was known. Skip this image, keep the rest.
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


# Raster only, deliberately. SVG is a document format: cairosvg and the
# `qlmanage` fallback both resolve references inside the file, so an `<image
# xlink:href="http://…">` or `file:///etc/passwd` in an SVG would make skrepka
# fetch it — breaking the "no outbound connections except Google" promise in
# SECURITY.md. 0.9 refuses .svg instead of rendering it unsandboxed; convert to
# PNG yourself (docs/LIMITATIONS.md).
_UPLOAD_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
})


def _resolve_upload_image(md_dir, path):
    """Resolve a markdown image reference to a local file that is safe to
    upload, or None to leave the reference in the text untouched.

    The markdown reaching this point is UNTRUSTED: it can come straight out of
    a downloaded document, and document text is written by third parties (see
    agents/CONTRACT.md §2.3). Left unchecked, `![x](/home/you/.ssh/id_rsa)` or
    `![x](../../../etc/passwd)` would be uploaded to Drive and published as
    `anyone:reader` for the duration of the insert — an arbitrary local file
    read with a public window, landing inside `--folder` when one is given.
    So containment is enforced here, fail closed:

    - absolute paths, `~`-paths and anything with a URL scheme are refused;
    - the path must resolve — symlinks included — inside the .md file's own
      directory tree;
    - the target must be a regular file carrying an image extension.

    A refused reference is simply left as literal markdown, exactly like a
    reference to a file that does not exist.
    """
    from urllib.parse import urlparse

    if not path or os.path.isabs(path) or path.startswith("~"):
        return None
    if urlparse(path).scheme:  # remote URL, not a local file
        return None
    base = os.path.realpath(md_dir)
    full = os.path.realpath(os.path.join(base, path))
    # realpath resolved the whole chain, so a symlink aimed out of the tree
    # fails this containment test rather than sneaking past it
    if full != base and not full.startswith(base + os.sep):
        return None
    if os.path.splitext(full)[1].lower() not in _UPLOAD_IMAGE_EXTS:
        return None
    if not os.path.isfile(full):
        return None
    return full


def _prepare_md_for_upload(md_path):
    """Replace ![alt](path) with text markers and return (temp_path, images_list)."""
    with open(md_path, "r") as f:
        md_text = f.read()

    md_dir = os.path.dirname(os.path.abspath(md_path))
    images = []

    def replace_image(m):
        alt, path = m.group(1), m.group(2)
        full_path = _resolve_upload_image(md_dir, path)
        if full_path:
            # full-uuid suffix guarantees marker uniqueness in the doc, so
            # the exact-substring deletion in post_process_images is
            # unambiguous; regenerate on (astronomically unlikely) collision
            while True:
                marker = f"«IMG:{uuid.uuid4().hex}»"
                if marker not in md_text:
                    break
            images.append((marker, alt, path, full_path))
            return marker
        return m.group(0)  # keep original: missing file, or refused as unsafe

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


def list_comments(file_id, output=None):
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
                # createdTime and reply ids are not decoration: when the anchor
                # accounting refuses on colliding keys it names threads by the
                # second they were created in, and without these fields there
                # is nowhere for a person to look that up (#16). Reply ids are
                # what makes the surplus reply deletable (#18).
                # author/me says whether the entry is the authorized account's
                # own. Without it «ответь только на мои комментарии» is not
                # expressible: the agent has to guess by display name, and a
                # wrong guess means talking to the customer in their document
                # instead of to the person who asked (живой случай 2026-08-09).
                fields="nextPageToken,comments(id,content,"
                       "author/displayName,author/me,"
                       "createdTime,quotedFileContent,resolved,"
                       "replies(id,content,author/displayName,author/me,"
                       "createdTime))",
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

    # Drive omits a boolean holding its default value, so `resolved` is
    # present on one listing and gone from the next — measured live 2026-08-09,
    # where the second call returned open threads without the key at all and
    # every consumer reading c["resolved"] died on KeyError (#41). Which fields
    # Google feels like omitting is not the caller's problem to know.
    unspecified = 0
    for c in comments:
        c["resolved"] = bool(c.get("resolved"))
        for entry in [c] + list(c.get("replies") or []):
            author = entry.get("author")
            if isinstance(author, dict) and "me" not in author:
                # Absent means «Google did not say», not «somebody else» — and
                # the whole «отвечай только на мои комментарии» capability
                # stands on this field. It is normalized so nothing crashes,
                # and the count is reported so a scoped request can be checked
                # against a number instead of being silently narrowed to zero.
                author["me"] = False
                unspecified += 1

    # a link that opens the document with this thread expanded (#20): naming
    # a thread by id left the person to find it by eye
    for c in comments:
        link = _thread_link(file_id, c.get("id"))
        if link:
            c["link"] = link

    summary = {"comments": len(comments),
               "unresolved": sum(1 for c in comments
                                 if not c.get("resolved")),
               # so a scoped request («отработай мои комментарии») can be
               # checked against a number, not against a display name the
               # agent had to guess
               "mine": sum(1 for c in comments
                           if (c.get("author") or {}).get("me"))}
    if unspecified:
        summary["authorship_unspecified"] = unspecified
    _emit_json(comments, output=output, summary=summary)


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
                f"non-contiguous sub-ranges). Re-create it with `skrepka "
                f"mark`."
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
            # `occurrence` is refused outright once the doc has anchored
            # comments, so sending everyone there would be a dead end for the
            # documents this tool is for (#24: a refusal must name a path that
            # exists). The quote is the path — make it longer.
            _error(
                f"quote is non-unique ({total} matches): {quote!r}. "
                f"Extend the quote until it is unique — include the "
                f"surrounding sentence. That fails when the paragraph itself "
                f"repeats word for word: no quote inside it is unique, so the "
                f"edit cannot be addressed at all. Do not guess a copy — tell "
                f"the person the text repeats and ask what to do. "
                f"'occurrence': N also disambiguates, but only on a doc "
                f"WITHOUT anchored comments — with them it is refused, so "
                f"sending an agent there alone would be a dead end."
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


class _HiddenMarkerProblem(str):
    """The marker census found anchors outside plain body paragraphs.

    `in_tables` / `elsewhere` split the ids by where they were hiding. A
    problem whose `elsewhere` is empty is bounded: every unseen anchor sits
    inside some table, and a table's extent IS known from the API side, so
    the caller can block table ranges instead of the whole document.
    """

    def __new__(cls, text, in_tables=(), elsewhere=()):
        obj = super().__new__(cls, text)
        obj.in_tables = frozenset(in_tables)
        obj.elsewhere = frozenset(elsewhere)
        return obj


def _parse_docx_anchor_spans(docx_bytes):
    """Parse word/document.xml and extract comment anchor spans.

    Returns (spans, problems, census):
      spans: [{"docx_id", "para_index", "para_text", "start_off",
               "end_para_index", "end_para_text", "end_off", "anchor_text",
               "has_objects"}] — offsets are UTF-16 units within their own
              paragraph's text. The two paragraph indices are equal for an
              ordinary anchor and differ when the selection was dragged
              across a paragraph break (#45).
      problems: reasons the mapping is unusable (unpaired ranges, inline
              objects in anchor paragraphs, malformed XML). Any problem ⇒
              caller fails closed.
      census: {"in_tables", "elsewhere", "in_body"} — sets of w:id, where
              every marker in document.xml was found. An id in NONE of them
              is not in document.xml at all, which is how a footnote or a
              header anchor looks (its markers live in footnotes.xml /
              header*.xml). Body edits cannot reach such an anchor.
    Strict contract per codex W8-r1 #2: linear parse, exact text, no
    normalization; w:t / w:tab / w:br / w:cr accounted for in UTF-16.
    """
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    spans, problems = [], []
    empty_census = {"in_tables": frozenset(), "elsewhere": frozenset(),
                    "in_body": frozenset()}
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml_bytes = z.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        return [], [f"malformed docx export: {e}"], empty_census

    w = _WORDML_NS
    seen_starts, seen_ends = {}, {}
    cross_para_open = {}

    # Global census of ALL range markers anywhere in the document. After the
    # body-paragraph walk, the processed multiset must equal this one — any
    # marker hidden in an unsupported container (w:sdt, w:tbl, w:fldSimple,
    # mc:AlternateContent, tracked changes, ...) becomes a problem instead
    # of being silently ignored (codex W8-r1 P0#1).
    #
    # The census also remembers WHERE it saw each marker. A table is not the
    # same kind of unknown as a tracked-change container: the table's extent
    # is readable from the API side, so an anchor hiding in one can be fenced
    # off instead of freezing the document (r8).
    global_starts, global_ends = {}, {}
    census_tables, census_other = set(), set()

    def _census(node, in_table):
        for child in node:
            tag = child.tag
            if tag == f"{w}commentRangeStart" or tag == f"{w}commentRangeEnd":
                cid = child.get(f"{w}id")
                bucket = global_starts if tag.endswith("Start") else global_ends
                bucket[cid] = bucket.get(cid, 0) + 1
                (census_tables if in_table else census_other).add(cid)
                continue
            _census(child, in_table or tag == f"{w}tbl")

    _census(root, False)

    body = root.find(f"{w}body")
    if body is None:
        return [], ["malformed docx export: no w:body"], empty_census
    # Only DIRECT body paragraphs are processed — API-side matching also
    # uses only top-level paragraphs, so text-alone matches can never cross
    # structural domains (codex W8-r1 P1).
    body_paras = [ch for ch in body if ch.tag == f"{w}p"]

    para_meta = []
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
                    cross_para_open[cid] = (p_index, state["off"])
                elif tag == f"{w}commentRangeEnd":
                    cid = child.get(f"{w}id")
                    seen_ends[cid] = seen_ends.get(cid, 0) + 1
                    if cid in state["local_open"]:
                        spans.append({
                            "docx_id": cid, "para_index": p_index,
                            "start_off": state["local_open"].pop(cid),
                            "end_para_index": p_index,
                            "end_off": state["off"],
                        })
                        cross_para_open.pop(cid, None)
                    elif cid in cross_para_open:
                        # A selection dragged across a paragraph break — what
                        # an editor does without thinking when the comment is
                        # about a heading and its subtitle, or a list item and
                        # the next one. This used to be a coordinate-free
                        # problem that froze the whole document (#45), while
                        # the coordinates were right there: the opening
                        # paragraph with its offset and this one with its own.
                        open_p, open_off = cross_para_open.pop(cid)
                        spans.append({
                            "docx_id": cid, "para_index": open_p,
                            "start_off": open_off,
                            "end_para_index": p_index,
                            "end_off": state["off"],
                        })
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
        para_meta.append({"text": "".join(state["parts"]),
                          "has_objects": state["has_objects"],
                          "has_unknown": state["has_unknown"]})

    # Texts are attached AFTER the walk, not inside it: a span that crosses a
    # paragraph break is not complete until both of its paragraphs have been
    # read, and the old in-loop assignment could only ever see the first one.
    for sp in spans:
        head, tail = para_meta[sp["para_index"]], para_meta[sp["end_para_index"]]
        sp["para_text"] = head["text"]
        sp["end_para_text"] = tail["text"]
        if sp["end_para_index"] == sp["para_index"]:
            sp["anchor_text"] = _slice_utf16(
                head["text"], sp["start_off"], sp["end_off"])
        else:
            # Display only — it names the anchor in refusals and nothing reads
            # it back. Paragraphs the walker skips (a table between the ends)
            # are missing from it; the RANGE is unaffected, because both ends
            # are computed from their own paragraphs.
            middle = [para_meta[i]["text"] for i
                      in range(sp["para_index"] + 1, sp["end_para_index"])]
            sp["anchor_text"] = "\n".join(
                [_slice_utf16(head["text"], sp["start_off"],
                              _utf16_len(head["text"]))]
                + middle
                + [_slice_utf16(tail["text"], 0, sp["end_off"])])
        # Either end being unreadable is enough to distrust the span: the
        # offsets are taken from both.
        sp["has_objects"] = head["has_objects"] or tail["has_objects"]
        unknown = sorted(set(head["has_unknown"]) | set(tail["has_unknown"]))
        if unknown:
            problems.append(
                f"anchor {sp['docx_id']} sits in a paragraph with "
                f"unsupported elements ({unknown[:3]}) "
                f"— text/offsets unreliable")

    processed = set(seen_starts) & set(seen_ends)
    census = {"in_tables": frozenset(census_tables - processed),
              "elsewhere": frozenset(census_other - processed),
              "in_body": frozenset(processed)}
    if seen_starts != global_starts or seen_ends != global_ends:
        hidden = (set(global_starts) | set(global_ends)) - processed
        in_tables = sorted(census["in_tables"])
        elsewhere = sorted(census["elsewhere"])
        if in_tables and not elsewhere:
            problems.append(_HiddenMarkerProblem(
                f"comment anchors sit inside tables: {in_tables} — their "
                f"exact position is not readable from the export, so edits "
                f"inside tables are refused and the rest of the document "
                f"stays editable",
                in_tables=in_tables))
        else:
            problems.append(_HiddenMarkerProblem(
                f"comment range markers outside plain body paragraphs "
                f"(tables/containers/tracked changes): "
                f"{sorted(hidden) or 'count mismatch'} — mapping unusable",
                in_tables=in_tables, elsewhere=elsewhere))
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
    return spans, problems, census


# Element kinds that CANNOT hide an anchor, each for a proven reason:
#   inlineObjectElement / equation  — the export marks the paragraph
#       `has_objects`, so an anchor in it fails closed before any mapping;
#   footnoteReference               — `w:footnoteReference` is outside the
#       parser's whitelist, so the same applies;
#   pageBreak / columnBreak         — the export writes `w:br` as \n, and a
#       para_text holding \n matches no API paragraph at all (zero candidates);
#   horizontalRule                  — such a paragraph shows no text, so any
#       anchor in it is empty and refused earlier.
# Everything else — person, richLink, autoText and any kind Google adds
# tomorrow — is treated as a possible host. A blocklist, not a whitelist: an
# unknown kind must fail towards protection, not away from it (r9 review).
_QUIET_ELEMENTS = frozenset((
    "textRun", "inlineObjectElement", "equation", "footnoteReference",
    "pageBreak", "columnBreak", "horizontalRule"))


def _pieces_fit(pieces, text):
    """Do these known textRun fragments occur inside `text`, in order?

    Piecewise, not as one substring: a paragraph «До ИТОГО После» with a chip
    in the middle arrives as fragments split around the part we cannot read.

    A soft line break is spelled `\\v` by the API and `\\n` by the export
    (#27), so a fragment carrying one would otherwise fit nothing and an
    unreadable paragraph holding a soft break would stop counting as a
    possible home for an anchor. That is not a fail-open — a readable twin
    would have to match a text containing `\\n`, and no API paragraph ever
    does, so the document is refused either way — but it made the refusal name
    the wrong reason. Normalizing here does NOT touch the exact-match proof in
    `_map_anchors_to_doc`: that one runs on `by_text`, over readable
    paragraphs, and still sees zero matches for a soft break. Whoever fixes
    #27 has to re-read both.
    """
    pos = 0
    for piece in pieces:
        piece = piece.replace("\v", "\n")
        if not piece:
            continue
        at = text.find(piece, pos)
        if at < 0:
            return False
        pos = at + len(piece)
    return True


def _map_anchors_to_doc(doc_tab, spans):
    """Map docx anchor spans to absolute doc index ranges.

    Paragraph matching is by EXACT text equality (no normalization).
    Returns (ranges, problems, ambiguous): ranges = [(start, end, anchor_text,
    docx_id)], ambiguous = spans that match SEVERAL paragraphs and get fenced
    off by the caller instead of being placed.

    Two identical paragraphs used to freeze the whole document (#26). They do
    not have to: the anchor is provably in ONE OF the matches, so every match
    can be protected without deciding which. What must never happen is picking
    one — measured attempts to choose (by ordinal among equals) can put the
    anchor on the wrong paragraph, and a misplaced anchor protects the wrong
    text while leaving the real thread naked.

    The choice is only safe to skip when the anchor is provably among the
    candidates. Three cases, and the branch order below follows them:

    1. the API reports the anchor's paragraph with text equal to `para_text`
       — it is one of `exact`;
    2. the API reports it with text we cannot read (a smart chip, a rich
       link) — then there is nothing to fence: `patch` writes through
       `replaceAllText`, which acts on the whole tab, so a fence around such
       a paragraph would not keep an edit elsewhere out of it. If any
       paragraph could be that home, the document fails closed exactly as it
       does today;
    An anchor whose selection was dragged across a paragraph break is placed
    by locating its two ends separately, each by its own paragraph's text
    (#45). It used to freeze the document for a reason that was never about
    danger — «we cannot count offsets across a break» — while the offsets were
    right there, one per end.

    3. the API reports text that differs from `para_text` — then NOTHING
       matches and the whole document fails closed. Today this is reachable
       only through a soft line break (the parser writes `w:br` as \\n, the
       API returns \\v), and an API paragraph can never contain \\n, since \\n
       ends a paragraph. **The proof leans on this**, not on `has_unknown`:
       whoever fixes the `w:br` mismatch has to revisit this branch.
    """
    body = doc_tab.get("body", {}) or {}
    paras, problems = [], []
    for el in body.get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        start, end = el.get("startIndex"), el.get("endIndex")
        if not isinstance(start, int) or not isinstance(end, int) or start >= end:
            # A fence needs both ends. Skipping the paragraph would drop it
            # from the candidate set silently, which is how an anchor ends up
            # outside its own fence — refuse instead.
            problems.append(
                f"paragraph with unusable indices {start!r}..{end!r} — the "
                f"anchor map cannot be trusted (fail closed)")
            continue
        elements = para.get("elements", [])
        if any("textRun" not in e for e in elements):
            pieces = [e["textRun"].get("content", "") for e in elements
                      if "textRun" in e]
            if pieces and pieces[-1].endswith("\n"):
                pieces[-1] = pieces[-1][:-1]
            kinds = {k for e in elements for k in e
                     if k not in ("startIndex", "endIndex")
                     and not k.startswith("suggested")}
            paras.append((start, end, None, pieces,
                          bool(kinds - _QUIET_ELEMENTS)))
            continue
        text = "".join(e["textRun"].get("content", "") for e in elements)
        if text.endswith("\n"):
            text = text[:-1]
        paras.append((start, end, text, (), False))

    # indexed once, not rescanned per span: a long document with many threads
    # would otherwise walk every paragraph for every anchor
    by_text, possible_hosts = {}, []
    for st, en, text, pieces, could_host in paras:
        if text is None:
            if could_host:
                possible_hosts.append((st, en, pieces))
        else:
            by_text.setdefault(text, []).append((st, en))

    ranges, ambiguous = [], []
    for s in spans:
        ptext = s.get("para_text")
        if ptext is None:
            problems.append(
                f"anchor {s['docx_id']} has no paragraph text — the export "
                f"could not be read (fail closed)")
            continue
        exact = by_text.get(ptext, [])
        if s.get("end_para_index", s["para_index"]) != s["para_index"]:
            # An anchor dragged across a paragraph break. Its two ends are
            # located INDEPENDENTLY, each by its own paragraph's text, and API
            # indices are absolute — so whatever lies between them (a table, a
            # chip, anything the walker skips) cannot move either end. This is
            # the anchor's exact extent, not a conservative envelope.
            etext = s.get("end_para_text")
            end_exact = by_text.get(etext, []) if etext is not None else []
            # Unlike the single-match branch below, opaque paragraphs ARE
            # checked here. That branch keeps a known hole (#30) only because
            # it must not freeze documents that work today; a crossing anchor
            # freezes its document today anyway, so this surface is new and
            # gets to be strict. Found in review: a chip paragraph could be
            # the real home of either end, and placing the anchor on the
            # readable twin leaves the live thread unprotected.
            hosts = [st for st, _en, pieces in possible_hosts
                     if _pieces_fit(pieces, ptext)
                     or (etext is not None and _pieces_fit(pieces, etext))]
            if hosts:
                problems.append(
                    f"anchor {s['docx_id']} spans paragraphs, and a paragraph "
                    f"whose text skrepka cannot read (a smart chip or a rich "
                    f"link at {hosts[:3]}) could hold one of its ends — "
                    f"undecidable (fail closed)")
                continue
            if not exact or not end_exact:
                problems.append(
                    f"anchor {s['docx_id']} spans paragraphs and one of its "
                    f"ends matched 0 times in the doc (need exactly 1): "
                    f"{(ptext if not exact else etext)[:50]!r}")
                continue
            if len(exact) > 1 or len(end_exact) > 1:
                # Fencing the pairs of candidates is combinatorial and buys
                # nothing today: such a document is refused as it is now, so
                # nothing regresses by leaving it refused.
                problems.append(
                    f"anchor {s['docx_id']} spans paragraphs and one of its "
                    f"ends repeats in the document word for word "
                    f"({len(exact)}/{len(end_exact)} matches) — which copy "
                    f"holds the comment is not readable (fail closed)")
                continue
            (base, head_end), (base_e, tail_end) = exact[0], end_exact[0]
            start_at, end_at = base + s["start_off"], base_e + s["end_off"]
            # `head_end - 1` and `tail_end - 1` are the paragraphs' own
            # newlines: the visible text ends before them.
            if not (base <= start_at < head_end
                    and base_e <= end_at <= tail_end - 1
                    and start_at < end_at and base_e > base):
                problems.append(
                    f"anchor {s['docx_id']} spans paragraphs but the two ends "
                    f"do not line up in the document "
                    f"([{start_at}, {end_at}) against paragraphs "
                    f"[{base}, {head_end}) and [{base_e}, {tail_end})) — "
                    f"the anchor map cannot be trusted (fail closed)")
                continue
            ranges.append((start_at, end_at, s.get("anchor_text", ""),
                           s["docx_id"]))
            continue
        if not exact:
            problems.append(
                f"anchor {s['docx_id']} paragraph matched 0 times in the doc "
                f"(need at least 1): {ptext[:50]!r}")
            continue
        if len(exact) > 1:
            # The fence is only honest while the anchor is PROVABLY among the
            # candidates, and the proof needs every paragraph that could be
            # its home to be readable. Checked here and nowhere else: at one
            # match the behaviour stays exactly as it has always been, so this
            # release never freezes a document that works today. The hole that
            # leaves at one match is older than #26 and is gated on a
            # measurement nobody has yet (#30) — closing it by guesswork was
            # tried three times in review and broke ordinary documents twice.
            hosts = [st for st, _en, pieces in possible_hosts
                     if _pieces_fit(pieces, ptext)]
            if hosts:
                problems.append(
                    f"anchor {s['docx_id']} matches {len(exact)} paragraphs, "
                    f"and a paragraph whose text skrepka cannot read (a smart "
                    f"chip or a rich link at {hosts[:3]}) could be its home "
                    f"too — undecidable (fail closed): {ptext[:50]!r}")
                continue
            ambiguous.append({
                "docx_id": s["docx_id"], "para_text": ptext,
                "start_off": s["start_off"], "end_off": s["end_off"],
                "candidates": exact,
            })
            continue
        base = exact[0][0]
        ranges.append((base + s["start_off"], base + s["end_off"],
                       s.get("anchor_text", ""), s["docx_id"]))
    return ranges, problems, ambiguous


# ---------------------------------------------------------------------------
# Anchor accounting + export canary (PLAN-sync-anchors v4)
# ---------------------------------------------------------------------------

_CANARY_NOTE = "(служебная строка синхронизации — можно удалить)"


def _trunc_seconds(ts):
    """Normalize an RFC3339 timestamp to whole seconds (docx w:date is the
    API createdTime TRUNCATED to seconds — verified C11b, 5/5 live)."""
    return re.sub(r"\.\d+(?=Z$|[+-]\d{2}:\d{2}$)", "", ts or "")


def _docx_comment_records(docx_bytes):
    """Parse word/comments.xml into [{docx_id, author, date_sec}].

    Returns (records, problems). Contract (codex sync-anchors r3 #4):
    every record must carry a non-empty w:id, UNIQUE across records —
    a duplicate or missing id makes the comments.xml ⇄ document.xml join
    unusable, so it is a problem (caller fails closed). A missing
    comments.xml part is an empty record list (valid for a doc whose
    anchored comments are all ghosts — accounting then fails closed on
    the API side of the equality, which is the intent).
    """
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    w = _WORDML_NS
    records, problems = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            if "word/comments.xml" not in z.namelist():
                return [], []
            root = ET.fromstring(z.read("word/comments.xml"))
    except Exception as e:
        return [], [f"malformed docx comments part: {e}"]
    seen_ids = set()
    for c in root.findall(f"{w}comment"):
        cid = c.get(f"{w}id")
        if not cid:
            problems.append("comments.xml entry without w:id")
            continue
        if cid in seen_ids:
            problems.append(f"duplicate w:id {cid} in comments.xml")
            continue
        seen_ids.add(cid)
        records.append({
            "docx_id": cid,
            "author": c.get(f"{w}author") or "",
            "date_sec": _trunc_seconds(c.get(f"{w}date") or ""),
        })
    return records, problems


class _AnchorProblem(str):
    """An accounting problem that knows WHICH export entries it is about.

    Subclasses `str` so every message-matching path keeps working unchanged.
    `docx_ids` is the set of comments.xml ids the problem is confined to; a
    problem carrying ids can be turned into document coordinates and blocks
    only the operations that touch them (issue #10). A plain `str` problem has
    no coordinates and keeps blocking the whole document.
    """

    def __new__(cls, text, docx_ids=()):
        obj = super().__new__(cls, text)
        obj.docx_ids = tuple(docx_ids)
        return obj


def _thread_link(file_id, comment_id):
    """Deep link that opens the document with this thread expanded (#20).

    Naming a thread by id left the person to hunt for it by eye; a link turns
    a refusal into an action. It also disambiguates a thread the export cannot
    place: a ghost simply does not open.
    """
    if not file_id or not comment_id:
        return None
    return (f"https://docs.google.com/document/d/{file_id}"
            f"/edit?disco={comment_id}")


def _comment_label(c, file_id=None):
    """Human-readable identity of a thread for refusal messages.

    «'slv fmts' @ 2026-07-30T12:46:33Z» is unreadable on a document where one
    person wrote every comment inside a minute (issue #10), so refusals name
    the comment id and the first words of its quote instead. quotedFileContent
    is a stale snapshot and stays banned from every safety decision — this is
    display only. With `file_id` the label also carries the ?disco= link.
    """
    quote = " ".join(
        ((c.get("quotedFileContent") or {}).get("value") or "").split())
    if len(quote) > 40:
        quote = quote[:40].rstrip() + "…"
    cid = c.get("id") or "?"
    label = f"{cid} «{quote}»" if quote else f"{cid} (без цитаты)"
    link = _thread_link(file_id, c.get("id"))
    return f"{label} {link}" if link else label


def _rfc3339_epoch(ts):
    """RFC3339 timestamp → epoch seconds, or None when it cannot be read.

    Comparing the strings themselves is wrong the moment two of them carry
    different offsets: '2026-01-01T01:00:00+05:00' is EARLIER than
    '2026-01-01T00:00:00-05:00' and lexicographically later (found in review).
    A timestamp without an offset is unreadable rather than assumed-UTC —
    guessing here would decide whether a thread is a ghost.
    """
    if not ts:
        return None
    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _ghost_verdict(c, records, doc_tab, file_id=None):
    """Is this witness-less thread provably a ghost? None when it is not.

    A thread the API calls live whose records are absent from the export is
    «a ghost or a stale export», and until #34 the two were treated as
    indistinguishable — so ANY vanished comment froze every replace in the
    document. Losing a comment without closing it is not a malfunction: the
    text got rewritten, the paragraph deleted, the block moved. A ghost has no
    anchor left, so blocking the document buys it nothing.

    Two signs, independent of each other, and both must agree:

    1. the export carries a record created LATER than this thread. The export
       is a snapshot of the comment store at some instant T; a record dated D
       proves T ≥ D, so a D later than this thread's creation leaves lag with
       nothing to explain. When the missing thread is the newest thing in the
       document the sign stays silent and the refusal stands — there lag
       really is indistinguishable;
    2. the text the comment was attached to is no longer in the document. This
       one does not come from the export at all — it is read from the API and
       the current snapshot, so a converter fault cannot forge it.

    Sign 1 rests on the export being assembled whole rather than in pieces —
    the same assumption the text canary already rests on, and NOT a measured
    fact. The review held a P0 on it: in a chain of three coincidences (thread
    alive, its text changed since, its record dropped by the converter) an
    edit could kill a live thread. The owner took that trade knowingly on
    2026-08-16 — see internal/DECISIONS.md — against the cost of the opposite,
    which happens routinely.

    When the stale quote IS still found in the document, the verdict carries
    those places: they get fenced, so an edit there is refused while the rest
    of the document stays editable. The fence proves nothing about where the
    anchor is; it only makes being wrong cost a local refusal instead of a
    thread.
    """
    if doc_tab is None:
        return None  # nothing to check sign 2 against — fail closed
    created = _rfc3339_epoch(c.get("createdTime"))
    if created is None:
        return None
    later = []
    for r in records:
        stamp = _rfc3339_epoch(r.get("date_sec"))
        # `is not None`, not `or 0`: a record whose date cannot be read must
        # be visibly not-later, not silently zero (found in review)
        if stamp is not None and stamp > created:
            later.append(r)
    if not later:
        return None
    quote = (c.get("quotedFileContent") or {}).get("value")
    if not quote:
        return None  # sign 2 is unverifiable, so it cannot agree
    if _count_text_outside_body(doc_tab, quote):
        # The old text survives in a header, a footer or a footnote, where the
        # fence cannot reach — `_text_buffer` walks the body only, while
        # `replaceAllText` does not. Nothing to fence with, so fail closed
        # (found in review).
        return None
    return {"id": c.get("id"),
            "link": _thread_link(file_id, c.get("id")),
            "quote": quote[:60],
            "fenced": _locate_comment_in_tab(doc_tab, c)}


def _fence_off_ghosts(ghosts):
    """Blocked ranges for vanished threads whose old text is still here.

    A ghost with nothing left to find fences nothing and costs the person
    nothing. One whose quote still matches somewhere gets those places fenced:
    if the thread is in fact alive and simply missing from the export, this is
    where it most likely still sits.
    """
    blocked = []
    for g in ghosts:
        for cs, ce in g.get("fenced") or ():
            who = (f"треда {g['id']} {g['link']}" if g.get("link")
                   else f"комментария {g.get('id')}")
            blocked.append((cs, ce, (
                f"комментарий {who} пропал из выгрузки документа, а текст, на "
                f"котором он висел («{g.get('quote', '')[:40]}»), ещё здесь — "
                f"поэтому правка этого места отклонена, а остальной документ "
                f"правится. Откройте тред по ссылке и посмотрите: если "
                f"комментарий действительно потерял привязку, удалите его — "
                f"место освободится; если он на месте, этот фрагмент правится "
                f"в интерфейсе")))
    return blocked


def _account_anchored_comments(anchored, records, spans, *, universe,
                               file_id=None, marker_census=None,
                               doc_tab=None):
    """Prove every live anchored THREAD keeps at least one anchor in the export.

    Ghosted threads vanish from the export ENTIRELY (C11a), and a stale export
    missing a fresh comment looks exactly the same — so every live anchored
    thread must be shown to be present. Keys are
    (author.displayName, createdTime→seconds); `universe` maps each key to the
    set of comment ids that produced it across EVERYTHING the API can see,
    deleted and resolved included.

      * a thread with no witness key — one that belongs to it alone — cannot
        be identified in the export at all, so it is refused;
      * a thread whose witness never appears in comments.xml is a ghost or a
        stale export;
      * a record key no live thread claims means the export is stale;
      * every record must join to exactly one anchor span via w:id.

    What this does NOT promise: that every anchor of a thread survives. A
    witness proves one anchor is mapped and therefore protected; an anchor
    that vanished from the export whole (record and span both) is invisible
    and can be overwritten, leaving the thread alive on its remaining anchor.
    skrepka protects threads, not individual anchors (see LIMITATIONS).

    Resolved threads are excluded unconditionally (r8). Google omits them from
    the export entirely (C11c), so counting them was a permanent shortfall that
    read as a ghost. On the `replaceAllText` path the exclusion is also
    provably free (C11d). On deletion paths it is not: `deleteContentRange`
    over a closed thread's anchor ghosts it (measured 2026-07-31). That is a
    decision, not an oversight — the conversation in a closed thread is over,
    and refusing the whole document costs the person more than the anchor
    does. The deletion paths warn instead and archive the closed threads'
    contents.

    `marker_census` is the export-side census of where each w:id was seen
    (from `_parse_docx_anchor_spans`). A record whose id appears NOWHERE in
    document.xml has its anchor outside the body — a footnote or a header —
    and no body edit can reach it, so it is not a problem.

    Returns (problems, metrics).
    """
    from collections import Counter

    problems, ghosts = [], []
    signatures = []  # (thread, Counter of its live entry keys)
    resolved_n = 0
    for c in anchored:
        if c.get("resolved"):
            # Google omits resolved threads from the export ENTIRELY — no
            # comments.xml record, no range (C11c, measured 2026-07-30).
            # Counting them was a permanent shortfall that read as a ghost and
            # blocked every replace, so closing one thread disabled editing for
            # that document for good.
            resolved_n += 1
            continue
        sig = Counter()
        entries = [c] + [r for r in (c.get("replies") or [])
                         if not r.get("deleted")]
        for entry in entries:
            author = (entry.get("author") or {}).get("displayName")
            created = entry.get("createdTime")
            if not author or not created:
                problems.append(
                    f"comment {_comment_label(c, file_id)} has an entry without "
                    f"author/createdTime — cannot account for it")
                continue
            sig[(author, _trunc_seconds(created))] += 1
        signatures.append((c, sig))
    docx_keys = Counter((r["author"], r["date_sec"]) for r in records)

    # --- witness rule (issue #14) -----------------------------------------
    # Counting entries was the whole trouble: a reply has no anchor of its
    # own — its record duplicates the parent's span — so it carried no
    # information about "will this thread keep an anchor", while every reply
    # added a key and a chance of collision. An agent answering threads in a
    # loop collided with itself and froze the document.
    #
    # Presence is proved by NAME instead. A witness key belongs to exactly one
    # thread across everything the API can see, so an export record carrying
    # it is that thread's record and its span is that thread's anchor. Nothing
    # else can forge it.
    live_keys = set()
    for _c, sig in signatures:
        live_keys |= set(sig)
    for c, sig in signatures:
        # Uniqueness is measured against the WHOLE API-visible universe, not
        # just live threads (codex r3): a leftover export record left by a
        # deleted reply or a resolved thread would otherwise pass for the
        # witness of a ghost.
        witnesses = [k for k in sig if universe.get(k) == {c.get("id")}]
        if not witnesses:
            problems.append(
                f"comment {_comment_label(c, file_id)} shares every (author, second) "
                f"key with another thread — nothing identifies its records in "
                f"the export, refusing. Reply to this thread (one at a time, "
                f"then re-run): the reply's own second becomes its witness")
            continue
        # At least ONE witness present is enough — a thread may have several,
        # and requiring a particular one would flap on partial staleness.
        if not any(docx_keys.get(k) for k in witnesses):
            verdict = _ghost_verdict(c, records, doc_tab, file_id)
            if verdict is not None:
                # Provably gone: it has no anchor left to protect, so the
                # document is not held hostage to it (#34). Named in the
                # receipt — removing someone's comment is the person's call
                # (CONTRACT §2.2), never ours.
                ghosts.append(verdict)
                continue
            # Not provable, so nothing is scoped: a thread missing from the
            # export has no span in document.xml, and there are no coordinates
            # to confine the refusal to.
            problems.append(
                f"comment {_comment_label(c, file_id)} is missing from the export "
                f"(ghost thread or stale export — indistinguishable)")
    unknown = {k for k in docx_keys if k not in live_keys}
    if unknown:
        # Global. An export record the API does not know means the export and
        # the API disagree about what exists; the span it maps to is not the
        # extent of the doubt.
        problems.append(
            "export contains comment entries unknown to the API (stale "
            "export): "
            + "; ".join(f"{a!r} @ {d}" for a, d in sorted(unknown)[:5]))
    # bijection: one span per comments.xml record, joined by w:id
    span_ids = Counter()
    for s in spans:
        sid = s.get("docx_id")
        if not sid:
            problems.append("anchor span without a w:id — join impossible")
            continue
        span_ids[sid] += 1
    record_ids = {r["docx_id"] for r in records}
    census_seen = set()
    if marker_census:
        census_seen = (set(marker_census.get("in_body") or ())
                       | set(marker_census.get("in_tables") or ())
                       | set(marker_census.get("elsewhere") or ()))
    outside_body = 0
    for rid in sorted(record_ids):
        n = span_ids.get(rid, 0)
        if n == 1:
            continue
        if n == 0 and marker_census is not None and rid not in census_seen:
            # The record exists (so the thread is IN the export — a ghost is
            # absent from it entirely, C11a) but document.xml carries no
            # marker for it at all. That is what an anchor on a footnote or in
            # a header looks like: its markers live in footnotes.xml /
            # header*.xml. Nothing we write into the body can reach it, so
            # there is nothing to protect and nothing to refuse (r8). Before
            # this, a single comment on a footnote froze every replace in the
            # document.
            outside_body += 1
            continue
        if n == 0 and rid in set((marker_census or {}).get("in_tables") or ()):
            # The marker IS in document.xml, inside a table the span parser
            # does not walk. Same conclusion as the parser's own hidden-marker
            # problem, and it has to be carried the same way, or this plain
            # string would freeze the document the fence was built to keep
            # editable (found in review).
            problems.append(_HiddenMarkerProblem(
                f"comments.xml entry {rid} anchors inside a table — its exact "
                f"position is not readable from the export",
                in_tables=(rid,)))
            continue
        # LOAD-BEARING for the witness rule: it proves a thread has an anchor
        # by "witness record ⇒ exactly one span ⇒ that span is protected".
        # The rule now reads: a witness record either maps to exactly one span
        # in the body, or provably has no anchor in the body at all (footnote
        # branch above), or anchors inside a table whose extent bounds it
        # (branch above). Everything else stays global. n > 1: tempting to confine to
        # those spans, but _parse_docx_anchor_spans already reports the same
        # document as globally broken ("comment range {id}: N starts / M
        # ends"), and a w:id whose markers do not pair up 1:1 has unreliable
        # offsets — the coordinates we would confine the refusal to are the
        # untrustworthy part.
        problems.append(
            f"comments.xml entry {rid} has {n} anchor spans in document.xml "
            f"(need exactly 1)")
    for sid in sorted(set(span_ids) - record_ids, key=str):
        # A span with no record is a local defect of the w:id join: it sits at
        # known coordinates and every other span still joins cleanly.
        problems.append(_AnchorProblem(
            f"anchor span {sid} has no comments.xml entry", (sid,)))
    metrics = {
        "api_anchored_live": len(anchored) - resolved_n,
        "api_anchored_resolved": resolved_n,
        "api_threads_accounted": len(signatures),
        "docx_comment_entries": len(records),
        "anchor_spans": len(spans),
        "anchors_outside_body": outside_body,
    }
    if ghosts:
        # carried in the metrics rather than a third return value: every
        # caller and test unpacks two, and a vanished thread is diagnosis,
        # not a decision anybody downstream re-makes
        metrics["ghosts"] = ghosts
    return problems, metrics


def _scope_anchor_problems(problems, anchors, attribution=None, file_id=None):
    """Split accounting problems into document-wide and range-confined ones.

    Returns (global_problems, blocked) where `blocked` is
    [(start, end, label)] in the `_find_protected_overlap` shape:
    TEXT-REMOVING operations overlapping those ranges are refused, the rest of
    the document stays editable (issue #10 — one murky thread used to forbid
    every edit).

    Inserts are exempt, like they are for healthy anchors, and for a stronger
    reason than C5: ghosting needs every anchored character gone, and
    insertText removes nothing. Whatever the span really is, adding text
    inside it cannot destroy it.

    A problem only earns a range when EVERY export entry it names maps to a
    document position. One unmapped id and it goes back to blocking the whole
    document: an anchor we cannot place is an anchor we cannot avoid.

    Only one problem class reaches here with ids today — an anchor span with
    no comments.xml entry. The others are either coordinate-free by nature
    (a thread absent from the export) or already reported as globally broken
    by the docx parser.
    """
    by_id = {}
    for as_, ae, atext, aid in anchors:
        by_id.setdefault(aid, []).append((as_, ae, atext))

    global_problems, blocked = [], []
    for p in problems:
        ids = getattr(p, "docx_ids", ())
        if not ids:
            global_problems.append(p)
            continue
        placed = []
        for i in ids:
            if not by_id.get(i):
                placed = None
                break
            placed.extend((s, e, t, i) for s, e, t in by_id[i])
        if placed is None:
            global_problems.append(p)
            continue
        for s, e, t, i in placed:
            # name the thread, not the export row: w:id is reassigned on every
            # export, so it identifies nothing a person can look up (#20)
            cid = (attribution or {}).get(i)
            link = _thread_link(file_id, cid)
            who = f"треда {cid} {link}" if link else f"docx id {i}"
            blocked.append((s, e, (
                f"an unaccounted comment anchor ({who}, «{t[:40]}») — {p}. "
                f"Разберитесь с этим тредом в UI (удалить или переоткрыть) "
                f"или правьте этот фрагмент в интерфейсе")))
    return global_problems, blocked


def _table_intervals(doc_tab):
    """Every table in the tab as a protected interval [(start, end, label)].

    Read from the API side, where a table is one structural element with its
    own bounds — which is exactly the part the docx export does not give us
    for an anchor hiding inside a cell.
    """
    out = []
    body = doc_tab.get("body", {}) or {}
    for el in body.get("content", []):
        if "table" not in el:
            continue
        s, e = el.get("startIndex"), el.get("endIndex")
        if not isinstance(s, int) or not isinstance(e, int) or s >= e:
            continue
        out.append((s, e, "a table holding a comment anchor"))
    return out


def _fence_off_tables(global_problems, doc_tab):
    """Turn "the anchor is somewhere in a table" into blocked table ranges.

    A comment in a table cell used to freeze the whole document: the marker
    census counts markers anywhere in document.xml, while the span parser
    walks only direct body paragraphs, so the two disagreed and the mismatch
    was a coordinate-free problem (r8).

    It does have coordinates, just coarser ones. If every unseen marker sits
    inside a table, the anchor is inside SOME table, and text-removing edits
    that touch no table cannot reach it. Tables are not editable through
    skrepka anyway (LIMITATIONS), so the fence costs the person nothing.

    Returns (remaining_global_problems, blocked). A mismatch that is not
    fully explained by tables keeps blocking everything.
    """
    remaining, blocked = [], []
    tables = None
    for p in global_problems:
        in_tables = getattr(p, "in_tables", frozenset())
        elsewhere = getattr(p, "elsewhere", frozenset())
        if not in_tables or elsewhere:
            remaining.append(p)
            continue
        if tables is None:
            tables = _table_intervals(doc_tab)
        if not tables:
            # markers claim to be in a table, the API side shows none — the
            # two views disagree about the document's shape, fail closed
            remaining.append(p)
            continue
        for s, e, label in tables:
            blocked.append((s, e, (
                f"{label} ({p}). Правьте этот фрагмент в интерфейсе Google "
                f"Docs — комментарий там не пострадает")))
    return remaining, blocked


def _anchor_map_remedy(shown):
    """The way out, chosen by the reason — not one advice for all of them.

    This refusal used to end with «разберитесь с комментариями-призраками»
    whatever went wrong. That is right for an unaccounted thread and plain
    wrong for a duplicated paragraph or a smart chip, and a refusal naming a
    path that does not exist is the #24 defect all over again.
    """
    if "cannot read" in shown:
        return ("В документе есть абзац, текст которого skrepka не читает "
                "(смарт-чип или ссылка-карточка), и он мог бы оказаться домом "
                "этого комментария. Уберите чип из отдельной строки или "
                "правьте документ в интерфейсе.")
    if "matches" in shown and "paragraph" in shown:
        return ("Абзац с комментарием повторяется в документе дословно, "
                "поэтому неясно, к какой копии он относится. Различите копии "
                "в интерфейсе Google Docs — хватит одного слова — и повторите "
                "команду.")
    return ("Разрулите комментарии-призраки в UI (удалить/переоткрыть тред) "
            "или правьте документ в UI.")


def _fence_off_ambiguous(ambiguous, attribution=None, file_id=None):
    """Turn "the anchor is in ONE of these identical paragraphs" into a fence.

    Every candidate gets the range the anchor would occupy inside it, so the
    real one is protected whichever copy it is. Returns (blocked, problems).

    A span that produces NO usable range is a problem, never a silent skip.
    The accounting chain above is load-bearing — a witness record means exactly
    one span, and that span must be PROTECTED — and until #26 "protected" meant
    "placed, or the whole document is frozen". Now it can mean "fenced", so an
    empty fence would quietly leave a thread naked on an editable document
    (found in review). `_table_intervals` skips malformed intervals; here that
    would be exactly the wrong reflex.
    """
    blocked, problems = [], []
    by_range = {}
    for a in ambiguous:
        usable, unusable = [], []
        for st, en in a["candidates"]:
            cs, ce = st + a["start_off"], st + a["end_off"]
            whole = False
            if ce <= cs:
                # A zero-width anchor can never overlap anything: both checks
                # are strict (`_blocked_hits`, `_find_protected_overlap`).
                # Fence the paragraph instead of a range that means nothing.
                # Unreachable through the parser, which refuses an empty
                # anchor earlier — kept because the fence must not depend on
                # someone else's check.
                cs, ce, whole = st, en, True
            # `en - 1` is the paragraph's own newline: the visible text ends
            # before it, so a correct anchor never reaches `en`. Only the
            # whole-paragraph fallback above is allowed that far — and it is
            # marked by a flag, not by comparing values: an anchor that
            # overshoots by exactly one unit would otherwise look identical to
            # a deliberate whole-paragraph fence and be waved through.
            limit = en if whole else en - 1
            (usable if st <= cs < ce <= limit else unusable).append((cs, ce))
        # ANY candidate we cannot fence, not just all of them: the anchor may
        # be in the one that was dropped, and a fence with a hole in it is the
        # thing the accounting chain above treats as proof of protection.
        if unusable or not usable:
            problems.append(
                f"anchor {a['docx_id']} matches several paragraphs and "
                f"{'one of them' if usable else 'none of them'} cannot hold "
                f"it ({(unusable or a['candidates'])[:3]}) — the anchor map "
                f"cannot be trusted (fail closed)")
            continue
        cid = (attribution or {}).get(a["docx_id"])
        link = _thread_link(file_id, cid)
        who = f"треда {cid} {link}" if link else f"docx id {a['docx_id']}"
        label = (
            f"комментарий {who} на абзаце, у которого в документе "
            f"{len(a['candidates'])} одинаковых копий («{a['para_text'][:40]}»): "
            f"какая из них прокомментирована, из выгрузки не видно, поэтому "
            f"правка любой копии отклонена, а остальной документ правится. "
            f"Различите копии в интерфейсе Google Docs — хватит одного слова — "
            f"и повторите команду")
        for rng in usable:
            by_range.setdefault(rng, []).append(label)
    # One fence per range, not per span: two threads on the same duplicated
    # paragraph would otherwise multiply intervals, and `_blocked_hits` walks
    # the list for every single operation.
    for (cs, ce), labels in sorted(by_range.items()):
        blocked.append((cs, ce, labels[0] if len(labels) == 1 else
                        f"{labels[0]} (и ещё таких комментариев: "
                        f"{len(labels) - 1})"))
    return blocked, problems


def _attribute_records_to_threads(anchored, records, universe):
    """Map export records to the threads they belong to: docx_id -> comment_id.

    Same witness rule the accounting proves presence with: a key owned by
    exactly one thread across everything the API can see identifies that
    thread's records, and a record's span is that thread's anchor. Records
    whose key is shared stay unattributed — and those are precisely the ones
    the accounting refuses on anyway.

    Used to name threads in refusals about anchors (#20) and to tell how many
    anchors of one thread an operation would cover.
    """
    live = [c for c in anchored if not c.get("resolved")]
    out = {}
    for c in live:
        cid = c.get("id")
        keys = set()
        entries = [c] + [r for r in (c.get("replies") or [])
                         if not r.get("deleted")]
        for entry in entries:
            author = (entry.get("author") or {}).get("displayName")
            created = entry.get("createdTime")
            if author and created:
                keys.add((author, _trunc_seconds(created)))
        witnesses = {k for k in keys if universe.get(k) == {cid}}
        if not witnesses:
            continue
        for r in records:
            if (r["author"], r["date_sec"]) in witnesses:
                out[r["docx_id"]] = cid
    return out


def _named_range_intervals(doc_tab):
    """All named-range segments as protected intervals [(start, end, name)].

    Named ranges are the machine-owned anchoring mechanism of `mark`/patch
    targeting; silently destroying one breaks future patch ops. Contract
    (codex sync-anchors r2 #5): a malformed segment (missing/non-int
    indices, inverted bounds) while namedRanges exist ⇒ fail closed; each
    valid segment is protected individually (no min/max across gaps).
    """
    out = []
    for name, entry in (doc_tab.get("namedRanges") or {}).items():
        nrs = entry.get("namedRanges") if isinstance(entry, dict) else None
        if not isinstance(nrs, list) or not nrs:
            _error(
                f"named range {name!r} has an unrecognized structure — "
                f"refusing structural edits (fail closed); release the "
                f"mark or edit in the UI")
        segments = 0
        for nr in nrs:
            ranges = nr.get("ranges") if isinstance(nr, dict) else None
            if not isinstance(ranges, list):
                _error(
                    f"named range {name!r} has an unrecognized structure — "
                    f"refusing structural edits (fail closed); release the "
                    f"mark or edit in the UI")
            for rng in ranges:
                s = rng.get("startIndex") if isinstance(rng, dict) else None
                e = rng.get("endIndex") if isinstance(rng, dict) else None
                if not isinstance(s, int) or not isinstance(e, int) or s >= e:
                    _error(
                        f"named range {name!r} has a malformed segment "
                        f"({s!r}..{e!r}) — refusing structural edits "
                        f"(fail closed); release the mark or edit in the UI")
                out.append((s, e, f"named range {name!r}"))
                segments += 1
        if not segments:
            _error(
                f"named range {name!r} exists but carries no ranges — "
                f"refusing structural edits (fail closed); release the "
                f"mark or edit in the UI")
    return out


def _find_protected_overlap(flat, protected):
    """Check every text-removing request against protected intervals.

    `protected` = [(start, end, label)] — union of comment anchor spans
    and named-range segments. insertText is allowed (C5 verified);
    deleteContentRange is checked for ANY overlap (sync removes whole
    paragraphs, so an overlapping anchor is in practice fully covered —
    C1: that ghosts the comment; the only reachable partial overlaps are
    newline edge cases whose deleteContentRange survival semantics are
    unverified, refused too). Any OTHER request type, or a malformed
    range, fails closed too — a future text-destroying request type must
    not slip through silently (codex r2 #6).

    Returns None when everything is provably safe, else a refusal
    message (the caller cleans up its canary before erroring out).
    """
    for req in flat:
        if "insertText" in req:
            continue
        rng = (req.get("deleteContentRange") or {}).get("range")
        if rng is None or len(req) != 1:
            return (
                f"internal: unexpected request type in the text batch "
                f"({sorted(req.keys())}) — anchor protection cannot prove "
                f"it safe (fail closed)")
        s, e = rng.get("startIndex"), rng.get("endIndex")
        if not isinstance(s, int) or not isinstance(e, int) or s >= e:
            return (
                f"internal: malformed deleteContentRange {s!r}..{e!r} in "
                f"the text batch (fail closed)")
        for ps, pe, label in protected:
            if s < pe and ps < e:
                # No generic remedy here: the labels carry their own, and they
                # differ (a healthy anchor is a job for `patch`, a fenced
                # duplicate is not — its quote is not unique either). Advising
                # both at once was how this refusal started contradicting
                # itself (found in review).
                return (
                    f"a sync edit would rewrite text carrying {label} "
                    f"(protected range [{ps}, {pe})) — that would destroy "
                    f"it (C1). Leave that paragraph unchanged locally.")
    return None


def _canary_delete_request(canary):
    return {"deleteContentRange": {"range": {
        "startIndex": canary["start"], "endIndex": canary["end"]}}}


def _cleanup_canary(docs_service, file_id, canary):
    """Best-effort removal of an orphaned canary paragraph.

    Fresh read → the canary text must occur exactly once → pinned delete
    of the preceding newline + the canary text (removes the canary
    paragraph, restores the previous structure). Returns True on success;
    False on ANY failure — callers then warn the user with the literal
    canary text (it is self-describing and harmless junk, not data loss).
    """
    try:
        doc = _safe_get_doc(docs_service, file_id)
        _, doc_tab = _select_tab(doc, tab_id=None)
        if _count_quote_occurrences(doc_tab, canary["text"]) != 1:
            return False
        s, e = _find_quote_in_doctab(doc_tab, canary["text"])
        docs_service.documents().batchUpdate(
            documentId=file_id,
            body={"requests": [{"deleteContentRange": {"range": {
                      "startIndex": s - 1, "endIndex": e}}}],
                  "writeControl": _write_control(doc.get("revisionId"))},
        ).execute()
        return True
    except Exception:
        return False


def _canary_present(docs_service, file_id, canary):
    """Fresh-read probe: True/False, or None when the read itself failed."""
    try:
        doc = _safe_get_doc(docs_service, file_id)
        _, doc_tab = _select_tab(doc, tab_id=None)
        return _count_quote_occurrences(doc_tab, canary["text"]) > 0
    except Exception:
        return None


def _fresh_anchor_snapshot(docs_service, drive_service, file_id, doc,
                           doc_tab, anchored, named_intervals, body_end,
                           *, fp1, universe):
    """Provably-fresh anchor map for structural edits on a commented doc.

    Export freshness is not provable read-only (files.export takes no
    revision), so a CANARY paragraph is inserted at the end of the doc,
    pinned to the planning revision R0; an export that contains the
    canary was generated after that insert, and a revisionId read equal
    to R1 (the insert's revision) closes the window from above — the
    export is exactly R0 + canary. The caller MUST delete the canary as
    the FIRST request of its final atomic batch (pinned to R1): the
    canary sits strictly at the doc end, so deleting it first restores
    R0 coordinates for every other request.

    `fp1` is the fingerprint of the very census that produced `anchored`
    (issue #12) — the caller passes both from one read, so the state the
    accounting trusts is exactly the state the fp1/fp2 sandwich proves stable.

    Returns (snapshot, retry_reason): snapshot is
    {"anchors", "fp1", "canary", "r1", "metrics"} on success;
    (None, reason) after cleanup for retryable races. Hard failures
    clean up the canary and _error (raises PatchOpError in per-op mode).
    """
    # a named range reaching the insertion point could be extended by the
    # canary insert (uncharacterized) — refuse before mutating
    for ps, pe, label in named_intervals:
        if pe >= body_end - 1:
            _error(
                f"{label} reaches the end of the document — the sync "
                f"canary cannot be inserted safely; release the mark or "
                f"edit in the UI")
    canary_text = f"⚓ skrepka-canary-{uuid.uuid4().hex} {_CANARY_NOTE}"
    payload = "\n" + canary_text  # own terminal paragraph (codex r3 #1)
    canary = {"text": canary_text,
              "start": body_end - 1,
              "end": body_end - 1 + _utf16_len(payload)}
    try:
        resp = docs_service.documents().batchUpdate(
            documentId=file_id,
            body={"requests": [{"insertText": {
                      "location": {"index": body_end - 1},
                      "text": payload}}],
                  "writeControl": _write_control(doc.get("revisionId"))},
        ).execute()
    except HttpError as e:
        reason = e.reason if hasattr(e, "reason") else str(e)
        status = getattr(getattr(e, "resp", None), "status", None)
        if status is not None and status < 500:
            # deterministic rejection of the pinned insert: nothing was
            # inserted — a concurrent edit since the planning read;
            # retryable for callers that can re-plan
            return None, f"canary insert rejected (doc changed): {reason}"
        return _canary_insert_ambiguous(docs_service, file_id, canary,
                                        reason)
    except Exception as e:
        return _canary_insert_ambiguous(docs_service, file_id, canary,
                                        str(e))
    def _abort(msg):
        cleaned = _cleanup_canary(docs_service, file_id, canary)
        if not cleaned:
            msg += (f" ВНИМАНИЕ: в конце документа осталась служебная "
                    f"строка «{canary_text}» — удалите её вручную "
                    f"(данные не потеряны).")
        _error(msg)

    # From here on the canary EXISTS in the doc: every exit — including an
    # unexpected exception (even a structurally odd insert RESPONSE) —
    # must go through cleanup (codex code-r1 #3, code-r2 #2).
    try:
        canary["r1"] = (resp.get("writeControl") or {}).get(
            "requiredRevisionId")
        docx_bytes = None
        for attempt in range(3):
            try:
                docx_bytes = drive_service.files().export(
                    fileId=file_id,
                    mimeType="application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document").execute()
            except Exception as e:
                _abort(f"docx export failed: "
                       f"{e.reason if hasattr(e, 'reason') else e}")
            xml = None
            try:
                import io
                import zipfile
                with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
                    xml = z.read("word/document.xml").decode(
                        "utf-8", "replace")
            except Exception as e:
                _abort(f"malformed docx export: {e}")
            if canary_text in xml:
                break
            docx_bytes = None
            time.sleep(1)
        if docx_bytes is None:
            _abort("export does not contain the freshness canary after 3 "
                   "attempts — cannot prove the anchor map is current "
                   "(fail closed)")
        try:
            doc_after = docs_service.documents().get(
                documentId=file_id, fields="revisionId").execute()
        except Exception as e:
            _abort(f"revision re-read failed: "
                   f"{e.reason if hasattr(e, 'reason') else e}")
        if not canary["r1"] or doc_after.get("revisionId") != canary["r1"]:
            # retryable — but only after a PROVEN cleanup, or the next
            # attempt would orphan this canary forever (codex code-r1 #2)
            if not _cleanup_canary(docs_service, file_id, canary):
                _error(
                    f"doc changed while preparing the anchor map, and the "
                    f"canary cleanup failed. ВНИМАНИЕ: в конце документа "
                    f"осталась служебная строка «{canary_text}» — удалите "
                    f"её вручную (данные не потеряны).")
            return None, "doc changed while preparing the anchor map"

        spans, problems, census = _parse_docx_anchor_spans(docx_bytes)
        # the canary paragraph itself carries no anchors and is not part
        # of the R0 snapshot — it cannot match any R0 paragraph (fresh
        # uuid), so it never enters the mapping
        records, rec_problems = _docx_comment_records(docx_bytes)
        acc_problems, metrics = _account_anchored_comments(
            anchored, records, spans, universe=universe, file_id=file_id,
            marker_census=census, doc_tab=doc_tab)
        anchors, map_problems, ambiguous = _map_anchors_to_doc(doc_tab, spans)
        attribution = _attribute_records_to_threads(anchored, records,
                                                    universe)
        # An anchor that matches several identical paragraphs is not placed —
        # every place it could be is fenced instead, so the document stays
        # editable everywhere else (#26).
        amb_blocked, amb_problems = _fence_off_ambiguous(
            ambiguous, attribution=attribution, file_id=file_id)
        all_problems = (problems + rec_problems + acc_problems + map_problems
                        + amb_problems)
        global_problems, blocked = _scope_anchor_problems(
            all_problems, anchors, attribution=attribution, file_id=file_id)
        # An anchor hiding inside a table has no readable offset, but the
        # table's own extent is readable from the API side — so the refusal
        # is confined to the tables instead of the document (r8).
        global_problems, table_blocked = _fence_off_tables(
            global_problems, doc_tab)
        # A thread that vanished from the export has no anchor left to
        # protect, so it no longer freezes the document — but where its old
        # text still stands is fenced, in case it is alive and merely missing
        # (#34).
        ghost_blocked = _fence_off_ghosts(metrics.get("ghosts") or ())
        blocked = blocked + table_blocked + amb_blocked + ghost_blocked
        if global_problems:
            shown = "; ".join(str(p) for p in global_problems[:4])
            _abort(
                "anchor accounting/mapping failed — paragraph "
                "replaces/deletes are blocked (fail closed): "
                + shown + ". " + _anchor_map_remedy(shown))
        # Fenced ranges are checked too, not only placed anchors: an anchor at
        # the very end of the document is exactly what this guard is for, and
        # an ambiguous one is invisible to `anchors` (found in review).
        # Today no fence can actually reach the canary — it is inserted after
        # the last paragraph, and a fence ends at that paragraph's end at the
        # furthest — so this is insurance against a future fence shape, not a
        # live check. Deliberately untested for that reason.
        for as_, ae, who in ([(a[0], a[1], f"docx id {a[3]}") for a in anchors]
                             + [(b[0], b[1], "огороженный диапазон")
                                for b in blocked]):
            if as_ < canary["end"] and canary["start"] < ae:
                _abort(
                    f"a comment anchor ({who}) intersects the "
                    f"canary paragraph — the trailing anchor extended over "
                    f"the insert (unverified territory, fail closed); edit "
                    f"in the UI")
    except (PatchOpError, SystemExit):
        raise  # _abort/_error already handled cleanup
    except Exception as e:
        _abort(f"anchor preflight failed unexpectedly: {e!r}")
    metrics["canary"] = "confirmed"
    metrics["blocked_anchors"] = len(blocked)
    # spans, not intervals: one anchor with ten candidates is ONE anchor we
    # could not place, and a receipt saying "ten" would be a lie
    metrics["ambiguous_anchors"] = len(ambiguous)
    return ({"anchors": anchors, "fp1": fp1, "canary": canary,
             "r1": canary["r1"], "metrics": metrics, "blocked": blocked,
             "attribution": attribution,
             "ghosts": metrics.get("ghosts") or []}, None)


def _canary_insert_ambiguous(docs_service, file_id, canary, reason):
    """The canary insert got a 5xx/transport failure: it may or may not
    have landed. Probe; a present canary is removed before retrying —
    otherwise the next attempt would orphan it (codex code-r1 #1/#2)."""
    present = _canary_present(docs_service, file_id, canary)
    if present is False:
        return None, f"canary insert did not land ({reason})"
    if present is True and _cleanup_canary(docs_service, file_id, canary):
        return None, (f"canary insert landed with a lost response "
                      f"({reason}) — cleaned up")
    _error(
        f"canary insert outcome unknown ({reason}). ВНИМАНИЕ: в конце "
        f"документа могла остаться служебная строка «{canary['text']}» — "
        f"проверьте и удалите её вручную (данные не потеряны).")


def _comments_fingerprint(drive_service, file_id):
    """Re-read the comment state and fingerprint it (the fp2 side).

    Reads through the same helper as the census, so fp1 — which the census
    now hands out directly — and fp2 are the same shape over the same fields
    and compare like for like. Raises on any error; callers treat that as
    fail-stop."""
    return _fingerprint_from_census(_list_comments_raw(drive_service, file_id))


def _refuse_on_suggestions(doc_tab):
    """Any pending suggestion in the TARGET tab blocks the operation.

    Kept for `sync`, which rewrites whole paragraphs through
    deleteContentRange and reconciles the document against a local file — a
    pending suggestion shifts that comparison in ways this round did not
    measure. `patch` judges suggestions by position instead; see
    `_refuse_on_suggestion_range`.
    """
    marker = _scan_suggestions(doc_tab)
    if marker:
        _error(
            f"target tab has pending suggestions ({marker}); accept/reject "
            f"them in the Google Docs UI first — sync is blocked while "
            f"suggestions exist (fail-closed policy, see FINDINGS.md). "
            f"Точечные правки через patch при этом работают."
        )


def _suggestion_intervals(doc_tab):
    """Body ranges carrying a pending suggestion: [(start, end, label)].

    Read off the SUGGESTIONS_INLINE snapshot, which is the same coordinate
    space every write uses. Anything suggested OUTSIDE the body — in a header,
    a footnote, the document style — has no body coordinates and is not
    represented here; nothing written into the body lands inside it either.
    """
    out = []
    body = doc_tab.get("body", {}) or {}
    for el in body.get("content", []):
        s, e = el.get("startIndex"), el.get("endIndex")
        if not isinstance(s, int) or not isinstance(e, int) or s >= e:
            continue
        para = el.get("paragraph")
        if not isinstance(para, dict):
            if _scan_suggestions(el):
                out.append((s, e, "предложенная правка"))
            continue
        # paragraph-level suggestions (style, bullets) cover the paragraph
        if any(k.startswith("suggested") and v for k, v in para.items()):
            out.append((s, e, "предложенная правка абзаца"))
            continue
        for pe in para.get("elements", []):
            ps, pe_end = pe.get("startIndex"), pe.get("endIndex")
            if not isinstance(ps, int) or not isinstance(pe_end, int):
                continue
            if _scan_suggestions(pe):
                out.append((ps, pe_end, "предложенная правка"))
    return out


def _refuse_on_suggestion_at(doc_tab, index, source):
    """An insert is refused only when it lands INSIDE a pending suggestion.

    Before r8 one unaccepted suggestion anywhere in the tab blocked every
    structural edit, inserts included — even in another paragraph. An insert
    removes nothing, so a suggestion it does not touch is none of its
    business; landing inside one is still refused, because what the text
    belongs to (the suggestion or the document) would be anyone's guess.
    """
    for s, e, label in _suggestion_intervals(doc_tab):
        if s < index < e:
            _error(
                f"вставка попадает внутрь предложенной правки ({label}, "
                f"диапазон [{s}, {e})) — примите или отклоните её в интерфейсе "
                f"Google Docs. Остальной документ правится. ({source})")


def _refuse_on_suggestion_range(doc_tab, start, end, source):
    """A replace is refused only when its range meets a pending suggestion.

    The document-wide refusal was a proxy for "the export might be
    untrustworthy on a document with tracked changes". Measured on a live
    document (2026-08-05): the export carries a pending suggestion as a real
    `w:ins`, paragraph texts match the API snapshot exactly, and the whole
    anchor pipeline — parse, account, map — comes out clean. So trust is now
    established directly, per document, by machinery that already fails
    closed: an anchor sharing a paragraph with tracked changes makes that
    paragraph unparseable and refuses on its own.

    What is still not measured is what `replaceAllText` does to a match that
    OVERLAPS suggested text. That is exactly what this refuses.
    """
    for s, e, label in _suggestion_intervals(doc_tab):
        if _ranges_overlap(start, end, s, e):
            _error(
                f"замена задевает предложенную правку ({label}, диапазон "
                f"[{s}, {e})) — примите или отклоните её в интерфейсе Google "
                f"Docs. Остальной документ правится. ({source})")


def _ops_overlap_conflicts(indexed):
    """Which ops in this file fight over the same text: {op index -> why}.

    `indexed` is {index: resolved op}. Both members of a conflicting pair are
    reported, because applying either one moves the other's target — there is
    no «first one wins» here, only an ambiguity the caller has to split.

    This used to `_error` and take the whole file down. Nothing has been
    written at this point, and the ops that do not overlap anything are
    unaffected by the ambiguity, so refusing them too was pure loss (#36).
    """
    conflicts = {}
    order = sorted(indexed, key=lambda i: (indexed[i]["affect_start"],
                                           indexed[i]["affect_end"]))
    for pos in range(len(order) - 1):
        i, j = order[pos], order[pos + 1]
        a, b = indexed[i], indexed[j]
        if _ranges_overlap(a["affect_start"], a["affect_end"],
                           b["affect_start"], b["affect_end"]):
            why = (f"ops overlap: {a['source']} and {b['source']} — "
                   f"split into separate patches")
            conflicts[i] = why
            conflicts[j] = why
    return conflicts


def _op_source_label(op, resolved=None):
    """How an operation is named in a receipt, resolved or not.

    A deferred op has no resolved record to take `source` from, and it still
    has to be nameable: «which of my ten edits is this» is the first thing a
    person asks of a refusal.
    """
    if resolved:
        return resolved["source"]
    if isinstance(op, dict):
        if "range" in op:
            return f"range={op['range']!r}"
        if "quote" in op:
            return f"quote={op['quote']!r}"
    return json.dumps(op, ensure_ascii=False)[:80]


def _common_affixes(a, b):
    """Longest common prefix and suffix of two strings, in code points.

    Capped so the two cannot overlap: without the cap «да, да, да» → «да, да»
    reports a prefix and a suffix that together exceed the string and the
    narrowed core comes out with negative length.
    """
    n = min(len(a), len(b))
    p = 0
    while p < n and a[p] == b[p]:
        p += 1
    s = 0
    while s < n - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    return p, s


def _points_for_units(text, units):
    """Leading code points of `text` covering at least `units` UTF-16 units.

    Doc indices are UTF-16 code units, Python strings are code points. Cutting
    slightly MORE than asked is safe here (a bigger cut only moves the border
    further from the anchor it is protecting); cutting less is not, so a
    surrogate pair is never split in the shrinking direction.
    """
    if units <= 0:
        return 0
    seen = 0
    for i, ch in enumerate(text):
        seen += 2 if ord(ch) > 0xFFFF else 1
        if seen >= units:
            return i + 1
    return len(text)


def _pure_insertion(search_text, new_text):
    """(where, text) when the replacement removes no original character.

    A replacement that merely extends its target — «…предложение.» →
    «…предложение. И ещё фраза.» — is textually an insertion, and an insertion
    cannot ghost anything (C5). It used to be refused as «fully covers the
    anchor», which is the single most common thing a person asks for on a
    commented sentence.
    """
    if len(new_text) <= len(search_text):
        return None
    if new_text.startswith(search_text):
        return "after", new_text[len(search_text):]
    if new_text.endswith(search_text):
        return "before", new_text[:len(new_text) - len(search_text)]
    return None


def _op_current_text(op, doc_tab, r):
    """The text a replace op currently occupies, or None if it is not one.

    Precedence mirrors `_resolve_op` exactly — `range` first, `quote` second.
    An op carrying BOTH is accepted there and targets the named range; reading
    the quote instead would classify it against text the op does not touch,
    and a no-op or a pure insertion decided on the wrong text writes the wrong
    thing (found in review).
    """
    if r["kind"] != "replace":
        return None
    if "range" in op:
        return _extract_exact_text_range(doc_tab, r["start"], r["end"])
    if "quote" in op:
        return op["quote"]
    return None


def _op_pure_insertion(op, doc_tab, r):
    """`_pure_insertion` for a resolved op, or None if it removes text.

    An op is judged by what it DOES, not by the word in its name: a replace
    whose new text merely extends the old removes nothing, so every rule that
    exists to protect text from removal has no business with it.
    """
    current = _op_current_text(op, doc_tab, r)
    if not current:
        return None
    return _pure_insertion(current, r["text"])


def _op_is_noop(op, doc_tab, r):
    """True for a replace whose new text is the text already there.

    It used to take the full destructive path: delete the text and insert the
    identical text back, with everything that hangs off a rewrite — styles,
    the canary, the anchor gates. Nothing about the document changes, so
    nothing about the document should be written (found in review).
    """
    current = _op_current_text(op, doc_tab, r)
    return current is not None and current == r["text"]


def _doomed_threads(start, end, anchors, attribution=None):
    """Threads a replace over [start, end) would ghost.

    A thread dies when its LAST surviving anchor is covered, not when any one
    of them is (measured: a replace covering two anchors of three left the
    thread alive on the third). So coverage is judged per thread wherever the
    export records can be attributed to one; an anchor nobody could attribute
    keeps the strict rule — for it, "the last one" is unknowable.

    Returns [(comment_id_or_None, [(as, ae, text, docx_id), ...])].
    """
    covered = lambda s, e: start <= s and e <= end  # noqa: E731
    by_thread, loose = {}, []
    for span in anchors:
        cid = (attribution or {}).get(span[3])
        if cid:
            by_thread.setdefault(cid, []).append(span)
        else:
            loose.append(span)
    doomed = [(cid, spans) for cid, spans in by_thread.items()
              if all(covered(s[0], s[1]) for s in spans)]
    doomed += [(None, [span]) for span in loose
               if covered(span[0], span[1])]
    return doomed


def _blocked_hits(start, end, blocked):
    """Blocked ranges a replace over [start, end) would touch (any overlap)."""
    return [(bs, be, label) for bs, be, label in blocked
            if start < be and bs < end]


def _narrow_replace(doc_tab, search_text, new_text, start, end,
                    anchors, blocked, attribution=None):
    """Shrink a replace to the part that actually changes, so a thread lives.

    Not "cut the whole common affix": that collapses the core to a word and
    loses uniqueness exactly in the typical case (a long quote with one word
    changed). It is enough that the left border moves PAST the anchor's first
    character or the right border BEFORE its last one, so the cuts considered
    are only the critical ones, and the core stays nearly the whole quote.

    Returns (search, new, start, end) for a variant that conflicts with
    nothing, or None — in which case the caller refuses exactly as it did
    before. A failed narrowing is never reported: it is an internal attempt,
    not a new class of error.
    """
    if not search_text or not new_text:
        return None  # a deletion: replaceAllText with an empty replacement
        #              is not characterized, and nothing survives a cut anyway
    prefix, suffix = _common_affixes(search_text, new_text)
    if not prefix and not suffix:
        return None
    l_max = _utf16_len(search_text[:prefix])
    r_max = _utf16_len(search_text[len(search_text) - suffix:]) if suffix else 0

    # what each conflict needs from a cut, in UTF-16 units
    l_cand, r_cand = {0}, {0}
    for _cid, spans in _doomed_threads(start, end, anchors, attribution):
        for as_, ae, _t, _i in spans:
            l_cand.add(as_ - start + 1)
            r_cand.add(end - ae + 1)
    for bs, be, _label in _blocked_hits(start, end, blocked):
        l_cand.add(be - start)
        r_cand.add(end - bs)
    # Style boundaries are candidates too. A paragraph that opens with a bold
    # lead-in spans mixed styles, and replaceAllText flattens those (C2) — so
    # the whole operation was refused even when the bold part was not what
    # changed. Cutting to the boundary leaves it alone (found in review).
    body = doc_tab.get("body", {}) or {}
    for rs, re_, _tr in _extract_runs_full(body.get("content", [])):
        if start < re_ < end:
            l_cand.add(re_ - start)
        if start < rs < end:
            r_cand.add(end - rs)
    l_cand = sorted(c for c in l_cand if 0 <= c <= l_max)
    r_cand = sorted(c for c in r_cand if 0 <= c <= r_max)

    for total in sorted({a + b for a in l_cand for b in r_cand}):
        for L in l_cand:
            R = total - L
            if R not in r_cand:
                continue
            pl = _points_for_units(search_text, L)
            sl = _points_for_units(search_text[::-1], R)
            if pl + sl >= len(search_text) or pl + sl >= len(new_text):
                continue
            core = search_text[pl:len(search_text) - sl]
            repl = new_text[pl:len(new_text) - sl]
            if not core or not repl or "\n" in core:
                continue
            start2 = start + _utf16_len(search_text[:pl])
            end2 = end - _utf16_len(search_text[len(search_text) - sl:]
                                    if sl else "")
            if _doomed_threads(start2, end2, anchors, attribution):
                continue
            if _blocked_hits(start2, end2, blocked):
                continue
            if _replace_all_match_count(doc_tab, core) != 1:
                continue
            if _find_quote_in_doctab(doc_tab, core) != (start2, end2):
                continue
            uniform, _differing = _match_style_signature(doc_tab, start2, end2)
            if not uniform:
                continue
            return core, repl, start2, end2
    return None


def _style_refusal(doc_tab, start, end, source):
    """Refusal message when a replace range spans mixed styles, or None."""
    uniform, differing = _match_style_signature(doc_tab, start, end)
    if uniform:
        return None
    return (
        f"replace target spans mixed text styles ({', '.join(differing)}) "
        f"— replaceAllText would flatten them (confirmed, C2). Split the "
        f"replacement into uniformly-styled pieces or edit in the UI. "
        f"({source})")


# Control characters — a newline among them, it starts a new paragraph —
# and the Private Use Area, which Docs is known to strip. Any of these
# and the text that lands is not what the positions were computed for.
_REWRITE_FORBIDDEN = re.compile("[\x00-\x1f\x7f-\x9f\ue000-\uf8ff]")


def _distinct_anchor_ranges(spans):
    """Ranges of a set of anchor spans, one entry per distinct range.

    A thread exports one record per ENTRY, and every one of them carries the
    parent's range — a thread with a single reply comes back with two
    identical anchors. Counting spans instead of ranges would mean the rewrite
    never fires on a thread anybody replied to.
    """
    return sorted({(s, e) for s, e, _t, _i in spans})


def _rewrite_anchor_requests(doc_tab, search_text, new_text, start, end,
                             anchors, attribution, named_intervals,
                             has_resolved):
    """Requests that rewrite a fully-covered anchor without ghosting it.

    A person does this by hand: type the new text INSIDE the comment's
    selection, then delete the old. Measured (docs/FINDINGS.md): the anchor
    grows over an insert made strictly inside it, survives a replace whose
    match leaves one of its characters alone, and survives the deletion of
    that last character — the thread dies only if the anchor collapses to
    nothing, not when the original text goes. All three fit in ONE atomic
    batch, so no half-rewritten state ever exists.

    Returns (requests, tail_len) or None. **None means the caller refuses
    exactly as it did before** — this is an added capability, not a new
    default, so every condition below can be as strict as it likes: the cost
    of getting one wrong is a feature that did not fire, never a damaged
    document.
    """
    if not new_text or _REWRITE_FORBIDDEN.search(new_text):
        # Docs strips some control characters and turns \n into a paragraph
        # break, so the text that lands would not be the text we measured
        # positions for.
        return None
    if "\n" in search_text:
        # `replaceAllText` does not match across a paragraph break, so request
        # 2 would change nothing while requests 1 and 3 landed anyway: text
        # damaged, outcome unknown. Unreachable before #45 made cross-paragraph
        # anchors placeable, and checked explicitly rather than left to
        # `_REWRITE_FORBIDDEN`, which only looks at the REPLACEMENT text.
        return None
    if has_resolved:
        # Closed threads are invisible to the export, so their anchors could
        # be anywhere — including under the character this batch deletes, and
        # deleteContentRange is what turns a closed thread into a ghost.
        # `patch` never used deletion before; it is not going to start by
        # gambling with a thread it cannot see.
        return None
    if any("tableOfContents" in el for el in
           (doc_tab.get("body", {}) or {}).get("content", [])):
        # The text walkers do not read a table of contents, so the local
        # uniqueness count would not see a match hiding there while
        # replaceAllText may well rewrite it (#23).
        return None
    for ns, ne, _label in named_intervals:
        if _ranges_overlap(start, end, ns, ne):
            return None  # the deletion would cut a machine-owned mark

    # NB: `anchors` holds only PLACED anchors. An anchor that matched several
    # identical paragraphs is not in there — it lives in the fence. What keeps
    # the deletion in this batch away from such an anchor is the caller's
    # `if hits: _error(...)`, which stands BEFORE any write and before the
    # `doomed` refusal; the `not hits` in the branch that calls this function
    # only avoids pointless work. Moving or weakening that check — not this
    # comment's neighbours — is what would remove the guarantee.
    doomed = _doomed_threads(start, end, anchors, attribution)
    if len(doomed) != 1:
        return None
    ranges = _distinct_anchor_ranges(doomed[0][1])
    if ranges != [(start, end)]:
        # the target must be exactly the anchor: an anchor strictly inside a
        # wider replace would need the original removed on BOTH sides, and a
        # deletion reaching into an anchor from outside is the one shape that
        # was measured to damage text
        return None
    doomed_cid = doomed[0][0]
    for as_, ae, _t, aid in anchors:
        if not _ranges_overlap(start, end, as_, ae):
            continue
        if doomed_cid is not None and attribution.get(aid) == doomed_cid:
            continue  # our own thread — its replies duplicate its range
        # Anyone else's anchor, or one nobody could attribute. A neighbour
        # touching the tail character would meet the deletion from outside,
        # and only strictly-inside deletion is measured safe. Identical
        # ranges are NOT an exception: two comments on the same selection
        # export byte-identical ranges, so «same range» does not mean «same
        # thread» (found in review). An unattributable span is refused for
        # the same reason — it may be our own reply's duplicate or a whole
        # other thread, and nothing in the geometry tells them apart.
        return None

    if len(search_text) < 2:
        # Two CODE POINTS, not two UTF-16 units: one emoji is two units but a
        # single character, and there is no position strictly inside it.
        return None
    head, tail = search_text[:-1], search_text[-1]
    tail_len = _utf16_len(tail)

    # Project the document as it will read between request 1 and request 2 and
    # verify there that the search string is unique AND lands where we think.
    # It cannot be checked against the live document: it does not exist yet.
    buf, index_map = _text_buffer(doc_tab)
    ins_at = end - tail_len
    try:
        pos = index_map.index(ins_at)
        at = index_map.index(start)
    except ValueError:
        # A position with no place in the text buffer. Refusing is the whole
        # answer here: raising would escape a PRECONDITION check as an
        # exception, and `patch` would read that as an unknown document state
        # and stop every remaining operation.
        return None
    projected = buf[:pos] + new_text + buf[pos:]
    needle = head + new_text
    if _count_in_buffer(projected, needle) != 1:
        return None
    if projected.find(needle) != at:
        return None
    if _count_text_outside_body(doc_tab, needle):
        return None

    requests = [
        {"insertText": {"location": {"index": ins_at}, "text": new_text}},
        {"replaceAllText": {"containsText": {"text": needle,
                                             "matchCase": True},
                            "replaceText": new_text}},
        {"deleteContentRange": {"range": {
            "startIndex": start + _utf16_len(new_text),
            "endIndex": start + _utf16_len(new_text) + tail_len}}},
    ]
    return requests, tail_len


def _execute_anchor_rewrite(docs_service, file_id, tid, requests, revision_id,
                            source, extra_requests_before=None):
    """Send the rewrite batch and read its verdict.

    Unlike `_execute_replace_all`, `occurrencesChanged` here is diagnosis
    AFTER the write, never a precondition: the insert and the deletion land
    whatever the replace matched, so anything other than exactly one match
    means the document has been changed in a way nobody planned. There is no
    `not_applied` outcome to report.
    """
    for req in requests:
        if tid and "replaceAllText" in req:
            req["replaceAllText"]["tabsCriteria"] = {"tabIds": [tid]}
        elif tid and "insertText" in req:
            req["insertText"]["location"]["tabId"] = tid
        elif tid and "deleteContentRange" in req:
            req["deleteContentRange"]["range"]["tabId"] = tid
    body = {"requests": list(extra_requests_before or []) + requests,
            "writeControl": _write_control(revision_id)}
    result = docs_service.documents().batchUpdate(
        documentId=file_id, body=body).execute()
    occ = next((rep.get("replaceAllText", {}) for rep in
                (result.get("replies") or []) if "replaceAllText" in rep),
               {}).get("occurrencesChanged")
    if occ != 1:
        raise PatchOpError(
            f"перезапись якоря: replaceAllText сообщил {occ!r} совпадений "
            f"вместо одного ({source}). Вставка и удаление в этом батче "
            f"выполняются в любом случае, поэтому документ изменён — "
            f"проверьте его состояние глазами, повторять вслепую нельзя",
            state="unknown")


def _resolve_replace_target(op, doc_tab, r, check_style=True):
    """Shared replace-target checks on a given snapshot: exact text,
    uniqueness, round-trip, style uniformity. Returns search_text.

    `check_style=False` defers the style check to the caller. The anchor-safe
    path needs that: it may narrow the range, and a mixed style sitting in the
    part the narrowing would leave alone must not decide the whole operation
    before narrowing has been tried (found in review).
    """
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

    total = _replace_all_match_count(doc_tab, search_text)
    if total != 1:
        outside = _count_text_outside_body(doc_tab, search_text)
        where = (f" ({outside} of them in a header/footer/footnote, which "
                 f"replaceAllText also rewrites)" if outside else "")
        _error(
            f"replace target must be unique in tab for the anchor-safe "
            f"path, found {total} matches{where} ({r['source']}); provide a "
            f"longer unique quote"
        )
    # Round-trip: the unique match must be exactly the resolved range.
    found = _find_quote_in_doctab(doc_tab, search_text)
    if found != (r["start"], r["end"]):
        _error(
            f"resolved text for {r['source']} matches a different "
            f"location ({found} vs ({r['start']}, {r['end']})) — refused"
        )
    if check_style:
        problem = _style_refusal(doc_tab, r["start"], r["end"], r["source"])
        if problem:
            _error(problem)
    return search_text


def _execute_replace_all(docs_service, file_id, tid, search_text, new_text,
                         revision_id, source, extra_requests_before=None):
    req = {"replaceAllText": {
        "containsText": {"text": search_text, "matchCase": True},
        "replaceText": new_text,
    }}
    if tid:
        req["replaceAllText"]["tabsCriteria"] = {"tabIds": [tid]}
    requests = list(extra_requests_before or []) + [req]
    result = docs_service.documents().batchUpdate(
        documentId=file_id,
        body={"requests": requests,
              "writeControl": _write_control(revision_id)},
    ).execute()
    # the reply is located by TYPE, not position: a canary delete prepended
    # to the batch shifts positional indices (codex sync-anchors r3 #2)
    occ = next((r.get("replaceAllText", {}) for r in
                (result.get("replies") or []) if "replaceAllText" in r),
               {}).get("occurrencesChanged", 0)
    if occ != 1:
        # occ == 0: nothing changed; occ > 1: the doc HAS been modified in
        # multiple places — the report must not invite a blind retry.
        raise PatchOpError(
            f"replaceAllText changed {occ} occurrences (expected exactly "
            f"1) for {source} — stopping; verify the doc state",
            state="not_applied" if occ == 0 else "unknown",
        )


def _apply_op_anchor_safe(docs_service, drive_service, file_id, op, tab_id,
                          warnings=None):
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
        r = _resolve_op(op, doc_tab, tid)
        _refuse_on_suggestion_at(doc_tab, r["start"], r["source"])
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

    # ---- replace path (W8 mapping + freshness canary, plan v4) ----
    last_reason = "unknown"
    for attempt in range(3):
        # Everything before the canary insert is read-only: an HTTP failure
        # here means the op was definitely NOT applied (codex W8-r1 P2#5).
        try:
            doc = _safe_get_doc(docs_service, file_id)
        except Exception as e:
            raise PatchOpError(
                f"W8 preflight read failed: "
                f"{e.reason if hasattr(e, 'reason') else e}",
                state="not_applied")
        if len(_collect_tabs(doc)) > 1:
            # DOCX ranges carry no tab identity (addendum p.6)
            _error("multi-tab document: anchor-mapped replaces are not "
                   "supported — edit in the UI")
        tid, doc_tab = _select_tab(doc, tab_id=tab_id)
        r = _resolve_op(op, doc_tab, tid)

        if _op_is_noop(op, doc_tab, r):
            return {"source": r["source"], "applied_as": "no-op",
                    "note": "текст уже такой — ничего не записано"}

        # A replacement that only EXTENDS its target removes no original
        # character, so no anchor can be ghosted by it and none of the export
        # machinery below is needed (r8). This is the common «допиши фразу к
        # прокомментированному предложению», which used to be refused.
        insertion = _op_pure_insertion(op, doc_tab, r)
        if insertion:
            where, text = insertion
            point = r["end"] if where == "after" else r["start"]
            _refuse_on_suggestion_at(doc_tab, point, r["source"])
            loc = {"index": point}
            if tid:
                loc["tabId"] = tid
            docs_service.documents().batchUpdate(
                documentId=file_id,
                body={"requests": [{"insertText": {"location": loc,
                                                   "text": text}}],
                      "writeControl": _write_control(doc.get("revisionId"))},
            ).execute()
            return {"source": r["source"], "applied_as": "insert",
                    "note": "замена ничего не удаляла — применена как вставка"}

        _refuse_on_suggestion_range(doc_tab, r["start"], r["end"],
                                    r["source"])
        search_text = _resolve_replace_target(op, doc_tab, r,
                                              check_style=False)
        body_content = (doc_tab.get("body", {}) or {}).get("content", [])
        body_end = body_content[-1]["endIndex"] if body_content else 2
        named_intervals = _named_range_intervals(doc_tab)
        _, anchored_now, fp1, universe = _census_comments(
            drive_service, file_id)

        snap, retry_reason = _fresh_anchor_snapshot(
            docs_service, drive_service, file_id, doc, doc_tab,
            anchored_now, named_intervals, body_end, fp1=fp1,
            universe=universe)
        if snap is None:
            last_reason = retry_reason
            continue
        if warnings is not None:
            # Collected for the caller to print ONCE. A vanished thread is a
            # property of the document, not of this operation — repeating it
            # per op would be ten copies of the same line on a ten-edit file.
            for g in snap.get("ghosts") or ():
                warnings.setdefault(g.get("id"), g)
        canary = snap["canary"]

        def _canary_msg(msg, cleaned):
            if not cleaned:
                msg += (f" ВНИМАНИЕ: в конце документа осталась служебная "
                        f"строка «{canary['text']}» — удалите её вручную "
                        f"(данные не потеряны).")
            return msg

        # What this operation would cost as written. Blocked ranges: an anchor
        # the accounting could not vouch for but WHOSE POSITION is known — any
        # overlap counts, unlike a healthy anchor its survival under a partial
        # rewrite is not something we can reason about. Doomed threads: full
        # coverage ghosts a comment (C1), partial overlap is verified safe.
        # (Inserts never reach here; they cannot remove text.)
        attribution = snap.get("attribution") or {}
        # hoisted: the refusal below has to say WHY the rewrite was off, and
        # a closed thread is the reason an agent cannot guess (#24)
        has_resolved = any(c.get("resolved") for c in anchored_now)
        start_at, end_at, applied_text = r["start"], r["end"], r["text"]
        hits = _blocked_hits(start_at, end_at, snap["blocked"])
        doomed = _doomed_threads(start_at, end_at, snap["anchors"], attribution)
        # mixed styles are a reason to narrow too, not only a reason to refuse:
        # replaceAllText flattens them, a range that avoids them does not
        style_problem = _style_refusal(doc_tab, start_at, end_at, r["source"])
        narrowed = None
        if hits or doomed or style_problem:
            narrowed = _narrow_replace(
                doc_tab, search_text, r["text"], start_at, end_at,
                snap["anchors"], snap["blocked"], attribution)
        rewrite = None
        if narrowed:
            # everything was re-checked inside the narrowing, on the range
            # that will actually be written
            search_text, applied_text, start_at, end_at = narrowed
            hits, doomed, style_problem = [], [], None
        elif doomed and not hits and not style_problem:
            # Narrowing could not save the thread, so try what a person does
            # by hand: write the new text inside the comment's selection and
            # take the old one out. Every precondition is inside; None here
            # means the refusal below stands exactly as before.
            rewrite = _rewrite_anchor_requests(
                doc_tab, search_text, r["text"], start_at, end_at,
                snap["anchors"], attribution, named_intervals,
                has_resolved=has_resolved)
            if rewrite:
                doomed = []
        if style_problem:
            cleaned = _cleanup_canary(docs_service, file_id, canary)
            _error(_canary_msg(style_problem, cleaned))
        if hits:
            bs, be, label = hits[0]
            cleaned = _cleanup_canary(docs_service, file_id, canary)
            _error(_canary_msg(
                f"эта замена задевает {label} (диапазон [{bs}, {be})) — "
                f"отклонена ИМЕННО ЭТА операция, остальной документ "
                f"правится. ({r['source']})", cleaned))
        if doomed:
            cid, spans = doomed[0]
            atext = spans[0][2]
            link = _thread_link(file_id, cid)
            who = (f"треда {cid} {link}" if link
                   else f"комментария (docx id {spans[0][3]})")
            cleaned = _cleanup_canary(docs_service, file_id, canary)
            # Since #21 a fully-covered anchor CAN be rewritten whole. When
            # that did not happen, the refusal must say what stopped it —
            # otherwise the agent reads «impossible» and goes looking for the
            # destructive path (#24).
            # Was the closed thread ACTUALLY what stopped it? The rewrite has
            # half a dozen other preconditions (a table of contents in the
            # tab, a named range in the way, a neighbour's anchor…), and
            # naming the wrong one sends the person to reopen threads for
            # nothing (found in review). The check is a pure re-run.
            blocked_by_closed = has_resolved and _rewrite_anchor_requests(
                doc_tab, search_text, r["text"], start_at, end_at,
                snap["anchors"], attribution, named_intervals,
                has_resolved=False) is not None
            why = (
                " Переписать прокомментированный фрагмент целиком, не теряя "
                "тред, skrepka умеет — но не на этом документе: в нём есть "
                "закрытый тред, его якорь в выгрузке не виден, а перезапись "
                "содержит удаление и может такой якорь расцепить. Переоткройте "
                "или удалите закрытые треды в интерфейсе, если нужна полная "
                "перезапись фрагмента."
                if blocked_by_closed else
                " Уцелеть должен ИСХОДНЫЙ символ якоря — повтор того же "
                "текста в замене не помогает. Оставьте в замене часть "
                "исходного якорного текста нетронутой или правьте этот "
                "фрагмент в интерфейсе.")
            _error(_canary_msg(
                f"замена накрывает целиком последний якорь {who} "
                f"(текст якоря «{atext[:60]}») — тред станет призраком (C1), "
                f"и сузить её не получилось: меняется весь якорный текст."
                f"{why} ({r['source']})", cleaned))
        try:
            fp2 = _comments_fingerprint(drive_service, file_id)
        except Exception as e:
            cleaned = _cleanup_canary(docs_service, file_id, canary)
            raise PatchOpError(_canary_msg(
                f"W8 final census failed: "
                f"{e.reason if hasattr(e, 'reason') else e}", cleaned),
                state="not_applied")
        if fp2 != snap["fp1"]:
            # retry only after a PROVEN cleanup — otherwise the next
            # attempt orphans this canary forever (codex code-r1 #2)
            if not _cleanup_canary(docs_service, file_id, canary):
                raise PatchOpError(
                    f"comments changed during mapping and the canary "
                    f"cleanup failed. ВНИМАНИЕ: в конце документа осталась "
                    f"служебная строка «{canary['text']}» — удалите её "
                    f"вручную (данные не потеряны).",
                    state="not_applied")
            last_reason = "comments changed during mapping"
            continue
        # no other network calls between the final census and the write;
        # the batch deletes the canary FIRST (atomic with the replace)
        try:
            if rewrite:
                _execute_anchor_rewrite(
                    docs_service, file_id, tid, rewrite[0], snap["r1"],
                    r["source"],
                    extra_requests_before=[_canary_delete_request(canary)])
            else:
                _execute_replace_all(
                    docs_service, file_id, tid, search_text, applied_text,
                    snap["r1"], r["source"],
                    extra_requests_before=[_canary_delete_request(canary)])
        except PatchOpError:
            raise  # occurrence mismatch: batch APPLIED, canary already gone
        except HttpError as e:
            reason = e.reason if hasattr(e, "reason") else str(e)
            status = getattr(getattr(e, "resp", None), "status", None)
            if status is not None and status < 500:
                # deterministic rejection of a pinned atomic batch ⇒ the
                # write did not land (codex sync-anchors r3 #3)
                cleaned = _cleanup_canary(docs_service, file_id, canary)
                raise PatchOpError(_canary_msg(
                    f"anchor-safe replace batch rejected: {reason}",
                    cleaned), state="not_applied")
            raise PatchOpError(
                *_ambiguous_batch_outcome(
                    docs_service, file_id, canary,
                    f"anchor-safe replace batch failed: {reason}"))
        except Exception as e:
            raise PatchOpError(
                *_ambiguous_batch_outcome(
                    docs_service, file_id, canary,
                    f"anchor-safe replace batch failed (transport): {e}"))
        if narrowed:
            # the operation that ran is textually not the one that was asked
            # for — the receipt has to say so
            return {"source": r["source"], "applied_as": "narrowed",
                    "narrowed_to": search_text,
                    "note": "замена сужена до изменённого фрагмента, чтобы "
                            "сохранить тред"}
        if rewrite:
            return {"source": r["source"], "applied_as": "rewritten",
                    "note": "фрагмент переписан целиком; тред перевешен на "
                            "новый текст и теперь относится к тексту, "
                            "которого не было, когда комментарий писали"}
        return None
    _error(
        f"anchor-mapped replace kept failing preflight after 3 attempts "
        f"({last_reason}) — the doc is being edited concurrently; retry later"
    )


def _ambiguous_batch_outcome(docs_service, file_id, canary, msg):
    """Classify a timeout/5xx after an atomic batch whose FIRST request
    deletes the canary. The canary still present ⇒ the batch did not land
    (atomicity) ⇒ safe to clean up and report not_applied. Canary absent
    or unreadable ⇒ outcome unknown — its absence alone is NOT proof of
    full application (a collaborator could have removed the junk line);
    callers must verify expected state (codex sync-anchors r3 #3).
    Returns (message, state) for PatchOpError.
    """
    present = _canary_present(docs_service, file_id, canary)
    if present is True:
        cleaned = _cleanup_canary(docs_service, file_id, canary)
        note = " (canary intact ⇒ batch not applied)"
        if not cleaned:
            note += (f" ВНИМАНИЕ: в конце документа осталась служебная "
                     f"строка «{canary['text']}» — удалите её вручную.")
        return msg + note, "not_applied"
    if present is False:
        return (msg + " (canary gone ⇒ batch likely applied — verify the "
                      "doc before retrying)"), "unknown"
    return msg + " (doc unreadable — outcome unknown)", "unknown"


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

    # Resolve every op against the current snapshot (validates targets early
    # for both paths) and classify what each one actually does.
    #
    # Per-op refusal used to live only INSIDE the apply loop, so everything
    # rejected earlier still took the whole file down with it: one non-unique
    # quote cost the person 36 correct edits in a live session (#36). Nothing
    # here writes anything, so a failure here is never a reason to throw away
    # the operations it has no relation to.
    global _RAISE_ERRORS
    resolved, insertions, noops = [], [], []
    deferred, early_refusals = {}, {}
    _RAISE_ERRORS = True
    try:
        for i, op in enumerate(ops):
            try:
                r = _resolve_op(op, doc_tab, tid)
                ins = _op_pure_insertion(op, doc_tab, r)
                noop = _op_is_noop(op, doc_tab, r)
            except PatchOpError as e:
                resolved.append(None)
                insertions.append(None)
                noops.append(False)
                deferred[i] = str(e)
                continue
            resolved.append(r)
            insertions.append(ins)
            noops.append(noop)
    finally:
        _RAISE_ERRORS = False

    # Ambiguous batches are rejected — but an op that writes nothing occupies
    # nothing, and used to veto a perfectly compatible neighbour purely by
    # holding a range (found in review). Both members of an overlapping pair
    # are refused: applying either one moves the other's target.
    early_refusals.update(_ops_overlap_conflicts(
        {i: r for i, r in enumerate(resolved) if r and not noops[i]}))

    # Suggestions are judged by position, not by their existence anywhere in
    # the tab (r8, after the export was measured). An op that removes nothing
    # is checked as a point; a replace is checked as a range.
    _RAISE_ERRORS = True
    try:
        for i, (r, ins, noop) in enumerate(zip(resolved, insertions, noops)):
            if r is None or noop or i in early_refusals:
                continue  # unresolved, writes nothing at all, or already out
            try:
                if r["kind"] == "insert" or ins:
                    point = r["start"]
                    if ins and ins[0] == "after":
                        point = r["end"]
                    _refuse_on_suggestion_at(doc_tab, point, r["source"])
                else:
                    _refuse_on_suggestion_range(doc_tab, r["start"], r["end"],
                                                r["source"])
            except PatchOpError as e:
                early_refusals[i] = str(e)
    finally:
        _RAISE_ERRORS = False

    _all, anchored, _, _ = _census_comments(drive_service, file_id)

    if not anchored:
        # ---- clean-doc path: single atomic index-based batch ----
        # Second census immediately before the destructive batch narrows the
        # race window (a comment added in between would change the strategy).
        _, anchored2, _, _ = _census_comments(drive_service, file_id)
        if anchored2:
            _error(
                "an anchored comment appeared while preparing the patch; "
                "re-run — the doc now requires the anchor-safe strategy"
            )
        # A deferred op cannot be rescued here: this path writes ONE atomic
        # batch against the planning snapshot, with no live re-read to resolve
        # it against later. It joins the refusals, and the batch carries the
        # rest.
        skipped = dict(deferred)
        skipped.update(early_refusals)
        ordered = sorted((i for i in range(len(resolved)) if i not in skipped),
                         key=lambda i: resolved[i]["affect_start"],
                         reverse=True)
        requests = []
        for i in ordered:
            r, ins = resolved[i], insertions[i]
            if noops[i]:
                continue
            if ins:
                # A replace that only extends its target: emit the insert
                # alone. Rewriting it as delete+insert would remove text that
                # the caller never asked to remove — and this path was already
                # told, by the suggestion gate above, that this op removes
                # nothing (found in review).
                loc = {"index": r["end"] if ins[0] == "after" else r["start"]}
                if r["tab_id"]:
                    loc["tabId"] = r["tab_id"]
                requests.append({"insertText": {"location": loc,
                                                "text": ins[1]}})
            elif r["kind"] == "replace":
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
        result = {
            "action": "patched" if not skipped else "partially-patched",
            "strategy": "index-atomic",
            "doc_id": file_id,
            "tab_id": tid,
            "ops_applied": len(resolved) - len(skipped),
            "revision_id_before": revision_id,
        }
        if skipped:
            result["refused"] = [
                {"op": i, "source": _op_source_label(ops[i], resolved[i]),
                 "error": skipped[i]} for i in sorted(skipped)]
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(3)
        print(json.dumps(result, ensure_ascii=False))
        return

    # ---- commented-doc path: per-op pinned batches ----
    # Replaces are protected per-op by W8 export-based anchor mapping
    # (full-anchor coverage refused; partial overlap allowed). Inserts are
    # gated by C5.
    # A deferred op has no resolved record, so its kind is read from the op
    # itself — the gate must not go blind just because a target did not
    # resolve on the planning snapshot.
    has_insert = any(r["kind"] == "insert" if r else
                     str(ops[i].get("op", "")).startswith("insert")
                     for i, r in enumerate(resolved))
    if has_insert and C5_INSERT_NEAR_ANCHOR_SAFE is not True:
        state = "unverified" if C5_INSERT_NEAR_ANCHOR_SAFE is None else "verified UNSAFE"
        _error(
            f"doc has {len(anchored)} anchored comment(s); insert ops are "
            f"blocked (C5 insert-near-anchor behavior is {state} — fail "
            f"closed). Run the C5 live UI verification and set "
            f"C5_INSERT_NEAR_ANCHOR_SAFE, or edit in the UI."
        )

    applied, op_notes, refused = [], [], []
    ghosts = {}
    failed_at, failure, app_state = None, None, None
    for i, op in enumerate(ops):
        if i in early_refusals:
            # rejected by a gate that judged it on the planning snapshot and
            # will judge it the same way on any other — no reason to send it
            refused.append({"op": i,
                            "source": _op_source_label(op, resolved[i]),
                            "error": early_refusals[i]})
            continue
        try:
            _RAISE_ERRORS = True  # nested preflight errors must not exit
            try:
                note = _apply_op_anchor_safe(docs_service, drive_service,
                                             file_id, op, tab_id,
                                             warnings=ghosts)
            finally:
                _RAISE_ERRORS = False
            applied.append(_op_source_label(op, resolved[i]))
            if i in deferred:
                # It resolved against the live document even though it did not
                # resolve against the planning snapshot — the ops before it
                # made its target unambiguous. That is legitimate and it is
                # also the one case where the overlap check never saw this
                # operation, so the receipt says so instead of implying a
                # guarantee that was not made (#36). Merged into this op's own
                # note, never appended as a second one: two receipt entries
                # for one operation read as two operations.
                note = dict(note or
                            {"source": _op_source_label(op, resolved[i])})
                note["deferred"] = (
                    "на исходном снимке цель не разрешалась "
                    f"({deferred[i]}); операция разрешена по живому документу "
                    "и в проверке пересечений с другими правками не "
                    "участвовала")
            if note:
                op_notes.append(note)
            continue
        except PatchOpError as e:
            reason, state = str(e), e.state
        except HttpError as e:
            reason = e.reason if hasattr(e, "reason") else str(e)
            # 5xx/transport after send: the write may or may not have landed
            status = getattr(getattr(e, "resp", None), "status", None)
            state = "unknown" if (status is None or status >= 500) else "not_applied"
        except Exception as e:  # network timeouts etc. — state unknown
            reason, state = str(e), "unknown"
        if state == "not_applied":
            # This operation provably wrote nothing, and the ops in one file
            # do not overlap (checked above), so the rest are independent of
            # it. Refusing THIS operation must not cost the person the other
            # nine (r8): the refusal is collected and the loop goes on.
            refused.append({"op": i,
                            "source": _op_source_label(op, resolved[i]),
                            "error": reason})
            continue
        # unknown state: what the document looks like is no longer known,
        # every later operation would be planned against a guess — stop.
        failed_at, failure, app_state = i, reason, state
        break

    # skrepka used to post an informational reply into every thread whose
    # STALE quote intersected an op — approximate by construction, and with
    # narrowing the applied range is narrower than the declared one, so the
    # notice could name text the edit never touched. On the acceptance run it
    # read as «комментарии отработаны не по смыслу». Writing into a person's
    # document has to earn its place; the receipt carries op_notes and thread
    # links instead (#22).
    result = {
        "action": ("patched" if failed_at is None and not refused
                   else "partially-patched"),
        "strategy": "anchor-safe-per-op",
        "doc_id": file_id,
        "tab_id": tid,
        "ops_applied": len(applied),
        "applied": applied,
    }
    if op_notes:
        result["op_notes"] = op_notes
    if ghosts:
        # Named, never removed: deleting somebody's comment is the person's
        # decision (CONTRACT §2.2). Before #34 each of these stopped every
        # replace in the document instead.
        result["ghost_threads"] = [
            {"id": g.get("id"), "link": g.get("link"), "quote": g.get("quote"),
             "note": ("комментарий пропал из документа — якоря у него больше "
                      "нет, на правки он не влияет. Убрать его можно вручную: "
                      "«Удалить» в панели комментариев"
                      if not g.get("fenced") else
                      "комментарий пропал из выгрузки, но текст, на котором он "
                      "висел, ещё в документе — правки этого места отклонены, "
                      "остальной документ правится. Посмотрите тред по ссылке: "
                      "он либо потерял привязку, либо цел, и это видно глазами")}
            for g in ghosts.values()]
    if refused:
        result["refused"] = refused
    if failed_at is not None:
        result["failed_at"] = failed_at
        result["error"] = failure
        result["failed_op_state"] = app_state
        result["remaining"] = [_op_source_label(ops[j], resolved[j])
                               for j in range(failed_at, len(ops))]
    if failed_at is not None or refused:
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

def reply_comment(file_id, comment_id, text, resolve=False, yes=False):
    """Post a reply to an existing comment. If resolve=True, also resolves the thread."""
    if resolve:
        # Resolving a thread is the reviewer's call, never the agent's. --yes
        # is the person's own non-interactive path (their scripts), not a door
        # for the agent: by contract the agent does not resolve at all, with or
        # without the flag (agents/CONTRACT.md §2.1).
        _require_consent(
            "resolve comment thread", yes,
            "Ask the person to close the thread in the Google Docs UI, and "
            "preferably after the edits land: a closed thread drops out of "
            "the export, so skrepka can no longer protect the text it is "
            "anchored to, and a later delete turns it into a ghost that "
            "reopening does not bring back.")
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
        # Pass-through API fields keep their API names (createdTime, id,
        # content, author, action); comment_id/resolved are skrepka's own.
        # The second a reply landed in is what anchor accounting keys on, so
        # it has to be greppable against `comments` output (#15, #16).
        "createdTime": reply.get("createdTime"),
        "action": reply.get("action"),
        "resolved": resolve,
    }, ensure_ascii=False))


def resolve_comment(file_id, comment_id, text=None, yes=False):
    """Resolve a comment by posting a reply with action=resolve.

    Per Drive API, 'resolved' is read-only; the only way to resolve is via
    replies.create with action: 'resolve'. Works for unanchored comments too.
    """
    reply_comment(file_id, comment_id, text or "Resolved.", resolve=True,
                  yes=yes)


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
    - NBSP -> plain space BEFORE markdownify: Google exports ordinary
      spaces as &nbsp;, and markdownify SWALLOWS a leading NBSP at an
      inline-tag boundary (live case: a bold run split by coloring one
      word exported as <b>вернувшимся</b><b>&nbsp;жирным</b> and the
      space vanished from the md). Both md pipelines already normalize
      NBSP after conversion; doing it before keeps markdownify's own
      whitespace logic correct.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, parse_qs
    html = html.replace(" ", " ").replace("&nbsp;", " ")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["style", "script"]):
        tag.decompose()
    # Google's comment-export artifacts must not leak into the md: the
    # Docs API text has no trace of comments, so a md carrying the inline
    # <sup><a href="#cmntN">[a]</a></sup> markers or the trailing comment
    # bodies could never round-trip (the sidecar would refuse sync on any
    # commented doc). The #cmnt/#cmnt_ref anchors are Google's own export
    # convention — real doc links export as #heading=/#bookmark= ids.
    def _is_comment_body_p(node):
        if getattr(node, "name", None) != "p":
            # bare whitespace between comment paragraphs is fine
            return isinstance(node, str) and not node.strip()
        first_a = node.find("a", href=True)
        return (first_a is not None
                and re.match(r"^#cmnt_ref\d+$", first_a["href"] or ""))

    for a in soup.find_all("a", href=re.compile(r"^#cmnt_ref\d+$")):
        if getattr(a, "decomposed", False):
            continue  # already removed with an earlier container
        p_tag = a.find_parent("p")
        target = p_tag if p_tag is not None else a
        # remove the enclosing <div> ONLY when it holds nothing but
        # comment bodies — a shared wrapper div must not lose real
        # content (codex code-r1 #4)
        div = p_tag.find_parent("div") if p_tag is not None else None
        if div is not None and all(_is_comment_body_p(ch)
                                   for ch in div.children):
            div.decompose()
        else:
            target.decompose()
    for a in soup.find_all("a", href=re.compile(r"^#cmnt\d+$")):
        sup = a.find_parent("sup")
        (sup if sup is not None else a).decompose()
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


def _is_google_auth_image_host(url):
    """True only for an HTTPS URL whose HOSTNAME is an exact Google image host.
    The OAuth token is attached ONLY to these — a substring test like
    `"google.com" in src` would leak the Drive token to a URL such as
    `https://google.com.attacker.example/x` embedded in a downloaded doc
    (codex R3 #1, token exfiltration)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or p.username or p.password:
        return False
    host = (p.hostname or "").lower()
    if host in ("drive.google.com", "docs.google.com"):
        return True
    return host == "googleusercontent.com" or host.endswith(
        ".googleusercontent.com")


_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _download_images_from_html(html, images_dir, creds):
    """Download images referenced in HTML, save locally, rewrite src to local paths.

    SSRF-safe (codex R3 #2): we ONLY fetch images whose hostname is an exact
    Google image host — the place a Doc's own images live after export. Any
    other src (an external/attacker URL, or one pointing at a private/loopback
    address) is left untouched, never fetched. Redirects are disabled so a
    Google URL cannot be bounced to an internal address, and the body is
    size-capped."""
    from bs4 import BeautifulSoup
    import requests as req

    soup = BeautifulSoup(html, "html.parser")
    # open the images dir once via a symlink-refusing traversal (created 0700
    # if missing); every image is written relative to this verified dir fd, so
    # neither the dir nor a predictable filename can be a symlink we follow
    # (safeio, r3 #9)
    dir_fd = safeio.secure_open_parent(os.path.join(images_dir, "img"),
                                       create=True)
    count = 0
    try:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if not src:
                continue
            if not _is_google_auth_image_host(src):
                # external/unknown host — do not fetch (SSRF vector); keep as-is
                continue
            count += 1
            ext = ".png"
            fname = f"image_{count:03d}{ext}"
            try:
                headers = {"Authorization": f"Bearer {creds.token}"}
                resp = req.get(src, headers=headers, timeout=30,
                               allow_redirects=False, stream=True)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if not ct.startswith("image/"):
                    raise ValueError(f"not an image (content-type {ct!r})")
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                    fname = f"image_{count:03d}{ext}"
                body, total = [], 0
                for chunk in resp.iter_content(65536):
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        raise ValueError("image exceeds size limit")
                    body.append(chunk)
                safeio.write_at(dir_fd, fname, b"".join(body))
                img["src"] = os.path.join(os.path.basename(images_dir), fname)
            except Exception as e:
                _warn(f"Failed to download image #{count}: {e}")
    finally:
        os.close(dir_fd)
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
        safeio.atomic_write(output, text)  # symlink-safe (r3 #9)
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
        safeio.atomic_write(output, content)  # symlink-safe (r3 #9)
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


def list_suggestions(file_id, output=None):
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
        _emit_json({
            "title": title,
            "has_suggestions": False,
            "changes": [],
            "diff": "",
        }, output=output, summary={"has_suggestions": False,
                                   "change_count": 0})
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

    _emit_json({
        "title": title,
        "has_suggestions": True,
        "change_count": len(changes),
        "changes": changes,
        "diff": "".join(diff_lines),
    }, output=output, summary={"has_suggestions": True,
                               "change_count": len(changes)})


# ---------------------------------------------------------------------------
# sync: three-way merge of local markdown into a Google Doc (PLAN.md W4/W5)
# ---------------------------------------------------------------------------

SIDECAR_SCHEMA_VERSION = 2
SIDECAR_SUFFIX = config.SIDECAR_SUFFIX  # single source of truth (shared w/ forget)

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


def _visible_run_styles(raw_runs):
    """[(content, textStyle)] -> [{"len": utf16_len, "style": textStyle}]
    for the paragraph's VISIBLE text: the trailing newline is stripped and
    empty/newline-only runs are dropped (the \\n mark is never rewritten and
    its style must not break uniformity — codex style-r1 #3)."""
    if raw_runs:
        last_c, last_s = raw_runs[-1]
        if last_c.endswith("\n"):
            raw_runs = raw_runs[:-1] + [(last_c[:-1], last_s)]
    return [{"len": _utf16_len(c), "style": s} for c, s in raw_runs if c]


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
            parts, spans, raw_runs, has_nontext = [], [], [], False
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
                raw_runs.append((tr.get("content", ""),
                                 tr.get("textStyle") or {}))
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
                        # raw styles for preservation across rewrites; NOT
                        # persisted to the sidecar (live-doc-only data).
                        # The paragraph mark is never rewritten, so the
                        # trailing \n is stripped and newline-only runs are
                        # dropped — they must not affect style uniformity
                        "para_style": para.get("paragraphStyle") or {},
                        "run_styles": _visible_run_styles(raw_runs),
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


def _closed_threads_in_edited_ranges(doc_tab, closed, edited):
    """Best-effort: which closed threads probably sat in an edited paragraph.

    Returns {thread id: how many places its stale quote matches}. The count is
    not decoration: `_locate_comment_in_tab` returns EVERY occurrence, so on a
    document with repeated paragraphs a thread standing in a clean copy is
    reported as hit when the other copy is edited (#29). One match means «here»;
    several mean «one of these», and the person deserves the difference — the
    note sends them to check a thread by eye.

    Display only — it never decides anything, the snapshot is stale by
    construction. Measured useful: on the acceptance run it named the doomed
    thread correctly.
    """
    suspects = {}
    if doc_tab is None or not edited:
        return suspects
    for c in closed:
        where = _locate_comment_in_tab(doc_tab, c)
        if any(_ranges_overlap(s, e, cs, ce)
               for cs, ce in where for s, e in edited):
            suspects[c.get("id")] = len(where)
    return suspects


def _archive_closed_threads(md_path, file_id, closed, doc_tab=None,
                            edited=()):
    """Write the conversation of every closed thread next to the markdown.

    A closed thread is invisible to the export, so skrepka cannot say where
    its anchor is, and `deleteContentRange` — which is how `sync` rewrites and
    removes paragraphs — turns such an anchor into a ghost (measured
    2026-07-31). r8 stops refusing over that: the conversation is over, and a
    frozen document costs the person more. What must not be lost is the words,
    so they are written out first.

    Best-effort «probably here»: the thread's stale quote is looked up in the
    tab the same way `_informational_replies` does. Display only — it never
    decides anything (the snapshot can be stale by construction).

    Returns the archive path, or None when there is nothing to archive.
    """
    if not closed:
        return None
    suspects = _closed_threads_in_edited_ranges(doc_tab, closed, edited)
    entries = []
    for c in closed:
        matches = suspects.get(c.get("id"))
        entries.append({
            "id": c.get("id"),
            "link": _thread_link(file_id, c.get("id")),
            "author": (c.get("author") or {}).get("displayName"),
            "created": c.get("createdTime"),
            "quote": (c.get("quotedFileContent") or {}).get("value"),
            "content": c.get("content"),
            "probably_in_an_edited_paragraph": matches is not None,
            # «here» and «one of N places» are different messages: the flag
            # sends the person to check a thread by eye, and on a document
            # with repeated paragraphs the old flat `true` sent them to a
            # thread the edit never touched (#29)
            "where": ("текст этого треда встречается в документе "
                      f"{matches} раз — правка задела одно из этих мест"
                      if matches and matches > 1 else
                      "правка переписывает место, где стоял этот тред"
                      if matches else None),
            "replies": [
                {"author": (r.get("author") or {}).get("displayName"),
                 "created": r.get("createdTime"),
                 "content": r.get("content")}
                for r in (c.get("replies") or []) if not r.get("deleted")],
        })
    path = md_path + ".skrepka-closed-threads.json"

    # The archive ACCUMULATES. A thread archived by an earlier sync may be
    # gone from today's census — deleted, or no longer anchored — and a plain
    # overwrite would then destroy the only copy of it that exists anywhere
    # (found in review). An archive that can be silently truncated is not an
    # archive.
    kept = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                previous = json.load(f)
            old = previous["threads"]
            if not isinstance(old, list):
                raise ValueError("threads is not a list")
        except Exception as e:
            raise RuntimeError(
                f"не удалось прочитать прежний архив закрытых тредов {path}: "
                f"{e}. Перезаписать его нельзя — это единственная копия тех "
                f"разговоров. Уберите файл в сторону и повторите.")
        fresh_ids = {e["id"] for e in entries}
        kept = [t for t in old
                if isinstance(t, dict) and t.get("id") not in fresh_ids]

    payload = {
        "doc_id": file_id,
        "note": ("Закрытые треды. Google не отдаёт их в экспорте, поэтому "
                 "skrepka не знает, где стояли их якоря, и правка через "
                 "удаление могла превратить их в призраков: в интерфейсе "
                 "такой тред не появится даже после переоткрытия. Текст "
                 "разговора сохранён здесь. Файл накапливается: треды из "
                 "прошлых прогонов остаются."),
        "threads": entries + kept,
    }
    return safeio.atomic_write(
        path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def _write_journal(md_path, journal):
    """Atomically write the sync recovery journal; returns its path. Symlink-safe
    via safeio (the predictable `.gdocs-sync-journal.json.tmp` was an overwrite
    vector — codex r3-io #P1)."""
    path = md_path + ".gdocs-sync-journal.json"
    safeio.atomic_write(path, json.dumps(journal, ensure_ascii=False, indent=1))
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
    # hardened write: a predictable `.gdocs-base.json.tmp` next to the user's
    # .md was the classic symlink-overwrite vector — go through safeio (r3 #9)
    path = md_output_path + SIDECAR_SUFFIX
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    safeio.atomic_write(path, text)
    return path  # preserve the exact prior return (caller-relative) path


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
        entry = {k2: d[k2] for k2 in d
                 if k2 not in ("start", "end", "para_style", "run_styles")}
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

# paragraphStyle fields that are writable via updateParagraphStyle and that
# the doc owner may have set by hand — preserved across a paragraph rewrite
# (setting namedStyleType RESETS all of them to the named style's defaults,
# so the namedStyleType update must carry them in the same request).
# headingId/tabStops are read-only and excluded.
_PRESERVE_PARA_FIELDS = (
    "alignment", "lineSpacing", "direction", "spacingMode", "spaceAbove",
    "spaceBelow", "borderBetween", "borderTop", "borderBottom", "borderLeft",
    "borderRight", "indentFirstLine", "indentStart", "indentEnd",
    "keepLinesTogether", "keepWithNext", "avoidWidowAndOrphan", "shading",
    "pageBreakBefore")

# textStyle fields preserved across a rewrite when they are UNIFORM over the
# old paragraph. bold/italic/link are excluded on purpose: the local markdown
# is the source of truth for those (they round-trip through md).
_PRESERVE_TEXT_FIELDS = (
    "underline", "strikethrough", "smallCaps", "backgroundColor",
    "foregroundColor", "fontSize", "weightedFontFamily", "baselineOffset")


def _uniform_text_style(run_styles):
    """Return (uniform_style, True) when every run agrees on the preserved
    textStyle fields (an empty dict = 'all defaults' is uniform too), else
    (None, False)."""
    filtered = [
        {k: ts[k] for k in _PRESERVE_TEXT_FIELDS if k in ts}
        for ts in (run_styles or [{}])
    ]
    first = json.dumps(filtered[0], sort_keys=True)
    for other in filtered[1:]:
        if json.dumps(other, sort_keys=True) != first:
            return None, False
    return filtered[0], True


def _capture_preserve(rel, with_text):
    """Capture style-preservation data from a remote element before rewrite.

    Returns (preserve_dict, text_dropped). preserve always carries the raw
    paragraphStyle. For rewrites (with_text=True) it carries "text_style":
    the uniform textStyle to restore over the new text, or {} when the old
    inline styling was non-uniform — the rewrite then CLEARS to the named
    style's defaults deterministically (inserted text inherits arbitrary
    neighbour styling otherwise — codex style-r1 #2) and text_dropped=True
    reports the loss honestly (span-mapping is a later roadmap item).
    For style-only changes (with_text=False, text untouched) it carries
    "run_spans": the old runs' exact lengths+styles, so per-run styling can
    be reapplied after the bold/italic/link reset (codex style-r1 #4).

    "type" carries the old STRUCTURAL type: namedStyleType alone cannot
    tell p from li (both NORMAL_TEXT), and a p<->li conversion is a
    restructure that must not drag old indents/styling along
    (codex style-r2 #1).
    """
    preserve = {"para_style": rel.get("para_style") or {},
                "type": rel.get("type")}
    if not with_text:
        preserve["run_spans"] = rel.get("run_styles") or []
        return preserve, False
    uniform, ok = _uniform_text_style(
        [r["style"] for r in rel.get("run_styles") or []])
    if ok:
        preserve["text_style"] = uniform
        return preserve, False
    preserve["text_style"] = {}
    return preserve, True


# inserted blocks: nothing to preserve, but the full-mask clear must still
# run — text inserted next to styled text inherits that styling, so a fresh
# block would otherwise come out red/10pt/etc. (codex style-r2 #2). No
# "type" key => same_type is always False => clean named style + clear.
_FRESH_BLOCK_PRESERVE = {"para_style": {}, "text_style": {}}


def _link_intervals(sig, start_index):
    """[(start, end, is_link)] for the block's NEW md spans, in order."""
    out = []
    offset = start_index
    for span_text, marks in json.loads(sig):
        span_len = _utf16_len(span_text)
        out.append((offset, offset + span_len,
                    any(m.startswith("link:") for m in marks)))
        offset += span_len
    return out


def _split_by_intervals(start, end, flagged):
    """Split [start, end) by [(s, e, flag)] coverage; yields
    (piece_start, piece_end, flag). Positions not covered by any interval
    (defensive; sig should tile the block) yield flag=False."""
    pos = start
    for s, e, flag in flagged:
        if e <= pos or s >= end:
            continue
        s2, e2 = max(s, pos), min(e, end)
        if s2 > pos:
            yield pos, s2, False
        if s2 < e2:
            yield s2, e2, flag
        pos = e2
        if pos >= end:
            return
    if pos < end:
        yield pos, end, False


def _style_requests_for_block(el, start_index, preserve=None):
    """Build style requests for a block whose PLAIN text sits at start_index.

    Returns a list of batchUpdate requests: namedStyleType, bullets,
    bold/italic/link spans (narrow field masks).

    preserve (from _capture_preserve) reapplies what the block's own style
    requests would otherwise destroy: hand-set paragraphStyle fields (the
    namedStyleType update resets them), the uniform textStyle of the old
    paragraph over rewritten text ({} = deterministic clear for non-uniform
    originals), and per-run styles for style-only blocks whose text was
    never rewritten. A type change (p->h2) is an intentional restructure:
    it gets the clean named style AND cleared text styling.
    """
    reqs = []
    text = el["text"]
    end_index = start_index + _utf16_len(text)
    para_range = {"startIndex": start_index, "endIndex": end_index + 1}
    target_named = _KIND_TO_NAMED_STYLE[el["type"]]
    para_style = {"namedStyleType": target_named}
    para_fields = ["namedStyleType"]
    same_type = True
    if preserve is not None:
        old = preserve.get("para_style") or {}
        # preserve only when the STRUCTURAL type is unchanged: a type change
        # (p->h2, p<->li, ...) is an intentional restructure and gets the
        # clean named style. Compared on el-types, not namedStyleType —
        # p and li share NORMAL_TEXT (codex style-r2 #1). Inserted blocks
        # carry no "type" and never preserve.
        same_type = preserve.get("type") == el["type"]
        if same_type:
            for k in _PRESERVE_PARA_FIELDS:
                if k in old:
                    para_style[k] = old[k]
                    para_fields.append(k)
    # one request per concern, but ORDER differs by block type: both bullet
    # ops rewrite paragraph indents (deleteParagraphBullets "visually
    # preserves" nesting by adjusting indentStart/indentFirstLine), so for
    # plain blocks it must run BEFORE the paragraphStyle update or preserved
    # indents get stomped; for list items createParagraphBullets must run
    # AFTER the namedStyleType reset so the bullet indents it sets survive.
    para_req = {"updateParagraphStyle": {
        "range": para_range,
        "paragraphStyle": para_style,
        # the API applies namedStyleType (with its reset) BEFORE the other
        # fields within one request, so preserved fields win
        "fields": ",".join(para_fields),
    }}
    if el["type"] == "li":
        reqs.append(para_req)
        reqs.append({"createParagraphBullets": {
            "range": para_range,
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
        }})
    else:
        reqs.append({"deleteParagraphBullets": {"range": para_range}})
        reqs.append(para_req)
    # Reset inline styles over the whole block, then apply spans.
    # Link clearing is SPECIAL (live-verified 2026-07-14): {"link": None}
    # is silently dropped from the body by the google client, and a
    # fields="link" unset is honored ONLY when the request range is
    # entirely linked — over a mixed range (whole block) it is silently
    # ignored. The whole-block reset below therefore handles rewritten/
    # inserted text (one insertText => uniformly inherited, so the range
    # is either fully linked or link-free); style-only blocks get precise
    # per-piece link clears in the run_spans pass further down.
    if text:
        reqs.append({"updateTextStyle": {
            "range": {"startIndex": start_index, "endIndex": end_index},
            "textStyle": {},
            "fields": "link",
        }})
        reqs.append({"updateTextStyle": {
            "range": {"startIndex": start_index, "endIndex": end_index},
            "textStyle": {"bold": False, "italic": False},
            "fields": "bold,italic",
        }})
        if preserve is not None and "text_style" in preserve:
            # rewritten text. Full field mask: inserted text may have
            # INHERITED stray styling from a neighbour, so absent captured
            # fields must be cleared back to the named-style defaults, not
            # left as inherited. A type change clears everything (clean
            # restructure — codex style-r1 #1); a non-uniform original
            # arrives here as {} = deterministic clear (codex style-r1 #2).
            # Applied BEFORE the md spans so a link span keeps its automatic
            # link color/underline.
            reqs.append({"updateTextStyle": {
                "range": {"startIndex": start_index, "endIndex": end_index},
                "textStyle": preserve["text_style"] if same_type else {},
                "fields": ",".join(_PRESERVE_TEXT_FIELDS),
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
    if preserve is not None and preserve.get("run_spans"):
        # style-only block: the text was never rewritten, so the old runs
        # map 1:1 onto the current text. Reapply preserved fields AFTER the
        # md spans: the bold/italic/link reset and re-linking slosh
        # neighbour/default styling over the range (documented side effects
        # of unsetting/setting link), and this pass makes the outcome
        # deterministic — a custom link color or a colored word survives a
        # bold toggle elsewhere in the paragraph (codex style-r1 #4).
        # Link topology decides what each piece gets (codex style-r2 #4):
        #   old link  & new link  -> captured style (custom look survives)
        #   old link  & new plain -> {} (clear the ghost link appearance)
        #   old plain & new link  -> NO restore (default link look stands)
        #   old plain & new plain -> captured style
        spans_total = sum(s["len"] for s in preserve["run_spans"])
        if spans_total == end_index - start_index:
            new_links = _link_intervals(el["sig"], start_index)
            offset = start_index
            for span in preserve["run_spans"]:
                run_start, run_end = offset, offset + span["len"]
                offset = run_end
                old_linked = "link" in span["style"]
                captured = {k: span["style"][k]
                            for k in _PRESERVE_TEXT_FIELDS
                            if k in span["style"]}
                for pc_start, pc_end, new_linked in _split_by_intervals(
                        run_start, run_end, new_links):
                    if new_linked and not old_linked:
                        continue
                    piece = {"startIndex": pc_start, "endIndex": pc_end}
                    if old_linked and not new_linked:
                        # the whole-block link reset is silently ignored
                        # over mixed ranges (API quirk, see above); this
                        # piece is entirely inside the old linked run, so
                        # a targeted unset works
                        reqs.append({"updateTextStyle": {
                            "range": piece, "textStyle": {},
                            "fields": "link",
                        }})
                        style = {}
                    else:
                        style = captured
                    reqs.append({"updateTextStyle": {
                        "range": piece,
                        "textStyle": style,
                        "fields": ",".join(_PRESERVE_TEXT_FIELDS),
                    }})
        else:
            # defensive: captured spans do not tile the block (should be
            # impossible for a style-only element) — skip the restore
            # rather than style wrong ranges
            _warn(f"style restore skipped for block at {start_index}: "
                  f"captured runs cover {spans_total} of "
                  f"{end_index - start_index} code units")
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
    _tid, doc_tab = tabs[0][0], tabs[0][2]
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
                    f"(base element {i}) — alignment would be ambiguous. "
                    f"Make the copies differ by a word and run it again, or "
                    f"edit in the UI. (`patch` does not help while the "
                    f"paragraph has a twin: it targets by quote, and a quote "
                    f"inside a duplicate is not unique either.)"
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

    # --- protected ranges: named ranges + comment anchors (plan v4) ---
    all_comments, anchored, fp1, universe = _census_comments(
        drive_service, file_id)
    body_content = (doc_tab.get("body", {}) or {}).get("content", [])
    body_end = body_content[-1]["endIndex"] if body_content else 2
    # named ranges only guard REMOVALS (plan scope) — an insert/style-only
    # sync must not trip over a malformed mark it cannot touch
    named_intervals = (_named_range_intervals(doc_tab)
                       if (replaced or deleted) else [])
    snap = None
    if anchored and (replaced or deleted):
        # W8 export-based anchor map with a freshness canary: paragraph
        # replaces/deletes are allowed as long as they touch no anchor
        snap, retry_reason = _fresh_anchor_snapshot(
            docs_service, drive_service, file_id, doc, doc_tab,
            anchored, named_intervals, body_end, fp1=fp1,
            universe=universe)
        if snap is None:
            # retryable race, but re-planning is this whole function —
            # a CLI re-run is the retry (nothing has been applied)
            _error(f"{retry_reason} — re-run sync (nothing applied)")

    def _canary_note(msg):
        """Clean up the canary before erroring out of a pre-batch refusal."""
        if snap is not None and not _cleanup_canary(
                docs_service, file_id, snap["canary"]):
            msg += (f" ВНИМАНИЕ: в конце документа осталась служебная "
                    f"строка «{snap['canary']['text']}» — удалите её "
                    f"вручную (данные не потеряны).")
        return msg

    # --- map plan to remote positions ---
    def remote_el(i):
        return remote_els[r_map[i]]

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
    expected, style_slots, styles_dropped = [], [], []

    def _entry_para(text):
        return ("para", _norm_ws(text))

    for j, rel in enumerate(remote_els):
        for lel in insert_before.get(j, []):
            style_slots.append((len(expected), lel, _FRESH_BLOCK_PRESERVE))
            expected.append(_entry_para(lel["text"]))
        a = action.get(j)
        if a and a[0] == "delete":
            continue
        if a and a[0] == "replace":
            # capture the OLD paragraph's styles: the rewrite keeps
            # paragraphStyle on its own, but our namedStyleType update
            # resets it, and the new text loses the old runs' textStyle
            preserve, dropped = _capture_preserve(rel, with_text=True)
            if dropped:
                styles_dropped.append({
                    "text": a[1]["text"][:60],
                    "reason": "old paragraph had non-uniform inline styling "
                              "(e.g. one colored word) — not restorable yet",
                })
            style_slots.append((len(expected), a[1], preserve))
            expected.append(_entry_para(a[1]["text"]))
        elif rel["type"] == "opaque":
            expected.append(("opaque", rel.get("hash", "")))
        else:
            if j in so_remote:
                # text untouched: runs keep their styles, only the
                # paragraphStyle needs shielding from the namedStyleType reset
                preserve, _ = _capture_preserve(rel, with_text=False)
                style_slots.append((len(expected), so_remote[j], preserve))
            expected.append(_entry_para(rel["text"]))
    for lel in end_inserts:
        style_slots.append((len(expected), lel, _FRESH_BLOCK_PRESERVE))
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
    if snap is not None:
        journal["anchor_accounting"] = snap["metrics"]
    if named_intervals:
        journal["named_ranges_protected"] = len(named_intervals)

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
    if snap is not None:
        # the canary delete goes FIRST: the canary sits strictly at the
        # end of the doc, so removing it first restores R0 coordinates
        # for every following request (plan v4 §1)
        flat = [_canary_delete_request(snap["canary"])] + flat

    # --- protected-interval check on the FINAL request list ---
    protected = list(named_intervals)
    if snap is not None:
        protected += [
            (as_, ae,
             f"the anchor of a live comment (docx id {aid}, «{atext[:40]}») "
             f"— точечную правку такого абзаца делает `patch`, он сохраняет "
             f"тред; либо правьте его в интерфейсе Google Docs")
            for as_, ae, atext, aid in snap["anchors"]]
        # anchors the accounting could not vouch for but could still place —
        # they narrow the refusal to the paragraphs they sit in (issue #10)
        protected += snap["blocked"]
    if flat and protected:
        overlap = _find_protected_overlap(flat, protected)
        if overlap:
            _error(_canary_note(overlap))

    # Closed threads: the words are saved before the write, because the write
    # may silently unhook them (r8). This is EVERY sync that edits, not only
    # one that deletes — a replaced paragraph is rewritten with
    # deleteContentRange too, and that is the operation that ghosts a closed
    # anchor.
    closed_note = None
    closed = [c for c in anchored if c.get("resolved")]
    if closed and (replaced or deleted):
        edited_ranges = [(remote_el(i)["start"], remote_el(i)["end"])
                         for i, _lel in replaced]
        edited_ranges += [(remote_el(i)["start"], remote_el(i)["end"])
                          for i in deleted]
        try:
            archive = _archive_closed_threads(
                md_path, file_id, closed, doc_tab=doc_tab,
                edited=edited_ranges)
        except Exception as e:
            # Fail closed, and only here. The archive is what makes this
            # edit's known cost acceptable: without it the sync can unhook a
            # closed thread and leave no copy of what was said (found in
            # review). The remedy is local and cheap — a writable path next
            # to the markdown.
            _error(_canary_note(
                f"не удалось сохранить разговор закрытых тредов рядом с "
                f"{md_path}: {e}. Правка остановлена, ничего не применено: в "
                f"документе {len(closed)} закрытых тредов, их якорей экспорт "
                f"не показывает, и после правки разговор было бы не "
                f"восстановить. Документ не тронут: дайте каталогу рядом с "
                f"файлом права на запись (или положите .md в другой) и "
                f"повторите."))
        suspects = _closed_threads_in_edited_ranges(doc_tab, closed,
                                                    edited_ranges)
        closed_note = {
            "count": len(closed),
            "probably_hit": sorted(suspects),
            "archive": archive,
            "note": ("Google не отдаёт закрытые треды в экспорте, поэтому "
                     "их якоря не защищены. Замерено: закрытый тред, чей "
                     "абзац переписан, исчезает совсем — переоткрытие его не "
                     "возвращает. Разговор сохранён в файле."),
        }
        if suspects:
            # «стояли» would be a claim, and on a document with repeated
            # paragraphs it is sometimes a false one — the quote matches in
            # several places and only one of them is being edited (#29)
            unsure = sum(1 for n in suspects.values() if n > 1)
            hedge = (f" (у {unsure} из них текст встречается в документе "
                     f"не один раз, так что это может быть и другая копия)"
                     if unsure else "")
            _warn(f"закрытых тредов в документе: {len(closed)}, из них "
                  f"{len(suspects)} по нашей оценке стояли в абзацах, которые "
                  f"правка переписывает{hedge} — такие треды исчезают из "
                  f"документа, и переоткрытие их не вернёт (замерено). "
                  f"Разговор сохранён: {archive}")
        else:
            _warn(f"в документе {len(closed)} закрытых тредов; их якоря "
                  f"экспорт не показывает, и правка могла их задеть. "
                  f"Разговор сохранён: {archive}")

    rev_pin = snap["r1"] if snap is not None else revision_id
    rev_after_text = rev_pin
    # set when the batch response was lost but the canary is GONE: the
    # batch most likely applied — positional verification then decides
    # (plan v4 §3: recovery-as-success after a lost response)
    outcome_uncertain = False
    if flat:
        if snap is not None:
            # comments must not have changed between the accounting census
            # and the write; no other network calls after this check
            try:
                fp2 = _comments_fingerprint(drive_service, file_id)
            except Exception as e:
                _error(_canary_note(
                    f"final comment census failed (nothing applied): "
                    f"{e.reason if hasattr(e, 'reason') else e}"))
            if fp2 != snap["fp1"]:
                _error(_canary_note(
                    "comments changed while preparing the sync — re-run "
                    "(nothing applied)"))
        try:
            resp = docs_service.documents().batchUpdate(
                documentId=file_id,
                body={"requests": flat,
                      "writeControl": _write_control(rev_pin)},
            ).execute()
        except HttpError as e:
            reason = e.reason if hasattr(e, "reason") else str(e)
            status = getattr(getattr(e, "resp", None), "status", None)
            if status is None or status >= 500:
                if snap is not None:
                    msg, state = _ambiguous_batch_outcome(
                        docs_service, file_id, snap["canary"],
                        f"sync text batch failed: {reason}")
                    if state == "not_applied":
                        _error(msg)  # proven by the intact canary
                    journal["phases"].append({"phase": "text-batch",
                                              "status": "ambiguous",
                                              "error": reason})
                    outcome_uncertain = True
                else:
                    journal["phases"].append({"phase": "text-batch",
                                              "status": "unknown",
                                              "error": reason})
                    _fail_partial(f"text batch outcome unknown: {reason}")
            else:
                # deterministic 4xx on a pinned atomic batch: not applied
                _error(_canary_note(
                    f"sync text batch failed (nothing applied — atomic): "
                    f"{reason}"))
        except Exception as e:
            # transport failure after send: outcome ambiguous
            if snap is not None:
                msg, state = _ambiguous_batch_outcome(
                    docs_service, file_id, snap["canary"],
                    f"sync text batch failed (transport): {e}")
                if state == "not_applied":
                    _error(msg)  # proven by the intact canary
                journal["phases"].append({"phase": "text-batch",
                                          "status": "ambiguous",
                                          "error": str(e)})
                outcome_uncertain = True
            else:
                journal["phases"].append({"phase": "text-batch",
                                          "status": "unknown",
                                          "error": str(e)})
                _fail_partial(
                    f"text batch outcome unknown (transport): {e}")
        if not outcome_uncertain:
            try:
                rev_after_text = (resp.get("writeControl") or {}).get(
                    "requiredRevisionId") or rev_after_text
            except (AttributeError, TypeError):
                # structurally odd SUCCESS response: the batch did execute
                # (same class as the insert-response case, codex code-r2
                # #2 / code-r3 #1) — the post-batch revision is unknowable,
                # so positional verification arbitrates, styles skipped
                journal["phases"].append({"phase": "text-batch",
                                          "status": "ambiguous",
                                          "error": "unparseable response"})
                outcome_uncertain = True
            else:
                journal["phases"].append({"phase": "text-batch",
                                          "status": "ok",
                                          "requests": len(flat)})

    # --- positional verification against the expected merged sequence ---
    try:
        doc2 = _safe_get_doc(docs_service, file_id)
    except Exception as e:
        _fail_partial(f"cannot re-read doc after text batch: {e}")
    rev2 = doc2.get("revisionId")
    journal["revision_after_text"] = rev2
    if rev2 != rev_after_text and not outcome_uncertain:
        # a collaborator edit landed between our text batch and this read;
        # a style-only edit would pass text verification but our style
        # batch would then overwrite it (codex sync-r2 #3) — fail closed.
        # In the lost-response case the post-batch revision is unknowable;
        # the positional verification below is the arbiter instead.
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
            f"landed mid-sync"
            + (" (and the batch response was lost — verify the doc)"
               if outcome_uncertain else ""),
            {"diverge_at": diverge})
    if outcome_uncertain:
        # lost response, canary gone, and the doc matches the expected
        # merged sequence exactly — the batch is proven applied
        rev_after_text = rev2
        journal["phases"].append({"phase": "text-batch",
                                  "status": "recovered-after-lost-response",
                                  "requests": len(flat)})

    # --- style pass: positions taken positionally from fresh_els ---
    styled = 0
    if outcome_uncertain:
        # the lost response hides whether a collaborator edited STYLES
        # between our batch and the re-read (text is positionally proven,
        # styles are not) — running the style pass could overwrite their
        # edit (codex code-r2 #1). Skip it and report honestly.
        journal["phases"].append({"phase": "style-batch",
                                  "status": "skipped-after-lost-response",
                                  "blocks": len(style_slots)})
    else:
        style_reqs = []
        for k, lel, preserve in style_slots:
            style_reqs.extend(
                _style_requests_for_block(lel, fresh_els[k]["start"],
                                          preserve))
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
            journal["phases"].append({"phase": "style-batch",
                                      "status": "ok", "blocks": styled})

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
        # symlink-/hardlink-safe staging: a planted `<md>.tmp` / `<md>.gdocs-
        # base.json.tmp` must not be followed or truncated (codex r3-io #P1).
        # atomic_write stages via an unpredictable O_EXCL temp and renames onto
        # these predictable names (rename replaces the NAME, never writing
        # through a symlink or a hard link); the os.replace commit below then
        # advances them, preserving the staged-then-decide recovery logic.
        tmp_md = md_path + ".tmp"
        tmp_sc = sidecar_path + ".tmp"
        safeio.atomic_write(tmp_md, new_md)
        safeio.atomic_write(
            tmp_sc, json.dumps(payload, ensure_ascii=False, indent=1))
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
    if snap is not None:
        result["anchor_accounting"] = snap["metrics"]
    if closed_note:
        result["closed_threads"] = closed_note
    if outcome_uncertain:
        result["recovered_after_lost_response"] = True
        if style_slots:
            result["style_pass_skipped"] = len(style_slots)
            result["style_note"] = (
                "ответ текстового батча был потерян; текст доказан "
                "позиционной проверкой, но стилевой проход пропущен, чтобы "
                "не затереть возможную параллельную правку — проверьте "
                "оформление изменённых абзацев в UI")
    if styles_dropped:
        result["inline_styles_dropped"] = styles_dropped
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
    all_comments, anchored, _, _ = _census_comments(drive_service, file_id)
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
                    "Use `patch` for iterative edits — since 0.10.0 it also "
                    "rewrites a commented fragment whole without losing the "
                    "thread, unless the doc has closed threads. If you hold a "
                    "freshly written .md instead of a list of edits: "
                    "`download` this doc, move your changes into the "
                    "downloaded file (its sidecar must stay beside it) and "
                    "`sync` — that path keeps the OPEN threads (a closed one may be "
                    "unhooked, its words archived beside the .md first), "
                    "but it refuses "
                    "outright when the new text rewrites commented "
                    "paragraphs, and those belong to `patch`. Whatever is "
                    "left unapplied is a list to show the person, not a "
                    "reason to come back here. If a full replace is "
                    "really wanted: ask the person in plain words about THIS "
                    "document, name what it loses, and wait for an explicit "
                    "yes before rerunning with --acknowledge-loss. A yes given "
                    "for another document, or earlier in this session, does "
                    "not carry over — ask again for each document. The backup "
                    "made first holds text and styles only: per C0 it will NOT "
                    "contain the comments."
                ),
                "document": meta.get("name"),
                "comments": len(all_comments),
                "anchored_comments": len(anchored),
                "named_ranges": sorted(named_ranges),
            }, ensure_ascii=False, indent=2))
            sys.exit(2)
        # --acknowledge-loss IS the acknowledgement (#17): the CLI used to also
        # demand a terminal, which an agent-run process never has, so the
        # legitimate destructive path was unreachable for the actual audience.
        # What stands between an agent and this branch is the contract: ask the
        # person in plain words and wait for an explicit yes before passing the
        # flag (agents/CONTRACT.md §2.2).
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
        # The refusal above is the only place the rule is stated, and an agent
        # that already knows the flag never sees it again. Measured in an
        # acceptance run: told "now do the same for the second doc", the agent
        # reused the working command and destroyed a document nobody had
        # consented to. The receipt has to carry the rule too.
        out["consent_note"] = (
            "--acknowledge-loss consumed a one-time consent: this document, "
            "this run. Before replacing any other document, ask the person "
            "again, name that document, and wait for a fresh explicit yes.")
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
    cm.add_argument("--output", default=None,
                    help="Write the full JSON to a file and print a short "
                         "receipt (large threads can exceed agent output "
                         "limits and get silently truncated)")

    # reply command
    rp = sub.add_parser("reply", help="Post a reply to a comment")
    rp.add_argument("file_id", help="Google Doc file ID or URL")
    rp.add_argument("comment_id", help="Comment ID to reply to")
    rp.add_argument("text", help="Reply text")
    rp.add_argument("--resolve", action="store_true",
                    help="Also mark the thread as resolved")
    rp.add_argument("--yes", action="store_true",
                    help="Confirm the resolve without a prompt (only with "
                         "--resolve; closing a thread is the person's call)")

    # resolve command
    rs = sub.add_parser("resolve", help="Resolve a comment thread")
    rs.add_argument("file_id", help="Google Doc file ID or URL")
    rs.add_argument("comment_id", help="Comment ID to resolve")
    rs.add_argument("--text", default=None,
                    help="Optional note (default: 'Resolved.')")
    rs.add_argument("--yes", action="store_true",
                    help="Confirm without a prompt (closing a thread is the "
                         "person's call, not the agent's)")

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
    sg.add_argument("--output", default=None,
                    help="Write the full JSON to a file and print a short "
                         "receipt (large diffs can exceed agent output "
                         "limits and get silently truncated)")

    # sync command (three-way merge)
    sy = sub.add_parser("sync", help="Three-way merge local .md into a Google Doc")
    sy.add_argument("file_id", help="Google Doc file ID or URL")
    sy.add_argument("file", help="Path to edited .md (sidecar must sit next to it)")

    # update command
    upd = sub.add_parser(
        "update",
        help="Replace a doc's whole content (destroys comment threads — "
             "see `patch` to keep them)")
    upd.add_argument("file_id", help="Google Doc file ID or URL")
    upd.add_argument("file", help="Path to .md file with new content")
    upd.add_argument("--title", help="New document title")
    upd.add_argument("--no-highlights", action="store_true",
                     help="Skip highlight post-processing")
    upd.add_argument(
        "--acknowledge-loss", action="store_true",
        # The one channel that reaches an agent reading --help before it ever
        # sees a refusal. Naming only the price taught agents that losing the
        # threads was the only way to do the job (#24) — it never was.
        help="Proceed although every comment thread is destroyed. Rarely the "
             "right call: `patch` applies edits and keeps the threads alive, "
             "including rewriting a commented fragment whole (unless the doc "
             "has closed threads). If you hold a freshly written .md, "
             "`download` the doc, move your changes into the downloaded file "
             "(its sidecar must stay next to it) and `sync` — it keeps OPEN "
             "threads, a closed one may be unhooked with its words archived "
             "beside the .md. What `patch` "
             "cannot place is a list for the person, not a reason to use this "
             "flag. The backup made first holds text and styles only — not "
             "the comments.")

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
        list_comments(_extract_doc_id(args.file_id), output=args.output)
    elif args.command == "reply":
        # a bare --yes would silently mean nothing here; refusing keeps the
        # flag tied to the one thing it confirms
        if args.yes and not args.resolve:
            _error("--yes applies only together with --resolve — nothing done")
        reply_comment(args.file_id, args.comment_id, args.text,
                      resolve=args.resolve, yes=args.yes)
    elif args.command == "resolve":
        resolve_comment(args.file_id, args.comment_id, text=args.text,
                        yes=args.yes)
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
        list_suggestions(args.file_id, output=args.output)
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
