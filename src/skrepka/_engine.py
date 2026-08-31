#!/usr/bin/env python3
"""Google Docs toolkit: upload, download, update, comments, suggestions."""

import argparse
import copy
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
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

    def __init__(self, msg, state="not_applied", reason=None, details=None):
        super().__init__(msg)
        self.state = state
        # Машиночитаемый диагноз рядом со строкой, а не вместо неё: строка
        # остаётся первичным значением исключения, поэтому всё, что читало
        # квитанции до #51, продолжает работать, а разбирающий по коду
        # подключается по желанию.
        self.reason = reason
        self.details = details


_RAISE_ERRORS = False  # per-op mode: _error raises PatchOpError instead of exiting


# Устойчивые коды причин. Меняются как публичный контракт: код может
# появиться, но существующий не переименовывается и не меняет смысла —
# иначе агент, который на него смотрит, начнёт молча ошибаться.
_REASON_CODES = frozenset((
    "quote_not_found",              # цитаты нет в целевой вкладке
    "quote_ambiguous",              # цитата встречается больше раза
    "suggestion_overlap",           # правка задевает непринятое предложение
    "named_range_overlap",          # правка задевает машинную пометку
    "comment_anchor_would_be_lost",  # последний якорь треда исчез бы
    "anchor_identity_collision",    # два треда неразличимы в выгрузке
    "unsupported_structure",        # конструкция, которую skrepka не правит
    "concurrent_edit",              # документ изменился под правкой
    "comment_thread_unresolvable",  # тред не адресуется: закрыт, удалён,
                                    # без якоря или его якорь не размещён
    "comment_thread_has_multiple_anchors",  # у треда несколько якорей, и
                                    # выбирать копию за человека нельзя
    "unknown_write_outcome",        # исход записи неизвестен
))

_DETAILS_CAP = 200  # длинная цитата в квитанции — это шум, а не диагностика


def _with_diag(entry, code, details):
    """Дописать диагноз к записи об отказе, если он есть.

    Один сборщик на все пути квитанции. Их три — ранний отказ на чистом
    документе, ранний отказ на комментированном и отказ в цикле записи, — и
    первая редакция #51 заполняла только третий: машинный контракт молча
    зависел от того, есть ли в документе комментарии (codex, ревью r19).
    """
    if code:
        entry["reason"] = code
    if details:
        entry["details"] = details
    return entry


def _bound_details(details):
    """Обрезать значения деталей: квитанция читается человеком и агентом."""
    if not isinstance(details, dict):
        return None
    out = {}
    for k, v in details.items():
        if isinstance(v, str) and len(v) > _DETAILS_CAP:
            v = v[:_DETAILS_CAP] + "…"
        elif isinstance(v, list):
            v = [(x[:_DETAILS_CAP] + "…") if isinstance(x, str)
                 and len(x) > _DETAILS_CAP else x for x in v[:10]]
        out[k] = v
    return out or None


def _error(msg, *, reason=None, details=None):
    """Print JSON error to stdout and exit (or raise in per-op mode)."""
    if reason is not None and reason not in _REASON_CODES:
        # Не assert: под `python -O` он исчезает, и опечатка в коде уехала бы
        # наружу к агенту, который на этот код смотрит. Контракт должен
        # падать в любом режиме.
        raise ValueError(f"unknown reason code {reason!r}")
    details = _bound_details(details)
    if _RAISE_ERRORS:
        raise PatchOpError(msg, reason=reason, details=details)
    payload = {"error": msg}
    if reason is not None:
        payload["reason"] = reason
    if details is not None:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False))
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
    "nextPageToken,comments(id,content,author/displayName,author/me,createdTime,"
    "quotedFileContent,resolved,deleted,anchor,"
    "replies(id,content,createdTime,author/displayName,author/me,deleted,action))"
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


# Where each request kind carries tab identity. Measured, not assumed (M19):
# a request that does not name a tab is NOT tab-neutral. `insertText` and
# `deleteContentRange` land in the FIRST tab of the document — silently,
# whenever the index happens to be valid there (M19-9) — and `replaceAllText`
# rewrites EVERY tab at once (M19-12).
#
# Values name the field that carries the tab: `tabsCriteria` for the kinds
# that act on a whole tab, otherwise the sub-objects one of which must be
# present and gets a `tabId`.
_TAB_SCOPE = {
    "deleteNamedRange": ("tabsCriteria",),
    "insertText": ("location", "endOfSegmentLocation"),
    "insertInlineImage": ("location", "endOfSegmentLocation"),
    "deleteContentRange": ("range",),
    "createNamedRange": ("range",),
    "updateTextStyle": ("range",),
    "updateParagraphStyle": ("range",),
    "createParagraphBullets": ("range",),
    "deleteParagraphBullets": ("range",),
}


def _scope_requests(requests, tid):
    """Put the target tab into every request of a batch, or refuse to send it.

    A WHITE list on purpose: a kind nobody taught to carry a tab is refused
    before anything is written, instead of being passed through unscoped and
    landing wherever Google decides. The previous shape — decorate the three
    kinds we knew, pass the rest — is what made the canary a mine: the
    requests it prepends never went through it at all.

    `tid` is None only for a legacy document with no `tabs` field; a document
    with several tabs and no chosen tab is refused earlier, by `_select_tab`.
    """
    if not tid:
        return list(requests)
    out = []
    for req in requests:
        kinds = [k for k in req]
        if len(kinds) != 1:
            _error(f"internal: a batch request naming {len(kinds)} kinds "
                   f"({', '.join(sorted(kinds)) or 'none'}) cannot be scoped "
                   f"to a tab (fail closed)")
        kind = kinds[0]
        where = _TAB_SCOPE.get(kind)
        if where is None:
            _error(f"internal: request {kind!r} has no known place for a tab "
                   f"id, and an unscoped request goes to the first tab or to "
                   f"all of them (M19) — refused before writing")
        req = copy.deepcopy(req)
        body = req[kind]
        if where == ("tabsCriteria",):
            body["tabsCriteria"] = {"tabIds": [tid]}
            out.append(req)
            continue
        holder = next((w for w in where if isinstance(body.get(w), dict)),
                      None)
        if holder is None:
            _error(f"internal: request {kind!r} carries none of "
                   f"{list(where)} to put the tab id in (fail closed)")
        body[holder]["tabId"] = tid
        out.append(req)
    return out


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


def _count_text_in_aux_segments(doc_tab, text):
    """Occurrences of `text` in the tab's headers, footers and footnotes.

    Nothing to do with the writer any more: since 0.17.0 an edit is addressed
    by absolute range, so where else a string occurs cannot affect it. What
    still needs this is ghost accounting, which asks a different question —
    «is the old quote still SOMEWHERE in this tab». `_text_buffer` walks the
    body only, so a quote surviving in a running header would otherwise read
    as gone, and a live thread would be declared a ghost.
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


def _count_quote_in_tab(doc_tab, text):
    """How many times `text` occurs in the tab, body and aux segments alike.

    Used by ghost accounting to answer «is this quote still present», never
    as a precondition for a write: the index writer does not search.
    """
    return (_count_quote_occurrences(doc_tab, text)
            + _count_text_in_aux_segments(doc_tab, text))


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
            documentId=doc_id, suggestionsViewMode="SUGGESTIONS_INLINE",
            includeTabsContent=True,
        ).execute()
        # This read and the writes below agree by construction: without a tab
        # id both address the FIRST tab (M19-9). What they cannot do is see
        # the others — so a multi-tab document loses its markers there, and
        # that used to happen in silence (codex, final round). Publication
        # creates single-tab documents, which is why this is a warning and not
        # a refusal.
        if len(_collect_tabs(doc)) > 1:
            _warn("документ многовкладочный: подсветки обрабатываются "
                  "только в первой вкладке, в остальных служебные метки "
                  "останутся как есть")
        _tid, doc = _select_tab(doc, tab_id=_collect_tabs(doc)[0][0])
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
            documentId=doc_id, suggestionsViewMode="SUGGESTIONS_INLINE",
            includeTabsContent=True,
        ).execute()
        revision_id = doc.get("revisionId")
        tabs = _collect_tabs(doc)
        if len(tabs) > 1:
            # Same shape as the highlight pass: read and write agree on the
            # first tab, but markers in the others are invisible to both.
            # Said out loud instead of silently skipped (codex, final round).
            _warn("документ многовкладочный: картинки подставляются только "
                  "в первой вкладке, метки в остальных останутся как есть")
        synthetic_tab = {"body": tabs[0][2].get("body", {})}
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


def _comment_tab_catalog(doc):
    """Flatten root/child tabs and name any identity defect honestly."""
    tabs = _collect_tabs(doc)
    if not doc.get("tabs"):
        # A legacy-shaped response has a body but no stable tab identity.
        # Its text remains useful as a candidate, never as exact attribution.
        return tabs, "tab_ids_not_returned"
    tab_ids = [tab_id for tab_id, _title, _doc_tab in tabs]
    if any(not tab_id for tab_id in tab_ids):
        return tabs, "missing_tab_id"
    if len(set(tab_ids)) != len(tab_ids):
        return tabs, "duplicate_tab_id"
    return tabs, None


def _docs_snapshot_fingerprint(doc):
    """Canonical fallback identity for view-only Docs reads without revision."""
    return _sha256_str(json.dumps(
        doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _read_comment_evidence(drive_service, docs_service, file_id,
                           initial_comments):
    """Read a bounded, stable comments/Docs/export snapshot for ``comments``.

    Drive comments and the Docs resource are separate APIs, and DOCX export is
    a third read.  A single pass can therefore combine states that never
    coexisted.  Bracket the export with both a complete paginated comment
    census and a Docs revision, retrying the whole bracket once.  Evidence is
    usable only when both boundaries agree.  The helper performs reads only.
    """
    before_comments = initial_comments
    last_problem = "snapshot_changed_during_read"
    for attempt in range(2):
        try:
            doc_before = _safe_get_doc(docs_service, file_id)
        except Exception as e:
            _warn(f"tab attribution unavailable: {getattr(e, 'reason', e)}")
            return (before_comments, None, None,
                    "document_tabs_unavailable", [], None)

        records, export_problem = [], None
        try:
            docx_bytes = drive_service.files().export(
                fileId=file_id,
                mimeType="application/vnd.openxmlformats-officedocument"
                         ".wordprocessingml.document",
            ).execute()
            records, record_problems = _docx_comment_records(docx_bytes)
            if record_problems:
                export_problem = "export_comments_unreadable"
                _warn("anchor export status unavailable: "
                      f"{record_problems[0]}")
            elif any(
                    not isinstance(record.get("author"), str)
                    or not record["author"].strip()
                    or _rfc3339_epoch(record.get("date_sec")) is None
                    for record in records):
                # An opaque record can be the missing target.  Ignoring it
                # would let another, later record turn uncertainty into a
                # false absence/ghost verdict.
                export_problem = "export_comment_identity_unreadable"
                _warn("anchor export status unavailable: a comments.xml "
                      "entry has no readable author/date identity")
        except Exception as e:
            export_problem = "document_export_unavailable"
            _warn("anchor export status unavailable: "
                  f"{getattr(e, 'reason', e)}")

        try:
            after_comments = _list_comments_raw(drive_service, file_id)
        except Exception as e:
            _warn("comment snapshot verification unavailable: "
                  f"{getattr(e, 'reason', e)}")
            return (before_comments, None, None,
                    "comment_snapshot_unavailable", records,
                    "comment_snapshot_unavailable")
        try:
            doc_after = _safe_get_doc(docs_service, file_id)
        except Exception as e:
            _warn(f"tab attribution unavailable: {getattr(e, 'reason', e)}")
            return (after_comments, None, None,
                    "document_tabs_unavailable", records,
                    "document_tabs_unavailable")

        before_revision = doc_before.get("revisionId")
        after_revision = doc_after.get("revisionId")
        comments_stable = (
            _fingerprint_from_census(before_comments)
            == _fingerprint_from_census(after_comments))
        if before_revision and after_revision:
            document_stable = before_revision == after_revision
            revision_shape_problem = False
        elif not before_revision and not after_revision:
            # View-only Docs responses commonly omit revisionId.  Comparing
            # the complete canonical resource still detects every body/tab
            # change relevant to both #37 and #44 without inventing a
            # revision or regressing all view-only reads to unknown.
            document_stable = (
                _docs_snapshot_fingerprint(doc_before)
                == _docs_snapshot_fingerprint(doc_after))
            revision_shape_problem = False
        else:
            document_stable = False
            revision_shape_problem = True
        if comments_stable and document_stable:
            tabs, catalog_problem = _comment_tab_catalog(doc_after)
            return (after_comments, tabs, catalog_problem, None, records,
                    export_problem)

        last_problem = (
            "document_revision_unavailable" if revision_shape_problem
            else "snapshot_changed_during_read")
        before_comments = after_comments
        if attempt == 0:
            continue

    _warn("comment attribution unavailable: the comments/Docs snapshot "
          "did not stay stable during two read attempts")
    return (before_comments, None, None, last_problem, records, last_problem)


def _comment_tab_attribution(comment, tabs=None, catalog_problem=None,
                             read_problem=None):
    """Return display-only tab evidence for one Drive comment.

    Drive does not expose a tab id on a comment (M19).  ``quotedFileContent``
    is a stale snapshot and cannot prove the current anchor position, but it
    can still support one deliberately narrow read-only statement: its exact
    text occurs in the body of exactly one tab in this Docs snapshot.  This
    helper never upgrades that hint into write authorization; reply/resolve
    keep using the DOCX marker proof in ``_reply_tab_preflight``.
    """
    quote = (comment.get("quotedFileContent") or {}).get("value") or ""
    if not quote:
        if comment.get("anchor"):
            return {
                "tab_id": None,
                "tab_title": None,
                "tab_attribution": {
                    "status": "unknown",
                    "candidates": [],
                    "reason": "anchor_without_quote",
                },
            }
        return {
            "tab_id": None,
            "tab_title": None,
            "tab_attribution": {
                "status": "document",
                "candidates": [],
                "reason": "unanchored_document_comment",
            },
        }

    if read_problem:
        return {
            "tab_id": None,
            "tab_title": None,
            "tab_attribution": {
                "status": "unknown",
                "candidates": [],
                "reason": read_problem,
            },
        }

    candidates = []
    for tab_id, title, doc_tab in tabs or []:
        occurrences = _count_quote_occurrences(doc_tab, quote)
        if occurrences:
            candidates.append({
                "tab_id": tab_id,
                "tab_title": title or None,
                "quote_occurrences": occurrences,
            })

    # A malformed/partial tab catalogue invalidates even a unique-looking
    # match.  In particular, a missing sibling id must not make the one tab
    # which happened to have an id look authoritative.
    if catalog_problem:
        reason = catalog_problem
    elif len(candidates) > 1:
        reason = "quote_matches_multiple_tabs"
    elif not candidates:
        reason = "quote_not_found_in_tabs"
    else:
        candidate = candidates[0]
        return {
            "tab_id": candidate["tab_id"],
            "tab_title": candidate["tab_title"],
            "tab_attribution": {
                "status": "exact",
                "candidates": candidates,
                "reason": "quote_matches_exactly_one_tab",
            },
        }

    return {
        "tab_id": None,
        "tab_title": None,
        "tab_attribution": {
            "status": "unknown",
            "candidates": candidates,
            "reason": reason,
        },
    }


def _comment_anchor_export_status(comment, *, records, universe, tabs,
                                  file_id=None, export_problem=None,
                                  read_problem=None):
    """Describe read-only export evidence without claiming live freshness.

    A plain Drive export has no canary and may be stale.  A matching record
    therefore means only ``record_present`` in THAT export, never "the anchor
    is live now".  Absence becomes ``ghost`` only under the stricter #34
    witness: the thread has a unique author/time identity, the export contains
    a later record, and the stale quote is absent from every current Docs tab.
    Every inconclusive shape stays ``unknown``.
    """
    base = {"export_freshness": "unproven"}
    if not (comment.get("quotedFileContent") or comment.get("anchor")):
        return {**base, "status": "not_applicable",
                "reason": "document_level_comment"}
    if comment.get("resolved"):
        # Resolved threads are deliberately omitted from DOCX exports (C11c).
        return {**base, "status": "not_applicable",
                "reason": "resolved_threads_omitted_from_export"}
    if export_problem:
        return {**base, "status": "unknown", "reason": export_problem}

    cid = comment.get("id")
    keys = set()
    for entry in [comment] + [
            r for r in (comment.get("replies") or [])
            if not r.get("deleted")]:
        author = (entry.get("author") or {}).get("displayName")
        created = entry.get("createdTime")
        if author and created:
            keys.add((author, _trunc_seconds(created)))
    witnesses = {key for key in keys if universe.get(key) == {cid}}
    if not witnesses:
        return {**base, "status": "unknown",
                "reason": ("shared_or_missing_export_identity")}

    present = [
        record for record in records
        if (record.get("author"), record.get("date_sec")) in witnesses
    ]
    if present:
        return {**base, "status": "record_present",
                "reason": "unique_thread_record_found_in_export",
                "record_count": len(present)}
    if any((record.get("author"), record.get("date_sec")) in keys
           for record in records):
        # A shared key may be this thread's reply. It cannot prove presence,
        # but it is enough to make declaring the thread absent unsafe.
        return {**base, "status": "unknown",
                "reason": "ambiguous_record_may_belong_to_thread"}

    quote = (comment.get("quotedFileContent") or {}).get("value") or ""
    if not quote:
        return {**base, "status": "unknown",
                "reason": "anchor_without_quote_missing_from_export"}
    if read_problem or not tabs:
        return {**base, "status": "unknown",
                "reason": read_problem or "document_tabs_unavailable"}
    if any(_count_quote_in_tab(doc_tab, quote)
           for _tab_id, _title, doc_tab in tabs):
        return {**base, "status": "unknown",
                "reason": "record_missing_but_quote_still_present"}

    # A whole but stale export can legitimately omit a thread while it is
    # resolved.  Seeing a record newer than the PARENT's creation is not
    # enough after the thread has since been reopened: that record may still
    # belong to the resolved interval.  Require evidence newer than every
    # current entry, including the resolve/reopen action replies.  Ordinary
    # replies are included too; the extra false-negative is the honest price
    # of not knowing which cached comment-store snapshot Drive exported.
    activity = []
    for entry in [comment] + [
            reply for reply in (comment.get("replies") or [])
            if not reply.get("deleted")]:
        stamp = _rfc3339_epoch(entry.get("createdTime"))
        if stamp is None:
            return {**base, "status": "unknown",
                    "reason": "thread_activity_time_unreadable"}
        activity.append(stamp)
    if not activity:
        return {**base, "status": "unknown",
                "reason": "thread_activity_time_unreadable"}

    doc_tabs = [doc_tab for _tab_id, _title, doc_tab in tabs]
    verdict = _ghost_verdict(
        comment, records, doc_tabs[0], file_id=file_id,
        other_tabs=doc_tabs[1:], freshness_floor=max(activity))
    if verdict is not None and not verdict.get("fenced"):
        return {**base, "status": "ghost",
                "reason": ("record_missing_after_newer_export_record_and_"
                           "quote_absent_from_document")}
    return {**base, "status": "unknown",
            "reason": "record_missing_export_freshness_unproven"}


def list_comments(file_id, output=None):
    """List comments and attach conservative, read-only tab attribution."""
    try:
        creds = get_creds()
        drive_service = get_drive_service(creds)
    except Exception as e:
        _error(f"auth failed: {e}")

    try:
        raw_comments = _list_comments_raw(drive_service, file_id)
    except HttpError as e:
        _error(f"failed to fetch comments: {e.reason if hasattr(e, 'reason') else e}")

    tabs, catalog_problem, read_problem = None, None, None
    records, export_problem = [], None
    if any(c.get("quotedFileContent") or c.get("anchor")
           for c in raw_comments if not c.get("deleted")):
        try:
            docs_service = get_docs_service(creds)
            (raw_comments, tabs, catalog_problem, read_problem, records,
             export_problem) = _read_comment_evidence(
                 drive_service, docs_service, file_id, raw_comments)
        except Exception as e:
            # Service construction can fail before the bounded reader gets a
            # chance to turn the failure into per-thread uncertainty.
            read_problem = "document_tabs_unavailable"
            _warn(f"tab attribution unavailable: {getattr(e, 'reason', e)}")

    universe = _key_owners_universe(raw_comments)
    # Deleted entries are needed only to keep export identity collisions
    # honest. Preserve the public `comments` view: no deleted threads/replies.
    comments = []
    for raw in raw_comments:
        if raw.get("deleted"):
            continue
        comment = copy.deepcopy(raw)
        comment["replies"] = [
            reply for reply in (comment.get("replies") or [])
            if not reply.get("deleted")
        ]
        comment.pop("deleted", None)
        for reply in comment["replies"]:
            # Accounting-only fields were not part of the public `comments`
            # payload before this command started requesting deleted entries.
            reply.pop("deleted", None)
            # `action` is kept internally until anchor classification: a
            # resolve/reopen entry is part of the export freshness floor.
        comments.append(comment)

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

    attribution_counts = {"exact": 0, "unknown": 0, "document": 0}
    anchor_counts = {"record_present": 0, "ghost": 0, "unknown": 0,
                     "not_applicable": 0}
    for c in comments:
        attribution = _comment_tab_attribution(
            c, tabs=tabs, catalog_problem=catalog_problem,
            read_problem=read_problem)
        c.update(attribution)
        attribution_counts[attribution["tab_attribution"]["status"]] += 1
        anchor_export = _comment_anchor_export_status(
            c, records=records, universe=universe, tabs=tabs,
            file_id=file_id, export_problem=export_problem,
            read_problem=read_problem)
        c["anchor_export"] = anchor_export
        anchor_counts[anchor_export["status"]] += 1
        for reply in c.get("replies") or []:
            # The Drive action is needed only for the internal freshness
            # floor.  Keep the historical public comments schema unchanged.
            reply.pop("action", None)
        # Same rule for the raw Drive `anchor`: it is requested so that a
        # thread with no quote can be classified as document-level, and it is
        # an opaque `kix.…` blob that skrepka cannot decode into coordinates.
        # Emitting it would turn an internal discriminator into a public field
        # somebody starts parsing (codex P2).
        c.pop("anchor", None)

    summary = {"comments": len(comments),
               "unresolved": sum(1 for c in comments
                                 if not c.get("resolved")),
               # so a scoped request («отработай мои комментарии») can be
               # checked against a number, not against a display name the
               # agent had to guess
               "mine": sum(1 for c in comments
                           if (c.get("author") or {}).get("me")),
               "tab_exact": attribution_counts["exact"],
               "tab_unknown": attribution_counts["unknown"],
               "document_level": attribution_counts["document"],
               "anchor_record_present": anchor_counts["record_present"],
               "anchor_ghost": anchor_counts["ghost"],
               "anchor_unknown": anchor_counts["unknown"]}
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


# Characters an edit may not carry, because Docs does not keep them as
# written and the text that lands would not be the text the positions were
# computed for. Measured 2026-08-23 on the M20 stand: `insertText` ACCEPTS a
# form feed — the reply is a plain success — and the character simply is not
# there afterwards. A silent drop is the worst shape a write can take.
#
# Three control characters are deliberately allowed, each because it is a
# thing an author writes: the tab, the paragraph break (`\n`, which is how an
# edit adds a paragraph), and the soft line break (`\v`, shift+enter — #40).
# The Private Use Area is refused for the same measured reason as the control
# characters: Docs is known to strip it.
_OP_TEXT_FORBIDDEN = re.compile("[\x00-\x08\x0c-\x1f\x7f-\x9f\ue000-\uf8ff]")


def _resolve_op(op, doc_tab, tab_id):
    """Resolve one op dict to an internal record with absolute indices.

    Op shapes supported:
      {"op": "replace_range",   "range": "<name>", "text": "..."}
      {"op": "replace_quote",   "quote": "...", "with": "...", "occurrence": N?}
      {"op": "insert_before_range", "range": "<name>", "text": "..."}
      {"op": "insert_after_range",  "range": "<name>", "text": "..."}
      {"op": "insert_before_quote", "quote": "...", "text": "...", "occurrence": N?}
      {"op": "insert_after_quote",  "quote": "...", "text": "...", "occurrence": N?}

    Есть ещё одна форма, которая СЮДА НЕ ПОПАДАЕТ и попасть не может:
      {"op": "replace_anchor", "comment_id": "...", "with": "..."}
    Её адрес — тред, а не текст, и координат у него до карты выгрузки не
    существует. Она разрешается позже, в `_resolve_anchor_target`.

    Returns a dict:
      {"op": ..., "start": int, "end": int, "text": str, "kind": "replace"|"insert",
       "affect_start": int, "affect_end": int, "source": "..."}
    """
    kind_name = op.get("op")
    if not kind_name:
        _error(f"op missing 'op' field: {op}")

    new_text = op.get("text") if "text" in op else op.get("with")
    if isinstance(new_text, str):
        bad = _OP_TEXT_FORBIDDEN.search(new_text)
        if bad:
            _error(
                f"op text holds a character Docs does not keep as written "
                f"({bad.group()!r} = U+{ord(bad.group()):04X}), so what would "
                f"land is not what the positions were computed for. Measured "
                f"2026-08-23: `insertText` accepts a form feed and drops it "
                f"silently. Allowed control characters are the tab, the "
                f"paragraph break (\\n) and the soft line break (\\v, "
                f"shift+enter). {op}")

    # Resolve target
    quote_total = None
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
            _error(f"quote not found: {quote!r}",
                   reason="quote_not_found", details={"quote": quote})
        if total > 1 and "occurrence" not in op:
            # Ambiguity is still a refusal — skrepka must not pick a copy on
            # the person's behalf. What changed is that the refusal now names
            # a path that WORKS on a commented document too: since the write
            # goes by index, the occurrence number is a real address (M24-0).
            _error(
                f"quote is non-unique ({total} matches): {quote!r}. "
                f"Say WHICH one with 'occurrence': N (1..{total}), counted "
                f"from the start of the tab — it works on documents with "
                f"comments as well. A longer quote also disambiguates, but "
                f"not when the paragraph itself repeats word for word. Do "
                f"not guess a copy: if it is not clear which one the person "
                f"meant, ask.",
                reason="quote_ambiguous",
                details={"quote": quote, "matches": total},
            )
        if occurrence > total:
            _error(
                f"occurrence {occurrence} out of range (only {total} matches): {quote!r}"
            )
        found = _find_quote_in_doctab(doc_tab, quote, occurrence=occurrence)
        if not found:
            _error(f"quote not found: {quote!r}",
                   reason="quote_not_found", details={"quote": quote})
        t_start, t_end = found
        source = f"quote={quote!r} (#{occurrence}/{total})"
        quote_total = total
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
            "quote_total": quote_total,
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
            "quote_total": quote_total,
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
            "quote_total": quote_total,
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

    `cell_paths` narrows that further where it can: {w:id -> cell path} for
    ids whose exact cell the census could name. An id missing from it is
    bounded only by «some table» — that is what an unaddressable container
    (a table wrapped in `w:sdt`) leaves behind.
    """

    def __new__(cls, text, in_tables=(), elsewhere=(), cell_paths=None,
                docx_lattice=None):
        obj = super().__new__(cls, text)
        obj.in_tables = frozenset(in_tables)
        obj.elsewhere = frozenset(elsewhere)
        obj.cell_paths = dict(cell_paths or {})
        # the export's lattice shape, so a fence built from one of those paths
        # is checked against the API the same way a placement is — a path is
        # only worth as much as the two sides agreeing under it
        obj.docx_lattice = docx_lattice
        return obj


class _LocalizedProblem(str):
    """A problem that already carries the document ranges it is confined to.

    `_AnchorProblem` names export ids and needs the placed `anchors` to turn
    them into coordinates. That is exactly what is unavailable for an anchor
    which could NOT be placed — and «could not be placed» is the ordinary
    outcome inside a table cell (no match, several matches, an unreadable
    neighbour). Such a problem knows its own fence: the cell, or the table.
    """

    def __new__(cls, text, ranges=(), docx_id=None):
        obj = super().__new__(cls, text)
        obj.ranges = tuple(ranges)
        obj.docx_id = docx_id
        return obj


# A path names a table cell the same way on both sides of the mapping: a tuple
# of (table_ordinal, row_index, GRID COLUMN) triples, one per nesting level,
# empty for the body proper.
#
# The third number is the grid column, NOT the position in the cell list, and
# that distinction is the whole reason M16 was measured. The two sides count
# cells differently: the API keeps one entry per grid square (a square covered
# by a horizontal merge stays in the list as an empty stub), while the export
# keeps one `w:tc` per physical cell and marks the first with `gridSpan`. On a
# row whose first two cells are merged, export cell #1 is API cell #2 — mapping
# by list position would put the anchor in the empty stub next door.
def _docx_grid_span(tc):
    """How many grid columns this `w:tc` occupies (`w:gridSpan`, default 1)."""
    node = tc.find(f"{_WORDML_NS}tcPr/{_WORDML_NS}gridSpan")
    if node is None:
        return 1
    try:
        n = int(node.get(f"{_WORDML_NS}val"))
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


# In-paragraph breaks: the one alphabet both sides speak (#27, measured M20).
#
# The export used to write EVERY break as \n — soft break, page break, column
# break alike — and \n is a character no API paragraph text can hold, since \n
# is what ends a paragraph. So a commented paragraph with shift+enter in it
# matched nothing, and one such paragraph closed the whole document to
# replaces. Measured live: 50 paragraphs, edits needed in 30, one soft break —
# every replace refused (postmortem 2026-08-20).
#
# Normalizing \v to \n at comparison time was the obvious fix and is a
# MEASURED fail-open: the export writes a soft-break paragraph and a
# page-break paragraph with the same visible text IDENTICALLY (M20-4), so an
# anchor sitting on the page-break one would match the soft-break twin exactly
# once — one candidate, no fence, anchor on the wrong paragraph.
#
# So the parser stops throwing the distinction away. `w:br` carries its kind
# in `w:type`, and each kind gets the spelling the API uses:
#
#   soft break   `w:br` without a type, or type="textWrapping"   ->  \v
#   page break   `w:br w:type="page"`                            ->  \f
#
# `w:cr` is NOT in the table and gets no branch of its own: WordML calls it a
# line break, but Google wrote none in the measurement (0 of 8 breaks, M20-1),
# so it is unmeasured — and the walker's catch-all already marks a paragraph
# holding an unknown tag unreadable. A branch that repeated that would read
# like logic while changing nothing (caught by the mutation stand).
#
# Anything else — a column break, a type nobody measured — leaves the
# paragraph UNREADABLE rather than guessing a character: a wrong guess shifts
# every offset after it, and an anchor placed on shifted offsets protects the
# wrong characters. This alphabet is for COMPARING the two sides only. Nothing
# addresses text by it: writes go through `_text_buffer`, which puts a \x00
# sentinel at every index gap, so a quote can never cross a page break.
_SOFT_BREAK = "\v"
_PAGE_BREAK = "\f"
_DOCX_BREAK_TEXT = {None: _SOFT_BREAK, "textWrapping": _SOFT_BREAK,
                    "page": _PAGE_BREAK}
# The API side of the same table. A soft break needs no entry — it arrives as
# \v inside `textRun` content and is already spelled. `pageBreak` is its own
# element, one index unit wide (M20-2), which is exactly what the export
# counts for `w:br`.
_API_BREAK_TEXT = {"pageBreak": _PAGE_BREAK}
# Neither spelling may arrive as ordinary text, or an API paragraph could read
# identical to an exported page break without holding one. Never measured in
# the wild, and cheap to refuse (codex plan r2).
_BREAK_CHARS = _SOFT_BREAK + _PAGE_BREAK


def _parse_docx_anchor_spans(docx_bytes):
    """Parse word/document.xml and extract comment anchor spans.

    Returns (spans, problems, census):
      spans: [{"docx_id", "para_index", "para_text", "start_off",
               "end_para_index", "end_para_text", "end_off", "anchor_text",
               "has_objects", "path", "end_path", "docx_lattice"}] — offsets
              are UTF-16 units within their own paragraph's text. The two
              paragraph indices are equal for an ordinary anchor and differ
              when the selection was dragged across a paragraph break (#45).
              `path` says which structural domain the paragraph belongs to:
              empty for the body, a chain of (table, row, grid column) for a
              table cell (#48). Two paragraphs with the same text in different
              domains are different paragraphs, and the mapper never confuses
              them.
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
                    "in_body": frozenset(), "paths": {},
                    "docx_lattice": {"tables": {}, "rows": {}}}
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml_bytes = z.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        return [], [f"malformed docx export: {e}"], empty_census

    w = _WORDML_NS
    seen_starts, seen_ends = {}, {}
    cross_para_open = {}

    body = root.find(f"{w}body")
    if body is None:
        return [], ["malformed docx export: no w:body"], empty_census

    # Global census of ALL range markers anywhere in the document. After the
    # paragraph walk, the processed multiset must equal this one — any marker
    # hidden in an unsupported container (w:sdt, w:fldSimple,
    # mc:AlternateContent, tracked changes, ...) becomes a problem instead
    # of being silently ignored (codex W8-r1 P0#1).
    #
    # The census also remembers WHERE it saw each marker. Since r11 that is a
    # CELL PATH, not merely «somewhere in a table»: a marker the walk could not
    # process fences its own cell instead of every table in the file (#48).
    global_starts, global_ends = {}, {}
    census_tables, census_other = set(), set()
    census_paths, path_untrusted = {}, set()

    def _census(node, path, in_table, trusted, container):
        """Count markers, tracking the enclosing cell path.

        `t_ord` counts tables the way the API lists them: direct children of a
        container (`body.content` / `tableCell.content`). A `w:tbl` reached
        through anything else — wrapped in `w:sdt`, say — is walked for its
        markers, but everything below it is marked untrusted, because the two
        sides would then number tables differently and a path nobody can trust
        must never become a fence.

        Descending through a non-container element does NOT by itself spoil
        the path: a marker hidden in a `w:sdt` inside a cell is still known to
        be in THAT cell, which is exactly the case worth fencing tightly.
        """
        t_ord = 0
        for child in node:
            tag = child.tag
            if tag == f"{w}commentRangeStart" or tag == f"{w}commentRangeEnd":
                cid = child.get(f"{w}id")
                bucket = global_starts if tag.endswith("Start") else global_ends
                bucket[cid] = bucket.get(cid, 0) + 1
                (census_tables if in_table else census_other).add(cid)
                if path:
                    # every path this id was seen at, not the first: a marker
                    # whose start hides in one cell and whose end hides in
                    # another would otherwise fence only the opening cell and
                    # leave the closing one editable (found in code review)
                    census_paths.setdefault(cid, set()).add(path)
                if not trusted:
                    path_untrusted.add(cid)
                continue
            if tag == f"{w}tbl":
                addressable = trusted and container
                for j, tr in enumerate(child.findall(f"{w}tr")):
                    col = 0
                    for tc in tr.findall(f"{w}tc"):
                        _census(tc, path + ((t_ord, j, col),), True,
                                addressable, True)
                        col += _docx_grid_span(tc)
                t_ord += 1
                continue
            _census(child, path, in_table, trusted, False)

    _census(body, (), False, True, True)

    # Paragraphs are collected across DOMAINS now: direct children of the body
    # and direct children of table cells, each carrying the path that says
    # which one it is. The r8 rule that a text-alone match may never cross a
    # structural boundary is not weakened by this — it moves into the mapper,
    # which searches a span only among paragraphs sharing its path (#48).
    body_paras, para_paths, para_top = [], [], []
    # The body as a flat list of its OWN elements, in order — what the proof
    # of a tab segment walks (`_prove_target_segment`). Every paragraph
    # remembers which of them holds it, including paragraphs nested in cells,
    # so a span can be told from which side of a tab boundary it came.
    outline = []
    docx_lattice = {"tables": {}, "rows": {}, "table_rows": {}, "paras": {}}

    def _collect(node, path, top=None):
        t_ord = 0
        docx_lattice["paras"].setdefault(path, [])
        for child in node:
            here = top
            if child.tag == f"{w}p":
                if not path:
                    here = len(outline)
                    outline.append({"kind": "p", "para": len(body_paras)})
                body_paras.append(child)
                para_paths.append(path)
                para_top.append(here)
            elif child.tag == f"{w}tbl":
                if not path:
                    here = len(outline)
                    outline.append({"kind": "tbl", "t_ord": t_ord,
                                    "cells": {}, "row_widths": {}})
                rows = child.findall(f"{w}tr")
                docx_lattice["table_rows"][(path, t_ord)] = len(rows)
                for j, tr in enumerate(rows):
                    col = 0
                    for tc in tr.findall(f"{w}tc"):
                        sub_path = path + ((t_ord, j, col),)
                        if not path:
                            outline[here]["cells"][(j, col)] = sub_path
                        _collect(tc, sub_path, here)
                        col += _docx_grid_span(tc)
                    docx_lattice["rows"][(path, t_ord, j)] = col
                    if not path:
                        outline[here]["row_widths"][j] = col
                t_ord += 1
        docx_lattice["tables"][path] = t_ord

    _collect(body, ())

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
        # `w:t`, `w:tab`, `w:br` and `w:cr` are read one by one below — each
        # break kind has its own spelling and its own reason (#27).
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
                            # No guard against `w:t` carrying a break spelling
                            # as ordinary text — one was written and deleted as
                            # dead code. XML 1.0 forbids U+000B and U+000C in
                            # content, entity or not, so such an export is
                            # malformed and this parser refuses it whole, one
                            # layer up (covered by a test). The API side is
                            # JSON and has no such rule, so `_api_para_read`
                            # keeps its own guard.
                            text = rc.text or ""
                            state["parts"].append(text)
                            state["off"] += _utf16_len(text)
                        elif rc.tag == f"{w}tab":
                            state["parts"].append("\t")
                            state["off"] += 1
                        elif rc.tag == f"{w}br":
                            spelled = _DOCX_BREAK_TEXT.get(rc.get(f"{w}type"))
                            if spelled is None:
                                # a column break, or a kind Google adds
                                # tomorrow: not guessed, not counted
                                state["has_unknown"].append(
                                    f"{rc.tag}[{rc.get(f'{w}type')}]")
                                continue
                            state["parts"].append(spelled)
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
        text = "".join(state["parts"])
        para_meta.append({"text": text,
                          "has_objects": state["has_objects"],
                          "has_unknown": state["has_unknown"],
                          "path": para_paths[p_index]})
        # the export's own text of every paragraph, in order, per container —
        # this is what proves a resolved cell is the SAME cell and not merely
        # a cell of the same shape (code review r11)
        docx_lattice["paras"][para_paths[p_index]].append(text)

    # Texts are attached AFTER the walk, not inside it: a span that crosses a
    # paragraph break is not complete until both of its paragraphs have been
    # read, and the old in-loop assignment could only ever see the first one.
    # texts of the body's own elements, for the segment proof. A paragraph
    # the walker could not read whole (an object, an unsupported element) is
    # marked: it can never be PROVEN equal to an API paragraph, and the proof
    # refuses instead of counting elements.
    for el in outline:
        if el["kind"] == "p":
            meta = para_meta[el["para"]]
            el["text"] = meta["text"]
            # `has_objects` deliberately does NOT count here: a drawing puts no
            # text into either side, so the paragraph still compares by text.
            # An anchor inside it is refused anyway, one layer down.
            el["unreadable"] = bool(meta["has_unknown"])
        else:
            texts = {}
            for pos, cpath in el["cells"].items():
                parts = [para_meta[i]["text"] for i, pth
                         in enumerate(para_paths) if pth == cpath]
                texts[pos] = "\n".join(parts)
            el["cell_text"] = texts

    # Built once: two full scans of `para_meta` per span made the parser
    # quadratic on a long document (codex P2).
    para_roll = [
        (para_top[i], meta["text"], para_paths[i],
         not meta["has_objects"] and not meta["has_unknown"])
        for i, meta in enumerate(para_meta)
    ]

    for sp in spans:
        head, tail = para_meta[sp["para_index"]], para_meta[sp["end_para_index"]]
        sp["top"] = para_top[sp["para_index"]]
        sp["end_top"] = para_top[sp["end_para_index"]]
        sp["para_text"] = head["text"]
        sp["end_para_text"] = tail["text"]
        sp["path"], sp["end_path"] = head["path"], tail["path"]
        # The export's own shape of the lattice, shared by reference. The
        # mapper checks it against the API before trusting a path: the number
        # of tables in a container and the grid width of a row must agree, or
        # the two sides are describing different documents (M16).
        sp["docx_lattice"] = docx_lattice
        # The export's own paragraph roll, shared by reference like the
        # lattice above. It is what lets `_twin_position` say WHICH copy of a
        # repeated paragraph an anchor sits on — on a document whose format
        # requires identical previews, nothing else tells the copies apart
        # (постмортем 2026-08-25). `readable` matters as much as the text: a
        # paragraph holding an inline object is counted by the export and
        # dropped by the API, so one of them would silently shift the count.
        sp["docx_paragraphs"] = para_roll
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
            reason = (f"anchor {sp['docx_id']} sits in a paragraph with "
                      f"unsupported elements ({unknown[:3]}) "
                      f"— text/offsets unreliable")
            # Inside a cell this is a local defect with a known extent, so it
            # is handed to the mapper to fence instead of freezing the
            # document. In the body there is nothing to fence with.
            if sp["path"] or sp["end_path"]:
                sp["unreadable"] = reason
            else:
                problems.append(reason)

    processed = set(seen_starts) & set(seen_ends)
    census = {"in_tables": frozenset(census_tables - processed),
              "elsewhere": frozenset(census_other - processed),
              "in_body": frozenset(processed),
              # only trustworthy, unambiguous paths travel: the accounting
              # fences by them too, and a fence is worth nothing if it can
              # land next door or cover half of a straddling pair
              "paths": {cid: next(iter(p)) for cid, p in census_paths.items()
                        if cid not in path_untrusted and len(p) == 1},
              # the export's own lattice shape, so a fence built from a path
              # can be checked against the API exactly as a placement is
              "docx_lattice": docx_lattice,
              # the body's own elements in order — the material the tab
              # segment proof works on
              "outline": outline}
    if seen_starts != global_starts or seen_ends != global_ends:
        hidden = (set(global_starts) | set(global_ends)) - processed
        in_tables = sorted(census["in_tables"])
        elsewhere = sorted(census["elsewhere"])
        # A path is only handed on when the census could name ONE cell and
        # nothing untrustworthy stood between it and the container. Two
        # different cells for one id means the marker pair straddles them, and
        # a fence around either half would leave the other open — such an id
        # goes back to the whole-table fence r8 used.
        paths = {cid: next(iter(census_paths[cid])) for cid in in_tables
                 if cid in census_paths and cid not in path_untrusted
                 and len(census_paths[cid]) == 1}
        if in_tables and not elsewhere:
            problems.append(_HiddenMarkerProblem(
                f"comment anchors sit inside tables: {in_tables} — their "
                f"exact position is not readable from the export, so edits "
                f"inside tables are refused and the rest of the document "
                f"stays editable",
                in_tables=in_tables, cell_paths=paths,
                docx_lattice=docx_lattice))
        else:
            problems.append(_HiddenMarkerProblem(
                f"comment range markers outside plain body paragraphs "
                f"(tables/containers/tracked changes): "
                f"{sorted(hidden) or 'count mismatch'} — mapping unusable",
                in_tables=in_tables, elsewhere=elsewhere, cell_paths=paths,
                docx_lattice=docx_lattice))
    for cid, n in seen_starts.items():
        if n != 1 or seen_ends.get(cid, 0) != 1:
            problems.append(
                f"comment range {cid}: {n} starts / "
                f"{seen_ends.get(cid, 0)} ends (need exactly 1/1)")
    for s in spans:
        local = bool(s.get("path") or s.get("end_path"))
        if s.get("has_objects"):
            reason = (f"anchor {s['docx_id']} sits in a paragraph with inline "
                      f"objects — offsets unreliable")
            if local:
                s.setdefault("unreadable", reason)
            else:
                # carries its own id so a caller that knows this span belongs
                # to ANOTHER tab can drop it: an anchor no write can reach is
                # not a reason to refuse (opus, final round)
                problems.append(_AnchorProblem(reason, (s["docx_id"],)))
        if not s.get("anchor_text"):
            reason = f"anchor {s['docx_id']} is empty"
            if local:
                s.setdefault("unreadable", reason)
            else:
                problems.append(_AnchorProblem(reason, (s["docx_id"],)))
    return spans, problems, census


# Element kinds that CANNOT hide an anchor, each for a proven reason. The
# question they answer is narrow: this API paragraph cannot be read, so could
# the anchor we are about to place somewhere else actually live HERE?
#   inlineObjectElement / equation  — the export marks the paragraph
#       `has_objects`, so an anchor in it fails closed before any mapping;
#   footnoteReference               — `w:footnoteReference` is outside the
#       parser's whitelist, so the same applies;
#   columnBreak                     — the export writes it as a `w:br` of a
#       kind M20 never measured, which makes the paragraph unclean, so an
#       anchor in it is refused on the export side before placement (#27 used
#       to reach the same conclusion through «\n matches nothing», which
#       stopped being true when the two sides started spelling breaks alike);
#   horizontalRule                  — such a paragraph shows no text, so any
#       anchor in it is empty and refused earlier.
# `pageBreak` is deliberately NOT here any more: it is spelled `\f` on both
# sides now, so such a paragraph is READ, not guessed at — it is an ordinary
# candidate. A pageBreak whose width is not one unit does not reach this set
# either: it arrives as «pageBreak/width», which stays a possible host.
# Everything else — person, richLink, autoText and any kind Google adds
# tomorrow — is treated as a possible host. A blocklist, not a whitelist: an
# unknown kind must fail towards protection, not away from it (r9 review).
_QUIET_ELEMENTS = frozenset((
    "textRun", "inlineObjectElement", "equation", "footnoteReference",
    "columnBreak", "horizontalRule"))

# Element kinds that contribute NO text to the export, so a paragraph holding
# them still compares by its text runs alone. Used only by the segment proof:
# a picture in a paragraph is ordinary in an editorial document, and refusing
# the whole tab over it would leave the live case of #31 unfixed (opus, final
# round). Breaks are NOT here and must never be: they DO contribute a
# character to both sides now (`\v`, `\f`), and a break kind that cannot be
# spelled makes the paragraph unreadable instead — dropping it silently would
# make two different paragraphs read the same.
_TEXTLESS_ELEMENTS = frozenset((
    "inlineObjectElement", "equation", "footnoteReference", "horizontalRule"))


def _api_para_read(para, *, textless_ok=False):
    """Spell one API paragraph the way the export writes it.

    Returns (text, pieces, opaque): `text` is None when the paragraph holds
    an element this cannot spell, `pieces` are its textRun fragments in order
    (what `_pieces_fit` matches against), and `opaque` names the element kinds
    that made it unreadable.

    One reader for all three consumers — the anchor lattice, a table cell and
    the tab outline — because three copies of «how a paragraph reads» is how
    the two sides end up disagreeing about the same paragraph.

    A break element is spelled only when BOTH hold: its kind was measured
    (M20), and its own extent is exactly one index unit — the single unit the
    export counts for `w:br`. A kind nobody measured (columnBreak) or a width
    that is not one leaves the paragraph unreadable: guessing there would
    shift every offset after the break, and an anchor placed on shifted
    offsets protects the wrong characters.

    `textless_ok` is for comparing TEXT alone, where an element that
    contributes no characters to either side (a picture) can be skipped. The
    lattice must not use it: it needs offsets, and a picture occupies index
    space the export does not spell.
    """
    parts, pieces, opaque = [], [], set()
    for e in para.get("elements", []) or []:
        kinds = [k for k in e if k not in ("startIndex", "endIndex")
                 and not k.startswith("suggested")]
        if kinds == ["textRun"]:
            # `kinds ==`, not `"textRun" in e`: an element carrying a textRun
            # AND something else would have been read as its text alone, and
            # whatever the neighbour is, it occupies index space the text does
            # not account for — every offset after it would shift (codex code
            # r2). The API documents a paragraph element as a union of one
            # kind; this is what happens if that ever stops being true.
            content = e["textRun"].get("content", "")
            if _PAGE_BREAK in content:
                # `\v` here IS the soft break — that is how the API spells it.
                # `\f` is different: as ordinary text it would let this
                # paragraph read exactly like an exported page break. XML
                # forbids it in the export, JSON does not here (codex plan r2).
                opaque.add("textRun(break)")
                continue
            parts.append(content)
            pieces.append(content)
            continue
        if not kinds:
            # An element that names no kind at all — every key of it is one
            # this reader does not know. It still occupies index space, so
            # skipping it would shift every offset after it. The old reader
            # refused any element without a `textRun` and was right to
            # (codex code r1).
            opaque.add("unknown")
            continue
        if len(kinds) == 1 and kinds[0] in _API_BREAK_TEXT:
            st, en = e.get("startIndex"), e.get("endIndex")
            if isinstance(st, int) and isinstance(en, int) and en - st == 1:
                parts.append(_API_BREAK_TEXT[kinds[0]])
                continue
            # a break of the right kind and the wrong width: unreadable, and
            # NOT quiet — «cannot spell it» must not read as «cannot hold an
            # anchor» (codex plan r2)
            opaque.add(f"{kinds[0]}/width")
            continue
        # A break sharing its element with anything else is not spelled
        # either: what the neighbour contributes is unknown, and the break's
        # one unit no longer accounts for the element's width.
        if textless_ok and not (set(kinds) - _TEXTLESS_ELEMENTS):
            continue
        opaque.update(kinds)
    text = "".join(parts)
    if text.endswith("\n"):
        text = text[:-1]
    if pieces and pieces[-1].endswith("\n"):
        pieces[-1] = pieces[-1][:-1]
    return (None if opaque else text), pieces, opaque


def _api_cell_text(cell):
    """Text of a table cell as the export writes it, or None if unreadable."""
    parts = []
    for el in cell.get("content", []) or []:
        para = el.get("paragraph")
        if not para:
            return None  # a nested table: not compared, not guessed
        text, _pieces, _opaque = _api_para_read(para)
        if text is None:
            return None
        parts.append(text)
    return "\n".join(parts)


def _api_outline(doc_tab):
    """The tab's own body elements in order, in the shape the export has.

    `text` is None for a paragraph the API will not spell out (a smart chip,
    a rich link): such an element can never be PROVEN equal to an exported
    one, and the proof of a segment refuses rather than counting elements.
    """
    out = []
    for el in (doc_tab.get("body", {}) or {}).get("content", []):
        if "table" in el:
            rows = el["table"].get("tableRows", []) or []
            cells = {}
            for j, row in enumerate(rows):
                for k, cell in enumerate(row.get("tableCells", []) or []):
                    cells[(j, k)] = _api_cell_text(cell)
            out.append({"kind": "tbl", "rows": len(rows), "cells": cells})
        elif "paragraph" in el:
            text, _pieces, _opaque = _api_para_read(el["paragraph"],
                                                    textless_ok=True)
            out.append({"kind": "p", "text": text})
    return out


def _same_body_text(api_text, docx_text):
    """Do the two sides read this body element the same way?

    Plain equality, and that is the point of #27: both sides now spell an
    in-paragraph break the same character (`_DOCX_BREAK_TEXT`,
    `_API_BREAK_TEXT`), so the normalization this used to carry — `\v` read
    as `\n` — is not just unnecessary but was the shape of a measured
    fail-open (M20-4). Two paragraphs with the same visible text, one holding
    a soft break and one a page break, are IDENTICAL under it.

    Kept as a named comparison rather than inlined: what it means for the two
    sides to «read an element the same way» is the whole load-bearing
    assumption of the tab proof, and it deserves a place to be argued.
    """
    if api_text is None or docx_text is None:
        return False
    return api_text == docx_text


def _prove_target_segment(outline, tabs, tid, canary_text=None):
    """Prove where the target tab's own elements start and end in the export.

    Returns (first, last, None) — inclusive indices into `outline`, the head
    paragraph included — or (None, None, why).

    The proof is deliberately about ONE tab. The question anchors ask is
    binary — inside the target tab or not — and answering it needs two
    offsets, not a partition of the whole document. A soft break, a picture
    or a pending suggestion in some OTHER tab then costs nothing at all.

    Nothing is counted: every element of the segment is either proven equal to
    an API element, or one of a CLOSED set of phantoms proven by its own
    content — the head paragraph (its text is the tab's title, which Google
    keeps unique and non-empty across a document, M19-21) and the freshness
    canary (its own uuid text, at the tail). A tolerance of one element would
    be invisible: tab index spaces start at the same 1 (M19-11) and coincide
    in length just as easily (M19-7).

    A tab's title may legitimately equal the TEXT of some paragraph elsewhere
    (M19-22), so a candidate head is not required to be unique in the export.
    Every candidate is walked instead, and the proof holds when exactly one of
    them accounts for the tab whole (opus, final round). Two would mean the
    document really is ambiguous, and then nothing is placed.
    """
    by_id = {t: (title, dt) for t, title, dt in tabs}
    order = [t for t, _title, _dt in tabs]
    if tid not in by_id:
        return None, None, f"целевая вкладка {tid!r} не найдена в документе"
    title, doc_tab = by_id[tid]
    if not title:
        return None, None, ("у целевой вкладки нет названия, а по названию "
                            "опознаётся её начало в выгрузке")
    heads = [i for i, el in enumerate(outline)
             if el["kind"] == "p" and not el.get("unreadable")
             and el.get("text") == title]
    if not heads:
        return None, None, (
            f"в выгрузке нет абзаца с названием вкладки «{title}», по "
            f"которому опознаётся её начало")
    nxt = order.index(tid) + 1
    next_title = by_id[order[nxt]][0] if nxt < len(order) else None
    api_elements = _api_outline(doc_tab)

    proven, whys = [], []
    for head in heads:
        last, why = _segment_from(outline, head, api_elements, title,
                                  canary_text, next_title)
        if why is None:
            proven.append((head, last))
        else:
            whys.append(why)
    if len(proven) == 1:
        return proven[0][0], proven[0][1], None
    # Two candidates accounting for the tab whole is insurance, not a live
    # case: the tail of a segment is pinned by the next tab's title or by the
    # end of the body, so a second candidate runs out before it gets there.
    # Deliberately not covered by a mutation for that reason — like
    # `cell_anchor_tables`, the formulation stays because a fence with a hole
    # in it is worse than an unreachable branch.
    if not proven:
        return None, None, whys[0]
    return None, None, (
        f"вкладке «{title}» в выгрузке одинаково подходят {len(proven)} "
        f"мест — какое из них её начало, из выгрузки не читается")


def _segment_from(outline, head, api_elements, title, canary_text,
                  next_title):
    """Walk one candidate segment. Returns (last_index, None) or (None, why)."""
    i = head + 1
    for n, api in enumerate(api_elements):
        if i >= len(outline):
            return None, (f"выгрузка кончилась на элементе {n} вкладки "
                          f"«{title}»")
        got = outline[i]
        if api["kind"] != got["kind"]:
            return None, (
                f"элемент {n} вкладки «{title}»: по документу это "
                f"{'таблица' if api['kind'] == 'tbl' else 'абзац'}, "
                f"а в выгрузке "
                f"{'таблица' if got['kind'] == 'tbl' else 'абзац'}")
        if api["kind"] == "p":
            if got.get("unreadable"):
                return None, (
                    f"абзац «{str(got.get('text'))[:40]}» вкладки «{title}» "
                    f"выгрузка отдаёт не целиком, поэтому границу вкладки "
                    f"доказать нечем")
            if api["text"] is None:
                return None, (
                    f"текст абзаца «{str(got.get('text'))[:40]}» вкладки "
                    f"«{title}» документ не отдаёт — так выглядят смарт-чип, "
                    f"разрыв страницы и подобное")
            if not _same_body_text(api["text"], got.get("text")):
                return None, (
                    f"элемент {n} вкладки «{title}»: текст расходится "
                    f"({api['text'][:40]!r} по документу против "
                    f"{str(got.get('text'))[:40]!r} по выгрузке)")
        else:
            why = _cells_agree(api, got, title, n)
            if why:
                return None, why
        i += 1
    if canary_text is not None:
        if (i >= len(outline) or outline[i]["kind"] != "p"
                or outline[i].get("text") != canary_text):
            return None, ("канарейка не нашлась в хвосте целевой вкладки — "
                          "выгрузка не та, что ожидалась")
        i += 1
    if next_title is not None:
        if (i >= len(outline) or outline[i]["kind"] != "p"
                or outline[i].get("text") != next_title):
            return None, (
                f"после вкладки «{title}» ожидался заголовок следующей "
                f"вкладки «{next_title}», а лежит другое — граница не "
                f"доказана")
    elif i != len(outline):
        return None, (f"после последней вкладки в выгрузке осталось "
                      f"{len(outline) - i} лишних элементов")
    return i - 1, None


def _cells_agree(api, got, title, n):
    """Compare a table by CONTENT, not by shape — the r11 rule.

    Two tabs of the «draft / clean copy» kind hold tables of identical shape,
    so rows, columns and gridSpan all agree while the cells hold different
    text. Every cell the export names is checked against the API cell at the
    same GRID position, which is the address M16 proved the two sides share.
    """
    if api["rows"] != len(got["row_widths"]):
        return (f"элемент {n} вкладки «{title}»: строк в таблице "
                f"{api['rows']} по API против {len(got['row_widths'])} "
                f"по выгрузке")
    # The whole grid, not only the squares the export names. The two sides
    # count cells differently — the API keeps one entry per grid square, the
    # export one per physical cell plus `gridSpan` (M16) — so a row whose
    # widths disagree means the tables are not the same table, and an API
    # square the export never covers must be the empty placeholder a merge
    # leaves behind. Without this a table could pass on its named cells alone
    # (codex, final round).
    for j, width in sorted(got["row_widths"].items()):
        api_width = len([1 for jj, _k in api["cells"] if jj == j])
        if api_width != width:
            return (f"элемент {n} вкладки «{title}»: в строке {j} по API "
                    f"{api_width} клеток сетки против {width} по выгрузке")
    for (j, col), text in sorted(got["cell_text"].items()):
        expect = api["cells"].get((j, col))
        if expect is None:
            return (f"элемент {n} вкладки «{title}»: ячейки ({j}, {col}) нет "
                    f"на стороне API или её текст не читается")
        if not _same_body_text(expect, text):
            return (f"элемент {n} вкладки «{title}»: ячейка ({j}, {col}) "
                    f"расходится ({expect[:30]!r} против {text[:30]!r})")
    for (j, k), text in sorted(api["cells"].items()):
        if (j, k) in got["cell_text"]:
            continue
        if text:
            return (f"элемент {n} вкладки «{title}»: клетка ({j}, {k}) есть "
                    f"по API с текстом {text[:30]!r}, а выгрузка её не "
                    f"показывает")
    return None


def _twin_position(sp, first=None, last=None):
    """Which copy of a repeated paragraph this anchor sits on, and whether
    that number can be trusted. Returns (ordinal, total, trusted).

    The export and the API walk the same document in the same order, so the
    Nth identical paragraph on one side is the Nth on the other — but only
    while both sides SEE the same paragraphs. They do not always: a paragraph
    holding an inline object, an equation or a footnote reference is counted
    by the export and dropped by the API, and a suggested insertion is read
    by the API and skipped by the export. Either one shifts the count
    silently, and the counts can still coincide (codex counterexample). So a
    single unreadable twin makes the whole ordinal untrusted, not just its
    own position.

    `first`/`last` confine the count to one tab's segment: on a multi-tab
    document the export has no tab boundaries, and counting across all of
    them would compare against an API side that only has the target tab.
    """
    rows = sp.get("docx_paragraphs") or []
    idx, text, path = sp.get("para_index"), sp.get("para_text"), sp.get("path")
    if idx is None or text is None:
        return None, None, False
    ordinal = total = 0
    trusted = True
    for i, (top, ptext, ppath, readable) in enumerate(rows):
        if ptext != text or ppath != path:
            continue
        if first is not None and not (first <= top <= last):
            continue
        if not readable:
            trusted = False
        if i < idx:
            ordinal += 1
        total += 1
    return ordinal, total, trusted


def _confine_spans_to_segment(spans, first, last):
    """Split spans into the target tab's own and the rest.

    Returns (inside, outside, why). A span with one end inside the segment
    and the other outside is not placed and not dismissed: it is a refusal,
    because an anchor that crosses a tab boundary is territory nobody has
    measured.

    «Did not match here» is NEVER a reason to call a span foreign — that is
    how a live thread in the TARGET tab would be dropped over a soft break
    (#27). Foreign is decided by position in the proven segment, positively.
    """
    inside, outside = [], []
    for sp in spans:
        tops = (sp.get("top"), sp.get("end_top"))
        if any(t is None for t in tops):
            return None, None, (f"спан {sp.get('docx_id')} не привязан ни к "
                                f"одному элементу тела — границу вкладки для "
                                f"него доказать нечем")
        # Three positions, not two. «Обе не внутри» недостаточно: спан,
        # начавшийся ДО отрезка и кончившийся ПОСЛЕ него, охватывает целевую
        # вкладку целиком, а по признаку «не внутри» выглядел бы чужим и
        # остался бы без защиты (найдено тестом).
        sides = [(-1 if t < first else (0 if t <= last else 1)) for t in tops]
        if sides == [0, 0]:
            # Recomputed against THIS tab's segment: the roll covers the whole
            # export, and the API side the mapper compares to holds only the
            # target tab (codex P2).
            (sp["twin_ordinal"], sp["twin_total"],
             sp["twin_trusted"]) = _twin_position(sp, first, last)
            inside.append(sp)
        elif sides[0] == sides[1]:
            outside.append(sp)
        else:
            return None, None, (
                f"комментарий {sp.get('docx_id')} растянут через границу "
                f"вкладки — такое размещение не замерено (fail closed)")
    return inside, outside, None


def _localize_spans(spans, outline, first, last, docx_lattice):
    """Re-address the target tab's spans to the segment.

    A span's path names a table by its ordinal in the whole body, and its
    lattice is the whole document's — both count the tables of every tab
    against the API's count for one (measured, M19-19). Returns
    (spans, None) or (None, why); a span in a table the segment does not
    contain is a refusal, not a silent drop.
    """
    lattice, remap = _segment_lattice(outline, first, last, docx_lattice)
    out = []
    for sp in spans:
        sp = dict(sp)
        sp["path"] = remap(sp["path"])
        sp["end_path"] = remap(sp["end_path"])
        if sp["path"] is None or sp["end_path"] is None:
            return None, (f"якорь {sp.get('docx_id')} лежит в таблице, "
                          f"которой нет в доказанном отрезке вкладки "
                          f"(fail closed)")
        sp["docx_lattice"] = lattice
        out.append(sp)
    return out, None


def _segment_lattice(outline, first, last, docx_lattice):
    """Re-number the export lattice to the segment, or drop what is outside.

    Without this the export counts the tables of ALL tabs against the API's
    count for one (measured, M19-19), and every anchor in a cell is refused
    with a reason that names a disagreement nobody has.
    """
    tmap = {}
    for i in range(first, last + 1):
        el = outline[i]
        if el["kind"] == "tbl":
            tmap[el["t_ord"]] = len(tmap)

    def remap(path):
        if not path:
            return ()
        (t, j, col), rest = path[0], path[1:]
        if t not in tmap:
            return None
        return ((tmap[t], j, col),) + rest

    out = {"tables": {}, "rows": {}, "table_rows": {}, "paras": {}}
    out["tables"][()] = len(tmap)
    for key in ("tables", "paras"):
        for path, v in docx_lattice.get(key, {}).items():
            if not path:
                continue
            moved = remap(path)
            if moved is not None:
                out[key][moved] = v
    for (path, t), v in docx_lattice.get("table_rows", {}).items():
        moved = remap(path + ((t, 0, 0),))
        if moved is not None:
            out["table_rows"][(moved[:-1], moved[-1][0])] = v
    for (path, t, j), v in docx_lattice.get("rows", {}).items():
        moved = remap(path + ((t, j, 0),))
        if moved is not None:
            out["rows"][(moved[:-1], moved[-1][0], j)] = v
    return out, remap


def _pieces_fit(pieces, text):
    """Do these known textRun fragments occur inside `text`, in order?

    Piecewise, not as one substring: a paragraph «До ИТОГО После» with a chip
    in the middle arrives as fragments split around the part we cannot read.

    Both sides spell an in-paragraph break the same character now (#27), so
    the fragments are compared as they come. This used to normalize `\\v` to
    `\\n` — a soft break was spelled differently on the two sides, and a
    fragment carrying one fitted nothing, which made an unreadable paragraph
    holding a soft break stop counting as a possible home for an anchor.
    """
    pos = 0
    for piece in pieces:
        if not piece:
            continue
        at = text.find(piece, pos)
        if at < 0:
            return False
        pos = at + len(piece)
    return True


def _api_lattice(doc_tab):
    """Read the API side of the document as domains addressed by path.

    Returns a dict with, keyed the same way `_parse_docx_anchor_spans` keys
    the export side:

      paras     {path: [(start, end, text, pieces, could_host)]} — paragraphs
                of that domain in order; `text` is None for a paragraph whose
                text cannot be read (a smart chip, a rich link);
      cells     {path: (start, end)} — the cell's own extent, straight from
                the API (M16: cells, rows and tables all carry their own
                indices, and there are no gaps between neighbours);
      tables    {(container_path, ordinal): (start, end)};
      n_tables  {container_path: how many tables it holds};
      n_cells   {(container_path, ordinal, row): how many grid squares};
      problems  paragraphs whose indices are unusable — a fence needs both
                ends, and silently dropping one is how an anchor ends up
                outside its own fence.

    The grid column is the API's own cell ordinal, because the API keeps one
    entry per grid square. The export does not, which is what `_docx_grid_span`
    exists to reconcile.
    """
    out = {"paras": {}, "cells": {}, "tables": {}, "n_tables": {},
           "n_cells": {}, "n_rows": {}, "problems": []}

    def walk(content, path):
        t_ord = 0
        out["paras"].setdefault(path, [])
        for el in content or []:
            if "table" in el:
                ts, te = el.get("startIndex"), el.get("endIndex")
                if isinstance(ts, int) and isinstance(te, int) and ts < te:
                    out["tables"][(path, t_ord)] = (ts, te)
                rows = el["table"].get("tableRows", []) or []
                out["n_rows"][(path, t_ord)] = len(rows)
                for j, row in enumerate(rows):
                    cells = row.get("tableCells", []) or []
                    out["n_cells"][(path, t_ord, j)] = len(cells)
                    for k, cell in enumerate(cells):
                        sub = path + ((t_ord, j, k),)
                        cs, ce = cell.get("startIndex"), cell.get("endIndex")
                        if (isinstance(cs, int) and isinstance(ce, int)
                                and cs < ce):
                            out["cells"][sub] = (cs, ce)
                        walk(cell.get("content", []), sub)
                t_ord += 1
                continue
            para = el.get("paragraph")
            if not para:
                continue
            start, end = el.get("startIndex"), el.get("endIndex")
            if (not isinstance(start, int) or not isinstance(end, int)
                    or start >= end):
                out["problems"].append(
                    f"paragraph with unusable indices {start!r}..{end!r} — the "
                    f"anchor map cannot be trusted (fail closed)")
                continue
            bucket = out["paras"].setdefault(path, [])
            text, pieces, opaque = _api_para_read(para)
            if text is None:
                # `could_host` asks whether an anchor could HIDE in here, and
                # only the kinds proven unable to hide one are subtracted. A
                # break we could not spell is not one of them.
                bucket.append((start, end, None, pieces,
                               bool(opaque - _QUIET_ELEMENTS)))
                continue
            bucket.append((start, end, text, (), False))
        out["n_tables"][path] = t_ord

    walk((doc_tab.get("body", {}) or {}).get("content", []), ())
    return out


def _resolve_cell(path, lattice, docx_lattice):
    """Locate a cell path on the API side, or say why it cannot be trusted.

    Returns (extent, problem): the cell's (start, end) when the path resolves
    and the two sides provably describe the SAME cell, otherwise (None, why).

    Every level of the path is checked, so a confirmed cell confirms all its
    ancestors too. The checks are cumulative and each one exists for a reason
    the previous cannot cover: the number of tables in a container, the number
    of rows in a table, the number of grid squares in a row (export: the sum
    of `gridSpan`; API: the length of `tableCells`), and finally the cell's
    own paragraph texts.

    There is deliberately NO half-way answer like «somewhere in this table».
    A table is identified by its ordinal, and an ordinal is exactly what a
    disagreement calls into question — fencing the table an unproven path
    points at would put the fence next to the anchor rather than around it
    (code review r11). Either the cell is proven, or nothing here is.
    """
    prefix = ()
    for depth, (t_ord, row, col) in enumerate(path):
        n_api = lattice["n_tables"].get(prefix)
        n_docx = (docx_lattice or {}).get("tables", {}).get(prefix)
        if n_api is None or n_docx is None or n_api != n_docx:
            return None, (
                f"the export and the API disagree about how many tables sit "
                f"in {prefix or 'the document body'} ({n_docx} against "
                f"{n_api})")
        extent = lattice["tables"].get((prefix, t_ord))
        if extent is None:
            return None, (
                f"the export puts the anchor in table {t_ord} of "
                f"{prefix or 'the body'}, and the API has no such table")
        rows_api = lattice["n_rows"].get((prefix, t_ord))
        rows_docx = (docx_lattice or {}).get("table_rows", {}).get(
            (prefix, t_ord))
        if rows_api is None or rows_docx is None or rows_api != rows_docx:
            return None, (
                f"the export and the API disagree about how many rows table "
                f"{t_ord} has ({rows_docx} against {rows_api})")
        cells_api = lattice["n_cells"].get((prefix, t_ord, row))
        cells_docx = (docx_lattice or {}).get("rows", {}).get(
            (prefix, t_ord, row))
        if cells_api is None or cells_docx is None or cells_api != cells_docx:
            return None, (
                f"the export and the API disagree about row {row} of table "
                f"{t_ord}: {cells_docx} grid columns against {cells_api}")
        prefix = prefix + ((t_ord, row, col),)
        cell = lattice["cells"].get(prefix)
        if cell is None:
            return None, (
                f"grid column {col} of row {row} in table {t_ord} is not "
                f"readable from the API")
        # Shape agreeing is not the same as the two sides describing the SAME
        # cell. Two tables of identical shape, ordered differently by the two
        # sides, would pass every count above and hand back a cell that only
        # looks right — and then a fence would sit on the wrong cell while the
        # real anchor stayed editable. So the contents are compared too.
        #
        # Compared as they come: since #27 both sides spell an in-paragraph
        # break the same character, so a cell whose paragraph holds a soft
        # break is provable like any other — and a cell holding a PAGE break
        # is no longer confused with it.
        paras_api = lattice["paras"].get(prefix, ())
        paras_docx = (docx_lattice or {}).get("paras", {}).get(prefix)
        if paras_docx is None or len(paras_docx) != len(paras_api):
            return None, (
                f"the export and the API disagree about the contents of grid "
                f"column {col} of row {row} in table {t_ord} "
                f"({paras_docx if paras_docx is None else len(paras_docx)} "
                f"paragraphs against {len(paras_api)})")
        for d_text, (_st, _en, a_text, _pieces, _host) in zip(paras_docx,
                                                              paras_api):
            if a_text is None:
                # A paragraph the API will not spell out (a smart chip) cannot
                # confirm anything, and treating it as a wildcard was a hole:
                # a cell holding one would «match» any cell of the same shape,
                # which is exactly how a swapped pair of tables would slip
                # through (code review r11). Unproven, then.
                return None, (
                    f"grid column {col} of row {row} in table {t_ord} holds a "
                    f"paragraph skrepka cannot read, so it cannot be told "
                    f"apart from a cell of the same shape")
            if a_text != d_text:
                return None, (
                    f"the export and the API read grid column {col} of row "
                    f"{row} in table {t_ord} differently ({d_text[:30]!r} "
                    f"against {a_text[:30]!r}) — this is not the same cell")
        if depth == len(path) - 1:
            return cell, None
    return None, "empty cell path"


def _common_container(a, b, lattice, docx_lattice):
    """The innermost structure that provably holds both ends of a span.

    Returns (extent, why_not). Two paths into the same table diverge at the
    triple naming row and column, so their common ancestor is that table —
    NOT the empty prefix, which would mean the body and bound nothing. When
    one path is a prefix of the other (a cell and a nested cell inside it, or
    the body and anything) the ancestor is the shorter one, and the body is no
    bound at all.

    Both ends are put through `_resolve_cell` FIRST. Without that this
    function would happily name an ancestor computed from a path the two
    sides read differently — the same defect the placement path had, arrived
    at from the other side (code review r11). Confirming an end confirms every
    level above it, so after this the ancestor needs no further proof.
    """
    for end in (a, b):
        if not end:
            continue
        _extent, why_not = _resolve_cell(end, lattice, docx_lattice)
        if why_not:
            return None, why_not
    prefix = ()
    for x, y in zip(a, b):
        if x == y:
            prefix = prefix + (x,)
            continue
        if x[0] != y[0]:
            # Different tables at this level. They still share whatever holds
            # BOTH tables — an outer cell, when the divergence happens inside
            # one. Only at the top level is that the body, which bounds
            # nothing (found in code review: two nested tables in one outer
            # cell used to fail closed on the whole document).
            if not prefix:
                return None, "the two ends sit in different tables of the body"
            outer = lattice["cells"].get(prefix)
            return outer, (None if outer else
                           "the enclosing cell is not readable from the API")
        n_api = lattice["n_tables"].get(prefix)
        n_docx = (docx_lattice or {}).get("tables", {}).get(prefix)
        if n_api is None or n_docx is None or n_api != n_docx:
            return None, (f"the export and the API disagree about how many "
                          f"tables sit in {prefix or 'the document body'}")
        extent = lattice["tables"].get((prefix, x[0]))
        if extent is None:
            return None, f"the API has no table {x[0]} there"
        return extent, None
    shorter = prefix
    if not shorter:
        return None, "the two ends share nothing but the document body"
    return lattice["cells"].get(shorter), (
        None if lattice["cells"].get(shorter) else
        "the enclosing cell is not readable from the API")


# Control pictures: the refusal has to SHOW the difference, not hint at it.
# A refusal that printed «matched 0 times» next to a paragraph whose only
# oddity was invisible sent a live session looking for ghost threads that were
# never there — two rounds and one false accusation of the version
# (postmortem 2026-08-20, ask 2).
_CONTROL_PICTURES = {"\v": "␋", "\f": "␌", "\t": "␉", "\n": "␊", "\r": "␍",
                     "\x00": "␀"}


def _visible_controls(text):
    """Text with its control characters shown, for a refusal a person reads."""
    if text is None:
        return ""
    for raw, shown in _CONTROL_PICTURES.items():
        text = text.replace(raw, shown)
    return text


def _closest_para(ptext, by_text_here):
    """The document paragraph that differs from this one only in a break.

    Deliberately not a fuzzy match. A «closest» paragraph found by similarity
    is a guess, and on the documents this tool is for — CRM deliverables, a
    quarter of whose paragraphs repeat word for word — the guess would often
    name somebody else's twin and print it in the refusal (codex code r1).
    «The same characters apart from how a break is spelled» is not a guess:
    it is the diagnosis itself, and the only case worth showing.
    """
    if not by_text_here:
        return None
    flat = re.sub(f"[{_BREAK_CHARS}]", "\uffff", ptext)
    for candidate in by_text_here:
        if (candidate != ptext
                and re.sub(f"[{_BREAK_CHARS}]", "\uffff", candidate) == flat):
            return candidate
    return None


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
       matches and the whole document fails closed.

    Case 3 is what #27 changed, so here is what is left in it. It used to be
    reachable through an ordinary soft line break: the parser wrote every
    `w:br` as \\n, and an API paragraph can never hold \\n, so one Shift+Enter
    in a commented paragraph closed the document to replaces — measured live,
    twice. Both sides now spell an in-paragraph break the same character, so
    a soft break and a page break both MATCH instead. What still reaches case
    3: a break kind nobody measured (a column break, `w:cr`), a break element
    whose width is not one unit, an element the export cannot read at all —
    and, as ever, the two sides genuinely describing different text.

    Cases 1 and 2 are unchanged, but the fence proof needs one addition,
    because case 3 no longer covers what it used to. A paragraph carrying a
    break used to match NOTHING; now it can match exactly one readable
    paragraph while an unreadable one (a smart chip) could be its real home.
    At exactly one match this function otherwise trusts the match without
    looking at opaque paragraphs — hole #30, older than #26 and left open
    because closing it would freeze documents that work today. Documents with
    a break in a commented paragraph do NOT work today: they are refused. So
    for those, and only those, the opaque check runs at one match too — the
    same argument the cross-paragraph branch (#45) makes about being new.
    """
    lattice = _api_lattice(doc_tab)
    problems = list(lattice["problems"])

    # indexed once, not rescanned per span: a long document with many threads
    # would otherwise walk every paragraph for every anchor. Indexed PER
    # DOMAIN: a paragraph in a cell is not a candidate for an anchor in the
    # body, however identical the text (#48).
    by_text, possible_hosts = {}, {}
    for path, paras in lattice["paras"].items():
        for st, en, text, pieces, could_host in paras:
            if text is None:
                if could_host:
                    possible_hosts.setdefault(path, []).append(
                        (st, en, pieces))
            else:
                by_text.setdefault(path, {}).setdefault(
                    text, []).append((st, en))

    def _fence_every_table(s, why):
        """The anchor is in SOME table and we cannot say which — fence them all.

        This is r8's answer, and the point of keeping it is that it needs no
        path to be right: the export says the marker is inside a table, every
        table is fenced, so nothing an edit does can reach it. A global refusal
        would be worse than the behaviour this release replaces, and fencing
        the ONE table an unproven path points at would be worse still — that
        ordinal is the very thing in doubt.
        """
        tables = _table_intervals(doc_tab)
        if not tables:
            problems.append(
                f"anchor {s['docx_id']} sits in a table cell that cannot be "
                f"located ({why}), and the API shows no tables at all — the "
                f"two views disagree about the document (fail closed)")
            return
        problems.append(_LocalizedProblem(
            f"anchor {s['docx_id']} sits in a table cell that cannot be "
            f"located ({why}), so every table is refused and the rest of the "
            f"document stays editable",
            ranges=tuple((ts, te) for ts, te, _label in tables),
            docx_id=s["docx_id"]))

    def _fence_at(s, where, why):
        """Confine a span we could not place to the structure that holds it.

        The anchor is provably inside `where` — the export says which cell,
        and M16 proved the path names the same cell on both sides — so the
        fence protects the thread while the rest of the document, other cells
        of the same table included, stays editable.
        """
        if where is None:
            # unreachable today — every caller resolves the cell first — but a
            # fence with a None range would be a fence with a hole in it, and
            # this whole design leans on fences having none
            problems.append(
                f"anchor {s['docx_id']} sits in a table cell and {why}, and "
                f"the cell has no readable extent (fail closed)")
            return
        problems.append(_LocalizedProblem(
            f"anchor {s['docx_id']} sits in a table cell and {why}",
            ranges=(where,), docx_id=s["docx_id"]))

    ranges, ambiguous = [], []
    for s in spans:
        ptext = s.get("para_text")
        domain = s.get("path") or ()
        end_domain = s.get("end_path", domain) or ()
        in_cell = bool(domain)
        if domain != end_domain:
            # A span whose ends live in different containers is NOT placed.
            # Placing it exactly would need a measurement nobody has: `patch`
            # deliberately allows an operation lying INSIDE a healthy anchor,
            # so an exact cross-cell range would let the text of an
            # intermediate cell be deleted while both ends survive untouched.
            # Confine it to the common ancestor instead; exact cross-cell
            # placement is its own issue with its own acceptance scenario.
            extent, why_not = _common_container(
                domain, end_domain, lattice, s.get("docx_lattice"))
            if extent is None:
                if domain and end_domain:
                    # both ends are in tables, so every table together still
                    # bounds the anchor even when neither end is proven
                    _fence_every_table(s, why_not)
                    continue
                problems.append(
                    f"anchor {s['docx_id']} runs between two containers and "
                    f"{why_not} — nothing bounds it (fail closed)")
                continue
            problems.append(_LocalizedProblem(
                f"anchor {s['docx_id']} has its two ends in different cells, "
                f"so it is fenced by the table that holds both instead of "
                f"being placed",
                ranges=(extent,), docx_id=s["docx_id"]))
            continue
        cell_extent = None
        if in_cell:
            # BEFORE anything is matched, not only when matching fails: the
            # path decides WHICH cell's paragraphs are candidates, so a path
            # the two sides read differently would quietly search the wrong
            # cell and place the anchor there. Found by mutation testing —
            # the check existed but only ran on the failure path.
            cell_extent, why_not = _resolve_cell(
                domain, lattice, s.get("docx_lattice"))
            if cell_extent is None:
                _fence_every_table(s, why_not)
                continue
        if s.get("unreadable"):
            # The parser could not trust this span's text or offsets, and said
            # so with a reason. In a cell that is a local defect: the cell is
            # known, so it is fenced and the document keeps working.
            _fence_at(s, cell_extent, s["unreadable"])
            continue
        by_text_here = by_text.get(domain, {})
        hosts_here = possible_hosts.get(domain, [])
        if ptext is None:
            problems.append(
                f"anchor {s['docx_id']} has no paragraph text — the export "
                f"could not be read (fail closed)")
            continue
        exact = by_text_here.get(ptext, [])
        if s.get("end_para_index", s["para_index"]) != s["para_index"]:
            # An anchor dragged across a paragraph break. Its two ends are
            # located INDEPENDENTLY, each by its own paragraph's text, and API
            # indices are absolute — so whatever lies between them (a table, a
            # chip, anything the walker skips) cannot move either end. This is
            # the anchor's exact extent, not a conservative envelope.
            etext = s.get("end_para_text")
            end_exact = by_text_here.get(etext, []) if etext is not None else []
            # Unlike the single-match branch below, opaque paragraphs ARE
            # checked here. That branch keeps a known hole (#30) only because
            # it must not freeze documents that work today; a crossing anchor
            # freezes its document today anyway, so this surface is new and
            # gets to be strict. Found in review: a chip paragraph could be
            # the real home of either end, and placing the anchor on the
            # readable twin leaves the live thread unprotected.
            # `hosts_here` is always empty for a cell: a cell holding a
            # paragraph the API will not spell out fails `_resolve_cell` and
            # never reaches this loop at all. So this branch is the body's.
            hosts = [st for st, _en, pieces in hosts_here
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
                if in_cell:
                    _fence_at(s, cell_extent,
                              "spans paragraphs and one of its ends matches "
                              "nothing in that cell")
                    continue
                problems.append(
                    f"anchor {s['docx_id']} spans paragraphs and one of its "
                    f"ends matched 0 times in the doc (need exactly 1): "
                    f"{_visible_controls(ptext if not exact else etext)[:50]!r}"
                    f" — the two sides read that paragraph differently, and "
                    f"that is not a ghost thread")
                continue
            if len(exact) > 1 or len(end_exact) > 1:
                # Fencing the pairs of candidates is combinatorial and buys
                # nothing today: such a document is refused as it is now, so
                # nothing regresses by leaving it refused.
                if in_cell:
                    _fence_at(s, cell_extent,
                              "spans paragraphs and one of its ends repeats "
                              "inside that cell word for word")
                    continue
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
            if in_cell:
                # A cell paragraph the API reports with different text. Since
                # #27 a soft break is no longer one of the ways to get here —
                # both sides spell it the same. What is left is a break kind
                # nobody measured, or the two sides genuinely reading the cell
                # differently. In the body that costs the whole document; here
                # it costs the cell.
                _fence_at(s, cell_extent, "its paragraph matches nothing in that cell")
                continue
            # The refusal has one job beyond saying no: name the true reason.
            # This one used to read «matched 0 times» and hand out advice
            # about ghost threads, so a live session spent two rounds looking
            # for ghosts that were not there while the difference — one
            # invisible character — was printed on the screen the whole time
            # (postmortem 2026-08-20, ask 2).
            near = _closest_para(ptext, by_text_here)
            problems.append(
                f"anchor {s['docx_id']} sits in a paragraph the document and "
                f"the export read differently, so it cannot be located "
                f"(fail closed). The export reads "
                f"{_visible_controls(ptext)[:70]!r}"
                + (f", the closest the document has is "
                   f"{_visible_controls(near)[:70]!r}" if near else
                   ", and no paragraph of the document is close to it")
                + ". This is not a ghost thread — the thread is in the "
                  "export; the two READINGS of one paragraph disagree.")
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
            if in_cell:
                # No candidate-by-candidate fence is needed here: the anchor
                # is in THIS cell whichever copy holds it, so fencing the cell
                # is already the tightest honest answer.
                _fence_at(
                    s, cell_extent,
                    f"its paragraph has {len(exact)} identical copies inside "
                    f"that cell")
                continue
            hosts = [st for st, _en, pieces in hosts_here
                     if _pieces_fit(pieces, ptext)]
            if hosts:
                problems.append(
                    f"anchor {s['docx_id']} matches {len(exact)} paragraphs, "
                    f"and a paragraph whose text skrepka cannot read (a smart "
                    f"chip or a rich link at {hosts[:3]}) could be its home "
                    f"too — undecidable (fail closed): "
                    f"{_visible_controls(ptext)[:50]!r}")
                continue
            # The copies are identical to the eye and to a text search, but
            # not to their POSITION. The export lists paragraphs in document
            # order, so the anchor's own copy number picks the right one —
            # provided both sides agree on how many copies there are. If they
            # disagree the two readings describe different documents and the
            # fence stands, exactly as before.
            if "twin_ordinal" in s:
                ordinal, total = s["twin_ordinal"], s["twin_total"]
                trusted = s.get("twin_trusted", False)
            else:
                ordinal, total, trusted = _twin_position(s)
            # Three conditions, and each closes its own way of being wrong:
            # `trusted` — every twin in the export is fully readable, so the
            # two walkers saw the same paragraphs; `not hosts_here` — no
            # paragraph of this domain is opaque to the API for the mirror
            # reason; equal totals — both sides ended up with the same number
            # of copies. Two of the three were not enough: an inline image on
            # one twin and a suggested insertion on another cancel out in the
            # count while the order moves (codex counterexample).
            if (ordinal is not None and trusted and not hosts_here
                    and total == len(exact)
                    and 0 <= ordinal < len(exact)):
                base = exact[ordinal][0]
                ranges.append((base + s["start_off"], base + s["end_off"],
                               s.get("anchor_text", ""), s["docx_id"]))
                continue
            ambiguous.append({
                "docx_id": s["docx_id"], "para_text": ptext,
                "start_off": s["start_off"], "end_off": s["end_off"],
                "candidates": exact,
            })
            continue
        # The hole #30 leaves at a single match stays open in the body — for
        # the same reason as ever: closing it would freeze documents that work
        # today. Inside a cell it is closed, and earlier than here: a cell
        # holding a paragraph the API will not spell out cannot be told apart
        # from any other cell of the same shape, so `_resolve_cell` refuses it
        # before a candidate is ever looked at.
        #
        # With ONE exception, and it is the surface #27 opened. A paragraph
        # carrying an in-paragraph break matched nothing before this release —
        # such a document was refused outright — so nothing that works today
        # passes through here, and the strict check the several-candidates
        # branch makes can be made here too: an opaque paragraph whose known
        # fragments fit this text could be the anchor's real home, and placing
        # it on the readable twin would leave the live thread unprotected.
        if any(c in ptext for c in _BREAK_CHARS):
            hosts = [st for st, _en, pieces in hosts_here
                     if _pieces_fit(pieces, ptext)]
            if hosts:
                problems.append(
                    f"anchor {s['docx_id']} sits in a paragraph with a line "
                    f"break, and a paragraph skrepka cannot read (a smart "
                    f"chip or a rich link at {hosts[:3]}) could be its home "
                    f"too — undecidable (fail closed): "
                    f"{_visible_controls(ptext)[:50]!r}")
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


def _ghost_verdict(c, records, doc_tab, file_id=None, other_tabs=(),
                   freshness_floor=None):
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
    created = (freshness_floor if freshness_floor is not None
               else _rfc3339_epoch(c.get("createdTime")))
    if created is None:
        return None
    later = []
    for r in records:
        stamp = _rfc3339_epoch(r.get("date_sec"))
        # `is not None`, not `or 0`: a record whose date cannot be read must
        # be visibly not-later, not silently zero (found in review)
        if stamp is not None and stamp > created:
            later.append(r)
    quote = (c.get("quotedFileContent") or {}).get("value")
    if not quote:
        # No quoted text at all, and no record in the export. Measured
        # 2026-08-16: a comment created through the API never attaches to
        # document text — but Drive stores the `anchor` field it was given
        # verbatim, and skrepka counts anything carrying `anchor` as anchored.
        # Such a thread has no text anchor to lose, yet it used to freeze
        # every replace in the document, which is what any other tool leaving
        # comments through the API does to a document today.
        #
        # A genuinely text-anchored comment always carries the quote (it is
        # filled at creation and survives losing the anchor — the living ghost
        # of 2026-08-09 still had it). One anchored to an IMAGE might not —
        # and that was the whole doubt, because an image anchor is real. It is
        # measured now (2026-08-16, #46): a comment on a picture carries no
        # quote and DOES leave a record in the export, markers and all, so it
        # never reaches this branch at all.
        #
        # Sign 1 is waived here for EXACTLY the shape that needed it waived:
        # an export with no records at all. That is what a document whose
        # comments were all left through the API looks like — no record can
        # ever be «later», the sign stays silent forever, and every replace is
        # refused for threads that have nothing to lose (#46).
        #
        # When the export DOES carry records, the sign costs nothing and stays
        # on. Narrower than the first version of this fix, and deliberately:
        # a quote can in principle be missing for a reason nobody has measured,
        # and where there is a cheap second opinion it is worth keeping
        # (code review r11).
        if records and not later:
            return None
        return {"id": c.get("id"),
                "link": _thread_link(file_id, c.get("id")),
                "quote": None,
                "fenced": []}
    if not later:
        return None
    # The quote may simply live in ANOTHER TAB. Sign 2 asks whether the text
    # is gone from the DOCUMENT, and reading it in the target tab alone makes
    # every thread of every neighbouring tab look dead: the quote is not
    # there, the fence comes out empty, and the person is told their comment
    # vanished while its protection is quietly dropped (r12, round 2).
    for other in other_tabs:
        if (_count_quote_occurrences(other, quote)
                or _count_text_in_aux_segments(other, quote)):
            return None
    if _count_text_in_aux_segments(doc_tab, quote):
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
                               doc_tab=None, other_tabs=()):
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
    in_other_tabs = 0
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
            verdict = _ghost_verdict(c, records, doc_tab, file_id,
                                     other_tabs=other_tabs)
            if verdict is not None:
                # Provably gone: it has no anchor left to protect, so the
                # document is not held hostage to it (#34). Named in the
                # receipt — removing someone's comment is the person's call
                # (CONTRACT §2.2), never ours.
                ghosts.append(verdict)
                continue
            # A thread missing from the export, whose text is not in the
            # target tab at all but IS in another one. Whatever happened to
            # its record, an edit confined to this tab cannot reach it, so it
            # is neither a ghost (nobody is told their comment vanished) nor a
            # reason to refuse (codex, final round). The quote must be absent
            # from THIS tab entirely — if it stands here, the anchor may be
            # here too, and then the refusal is the honest answer.
            quote = (c.get("quotedFileContent") or {}).get("value")
            if quote and other_tabs and not _count_quote_in_tab(
                    doc_tab, quote):
                if any(_count_quote_occurrences(o, quote)
                       or _count_text_in_aux_segments(o, quote)
                       for o in other_tabs):
                    in_other_tabs += 1
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
            path = ((marker_census or {}).get("paths") or {}).get(rid)
            problems.append(_HiddenMarkerProblem(
                f"comments.xml entry {rid} anchors inside a table — its exact "
                f"position is not readable from the export",
                in_tables=(rid,),
                cell_paths={rid: path} if path else None,
                docx_lattice=(marker_census or {}).get("docx_lattice")))
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
        # threads whose text lives in another tab entirely: an edit
        # confined to this tab cannot reach them
        "threads_in_other_tabs": in_other_tabs,
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
        own = getattr(p, "ranges", ())
        if own:
            # A problem that carries its own coordinates. It exists because
            # the anchor could NOT be placed — so there is nothing in
            # `anchors` to look its position up by, and the range came from
            # the structure instead: the cell that holds it (#48).
            cid = (attribution or {}).get(getattr(p, "docx_id", None))
            link = _thread_link(file_id, cid)
            who = (f"треда {cid} {link}" if link
                   else f"docx id {getattr(p, 'docx_id', '?')}")
            for s, e in own:
                blocked.append((s, e, (
                    f"комментарий {who} стоит в этой ячейке таблицы, но его "
                    f"точное место в ней из выгрузки не читается ({p}), "
                    f"поэтому правки этой ячейки отклонены — остальной "
                    f"документ, включая другие ячейки, правится как обычно. "
                    f"Правьте эту ячейку в интерфейсе Google Docs — "
                    f"комментарий там не пострадает")))
            continue
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
    """Turn "the anchor is somewhere in a table" into blocked ranges.

    Since r11 the span parser walks table cells, so an ordinary comment in a
    cell never reaches here at all — it is placed like any other anchor. What
    is left for this function is a marker the walk still cannot process: one
    wrapped in `w:sdt`, in a tracked change, in a container nobody has taught
    the parser about.

    Such a marker is bounded all the same, and usually tighter than r8 could
    bound it: the census remembers the CELL it saw the marker in, so the fence
    is that cell. Only when the path is missing or untrustworthy does the
    fence fall back to every table in the file — which is where r8 left it.

    Returns (remaining_global_problems, blocked). A mismatch that is not fully
    explained by tables keeps blocking everything.
    """
    remaining, blocked = [], []
    tables = None
    lattice = None
    for p in global_problems:
        in_tables = getattr(p, "in_tables", frozenset())
        elsewhere = getattr(p, "elsewhere", frozenset())
        if not in_tables or elsewhere:
            remaining.append(p)
            continue
        cell_paths = getattr(p, "cell_paths", None) or {}
        if cell_paths and set(cell_paths) >= set(in_tables):
            if lattice is None:
                lattice = _api_lattice(doc_tab)
            # the SAME agreement gate a placement goes through: a path is
            # only worth as much as the two sides agreeing under it, and a
            # fence built on a path the API reads differently would sit on
            # the wrong cell while the real one stayed editable (code review)
            extents = [_resolve_cell(path, lattice,
                                     getattr(p, "docx_lattice", None))[0]
                       for path in cell_paths.values()]
            # `_table_intervals` below fences EVERY table in the file, which
            # is r8's answer and needs no path to be right. That is the only
            # fallback there is: a path the two sides read differently cannot
            # name a table any more reliably than it names a cell.
            if all(extents):
                # One fence per distinct cell: two markers hiding in the same
                # cell must not stack two identical intervals, which
                # `_blocked_hits` would then walk twice for every operation.
                for s, e in sorted(set(extents)):
                    blocked.append((s, e, (
                        f"ячейка таблицы, в которой стоит комментарий ({p}). "
                        f"Правьте её в интерфейсе Google Docs — комментарий "
                        f"там не пострадает")))
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


def _dedupe_blocked(blocked):
    """One fence per range, order preserved, distinct reasons kept.

    Two sources can describe the same cell — the parser's hidden-marker
    problem and the accounting's missing-span problem are the same marker seen
    twice — and every duplicate is walked again by `_blocked_hits` for every
    single operation and counted again in the receipt.

    Identical labels collapse to one. DIFFERENT labels do not: two distinct
    comments in one cell are two threads the person may need to look at, and
    dropping the second would make the refusal name one of them and hide the
    other (code review r11).
    """
    out, seen = [], {}
    for s, e, label in blocked:
        if (s, e) not in seen:
            seen[(s, e)] = [label]
            out.append((s, e))
            continue
        if label not in seen[(s, e)]:
            seen[(s, e)].append(label)
    return [(s, e, seen[(s, e)][0] if len(seen[(s, e)]) == 1
             else f"{seen[(s, e)][0]} (и ещё причин здесь: "
                  f"{len(seen[(s, e)]) - 1})")
            for s, e in out]


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
    if "shares every" in shown:
        # Замерено M27 на живом документе: два ответа ОДНОГО автора, ушедшие
        # в РАЗНЫЕ треды в одну и ту же секунду, делают эти треды
        # неразличимыми в выгрузке, и замены останавливаются во всём файле —
        # включая абзацы без комментариев. Совет про призраков, который
        # стоял здесь раньше, к этому случаю не относится вовсе: разбор
        # уходил в их поиск, а искать было нечего (пост-мортем 20 августа).
        return ("Ответы ушли в разные треды в одну и ту же секунду, и "
                "различить эти треды в выгрузке теперь нечем. Ответьте ещё "
                "раз в КАЖДЫЙ из названных выше тредов, по одному, дожидаясь "
                "смены секунды: своя секунда становится приметой треда, и "
                "одному треду чужая примета не помогает. Потом повторите "
                "команду. Отвечать пачкой без пауз нельзя — ответы снова "
                "попадут в одну секунду.")
    if "missing from the export" in shown:
        # A thread the export does not carry AND we could not prove harmless.
        # «Переоткрыть» is nonsense for it (it was never closed), and telling
        # the person to delete somebody's comment is not ours to say. What
        # actually helps: it is the newest thing in the document, so nothing
        # in the export dates later than it — one reply anywhere, or a minute
        # of waiting, and the next run has its bearings (#34, #46).
        return ("Комментарий, названный выше, есть в списке комментариев, но "
                "не доехал до выгрузки документа, а более свежих записей в "
                "ней нет — значит отличить «он потерял привязку» от «выгрузка "
                "просто старше него» пока нечем. Ответьте в любой другой тред "
                "или подождите минуту и повторите команду. Если этот "
                "комментарий оставлен не через интерфейс Google Docs, он к "
                "тексту не привязан вовсе — тогда его можно удалить, и он "
                "перестанет мешать.")
    if any(m in shown for m in ("no comments.xml entry", "unknown to the API",
                                "stale export", "without a w:id",
                                "anchor spans in document.xml")):
        # Две половины выгрузки разошлись: в тексте есть отметка комментария,
        # которой нет в списке, или наоборот. Почти всегда это гонка между
        # двумя чтениями одного документа, и лечится она повтором, а не
        # разбором комментариев — которых человек, скорее всего, не трогал.
        return ("Выгрузка документа пришла несогласованной: разметка "
                "комментариев в тексте и их список не сходятся. Обычно это "
                "гонка двух чтений — повторите команду через минуту.")
    # Раньше здесь стояло «разрулите комментарии-призраки в UI» — один совет
    # на все оставшиеся причины сразу. Он был верен для одной из них и
    # уводил в сторону на остальных, а просьба к редактору сделать
    # техническую работу запрещена продуктовой рамкой. Причина названа выше;
    # запасной вариант говорит, что известно, и не выдумывает выхода.
    return ("Что именно не сошлось — сказано выше. Вставки при этом "
            "работают: они ничего не удаляют.")


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


def _table_of_contents_intervals(doc_tab):
    """Structural intervals of every table of contents in the tab body.

    Google builds a table of contents from the headings and rebuilds it
    itself; its text is not something an editor writes, and none of the text
    walkers read it — `_extract_text_runs` knows only paragraphs and tables.
    So a quote can never resolve inside one, and a named range spanning one
    is already refused as non-contiguous.

    These intervals exist to keep that true by geometry rather than by luck:
    an address that does reach a table of contents is refused by place, and
    only that address. Until 0.18 the mere PRESENCE of a table of contents
    switched off rewriting a commented fragment in the whole document — a
    global refusal for a local reason, and the reason itself (a text search
    that reached where the uniqueness count could not look) went away with
    `replaceAllText`.
    """
    out = []
    for el in (doc_tab.get("body", {}) or {}).get("content", []):
        if "tableOfContents" not in el:
            continue
        s, e = el.get("startIndex"), el.get("endIndex")
        if isinstance(s, int) and isinstance(e, int) and s < e:
            out.append((s, e, "оглавление"))
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
                # The label goes LAST: it carries the remedy, sometimes
                # several sentences of it, and a protected-range aside
                # wedged into the middle buried the part the person acts on.
                return (
                    f"a sync edit would remove or rewrite protected text at "
                    f"[{ps}, {pe}) — that would destroy it (C1). Nothing was "
                    f"applied. Это {label}")
    return None


def _canary_delete_request(canary):
    """Delete the canary — in ITS OWN tab.

    The tab travels inside the canary because this request is prepended to
    the final batch as `extra_requests_before`, and that list used to bypass
    the loop that scoped requests: an unscoped delete removes a range of the
    FIRST tab (M19-9) — somebody else's text — while the canary stays where
    it is.
    """
    return _scope_requests([{"deleteContentRange": {"range": {
        "startIndex": canary["start"], "endIndex": canary["end"]}}}],
        canary.get("tab_id"))[0]


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
        # the canary's OWN tab: `tab_id=None` used to be passed here, and on a
        # multi-tab document `_select_tab` refuses — so cleanup could not run
        # at all, and every abort left the service line behind
        tid = canary.get("tab_id")
        _, doc_tab = _select_tab(doc, tab_id=tid)
        if _count_quote_occurrences(doc_tab, canary["text"]) != 1:
            return False
        s, e = _find_quote_in_doctab(doc_tab, canary["text"])
        # `s - 1` is meant to be the canary paragraph's own newline. Meant to
        # be — nobody checked. Between the insert and the cleanup a person can
        # merge that line, type in front of the canary or move it; then this
        # delete takes their character along with the service line. Checked
        # now: if the preceding character is not the newline, nothing is
        # deleted and the caller warns with the literal text instead (codex,
        # final round).
        if _extract_exact_text_range(doc_tab, s - 1, s) != "\n":
            return False
        docs_service.documents().batchUpdate(
            documentId=file_id,
            body={"requests": _scope_requests(
                      [{"deleteContentRange": {"range": {
                          "startIndex": s - 1, "endIndex": e}}}], tid),
                  "writeControl": _write_control(doc.get("revisionId"))},
        ).execute()
        return True
    except Exception:
        return False


def _canary_present(docs_service, file_id, canary):
    """Fresh-read probe: True/False, or None when the read itself failed."""
    try:
        doc = _safe_get_doc(docs_service, file_id)
        _, doc_tab = _select_tab(doc, tab_id=canary.get("tab_id"))
        return _count_quote_occurrences(doc_tab, canary["text"]) > 0
    except Exception:
        return None


def _fresh_anchor_snapshot(docs_service, drive_service, file_id, doc,
                           doc_tab, anchored, named_intervals, body_end,
                           *, fp1, universe, tid):
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
    # `body_end` is counted in the TARGET tab, so the insert must name that
    # tab: without it the request goes to the first tab (M19-9) — silently
    # when the index happens to fit there, and with a 400 when it does not.
    # The tab travels on in the canary itself: every later step (delete,
    # cleanup, presence probe) needs it and none of them sees `tid`.
    canary = {"text": canary_text,
              "start": body_end - 1,
              "end": body_end - 1 + _utf16_len(payload),
              "tab_id": tid}
    try:
        resp = docs_service.documents().batchUpdate(
            documentId=file_id,
            body={"requests": _scope_requests([{"insertText": {
                      "location": {"index": body_end - 1},
                      "text": payload}}], tid),
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
    def _abort(msg, reason=None, details=None):
        cleaned = _cleanup_canary(docs_service, file_id, canary)
        if not cleaned:
            msg += (f" ВНИМАНИЕ: в конце документа осталась служебная "
                    f"строка «{canary_text}» — удалите её вручную "
                    f"(данные не потеряны).")
        _error(msg, reason=reason, details=details)

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
        # The accounting keeps seeing ALL spans on purpose: `comments.xml` is
        # one per document and its w:id are document-wide (M19-16), so the
        # join «one record ⇔ one span» is documentary by construction and
        # multi-tab does not touch it. Only the MAPPING is confined to the
        # target tab.
        tabs_now = _collect_tabs(doc)
        acc_problems, metrics = _account_anchored_comments(
            anchored, records, spans, universe=universe, file_id=file_id,
            marker_census=census, doc_tab=doc_tab,
            other_tabs=[dt for t, _title, dt in tabs_now if t != tid])
        mapped_spans = spans
        foreign_ids = set()
        if len(tabs_now) > 1:
            first, last, why = _prove_target_segment(
                census["outline"], tabs_now, tid, canary_text=canary_text)
            if why:
                _abort(
                    f"границу целевой вкладки в выгрузке доказать не "
                    f"удалось: {why}. Пока она не доказана, разместить "
                    f"якорь нельзя — у вкладок свои индексные пространства, "
                    f"и промах молчалив. Правьте эту вкладку в интерфейсе "
                    f"Google Docs")
            mine, foreign, why = _confine_spans_to_segment(spans, first,
                                                            last)
            if why:
                _abort(f"комментарии и вкладки не сходятся: {why}")
            foreign_ids = {sp["docx_id"] for sp in foreign}
            mapped_spans, why = _localize_spans(
                mine, census["outline"], first, last,
                census["docx_lattice"])
            if why:
                _abort(why)
        anchors, map_problems, ambiguous = _map_anchors_to_doc(
            doc_tab, mapped_spans)
        attribution = _attribute_records_to_threads(anchored, records,
                                                    universe)
        # Сколько РАЗНЫХ физических мест занимает каждый тред во всём
        # документе. Считается по ПОЛНОМУ списку спанов, до сведения к
        # выбранной вкладке: тред с якорем здесь и вторым якорем в соседней
        # вкладке после сведения выглядит как тред с одним якорем, и
        # адресация по треду молча выбрала бы этот один (ревью codex).
        # Записи-дубли от ответов несут физически то же место и схлопываются
        # сами: ключ — положение, а не номер записи.
        places = {}
        for sp in spans:
            cid_of = attribution.get(sp["docx_id"])
            if not cid_of:
                continue
            places.setdefault(cid_of, set()).add((
                tuple(sp.get("path") or ()), sp["para_index"],
                sp["start_off"], sp["end_para_index"], sp["end_off"]))
        thread_places = {c: len(v) for c, v in places.items()}
        # An anchor that matches several identical paragraphs is not placed —
        # every place it could be is fenced instead, so the document stays
        # editable everywhere else (#26).
        amb_blocked, amb_problems = _fence_off_ambiguous(
            ambiguous, attribution=attribution, file_id=file_id)
        all_problems = (problems + rec_problems + acc_problems + map_problems
                        + amb_problems)
        if foreign_ids:
            # A problem about an anchor in ANOTHER tab is not this tab's
            # problem: the write cannot reach it. Confining the mapping was
            # not enough — the parser and the accounting report per-anchor
            # troubles too (a picture in the anchor's paragraph, an empty
            # anchor, a span with no comments.xml entry), and each of those
            # still aborted the whole document, which is exactly the
            # behaviour this round set out to remove (opus, final round).
            all_problems = [
                p for p in all_problems
                if not (set(getattr(p, "docx_ids", ()))
                        and set(getattr(p, "docx_ids", ())) <= foreign_ids)]
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
        # One interval per range. The same cell can be fenced twice — the
        # parser and the accounting report the same hidden marker separately —
        # and a duplicate costs a second walk in `_blocked_hits` for every
        # operation, a second candidate in `_narrow_replace`, and a receipt
        # that overcounts what was refused (code review r11).
        blocked = _dedupe_blocked(
            blocked + table_blocked + amb_blocked + ghost_blocked)
        if global_problems:
            shown = "; ".join(str(p) for p in global_problems[:4])
            _abort(
                "anchor accounting/mapping failed — paragraph "
                "replaces/deletes are blocked (fail closed): "
                + shown + ". " + _anchor_map_remedy(shown),
                reason=("anchor_identity_collision"
                        if "shares every" in shown else None),
                details={"problems": [str(p) for p in global_problems[:4]]})
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
    # Which tables to hand `sync` as indivisible. Only when a cell anchor was
    # actually placed: a document with no comments inside tables is untouched
    # by this, and one where nothing could be placed is already fenced.
    cell_tables = []
    if any(s.get("path") for s in spans):
        cell_tables = [(ts, te, (
            "таблица, в одной из ячеек которой стоит комментарий — `sync` "
            "правит таблицу только целиком, поэтому такая правка отклонена; "
            "точечную правку ячейки делает `patch`"))
            for ts, te, _label in _table_intervals(doc_tab)]
    return ({"anchors": anchors, "fp1": fp1, "canary": canary,
             "r1": canary["r1"], "metrics": metrics, "blocked": blocked,
             "attribution": attribution, "cell_anchor_tables": cell_tables,
             "thread_places": thread_places,
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
                f"Google Docs. Остальной документ правится. ({source})",
                reason="suggestion_overlap",
                details={"label": label, "range": [s, e]})


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
                f"Docs. Остальной документ правится. ({source})",
                reason="suggestion_overlap",
                details={"label": label, "range": [s, e]})


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
        if "comment_id" in op:
            return f"comment={op['comment_id']!r}"
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


_ANCHOR_EFFECTS = frozenset(("extended", "edited", "dropped", "rewritten"))


def _utf16_cut(text, at):
    """Разрезать `text` по смещению `at` в единицах UTF-16.

    Возвращает (голова, хвост) либо None, если разрез пришёлся внутрь
    суррогатной пары или за конец строки. Срез по code points здесь не
    годится: индексы Docs считаются в единицах UTF-16, и одна эмодзи — две
    единицы, то есть смещения в тексте якоря и в документе расходятся ровно
    там, где ошибка тише всего.
    """
    if at < 0:
        return None
    n = 0
    for i, ch in enumerate(text):
        if n == at:
            return text[:i], text[i:]
        n += 2 if ord(ch) > 0xFFFF else 1
    return (text, "") if n == at else None


def _anchor_effects(anchors, attribution, *, del_start, del_end, new_text,
                    rewritten_cid=None):
    """Что правка сделала с текстом под каждым задетым комментарием.

    Считается арифметикой по одному замеренному правилу (M28): **сначала
    схлопывается удаление, потом вставка судится по сжавшемуся якорю** — она
    входит в него тогда и только тогда, когда её точка оказалась СТРОГО
    внутри остатка. Этим правилом объясняются все пятнадцать геометрий M26 и
    M28 без исключений, поэтому второй выгрузки после записи не нужно.

    Задет тот якорь, чьё пересечение с удаляемым диапазоном непусто. Для
    чистой вставки (`del_start == del_end`) то же условие вырождается в
    «точка строго внутри», что и измерено: на обеих границах якорь остаётся
    дословно прежним.

    `quotedFileContent` здесь не участвует ни в одном поле: он показывает
    текст на момент, когда комментарий писали, и ровно на нём в 0.10
    построили автоматические записи в треды, которые говорили неправду и
    были удалены (#22).

    Возвращает (effects, affected_comment_ids).
    """
    delta = _utf16_len(new_text) - (del_end - del_start)
    effects, affected = [], []
    for a_s, a_e, a_text, a_id in anchors:
        if not (del_start < a_e and a_s < del_end):
            continue  # правка прошла мимо этого якоря
        cid = (attribution or {}).get(a_id)
        entry = {"comment_id": cid, "range_before": [a_s, a_e],
                 "text_before": a_text}
        if rewritten_cid is not None and cid == rewritten_cid:
            # M26-C: три запроса оставляют якорь ровно на вписанном тексте.
            entry.update(range_after=[del_start,
                                      del_start + _utf16_len(new_text)],
                         text_after=new_text, effect="rewritten")
        else:
            head_len = max(0, min(a_e, del_start) - a_s)
            tail_len = max(0, a_e - max(a_s, del_end))
            if not head_len and not tail_len:
                # Якорь накрыт целиком. Тред при этом жив — иначе правка была
                # бы отклонена как губительная (C1) — но ЭТОТ якорь исчез, и
                # разговор держится на другом своём якоре.
                entry.update(range_after=None, text_after=None,
                             effect="dropped")
            else:
                # Вставка попадает внутрь якоря только когда обе стороны
                # пережили удаление: иначе её точка оказывается ровно на
                # границе сжавшегося якоря, а граница не поглощает (M28).
                absorbed = bool(head_len and tail_len and new_text)
                entry.update(
                    range_after=[
                        a_s if head_len else del_start + _utf16_len(new_text),
                        a_e + delta if tail_len else del_start],
                    text_after=_anchor_text_after(
                        a_text, a_e - a_s, head_len, tail_len,
                        new_text if absorbed else ""),
                    # Ни один исходный символ не пропал — значит якорь только
                    # вырос. Сегодня сюда приходит лишь заякоренная вставка,
                    # а её путь карты не строит; ветка живёт ради того, чтобы
                    # правило M28 было записано целиком и проверялось.
                    effect=("extended"
                            if head_len + tail_len == a_e - a_s
                            else "edited"))
        effects.append(entry)
        if cid and cid not in affected:
            affected.append(cid)
    return effects, affected


def _anchor_text_after(a_text, span_len, head_len, tail_len, inserted):
    """Текст якоря после правки, или None, если посчитать его дословно нельзя.

    Дословно нельзя ровно тогда, когда текст якоря и его геометрия не
    сходятся — внутри якоря сидит инлайновый объект, — либо разрез пришёлся
    внутрь суррогатной пары. Назвать в этом случае приблизительный текст
    хуже, чем не назвать никакого: на приблизительном тексте и построили
    автоответы, удалённые в 0.10.
    """
    if _utf16_len(a_text) != span_len:
        return None
    head = _utf16_cut(a_text, head_len)
    tail = _utf16_cut(a_text, span_len - tail_len)
    if head is None or tail is None:
        return None
    return head[0] + inserted + tail[1]


def _effect_receipt(source, applied_as, *, start, end, old_text, new_text,
                    anchors=None, attribution=None, rewritten_cid=None,
                    unknown_ids=None, note=None, **extra):
    """Квитанция одной успешно применённой операции.

    Диапазоны и тексты здесь — фактически выполненные, а не заказанные:
    после сужения стоит сужённый диапазон, после перезаписи — вычисленный
    новый. Это предусловие любых автоматических ответов: в 0.10 их удалили
    (#22) не за саму идею, а за то, что они срабатывали по устаревшей цитате
    и называли текст, которого правка не касалась.
    """
    entry = {"source": source, "applied_as": applied_as,
             "range_before": [start, end],
             "range_after": [start, start + _utf16_len(new_text)],
             "text_before": old_text, "text_after": new_text}
    if anchors is None:
        # Путь без экспортной карты: вставка ничего не удаляет и потому карту
        # не строит (C5). Промолчать здесь нельзя — пустой список эффектов
        # прочитался бы как «ни один комментарий не задет», а это не то же
        # самое, что «мы не смотрели».
        entry["effects_basis"] = "not-mapped"
    else:
        effects, affected = _anchor_effects(
            anchors, attribution, del_start=start, del_end=end,
            new_text=new_text, rewritten_cid=rewritten_cid)
        # Закрытый тред в выгрузку не попадает вовсе — ни записи в
        # `comments.xml`, ни разметки в тексте (M13). Значит карта знает
        # только открытые треды, и правка могла пройти ровно по якорю
        # закрытого. Промолчать о нём — это то самое утверждение «не задет»,
        # которое запрещено: своё незнание надо называть (ревью codex).
        entry["effects_basis"] = ("export-map-open-threads-only"
                                  if unknown_ids else "export-map")
        entry["affected_comment_ids"] = affected
        entry["anchor_effects"] = effects
        if unknown_ids:
            entry["unknown_effect_comment_ids"] = list(unknown_ids)
    if note:
        entry["note"] = note
    entry.update(extra)
    return entry


def _narrow_replace(doc_tab, search_text, new_text, start, end,
                    anchors, blocked, attribution=None, interior_only=False):
    """Shrink a replace to the part that actually changes, so a thread lives.

    The BIGGEST safe cut wins, that is: the smallest edit. Until 0.18 it was
    the other way round — the smallest cut, keeping the core nearly the whole
    quote — and the reason was uniqueness: a search-based writer needed the
    core to occur once in the tab, and cutting the whole common affix
    collapsed it to a word that repeated. The index writer does not search,
    so that reason is gone, and keeping it would now actively harm: shaving
    one character off a replace that covers a whole commented paragraph
    leaves the thread alive but anchored to a single letter. An author who
    edits by hand changes what changed, not everything but one character.

    `interior_only` keeps only cuts that leave original text on BOTH sides.
    Such a cut puts the insertion strictly inside what is left of the anchor,
    and by M28 the anchor then absorbs it — so afterwards the comment covers
    exactly the new text. A cut that touches either border leaves the
    insertion on the boundary, where the anchor does NOT take it: the
    document reads right and the comment ends up on a fragment of what was
    asked for. `replace_anchor` promises the whole of it, so it asks for
    interior cuts only (ревью codex).

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
    # The whole common affix is a candidate too, and it is the one an author
    # would pick: change what changed. It was NOT in this set until 0.18,
    # because a search-based writer needed the core to stay long enough to be
    # unique. The index writer addresses a range, so the natural cut is
    # available again — and without it a replace covering a whole commented
    # paragraph gets shaved by one character and leaves the thread anchored
    # to a single letter.
    l_cand.add(l_max)
    r_cand.add(r_max)
    l_cand = sorted(c for c in l_cand if 0 <= c <= l_max)
    r_cand = sorted(c for c in r_cand if 0 <= c <= r_max)

    # descending: the largest cut is the smallest edit, and it leaves the
    # anchor the most of its own text
    for total in sorted({a + b for a in l_cand for b in r_cand},
                        reverse=True):
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
            if interior_only and not (start2 > start and end2 < end):
                continue
            if _doomed_threads(start2, end2, anchors, attribution):
                continue
            if _blocked_hits(start2, end2, blocked):
                continue
            # Uniqueness used to be required here because the write was a
            # text search. The index writer addresses the range directly, so
            # the honest check is the direct one: does that range still hold
            # the text we narrowed to. A document whose format REQUIRES
            # identical paragraphs used to lose narrowing entirely on this.
            if _extract_exact_text_range(doc_tab, start2, end2) != core:
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
        f"— the new text can only carry one of them, and choosing which is "
        f"not the program's call. Narrow the replacement to a uniformly "
        f"styled piece. ({source})")


# Control characters — a newline among them, it starts a new paragraph —
# and the Private Use Area, which Docs is known to strip. Any of these
# and the text that lands is not what the positions were computed for.
#
# U+000B is the exception, and it is cut out of the range by hand rather than
# by widening it: the soft line break is an ordinary character to Google in
# all three write operations (M20-5, M20-6, M20-7 — matched by
# `replaceAllText`, accepted in its replacement, accepted by `insertText`),
# and it is the character an editorial preview is BUILT of — heading and
# subheading in one paragraph. Forbidding it here did not protect anything:
# it sent the operator to the Docs API by hand, past every check skrepka
# makes, and that hand-written batch cut a space out of a live anchor
# (postmortem 2026-08-19). #40.
_REWRITE_FORBIDDEN = re.compile("[\x00-\x0a\x0c-\x1f\x7f-\x9f\ue000-\uf8ff]")


def _distinct_anchor_ranges(spans):
    """Ranges of a set of anchor spans, one entry per distinct range.

    A thread exports one record per ENTRY, and every one of them carries the
    parent's range — a thread with a single reply comes back with two
    identical anchors. Counting spans instead of ranges would mean the rewrite
    never fires on a thread anybody replied to.
    """
    return sorted({(s, e) for s, e, _t, _i in spans})


# Code points that continue a grapheme cluster rather than starting one.
# The rewrite batch inserts immediately before the LAST character of the old
# text, and Google refuses an insert that lands inside a cluster — measured
# 2026-08-18 on a stressed vowel: «The insertion index cannot be within a
# grapheme cluster» (#47).
#
# This is not full UAX #29 segmentation, and does not pretend to be: it names
# the continuations that actually turn up at the end of edited prose. Anything
# exotic it misses is still refused — by Google, one step later, which costs a
# refusal and never a damaged document.
_ZWJ = "\u200d"


def _splits_grapheme_cluster(head, tail):
    """Would inserting between `head` and `tail` land inside a cluster?"""
    if unicodedata.combining(tail):
        return True                      # буква со знаком ударения: measured
    if "\ufe00" <= tail <= "\ufe0f" or tail == "\u20e3":
        return True                      # variation selector, keycap
    if head.endswith(_ZWJ):
        return True                      # склеенное эмодзи: 👨‍👩‍👦
    if head and 0x1F1E6 <= ord(head[-1]) <= 0x1F1FF \
            and 0x1F1E6 <= ord(tail) <= 0x1F1FF:
        return True                      # флаг из пары региональных индикаторов
    return False


def _rewrite_anchor_requests(doc_tab, search_text, new_text, start, end,
                             anchors, attribution, named_intervals,
                             closed_present=False):
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
    # There used to be a gate here: one closed thread ANYWHERE in the document
    # switched this whole capability off (#38), because a closed thread's
    # anchor is invisible to the export and could be under the character this
    # batch deletes. It cost a live session the ability to rewrite a phrase
    # after the customer closed unrelated threads at the other end of the
    # file, and the refusal asked the editor to reopen them — technical work
    # asked of a person, which the product frame forbids.
    #
    # Measured 2026-08-16 (M13, docs/FINDINGS.md), on the worst shape there
    # is: a closed thread whose ENTIRE anchor is the character this batch
    # deletes. It survived — still in the resolved list, conversation intact,
    # marked by Google as «Исходный контент удален». It loses its attachment,
    # not its words, and that is exactly what happens when a person edits the
    # same text by hand. Everything else in the batch is `replaceAllText`,
    # which is measured not to hurt closed threads even at full coverage.
    for ts, te, _label in _table_of_contents_intervals(doc_tab):
        if _ranges_overlap(start, end, ts, te):
            return None  # the edit reaches into a table of contents
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
    if _splits_grapheme_cluster(head, tail):
        # The batch inserts the new text immediately BEFORE this character, and
        # Google refuses an insert that lands inside a grapheme cluster —
        # measured on a stressed vowel (#47): «The insertion index cannot be
        # within a grapheme cluster». Nothing is at risk here, the operation
        # simply cannot be built, so it is refused by its real reason instead
        # of by a raw API error further down.
        return None

    ins_at = end - tail_len
    head_len = _utf16_len(head)
    new_len = _utf16_len(new_text)
    if ins_at != start + head_len:
        # The anchor is not contiguous in index space — an inline object sits
        # inside it — so the arithmetic below would address the wrong places.
        return None
    # Every request addresses an absolute range, so nothing here depends on
    # the text being unique anywhere. Until 0.18 request 2 was a
    # `replaceAllText` over `head + new_text`, and it dragged a whole family
    # of preconditions behind it: a projection of the document as it would
    # read BETWEEN requests, a uniqueness count on that projection, and a
    # separate count over headers and footnotes, which `replaceAllText` also
    # rewrites. All three went away with it (M26, block C).
    requests = [
        # 1. new text lands STRICTLY inside the anchor, which grows over it
        {"insertText": {"location": {"index": ins_at}, "text": new_text}},
        # 2. the old head, still in its original coordinates: the insert
        #    happened to its right and did not move it
        {"deleteContentRange": {"range": {"startIndex": start,
                                          "endIndex": ins_at}}},
        # 3. the surviving tail character, now right after the new text
        #    because deleting the head pulled everything left
        {"deleteContentRange": {"range": {
            "startIndex": start + new_len,
            "endIndex": start + new_len + tail_len}}},
    ]
    return requests, tail_len


def _why_no_rewrite(doc_tab, search_text, new_text, start, end, anchors,
                    attribution, named_intervals, closed_present=False):
    """Why the whole-fragment rewrite did not fire, in words for a person.

    `_rewrite_anchor_requests` answers None to half a dozen different
    questions, and the refusal used to give the same advice — «leave part of
    the original anchor text alone» — for all of them. That advice is sound
    for exactly one of the reasons and misleading for the rest: it sends the
    person to rewrite their edit when what is in the way is a table of
    contents, a named range or a neighbour's comment (#24 all over again).

    Cheap re-checks in the order that matters, most specific first. Not a
    second implementation of the preconditions — only the ones a person can
    act on; anything else falls through to the original sentence, which is
    the one that is true when nothing structural is in the way.
    """
    if "\n" in search_text:
        return ("Этот фрагмент занимает несколько абзацев, а переписать "
                "целиком skrepka умеет только фрагмент внутри одного. "
                "Правьте абзацы по отдельности.")
    bad = _REWRITE_FORBIDDEN.search(new_text or "")
    if bad:
        # The two layers допускают разное, и это не небрежность: обычная
        # правка живёт по `_OP_TEXT_FORBIDDEN` (таб, `\n`, `\v`), а перезапись
        # прокомментированного фрагмента считает точные позиции тремя
        # запросами, и `\n` внутри неё создал бы абзац посреди этой
        # арифметики. Мягкий перенос разрешён в обеих (#40). Разница названа
        # вслух, потому что иначе одна и та же операция «проходит здесь и не
        # проходит там» без объяснения (codex, круг 3 по коду).
        what = {"\n": "перевод строки", "\t": "табуляция"}.get(
            bad.group(), f"символ U+{ord(bad.group()):04X}")
        return (f"В новом тексте есть {what}, а переписать прокомментированный "
                f"фрагмент целиком можно только текстом внутри одного абзаца: "
                f"перезапись считает позиции по трём запросам, и новый абзац "
                f"посреди неё ломает счёт. Мягкий перенос (shift+enter) в этом "
                f"тексте разрешён — остальное разбейте на отдельные операции.")
    for ts, te, _label in _table_of_contents_intervals(doc_tab):
        if _ranges_overlap(start, end, ts, te):
            return ("Этот фрагмент попадает в оглавление, а его Google строит "
                    "сам по заголовкам. Поправьте заголовок — оглавление "
                    "обновится следом.")
    for ns, ne, label in named_intervals:
        if _ranges_overlap(start, end, ns, ne):
            return (f"На этом фрагменте стоит {label} — перезапись задела бы "
                    f"машинную пометку. Снимите её или правьте фрагмент в "
                    f"интерфейсе.")
    doomed = _doomed_threads(start, end, anchors, attribution)
    doomed_cid = doomed[0][0] if len(doomed) == 1 else None
    for as_, ae, _t, aid in anchors:
        if not _ranges_overlap(start, end, as_, ae):
            continue
        if doomed_cid is not None and attribution.get(aid) == doomed_cid:
            continue
        return ("На этом же фрагменте есть ещё один комментарий, и переписать "
                "его целиком, не задев соседний, нельзя. Правьте фрагмент в "
                "интерфейсе.")
    if len(search_text) < 2:
        # Замерено (M29): у протокола есть нижняя граница по длине, и она
        # ровно здесь. Обе формы обхода — вставить рядом и снести символ —
        # убивают тред, потому что вставка на границе якорем не поглощается
        # (M28), а якорь, схлопнувшийся в ноль, становится призраком.
        # Совет тут редакторский, а не про интерфейс: человеку есть что
        # сделать, и это его работа, а не техническая.
        return ("Под комментарием один символ, а перевесить разговор на "
                "новый текст можно только изнутри старого: новый текст "
                "вписывается ВНУТРЬ выделения, и внутри одного символа "
                "места нет. Выделите комментарием чуть больше текста — "
                "хотя бы слово целиком, — и правка пройдёт.")
    if len(search_text) >= 2 and _splits_grapheme_cluster(search_text[:-1],
                                                          search_text[-1]):
        return ("Фрагмент кончается символом, который нельзя отделить от "
                "предыдущего — буквой со знаком ударения, составным эмодзи. "
                "Перезапись вставляет новый текст прямо перед последним "
                "символом, а Google не даёт вставить его внутрь такой пары. "
                "Правьте фрагмент в интерфейсе или сдвиньте границу "
                "выделения на один символ.")
    return ("Уцелеть должен ИСХОДНЫЙ символ якоря — повтор того же текста в "
            "замене не помогает. Оставьте в замене часть исходного якорного "
            "текста нетронутой или правьте этот фрагмент в интерфейсе.")


def _execute_anchor_rewrite(docs_service, file_id, tid, requests, revision_id,
                            source, extra_requests_before=None):
    """Send the rewrite batch.

    Since 0.18 every request in it addresses an absolute range, so there is
    no match count to read back and no verdict to derive from the reply: the
    batch is pinned to a revision and either applies whole or fails whole.
    The `occurrencesChanged` check that used to stand here belonged to the
    `replaceAllText` in request 2 and went away with it (M26).
    """
    # `extra_requests_before` goes through the same white list as the rest:
    # it used to be concatenated AFTER the scoping loop, which is how the
    # canary delete could reach the first tab (measured M19-9).
    body = {"requests": _scope_requests(
                list(extra_requests_before or []) + list(requests), tid),
            "writeControl": _write_control(revision_id)}
    docs_service.documents().batchUpdate(
        documentId=file_id, body=body).execute()


def _resolve_replace_target(op, doc_tab, r, check_style=True):
    """Resolve the exact text of a replace target on a given snapshot.

    Uniqueness is deliberately NOT checked here any more. It was never a
    property of the edit — it was a requirement of `replaceAllText`, which
    addresses by searching the tab for a string. Since the write goes by
    absolute index (`_execute_index_replace`), the range is the address, and
    a paragraph that repeats word for word is editable like any other. A
    document whose format requires identical previews used to be uneditable
    for exactly this reason (постмортем 2026-08-25).

    `check_style=False` defers the style check to the caller. The anchor-safe
    path needs that: it may narrow the range, and a mixed style sitting in the
    part the narrowing would leave alone must not decide the whole operation
    before narrowing has been tried (found in review).
    """
    if "quote" in op:
        search_text = op["quote"]
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

    # The resolved range must still hold the text it was resolved from: the
    # snapshot can be older than the write. This replaces the old global
    # uniqueness + round-trip pair, which proved the same thing indirectly
    # and only for a text search.
    here = _extract_exact_text_range(doc_tab, r["start"], r["end"])
    if here != search_text:
        _error(
            f"text at the resolved range changed since it was addressed "
            f"({here!r} vs {search_text!r}) for {r['source']} — refused",
            reason="concurrent_edit",
            details={"expected": search_text, "found": here},
        )
    if check_style:
        problem = _style_refusal(doc_tab, r["start"], r["end"], r["source"])
        if problem:
            _error(problem)
    return search_text


def _range_style(doc_tab, start, end):
    """The textStyle of a uniformly-styled range, for re-applying it.

    `insertText` inherits formatting unpredictably (FINDINGS, 2026-07-14):
    the honest way is to state the style rather than hope for it. Callers
    only reach here for a range that `_match_style_signature` called uniform,
    so the first intersecting run speaks for all of them.

    The dict is deliberately allowed to be empty. It is always sent with a
    mask over EVERY style field, so an absent field means «switch this off»
    rather than «leave whatever was inherited». Without that, plain text
    written next to a link keeps the link: stating only the fields that are
    set can never take a field away (codex P1).
    """
    body = doc_tab.get("body", {}) or {}
    for s, e, tr in _extract_runs_full(body.get("content", [])):
        if s < end and e > start and not tr.get("suggestedInsertionIds"):
            return {f: v for f, v in (tr.get("textStyle") or {}).items()
                    if f in _STYLE_FIELDS and v is not None}
    return {}


def _execute_index_replace(docs_service, file_id, tid, start, end, new_text,
                           style, revision_id, extra_requests_before=None,
                           paragraph_safe=True):
    """Replace [start, end) by absolute index — no text search involved.

    This is what lets a repeated paragraph be addressed at all: the range is
    already known, and `replaceAllText` threw that knowledge away to search
    the tab by text, which is why a document whose format REQUIRES identical
    previews could not be edited (постмортем 2026-08-25).

    Deleting first and inserting into the collapsed point is the measured
    protocol M24-0. Style is stated explicitly because `insertText` inherits
    it from a neighbour and would, for example, hand the new text the link of
    the word next to it.
    """
    requests = list(extra_requests_before or [])
    if end < start:
        # Перевёрнутый диапазон никогда не приходит от разрешения операции, но
        # если однажды придёт — молча пропустить удаление и записать квитанцию
        # по этим координатам хуже, чем отказать: арифметика эффекта построит
        # на нём правдоподобный текст, которого в документе нет. Проверка
        # стоит ДО записи, потому что после неё отказывать уже поздно.
        raise PatchOpError(
            "internal: index replace got an inverted range — refused "
            "before the write",
            state="not_applied")
    if start < end:
        if paragraph_safe is False:
            raise PatchOpError(
                "internal: index replace asked to delete a paragraph "
                "boundary — refused before the write",
                state="not_applied")
        requests.append({"deleteContentRange": {"range": {
            "startIndex": start, "endIndex": end}}})
    if new_text:
        requests.append({"insertText": {
            "location": {"index": start}, "text": new_text}})
        if style is not None:
            # The mask covers every style field, always. A field the source
            # range did not have is then actively cleared on the new text
            # instead of being inherited from whatever sat next to the
            # insertion point — the case that would otherwise hand a plain
            # word the link of its neighbour (codex P1).
            requests.append({"updateTextStyle": {
                "range": {"startIndex": start,
                          "endIndex": start + _utf16_len(new_text)},
                "textStyle": dict(style),
                "fields": ",".join(_STYLE_FIELDS),
            }})
    docs_service.documents().batchUpdate(
        documentId=file_id,
        body={"requests": _scope_requests(requests, tid),
              "writeControl": _write_control(revision_id)},
    ).execute()


def _check_occurrence_stability(r, expected):
    """Номер вхождения имеет смысл только вместе с их числом.

    Операция с явным `occurrence` адресует N-ю копию среди M одинаковых. Если
    предыдущая правка того же файла убрала одну из копий, N-я стала другой —
    а проверка «в этом диапазоне та самая цитата» на близнецах тавтологична и
    промолчит. Так правка уезжает в чужую копию, и обе операции числятся
    применёнными (ревью codex).

    Требование одно: число совпадений на момент записи такое же, каким его
    видела плановая фаза. Операция БЕЗ `occurrence` этой проверки не знает и
    не нуждается в ней — её защищает уникальность: одно совпадение и есть
    доказательство цели, сколько бы их ни было раньше. Именно поэтому
    операция, ставшая однозначной благодаря соседке, по-прежнему проходит
    (#36).
    """
    if expected is None:
        return
    now = r.get("quote_total")
    if now != expected:
        _error(
            f"число одинаковых мест изменилось под этой правкой: было "
            f"{expected}, стало {now}. Номер вхождения теперь показывает на "
            f"другую копию, чем когда его выбирали, — правка отклонена, "
            f"пока адрес не назван заново. ({r['source']})",
            reason="concurrent_edit",
            details={"expected_matches": expected, "found_matches": now})


# Операции, чья цель по построению разрешается ПОЗДНО: их координаты
# существуют только в свежей карте выгрузки, а она строится под самой
# записью. Плановая фаза их не резолвит и не может — это свойство задачи, а
# не ошибка входа, и путать одно с другим нельзя (ревью codex).
_LATE_BOUND_OPS = frozenset(("replace_anchor",))


def _validate_anchor_op(op):
    """Схема `replace_anchor`, проверяемая до карты и до всякой записи.

    Разбирается в плановой фазе: ошибка схемы обязана выглядеть ошибкой
    схемы, а не «цель не разрешилась». Иначе настоящие опечатки в
    `comment_id` терялись бы среди нормального для этой операции позднего
    разрешения.

    Возвращает (comment_id, new_text).
    """
    cid = op.get("comment_id")
    if not isinstance(cid, str) or not cid.strip():
        _error(f"replace_anchor: нужен непустой строковый 'comment_id' — "
               f"id треда из выдачи `comments`. {op}")
    new_text = op.get("with") if "with" in op else op.get("text")
    if not isinstance(new_text, str) or not new_text:
        _error(
            f"replace_anchor: нужен непустой 'with'. Чтобы убрать текст "
            f"из-под комментария целиком, у правки должен быть текст, "
            f"который займёт его место: якорь, схлопнувшийся в ноль, уносит "
            f"разговор в историю комментариев. {op}",
            reason="unsupported_structure",
            details={"construct": "empty_replacement"})
    if "\n" in new_text:
        # Протокол перезаписи вписывает новый текст внутрь якоря и запрещает
        # там перевод строки (`_REWRITE_FORBIDDEN`). Сказать это прямо
        # дешевле, чем дать операции дойти до конца и отказать словами
        # «переписать не вышло»: контракт и поведение обязаны совпадать.
        _error(
            f"replace_anchor: новый текст с переводом строки пока не "
            f"поддержан — абзац под комментарием разделить нечем. Разбейте "
            f"правку на части внутри абзаца. {op}",
            reason="unsupported_structure",
            details={"construct": "paragraph_in_replacement"})
    bad = _OP_TEXT_FORBIDDEN.search(new_text)
    if bad:
        _error(
            f"op text holds a character Docs does not keep as written "
            f"({bad.group()!r} = U+{ord(bad.group()):04X}), so what would "
            f"land is not what the positions were computed for. {op}")
    return cid, new_text


def _resolve_anchor_target(op, doc_tab, snap, tid, file_id):
    """Разрешить `replace_anchor` в диапазон по свежей карте выгрузки.

    Адрес — тред, а не текст. Поэтому здесь два условия, и оба обязательны:
    во ВСЁМ документе у треда ровно одно физическое место (иначе выбирать
    копию пришлось бы за человека), и это место размещено в выбранной
    вкладке доказанно, то есть попало в `snap["anchors"]`. Второе условие
    не следует из первого: маппер не размещает якорь, чью копию он не смог
    отличить от близнеца, и такой якорь физически один, но координат у него
    нет.

    Номер `w:id` адресом не является нигде: Google назначает его сам, и
    смерть чужого якоря сдвигает нумерацию остальных (замерено, M28-E).
    """
    cid, new_text = _validate_anchor_op(op)
    source = f"comment={cid!r}"
    places = (snap.get("thread_places") or {}).get(cid)
    link = _thread_link(file_id, cid)
    where = f" {link}" if link else ""
    if not places:
        _error(
            f"тред {cid}{where} не адресуется: у него нет живого якоря в "
            f"выгрузке. Так выглядит закрытый, удалённый или потерявший "
            f"привязку комментарий — а ещё комментарий из другого "
            f"документа. Прочитайте `comments` заново и возьмите id оттуда. "
            f"({source})",
            reason="comment_thread_unresolvable",
            details={"comment_id": cid})
    if places > 1:
        _error(
            f"у треда {cid}{where} несколько якорей ({places}), и выбирать "
            f"за вас, какой из них переписать, скрепка не станет. Адресуйте "
            f"нужное место цитатой или пометкой. ({source})",
            reason="comment_thread_has_multiple_anchors",
            details={"comment_id": cid, "anchors": places})
    attribution = snap.get("attribution") or {}
    spans = [a for a in snap["anchors"] if attribution.get(a[3]) == cid]
    ranges = _distinct_anchor_ranges(spans)
    if len(ranges) != 1:
        _error(
            f"якорь треда {cid}{where} не размещён в этой вкладке: его "
            f"место в документе известно, а координаты — нет. Так бывает, "
            f"когда абзац повторяется дословно и отличить копии нечем, и "
            f"когда якорь стоит в другой вкладке. Адресуйте правку цитатой "
            f"с номером вхождения. ({source})",
            reason="comment_thread_unresolvable",
            details={"comment_id": cid, "placed_ranges": len(ranges)})
    start, end = ranges[0]
    here = _extract_exact_text_range(doc_tab, start, end)
    if here is None:
        _error(
            f"под комментарием {cid}{where} не сплошной текст — внутри "
            f"якоря объект или структурный элемент, и переписать его как "
            f"текст нельзя. ({source})",
            reason="unsupported_structure",
            details={"construct": "non_contiguous_anchor"})
    if here != spans[0][2]:
        # Выгрузка это R0 плюс канарейка, снимок Docs это R0: тексты обязаны
        # сойтись. Расхождение значит, что документ правят прямо сейчас.
        _error(
            f"текст под комментарием {cid} в выгрузке и в документе разный "
            f"({spans[0][2]!r} против {here!r}) — документ правят прямо "
            f"сейчас. Повторите через минуту. ({source})",
            reason="concurrent_edit",
            details={"expected": spans[0][2], "found": here})
    if "\n" in here:
        _error(
            f"комментарий {cid}{where} растянут через границу абзаца — "
            f"переписать такой фрагмент значит слить два абзаца в один и "
            f"потерять оформление второго. Правьте абзацы по отдельности. "
            f"({source})",
            reason="unsupported_structure",
            details={"construct": "paragraph_boundary"})
    return {"op": "replace_anchor", "start": start, "end": end,
            "text": new_text, "kind": "replace",
            "affect_start": start, "affect_end": end,
            "source": source, "tab_id": tid, "comment_id": cid,
            "quote_total": None}


def _anchor_target_gate(doc_tab, start, end, source, named_intervals):
    """Ограды, которые новый адрес обязан пройти сам.

    У адресации цитатой недостижимость оглавления и именованных диапазонов
    доказывалась СПОСОБОМ АДРЕСАЦИИ: буфер тела оглавление не читает вовсе,
    а именованный диапазон, задевающий его, отклоняется раньше как
    несплошной. Адрес по треду это доказательство отменяет — координаты
    приходят прямо из карты, — поэтому обе ограды здесь стоят явно, и на
    ИСХОДНОМ якоре, до всякого сужения (ревью codex).

    Проверка на исходном якоре, а не на записываемом куске, сознательно
    строгая: `replace_anchor` никогда не адресует именованный диапазон по
    имени, значит любое пересечение с ним побочное, и резать чужую пометку
    ради правки по треду не за что.
    """
    _refuse_on_suggestion_range(doc_tab, start, end, source)
    for ts, te, label in _table_of_contents_intervals(doc_tab):
        if _ranges_overlap(start, end, ts, te):
            _error(
                f"комментарий стоит в оглавлении (диапазон [{ts}, {te})) — "
                f"его строит Google по заголовкам и перестраивает сам, "
                f"поэтому правка тут не удержится. Поправьте заголовок, "
                f"оглавление обновится следом. ({source})",
                reason="unsupported_structure",
                details={"construct": "table_of_contents",
                         "range": [ts, te]})
    for ns, ne, label in named_intervals:
        if _ranges_overlap(start, end, ns, ne):
            _error(
                f"правка по треду задевает {label} (диапазон [{ns}, {ne})) "
                f"— это машинная пометка, и резать её ради правки по треду "
                f"скрепка не станет. Снимите пометку или адресуйте правку "
                f"через неё. ({source})",
                reason="named_range_overlap",
                details={"label": label, "range": [ns, ne]})


def _apply_op_anchor_safe(docs_service, drive_service, file_id, op, tab_id,
                          warnings=None, expect_occurrences=None):
    """Apply ONE op on a commented doc.

    Inserts: fresh read, re-resolve, pinned batch (C5 verified safe).
    Replaces: W8 export-based anchor mapping sandwich —
      fingerprint → read(R) → export docx → read(R'); R≠R' ⇒ retry
      → mapping + target checks + coverage on the R' snapshot
      → fingerprint recheck → batchUpdate pinned to R'.
    A replacement fully covering a live anchor is refused (C1 verified:
    it would ghost the comment). Partial overlap is safe — the anchor
    shrinks to the surviving original characters.

    `replace_anchor` идёт тем же путём записи и теми же оградами, но её
    цель разрешается ПОЗЖЕ — после карты, потому что раньше её координат не
    существует. Всё, что для прочих операций проверяется до канарейки, для
    неё проверяется после, и каждый такой выход обязан канарейку убрать.
    """
    kind_name = op.get("op", "")
    by_anchor = kind_name in _LATE_BOUND_OPS
    if not kind_name.startswith("replace"):
        # ---- insert path ----
        doc = _safe_get_doc(docs_service, file_id)
        tid, doc_tab = _select_tab(doc, tab_id=tab_id)
        r = _resolve_op(op, doc_tab, tid)
        _check_occurrence_stability(r, expect_occurrences)
        _refuse_on_suggestion_at(doc_tab, r["start"], r["source"])
        if not r["text"]:
            return {"source": r["source"], "applied_as": "no-op",
                    "note": "вставлять нечего — ничего не записано"}
        docs_service.documents().batchUpdate(
            documentId=file_id,
            body={"requests": _scope_requests([{"insertText": {
                      "location": {"index": r["start"]},
                      "text": r["text"]}}], tid),
                  "writeControl": _write_control(doc.get("revisionId"))},
        ).execute()
        return _effect_receipt(r["source"], "insert", start=r["start"],
                               end=r["start"], old_text="",
                               new_text=r["text"])

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
        # A multi-tab document is no longer refused here. The export
        # carries no tab id at all (M19-4), but it does carry each tab's
        # OWN elements, and `_fresh_anchor_snapshot` proves the target
        # tab's extent in it before a single anchor is placed. What used
        # to be «several tabs ⇒ refuse» is now «the tab's extent is not
        # proven ⇒ refuse», which is a condition about this document
        # rather than about tabs as such.
        tid, doc_tab = _select_tab(doc, tab_id=tab_id)
        r = None if by_anchor else _resolve_op(op, doc_tab, tid)
        if r is not None:
            _check_occurrence_stability(r, expect_occurrences)

        if r is not None and _op_is_noop(op, doc_tab, r):
            return {"source": r["source"], "applied_as": "no-op",
                    "note": "текст уже такой — ничего не записано"}

        # A replacement that only EXTENDS its target removes no original
        # character, so no anchor can be ghosted by it and none of the export
        # machinery below is needed (r8). This is the common «допиши фразу к
        # прокомментированному предложению», which used to be refused.
        # Для правки по треду короткий путь чистой вставки НЕ берётся, и
        # это не оптимизация, а смысл операции. Вставка на границе якоря им
        # не поглощается (M28), значит «допиши фразу» оставило бы разговор
        # на старой части, а дописанное снаружи — документ верный, а
        # операция сделала не то, что просили. Расширяющая замена идёт
        # общим путём и кончается перезаписью, после которой комментарий
        # покрывает весь новый текст (M26-C).
        insertion = None if by_anchor else _op_pure_insertion(op, doc_tab, r)
        if insertion:
            where, text = insertion
            point = r["end"] if where == "after" else r["start"]
            _refuse_on_suggestion_at(doc_tab, point, r["source"])
            docs_service.documents().batchUpdate(
                documentId=file_id,
                body={"requests": _scope_requests([{"insertText": {
                          "location": {"index": point},
                          "text": text}}], tid),
                      "writeControl": _write_control(doc.get("revisionId"))},
            ).execute()
            return _effect_receipt(
                r["source"], "insert", start=point, end=point,
                old_text="", new_text=text,
                note="замена ничего не удаляла — применена как вставка")

        search_text = None
        if not by_anchor:
            _refuse_on_suggestion_range(doc_tab, r["start"], r["end"],
                                        r["source"])
            search_text = _resolve_replace_target(op, doc_tab, r,
                                                  check_style=False)
        # A quote is allowed to span a paragraph break, and until 0.17 that
        # was harmless: `replaceAllText` kept the boundary. The index writer
        # really deletes the range, so the `\n` would go with it — merging two
        # paragraphs and dropping the second one's paragraphStyle. Nothing
        # measures that (M24-0 did not), so it is refused until it does. A
        # newline in the NEW text is a different thing and stays allowed: it
        # adds a paragraph rather than destroying one.
        if search_text is not None and "\n" in search_text:
            _error(
                f"эта правка удаляет границу абзаца — два абзаца слились бы "
                f"в один, а оформление второго пропало. Разделите её на "
                f"правки внутри каждого абзаца. ({r['source']})",
                reason="unsupported_structure",
                details={"construct": "paragraph_boundary"})
        # Ограды по оглавлению здесь нет намеренно, и это не забывчивость.
        # Адрес сюда попасть не может: цитата ищется по буферу тела, а он
        # оглавление не читает вовсе (`_extract_text_runs` знает абзацы и
        # таблицы), именованный же диапазон, задевающий оглавление,
        # отклоняется раньше как несплошной по текстовым ранам. Проверка,
        # которую нечем вызвать, — не защита, а обещание защиты: она
        # пережила бы любую мутацию и убедила бы следующего читателя, что
        # место закрыто. Закрыто оно геометрией.
        body_content = (doc_tab.get("body", {}) or {}).get("content", [])
        body_end = body_content[-1]["endIndex"] if body_content else 2
        named_intervals = _named_range_intervals(doc_tab)
        _, anchored_now, fp1, universe = _census_comments(
            drive_service, file_id)
        # Закрытые треды видны переписи, но не выгрузке: их якоря невидимы, и
        # карта эффектов их не покрывает. Список нужен каждой квитанции этого
        # пути, чтобы она ограничила область своего знания вслух.
        closed_ids = [c.get("id") for c in anchored_now if c.get("resolved")]

        snap, retry_reason = _fresh_anchor_snapshot(
            docs_service, drive_service, file_id, doc, doc_tab,
            anchored_now, named_intervals, body_end, fp1=fp1,
            universe=universe, tid=tid)
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

        def _owning_canary(fn):
            """Выполнить ПОЗДНЕЕ разрешение цели, владея канарейкой.

            Владение здесь ровно одно и названо точно: весь шаг позднего
            адреса — резолвер, его ограды, чтение текста и распознавание
            no-op. Раньше в это окно попадали только явные отказы, и каждый
            чистил канарейку руками; поздний адрес приводит сюда целую
            функцию, и оставить это на дисциплину вызывающего значит однажды
            забыть служебную строку в чужом документе.

            Что этим НЕ покрыто и не покрывалось никогда: общий расчёт
            стоимости правки ниже — он один на оба пути, и его окно ровно
            такое же, каким было до появления поздних адресов.

            Неудачную уборку нельзя проглатывать: для отказа она уходит в
            текст ошибки, для всего остального — предупреждением, иначе
            человек не узнает, что в документе осталась служебная строка
            (ревью codex).
            """
            try:
                return fn()
            except PatchOpError as exc:
                cleaned = _cleanup_canary(docs_service, file_id, canary)
                raise PatchOpError(_canary_msg(str(exc), cleaned),
                                   state=exc.state, reason=exc.reason,
                                   details=exc.details) from None
            except BaseException:
                if not _cleanup_canary(docs_service, file_id, canary):
                    _warn(f"в конце документа осталась служебная строка "
                          f"«{canary['text']}» — удалите её вручную "
                          f"(данные не потеряны)")
                raise

        def _late_target():
            """Цель поздней операции существует только здесь: карта
            построена, свежесть доказана канарейкой.

            Равенство текста проверяется СРАЗУ после разрешения цели, до
            оград. Иначе `replace_anchor`, которому писать нечего, получал бы
            отказ по предложению или пометке, тогда как равная ему обычная
            замена возвращает честный ноль: записи в обоих случаях нет вовсе
            (ревью codex).
            """
            found = _resolve_anchor_target(op, doc_tab, snap, tid, file_id)
            here = _extract_exact_text_range(doc_tab, found["start"],
                                             found["end"])
            if here == found["text"]:
                return found, here, True
            _anchor_target_gate(doc_tab, found["start"], found["end"],
                                found["source"], named_intervals)
            return found, here, False

        if by_anchor:
            r, search_text, nothing_to_do = _owning_canary(_late_target)
            if nothing_to_do:
                cleaned = _cleanup_canary(docs_service, file_id, canary)
                return {"source": r["source"], "applied_as": "no-op",
                        "note": _canary_msg(
                            "текст под комментарием уже такой — ничего не "
                            "записано", cleaned)}

        # What this operation would cost as written. Blocked ranges: an anchor
        # the accounting could not vouch for but WHOSE POSITION is known — any
        # overlap counts, unlike a healthy anchor its survival under a partial
        # rewrite is not something we can reason about. Doomed threads: full
        # coverage ghosts a comment (C1), partial overlap is verified safe.
        # (Inserts never reach here; they cannot remove text.)
        attribution = snap.get("attribution") or {}
        start_at, end_at, applied_text = r["start"], r["end"], r["text"]
        hits = _blocked_hits(start_at, end_at, snap["blocked"])
        doomed = _doomed_threads(start_at, end_at, snap["anchors"], attribution)
        # A pure deletion has no style question at all: there is no new text
        # to format. The old refusal came from `replaceAllText`, which would
        # have flattened the surviving runs — `deleteContentRange` touches no
        # style. This is the header case «убрать ссылку „Бриф“» from the
        # постмортем 2026-08-25, refused for a reason that never applied.
        # A non-empty replacement still needs one style to state, so a mixed
        # range remains a reason to narrow, and then to refuse.
        style_problem = (
            None if not applied_text
            else _style_refusal(doc_tab, start_at, end_at, r["source"]))
        narrowed = None
        if hits or doomed or style_problem:
            narrowed = _narrow_replace(
                doc_tab, search_text, r["text"], start_at, end_at,
                snap["anchors"], snap["blocked"], attribution,
                interior_only=by_anchor)
        rewrite, rewritten_cid = None, None
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
                closed_present=any(c.get("resolved") for c in anchored_now))
            if rewrite:
                rewritten_cid = doomed[0][0]
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
                f"правится. ({r['source']})", cleaned),
                reason="named_range_overlap",
                details={"label": label, "range": [bs, be]})
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
            # A closed thread used to be the usual answer here and no longer
            # is: the gate it held is gone (M13). What remains are the
            # rewrite's own preconditions, and they need telling apart —
            # «оставьте часть исходного текста» is sound advice for exactly
            # one of them and misleading for the rest (found in review).
            why = " " + _why_no_rewrite(
                doc_tab, search_text, r["text"], start_at, end_at,
                snap["anchors"], attribution, named_intervals,
                closed_present=any(c.get("resolved") for c in anchored_now))
            _error(_canary_msg(
                f"замена накрывает целиком последний якорь {who} "
                f"(текст якоря «{atext[:60]}») — тред станет призраком (C1), "
                f"и сузить её не получилось: меняется весь якорный текст."
                f"{why} ({r['source']})", cleaned),
                reason="comment_anchor_would_be_lost",
                details={"comment_id": cid, "anchor_text": atext})
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
                _execute_index_replace(
                    docs_service, file_id, tid, start_at, end_at,
                    applied_text,
                    _range_style(doc_tab, start_at, end_at),
                    snap["r1"],
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
            return _effect_receipt(
                r["source"], "narrowed", start=start_at, end=end_at,
                old_text=search_text, new_text=applied_text,
                anchors=snap["anchors"], attribution=attribution,
                unknown_ids=closed_ids,
                narrowed_to=search_text,
                note="замена сужена до изменённого фрагмента, чтобы "
                     "сохранить тред")
        if rewrite:
            return _effect_receipt(
                r["source"], "rewritten", start=start_at, end=end_at,
                old_text=search_text, new_text=applied_text,
                anchors=snap["anchors"], attribution=attribution,
                unknown_ids=closed_ids,
                rewritten_cid=rewritten_cid,
                note="фрагмент переписан целиком; тред перевешен на новый "
                     "текст и теперь относится к тексту, которого не было, "
                     "когда комментарий писали")
        return _effect_receipt(
            r["source"], "replace", start=start_at, end=end_at,
            old_text=search_text, new_text=applied_text,
            anchors=snap["anchors"], attribution=attribution,
            unknown_ids=closed_ids)
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
    # Поздние операции: цель разрешается только под записью, по свежей карте
    # комментариев. Держим их отдельно от `deferred` — там операции, чья цель
    # не разрешилась СЛУЧАЙНО, а здесь так устроена сама операция, и путать
    # архитектуру с ошибкой схемы значит прятать настоящие опечатки.
    late_bound = set()
    # Код и подробности для тех отказов, что рождаются ДО цикла записи:
    # иначе машинный контракт зависел бы от того, есть ли в документе
    # комментарии, и от состояния ошибки (codex, ревью r19).
    early_diag = {}
    _RAISE_ERRORS = True
    try:
        for i, op in enumerate(ops):
            if str(op.get("op", "")) in _LATE_BOUND_OPS:
                resolved.append(None)
                insertions.append(None)
                noops.append(False)
                late_bound.add(i)
                try:
                    _validate_anchor_op(op)
                except PatchOpError as e:
                    early_refusals[i] = str(e)
                    early_diag[i] = (e.reason, e.details)
                continue
            try:
                r = _resolve_op(op, doc_tab, tid)
                ins = _op_pure_insertion(op, doc_tab, r)
                noop = _op_is_noop(op, doc_tab, r)
            except PatchOpError as e:
                resolved.append(None)
                insertions.append(None)
                noops.append(False)
                deferred[i] = str(e)
                early_diag[i] = (e.reason, e.details)
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
    # Отложенная операция живёт уникальностью: на момент записи цель у неё
    # ровно одна, и это и есть доказательство адреса. С ЯВНЫМ номером
    # вхождения доказательства нет: номер осмыслен только вместе с числом
    # копий, а плановая фаза его не видела — она эту операцию отвергла.
    # Пропустить такую значит применить голый порядковый номер к состоянию
    # документа, которого человек не видел.
    for i in list(deferred):
        if "occurrence" in (ops[i] if isinstance(ops[i], dict) else {}):
            early_refusals[i] = (
                f"на исходном снимке эта правка не разрешилась "
                f"({deferred[i]}), а номер вхождения имеет смысл только "
                f"вместе с числом копий — примерить его к документу, "
                f"который человек не видел, нельзя. Прочитайте документ "
                f"заново и назовите адрес ещё раз.")
            early_diag[i] = ("concurrent_edit",
                             {"occurrence": ops[i].get("occurrence")})

    # Две правки по одному треду в одном файле — почти наверняка описка, и
    # разобрать её за человека нечем: какую из них он имел в виду, знает
    # только он. Отклоняются обе, как и пересекающиеся диапазоны.
    by_thread = {}
    for i in sorted(late_bound):
        if i in early_refusals:
            continue
        by_thread.setdefault(ops[i].get("comment_id"), []).append(i)
    for cid, group in by_thread.items():
        if len(group) < 2:
            continue
        for i in group:
            early_refusals[i] = (
                f"в файле {len(group)} правки по одному треду {cid!r} — "
                f"отклонены все: какую из них применить, скрепка за вас не "
                f"решит. Оставьте одну.")
            early_diag[i] = ("unsupported_structure",
                             {"comment_id": cid, "ops": group})

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
                early_diag[i] = (e.reason, e.details)
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
        for i in sorted(late_bound):
            # На документе без заякоренных комментариев адресовать по треду
            # нечего по определению: карты не существует, потому что не
            # существует якорей.
            skipped.setdefault(i, (
                f"в документе нет заякоренных комментариев — правку по "
                f"треду адресовать не к чему. "
                f"({_op_source_label(ops[i], None)})"))
            early_diag.setdefault(i, ("comment_thread_unresolvable",
                                      {"comment_id":
                                       ops[i].get("comment_id")}))
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
                requests += _scope_requests(
                    [{"insertText": {"location": loc, "text": ins[1]}}],
                    r["tab_id"])
            elif r["kind"] == "replace":
                if r["end"] > r["start"]:
                    requests += _scope_requests(
                        [{"deleteContentRange": {"range": {
                            "startIndex": r["start"],
                            "endIndex": r["end"]}}}], r["tab_id"])
                if r["text"]:
                    requests += _scope_requests(
                        [{"insertText": {"location": {"index": r["start"]},
                                         "text": r["text"]}}], r["tab_id"])
            elif r["kind"] == "insert" and r["text"]:
                requests += _scope_requests(
                    [{"insertText": {"location": {"index": r["start"]},
                                     "text": r["text"]}}], r["tab_id"])
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
            # Заякоренных комментариев в документе нет вовсе — этим путём он
            # сюда и попал, — значит задевать нечего. Пустой список стоит
            # здесь явно: молчание прочиталось бы как «этот путь про эффекты
            # ничего не сообщает», и вызывающему пришлось бы догадываться по
            # названию стратегии.
            "affected_comment_ids": [],
        }
        if skipped:
            result["refused"] = [
                _with_diag(
                    {"op": i,
                     "source": _op_source_label(ops[i], resolved[i]),
                     "error": skipped[i]},
                    *early_diag.get(i, (None, None)))
                for i in sorted(skipped)]
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
    # Треды, которых уже коснулись правки этого прогона. Правка по треду,
    # чей разговор уже сдвинут соседней операцией, отклоняется: её адрес —
    # тред, и любой текст под ним для неё законен, так что она молча накрыла
    # бы результат предыдущей и обе числились бы применёнными (ревью codex).
    # У обычной замены этой беды нет: `_resolve_replace_target` сверяет
    # текст в разрешённом диапазоне с тем, по которому адресовались.
    touched_threads = set()
    # Правка, писавшая БЕЗ карты комментариев, оставляет вопрос без ответа:
    # задела она чей-то якорь или нет, неизвестно (`effects_basis:
    # "not-mapped"` — так пишут вставки, они карту не строят). Список задетых
    # тредов после такой правки неполон по построению, и опираться на него
    # больше нельзя (ревью codex).
    unmapped_write = False
    failed_at, failure, app_state = None, None, None
    failed_code, failed_details = None, None
    for i, op in enumerate(ops):
        if i in early_refusals:
            # rejected by a gate that judged it on the planning snapshot and
            # will judge it the same way on any other — no reason to send it
            refused.append(_with_diag(
                {"op": i, "source": _op_source_label(op, resolved[i]),
                 "error": early_refusals[i]},
                *early_diag.get(i, (None, None))))
            continue
        if i in late_bound and op.get("comment_id") in touched_threads:
            refused.append(_with_diag(
                {"op": i, "source": _op_source_label(op, resolved[i]),
                 "error": (
                     f"тред {op.get('comment_id')!r} уже задет правкой из "
                     f"этого же файла — текст под ним теперь не тот, для "
                     f"которого писали эту правку. Прочитайте документ "
                     f"заново и адресуйте её ещё раз.")},
                "concurrent_edit", {"comment_id": op.get("comment_id")}))
            continue
        if i in late_bound and unmapped_write:
            # Вставка карту комментариев не строит и потому не может сказать,
            # попала ли она внутрь чьего-то якоря. Пропустить правку по треду
            # после такой значит позволить ей молча накрыть результат
            # соседки: список задетых тредов после вставки неполон.
            refused.append(_with_diag(
                {"op": i, "source": _op_source_label(op, resolved[i]),
                 "error": (
                     "раньше в этом же файле прошла правка, которая карту "
                     "комментариев не строила (вставка), — задела она текст "
                     "под этим тредом или нет, неизвестно. Поставьте правки "
                     "по тредам ПЕРЕД вставками или разнесите их на два "
                     "вызова.")},
                "concurrent_edit", {"comment_id": op.get("comment_id")}))
            continue
        try:
            _RAISE_ERRORS = True  # nested preflight errors must not exit
            try:
                note = _apply_op_anchor_safe(
                    docs_service, drive_service, file_id, op, tab_id,
                    warnings=ghosts,
                    # Номер вхождения осмыслен только вместе с числом копий,
                    # а копию мог унести любой сосед по файлу.
                    expect_occurrences=(
                        resolved[i]["quote_total"]
                        if resolved[i] and "occurrence" in op else None))
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
            if i in late_bound:
                note = dict(note or
                            {"source": _op_source_label(op, resolved[i])})
                note["late_bound"] = (
                    "цель этой правки — тред, а не текст, и её координаты "
                    "существуют только в свежей карте комментариев; в "
                    "проверке пересечений с другими правками файла она не "
                    "участвовала")
            if note:
                if note.get("effects_basis") == "not-mapped":
                    unmapped_write = True
                touched_threads.update(note.get("affected_comment_ids") or ())
                op_notes.append(note)
            continue
        except PatchOpError as e:
            reason, state = str(e), e.state
            code, details = e.reason, e.details
        except HttpError as e:
            reason = e.reason if hasattr(e, "reason") else str(e)
            # 5xx/transport after send: the write may or may not have landed
            status = getattr(getattr(e, "resp", None), "status", None)
            state = "unknown" if (status is None or status >= 500) else "not_applied"
            code = "unknown_write_outcome" if state == "unknown" else None
            details = {"http_status": status} if status else None
        except Exception as e:  # network timeouts etc. — state unknown
            reason, state = str(e), "unknown"
            code, details = "unknown_write_outcome", None
        if state == "not_applied":
            # This operation provably wrote nothing, and the ops in one file
            # do not overlap (checked above), so the rest are independent of
            # it. Refusing THIS operation must not cost the person the other
            # nine (r8): the refusal is collected and the loop goes on.
            refused.append(_with_diag(
                {"op": i, "source": _op_source_label(op, resolved[i]),
                 "error": reason}, code, details))
            continue
        # unknown state: what the document looks like is no longer known,
        # every later operation would be planned against a guess — stop.
        failed_at, failure, app_state = i, reason, state
        failed_code, failed_details = code, details
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
        _with_diag(result, failed_code, failed_details)
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

    # Through the same gate as every other write: a request that names no tab
    # is not tab-neutral (M19), and a second hand-rolled copy of that rule is
    # a second place to forget it (r12, round 2).
    requests = _scope_requests([{
        "createNamedRange": {
            "name": name,
            "range": {"startIndex": start_idx, "endIndex": end_idx},
        }
    }], tid)

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


def _suggestion_tabs_by_id(doc, view_name):
    """Return ordered tabs plus an id map, refusing ambiguous responses."""
    tabs = _collect_tabs(doc)
    seen = {}
    real_tabs = bool(doc.get("tabs"))
    for tab_id, title, doc_tab in tabs:
        if real_tabs and tab_id is None:
            _error(
                f"suggestion {view_name} view returned a tab without a tab "
                f"ID — retry; the document may be changing")
        if tab_id in seen:
            _error(
                f"suggestion {view_name} view returned duplicate tab ID "
                f"{tab_id!r} — retry; nothing was inferred")
        seen[tab_id] = (title, doc_tab)
    return tabs, seen


def _suggestion_tab_diff(original_text, accepted_text, tab_id, title,
                         legacy=False):
    """Build one tab's unified and structured suggestion diff."""
    if original_text == accepted_text:
        return [], ""
    if legacy:
        fromfile, tofile = "original", "with_suggestions"
    else:
        label = f"tab {tab_id} {title}".rstrip()
        fromfile, tofile = f"original — {label}", f"accepted — {label}"
    diff_lines = list(difflib.unified_diff(
        original_text.splitlines(keepends=True),
        accepted_text.splitlines(keepends=True),
        fromfile=fromfile, tofile=tofile, n=2))
    changes = []
    current = None
    for line in diff_lines:
        if line.startswith("@@"):
            if current:
                changes.append(current)
            current = {
                "tab_id": tab_id,
                "tab_title": title,
                "deleted": [],
                "inserted": [],
                "context": line.strip(),
            }
        elif current is not None:
            if line.startswith("-") and not line.startswith("---"):
                current["deleted"].append(line[1:].rstrip("\n"))
            elif line.startswith("+") and not line.startswith("+++"):
                current["inserted"].append(line[1:].rstrip("\n"))
    if current:
        changes.append(current)
    return changes, "".join(diff_lines)


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
            suggestionsViewMode="PREVIEW_WITHOUT_SUGGESTIONS",
            includeTabsContent=True,
        ).execute()
        doc_accepted = docs_service.documents().get(
            documentId=file_id,
            suggestionsViewMode="PREVIEW_SUGGESTIONS_ACCEPTED",
            includeTabsContent=True,
        ).execute()
    except HttpError as e:
        _error(f"failed to read document: {e.reason if hasattr(e, 'reason') else e}")

    title = doc_original.get("title", "")
    ordered, original_tabs = _suggestion_tabs_by_id(doc_original, "without")
    _accepted_ordered, accepted_tabs = _suggestion_tabs_by_id(
        doc_accepted, "accepted")
    original_ids, accepted_ids = set(original_tabs), set(accepted_tabs)
    if original_ids != accepted_ids:
        missing = sorted((original_ids - accepted_ids), key=str)
        extra = sorted((accepted_ids - original_ids), key=str)
        details = []
        if missing:
            details.append(f"missing from accepted view: {missing}")
        if extra:
            details.append(f"missing from original view: {extra}")
        _error(
            "suggestion tab sets differ between preview views (" +
            "; ".join(details) +
            ") — retry; the document may have changed between reads")

    legacy = not bool(doc_original.get("tabs"))
    changes, diff_parts, tab_results = [], [], []
    for tab_id, tab_title, original_tab in ordered:
        accepted_title, accepted_tab = accepted_tabs[tab_id]
        # The original title is the stable attribution displayed to callers.
        # A title-only race cannot make a suggestion disappear, while ID set
        # changes above do fail closed.
        display_title = tab_title or accepted_title or title
        original_text = _extract_full_text(original_tab.get("body", {}))
        accepted_text = _extract_full_text(accepted_tab.get("body", {}))
        tab_changes, tab_diff = _suggestion_tab_diff(
            original_text, accepted_text, tab_id, display_title,
            legacy=legacy)
        changes.extend(tab_changes)
        if tab_diff:
            diff_parts.append(tab_diff)
        tab_results.append({
            "id": tab_id,
            "title": display_title,
            "has_suggestions": bool(tab_changes),
            "change_count": len(tab_changes),
        })

    tabs_with = sum(1 for tab in tab_results if tab["has_suggestions"])
    payload = {
        "title": title,
        "has_suggestions": bool(changes),
        "change_count": len(changes),
        "tab_count": len(tab_results),
        "tabs_with_suggestions": tabs_with,
        "tabs": tab_results,
        "changes": changes,
        "diff": "".join(diff_parts),
    }
    _emit_json(payload, output=output, summary={
        "has_suggestions": bool(changes),
        "change_count": len(changes),
        "tab_count": len(tab_results),
        "tabs_with_suggestions": tabs_with,
    })


# ---------------------------------------------------------------------------
# sync: three-way merge of local markdown into a Google Doc (PLAN.md W4/W5)
# ---------------------------------------------------------------------------

SIDECAR_SCHEMA_VERSION = 3
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


def _para_state_fingerprint(el):
    """Full-state fingerprint of a paragraph, blind to run FRAGMENTATION.

    Remote-change detection has to see a collaborator's underline/colour/font
    edit, so the whole paragraph state goes in. But Docs re-splits and
    re-merges runs on its own initiative, with no styling change at all:
    inserting and deleting the export canary at the END of the body is enough
    to redraw the run boundaries of the last paragraph. Hashing the element
    verbatim then reads that as somebody else's edit, and the NEXT sync of the
    same document dies on a conflict nobody made — which is exactly the
    situation a refused sync leaves behind, right when the refusal has just
    told the person to fix the file and run it again (found in acceptance).

    So adjacent runs that agree on style are merged before hashing: the same
    look always hashes the same, and any real style change still lands.
    """
    para = el.get("paragraph") or {}
    runs, others = [], []
    for e in para.get("elements", []) or []:
        tr = e.get("textRun")
        if tr is None:
            others.append(_strip_indices(e))
            continue
        if not tr.get("content"):
            # a zero-length run carries no look and no text; leaving it in
            # would make A|""|B hash differently from AB (codex code-r2 #1)
            continue
        style = json.dumps(tr.get("textStyle") or {}, sort_keys=True)
        if runs and runs[-1][1] == style:
            runs[-1][0] += tr.get("content", "")
        else:
            runs.append([tr.get("content", ""), style])
    return _sha256_str(json.dumps({
        "paragraphStyle": para.get("paragraphStyle") or {},
        "bullet": para.get("bullet") or {},
        "runs": runs,
        "other": others,
    }, sort_keys=True))


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
                        # full-state fingerprint: all textStyle fields,
                        # paragraphStyle and bullet — remote-change detection
                        # must see underline/color/font edits too (codex
                        # sync-r2 #2) — but blind to run fragmentation, which
                        # Docs redraws on its own (see _para_state_fingerprint)
                        "doc_fp": _para_state_fingerprint(el),
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


def _archived_reply_key(reply):
    """Stable reply identity available in both old and fresh archives.

    Reply ids were not stored in the archive before #28, so the only common
    identity is the exact author/created pair.  An incomplete pair is not an
    identity: collapsing two such records would lose words silently.
    """
    author = reply.get("author")
    created = reply.get("created")
    if not author or not created:
        return None
    return author, created


def _merge_archived_replies(old, fresh):
    """Merge reply snapshots without deleting anything from the archive.

    Old order is retained.  A fresh record replaces an old one only when its
    exact (author, created) identity occurs once on BOTH sides; this makes an
    edited reply fresh while a reply absent after deletion remains archived.
    Colliding or incomplete identities are kept as a union instead of being
    guessed — duplicates cost space, whereas a wrong merge loses the archive's
    only copy of somebody's words.
    """
    from collections import Counter

    if not isinstance(old, list):
        raise ValueError("archived thread replies is not a list")
    if not isinstance(fresh, list):
        raise ValueError("fresh thread replies is not a list")
    if any(not isinstance(reply, dict) for reply in old + fresh):
        raise ValueError("thread replies contains a non-object entry")

    old_counts = Counter(
        key for reply in old if (key := _archived_reply_key(reply)) is not None)
    fresh_counts = Counter(
        key for reply in fresh
        if (key := _archived_reply_key(reply)) is not None)
    fresh_by_key = {}
    for reply in fresh:
        key = _archived_reply_key(reply)
        if (key is not None and old_counts[key] == 1
                and fresh_counts[key] == 1):
            fresh_by_key[key] = reply

    merged, replaced = [], set()
    for reply in old:
        key = _archived_reply_key(reply)
        if key in fresh_by_key:
            merged.append(dict(fresh_by_key[key]))
            replaced.add(key)
        else:
            merged.append(dict(reply))
    for reply in fresh:
        key = _archived_reply_key(reply)
        if key in replaced or reply in merged:
            continue
        merged.append(dict(reply))
    return merged


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
            old_by_id = {}
            for thread in old:
                if not isinstance(thread, dict):
                    raise ValueError("threads contains a non-object entry")
                thread_id = thread.get("id")
                if thread_id in old_by_id:
                    raise ValueError(
                        f"duplicate thread id {thread_id!r} in archive")
                if not isinstance(thread.get("replies", []), list):
                    raise ValueError(
                        f"thread {thread_id!r} replies is not a list")
                old_by_id[thread_id] = thread

            for entry in entries:
                prior = old_by_id.pop(entry["id"], None)
                if prior is not None:
                    entry["replies"] = _merge_archived_replies(
                        prior.get("replies", []), entry["replies"])
            # Dict insertion order is the previous archive order, so threads
            # absent from this snapshot keep their stable place after fresh
            # entries exactly as they did before #28.
            kept = list(old_by_id.values())
        except Exception as e:
            raise RuntimeError(
                f"не удалось прочитать прежний архив закрытых тредов {path}: "
                f"{e}. Перезаписать его нельзя — это единственная копия тех "
                f"разговоров. Уберите файл в сторону и повторите.")

    payload = {
        "doc_id": file_id,
        "note": ("Закрытые треды. Google не отдаёт их в экспорте, поэтому "
                 "skrepka не знает, где стояли их якоря, и правка через "
                 "удаление могла превратить их в призраков: в интерфейсе "
                 "такой тред не появится даже после переоткрытия. Текст "
                 "разговора сохранён здесь. Файл накапливается: треды и "
                 "ответы из прошлых прогонов остаются."),
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
        elif t == "hardbreak":
            # Two spaces at the end of a line: a REAL soft break in the
            # document (\v), which `upload` and `download` carry both ways
            # since #27. `sync` still treats the paragraph as one indivisible
            # atom — its merge works on whole paragraphs and would have to
            # rebuild the break to touch anything inside — so the note names
            # the path that exists instead of calling it unsupported.
            return None, ("мягкий перенос строки в абзаце — целиком такой "
                          "абзац sync не правит, для правки внутри него "
                          "используйте patch")
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
            documentId=file_id, fields="revisionId").execute().get("revisionId")
        data = drive_service.files().export(
            fileId=file_id, mimeType="text/html").execute()
        doc = _safe_get_doc(docs_service, file_id)
        if r1 is None:
            # Google omits revisionId entirely for a doc the account cannot
            # edit (measured M21: canEdit=False => no field at all). Reading
            # such a doc is the most harmless thing a person can ask for, so
            # the export proceeds unproven; the sidecar records that there is
            # no merge base, and every write path already fails closed on a
            # missing revision (_write_control).
            return data, doc
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
    if doc.get("revisionId") is None:
        # No revision means no provable merge base — and no write either:
        # Google withholds the field on docs the account cannot edit. The
        # markdown is still handed over; only sync is closed, and it says why
        # instead of dying on a missing revision at write time.
        payload["sync_supported"] = False
        payload["reason"] = (
            "Google did not return a revision id for this doc — that happens "
            "when the account has no edit right on it (view/comment access). "
            "The markdown is fine to read and edit locally; syncing it back "
            "needs edit access")
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


def _pair_moved_blocks(deleted, inserted, base_els, local_els):
    """Pair a delete with an insert of the SAME paragraph — a reorder.

    difflib has no notion of a move: every reordering arrives as a delete at
    the old place and an insert at the new one, both carrying an identical
    key (measured M21). Pairing them is what lets a moved block keep its own
    styling instead of arriving as a fresh markdown block.

    Returns {(type, normalized text): base_index}.

    The pair must also agree in UTF-16 LENGTH, because the captured runs are
    reapplied by length at the new address. Today that follows from the key
    alone (_norm_ws only swaps NBSP for space, and both are one unit), which
    is exactly why the check is written down: the day _norm_ws starts folding
    anything wider, run boundaries would silently land off by a character.

    A key occurring more than once on either side is never a move — which of
    the twins carries the thread is not provable. Twins are counted over the
    WHOLE sequences, not just the changed zone: a paragraph whose twin sits
    quietly in an untouched part of the document is still a paragraph nobody
    can tell apart. `_dup_guard` already refuses such a sync upstream; this is
    the second line of the same fence, kept because a fence must not depend on
    another check staying as it is.
    """
    from collections import Counter

    def key_of(el, opaque):
        return (None if el["type"] == opaque
                else (el["type"], _norm_ws(el["text"])))

    d_keys = [key_of(base_els[i], "opaque") for i in deleted]
    i_keys = [key_of(lel, "opaque-md") for _g, lel in inserted]
    d_count = Counter(k for k in (key_of(e, "opaque") for e in base_els)
                      if k is not None)
    i_count = Counter(k for k in (key_of(e, "opaque-md") for e in local_els)
                      if k is not None)
    widths = {}
    for k, (_g, lel) in zip(i_keys, inserted):
        if k is not None:
            widths[k] = _utf16_len(lel["text"])
    return {k: i for k, i in zip(d_keys, deleted)
            # unique in BOTH whole sequences, and actually inserted (a key
            # counted once in local may well be an untouched paragraph —
            # `k in widths` is what says the plan really inserts this one)
            if k is not None and d_count[k] == 1 and i_count.get(k) == 1
            and k in widths
            and widths[k] == _utf16_len(base_els[i]["text"])}


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
                  "elements", "md_path", "md_sha256"):  # v2 added doc_fp, v3
                  # changed how it is computed
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
            universe=universe, tid=_tid)
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
    moved_src = _pair_moved_blocks(deleted, inserted, base_els, local_els)

    def _entry_para(text):
        return ("para", _norm_ws(text))

    def _insert_preserve(lel):
        """Style data for a block being inserted.

        A MOVED block is a style-only change at a new address: its text is
        byte-identical to the old paragraph's, so the captured runs tile the
        new one exactly and every preserved field lands where it was. The
        rewrite branch would not do — it keeps inline styling only when the
        whole paragraph agrees on it, so a single coloured word (the ordinary
        case, and the one M21 measured) would still be lost.
        """
        if lel["type"] == "opaque-md":
            return _FRESH_BLOCK_PRESERVE
        src = moved_src.get((lel["type"], _norm_ws(lel["text"])))
        if src is None or src not in r_map:
            return _FRESH_BLOCK_PRESERVE
        preserve, _dropped = _capture_preserve(remote_els[r_map[src]],
                                               with_text=False)
        return preserve

    for j, rel in enumerate(remote_els):
        for lel in insert_before.get(j, []):
            style_slots.append((len(expected), lel, _insert_preserve(lel)))
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
        style_slots.append((len(expected), lel, _insert_preserve(lel)))
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
        # The doc's last paragraph is terminated by the newline at
        # body_end - 1, so a leading newline is what starts a fresh paragraph
        # after it — right when that paragraph holds text, wrong when the doc
        # already ends with an EMPTY one (anyone who pressed Enter at the
        # end). Then the extra newline strands that empty paragraph between
        # the old tail and the appended block: a blank line the person never
        # typed, and a fresh one on every sync that appends (measured M21).
        tail_para = (body_content[-1].get("paragraph") or {}) if body_content \
            else {}
        tail_empty = "".join(
            (e.get("textRun") or {}).get("content", "")
            for e in tail_para.get("elements", []) or []) == "\n"
        joined = ("" if tail_empty else "\n") + \
            "".join(lel["text"] + "\n" for lel in end_inserts)[:-1]
        text_requests.append((body_end - 1, [
            {"insertText": {"location": {"index": body_end - 1},
                            "text": joined}},
        ]))

    # --- journal skeleton (codex sync-r1 #8) ---
    revision_id = doc.get("revisionId")
    moved_dst = set(moved_src.values())
    journal = {
        "doc_id": file_id,
        "revision_before": revision_id,
        "plan": {
            "replaced": [{"base_index": i, "text": lel["text"][:80]}
                         for i, lel in replaced],
            # moved blocks are listed once, under "moved" — leaving them in
            # here as well is how the report used to read a single reorder
            # as three separate events
            "deleted": [{"base_index": i,
                         "text": (base_els[i].get("text") or "")[:80]}
                        for i in deleted if i not in moved_dst],
            "inserted": [{"gap": g, "text": lel["text"][:80]}
                         for g, lel in inserted
                         if (lel["type"], _norm_ws(lel["text"]))
                         not in moved_src],
            "moved": [{"base_index": i, "type": kind, "text": text[:80]}
                      for (kind, text), i in sorted(moved_src.items(),
                                                    key=lambda kv: kv[1])],
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
        # A move and a rewrite need DIFFERENT advice. `patch` rewrites a
        # commented paragraph in place and keeps the thread, so it is the
        # right answer for an edit — and the wrong one for a reorder: it has
        # no move operation, and the person did not change the paragraph, they
        # dragged it somewhere else. Naming the ranges the plan moves is what
        # lets the refusal tell the two apart (M21).
        moved_ranges = [(remote_els[r_map[i]]["start"],
                         remote_els[r_map[i]]["end"])
                        for i in moved_src.values() if i in r_map]

        def _anchor_note(as_, ae, atext, aid):
            head = (f"the anchor of a live comment (docx id {aid}, "
                    f"«{atext[:40]}»)")
            if any(as_ < me and ms < ae for ms, me in moved_ranges) and not \
                    any(ms <= as_ and ae <= me for ms, me in moved_ranges):
                # a selection dragged across a paragraph break (#45), only
                # part of which moves: `patch` is no more able to help here
                # than it is with a whole moved block, so the advice must not
                # promise it
                return (
                    f"{head} — это выделение захватывает несколько абзацев, и "
                    f"часть из них в файле переставлена. Переезд рвёт "
                    f"привязку треда, а собрать её заново программа не может. "
                    f"Верните переставленные абзацы на прежние места в файле — "
                    f"остальные правки пройдут")
            if any(ms <= as_ and ae <= me for ms, me in moved_ranges):
                return (
                    f"{head} — этот абзац в файле не переписан, а переставлен. "
                    f"Переезд в Google Docs выполняется только удалением на "
                    f"старом месте и вставкой на новом, удаление уносит "
                    f"привязку треда, а заново привязать комментарий к тексту "
                    f"программа не может. Верните этот блок на прежнее место в "
                    f"файле — остальные правки пройдут, — а саму перестановку "
                    f"сделайте руками в документе")
            return (f"{head} — точечную правку такого абзаца делает `patch`, "
                    f"он сохраняет тред; либо правьте его в интерфейсе "
                    f"Google Docs")

        protected += [(as_, ae, _anchor_note(as_, ae, atext, aid))
                      for as_, ae, atext, aid in snap["anchors"]]
        # anchors the accounting could not vouch for but could still place —
        # they narrow the refusal to the paragraphs they sit in (issue #10)
        protected += snap["blocked"]
        # An anchor placed inside a table cell protects its own range, which
        # is enough for `patch` — a replace has to be unique across the tab,
        # so an identical twin elsewhere refuses the operation before position
        # matters. `sync` does no such count: it deletes by absolute range and
        # treats a table as indivisible. So for sync every table is protected
        # as soon as one cell anchor exists — the same thing that happened
        # before r11, and it costs sync nothing it could do anyway.
        #
        # Insurance, not a live check, and deliberately untested for the same
        # reason the canary-intersection guard above is: to place an anchor in
        # the WRONG table the two sides would have to agree on that cell's
        # contents word for word, and then no test could tell the two tables
        # apart either. Kept because the argument is about today's checks, and
        # a fence must not depend on another check staying as it is.
        protected += snap.get("cell_anchor_tables") or []
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
                # the text batch already landed: the document is reordered
                # and rewritten, only the look is missing. Saying «nothing
                # was applied» here would be a lie, and a moved block whose
                # styling never arrived looks exactly like a block that
                # silently lost it — name the boundary (codex code-r2 #2).
                _fail_partial(
                    f"style batch failed: {e} — ТЕКСТ УЖЕ ПРИМЕНЁН "
                    f"(переехало блоков: {len(moved_src)}, изменено: "
                    f"{len(replaced)}), а оформление к нему не применилось: "
                    f"переехавшие блоки могли остаться без цвета, подсветки и "
                    f"кегля. Документ цел, скачайте его заново и сверьте "
                    f"оформление изменённых мест")
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
        # a moved block is one event, not a delete plus an insert — counting
        # it three times is how the old report read
        "moved": len(moved_src),
        "inserted": len(inserted) - len(moved_src),
        "deleted": len(deleted) - len(moved_src),
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
