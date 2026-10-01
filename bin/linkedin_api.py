#!/usr/bin/env python3
"""Shared LinkedIn API client for the Muse LinkedIn connector.

Auth model
----------
The access token is resolved in this order:

1. ``LINKEDIN_ACCESS_TOKEN`` environment variable (standalone use, or a
   token generated with LinkedIn's own OAuth 2.0 tools).
2. The Muse custom-connector credential helper (used when this code runs
   inside the scaffolded Muse skill, where the token lives in Muse's
   Secure Credentials Store and is never written to disk).

When the connector is integrated with Muse, the scaffolder generates the
``Tooling``/``Auth`` wiring in ``SKILL.md`` and the helper import below is
the single integration point — the rest of this module does not change.

API facts (verified against LinkedIn developer documentation)
--------------------------------------------------------------
- API host:            https://api.linkedin.com
- Authorization URL:   https://www.linkedin.com/oauth/v2/authorization
- Token URL:           https://www.linkedin.com/oauth/v2/accessToken
  (client_id + client_secret are sent in the POST body)
- Scopes used:         openid profile email w_member_social
- Access tokens live ~60 days. Refresh tokens are issued only to
  eligible/approved apps; if yours did not get one, re-authorize when
  the access token expires.
- Media upload URLs returned by initializeUpload live on
  https://www.linkedin.com/dms-uploads/... Upload the bytes to the
  URL exactly as returned, WITH the Authorization header, the file's
  real Content-Type, and a browser-like User-Agent. Verified live
  2026-10-01: a bare PUT (no auth, Python's default
  application/x-www-form-urlencoded type and UA) is rejected by
  LinkedIn's front end with an HTML 400 page; auth + the real MIME
  type + browser UA returns 201. Both hosts are declared on the
  connector for Muse mode.
- Comments and reactions use a separate scope, ``w_member_social_feed``,
  granted only through LinkedIn's vetted Community Management API
  product. The functions are implemented here; without that product
  they return 403. Never request an unapproved scope at authorize
  time — LinkedIn rejects the whole authorization.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.linkedin.com"
# REST (/rest/*) endpoints require a LinkedIn-Version header (YYYYMM).
# LinkedIn supports each monthly version for roughly a year, then
# rejects it outright (HTTP 426 NONEXISTENT_VERSION) — there is no
# fallback, so bump this on a yearly cadence, or override it with the
# LINKEDIN_API_VERSION environment variable. 202508 was sunset on
# 2026-08-17; 202608 is current as of this writing.
LINKEDIN_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202608")


class LinkedInError(RuntimeError):
    """Raised for any non-2xx LinkedIn API response."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"LinkedIn API error {status}: {body}")
        self.status = status
        self.body = body


def get_access_token() -> str:
    """Return the LinkedIn access token, or exit with guidance."""
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    if token:
        return token

    # --- Muse custom-connector integration point -------------------------
    # Inside Muse, the scaffolded skill imports the connector credential
    # helper here instead of reading an environment variable, e.g.:
    #
    #     from connector_credentials import get_token  # generated helper
    #     return get_token("custom.linkedin-token")
    #
    # The token is fetched from the Secure Credentials Store at call time
    # and is never printed or written to a file.
    # ----------------------------------------------------------------------
    print(
        "No LinkedIn access token available.\n"
        "Standalone use: set LINKEDIN_ACCESS_TOKEN in your environment.\n"
        "In Muse: connect LinkedIn first (see README.md, Part 2).",
        file=sys.stderr,
    )
    raise SystemExit(2)


def request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict | None = None,
    rest: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    """Make an authenticated LinkedIn API call and return parsed JSON.

    ``path`` is appended to the API base, e.g. ``/v2/userinfo``.
    Set ``rest=True`` for ``/rest/*`` endpoints, which additionally need
    the LinkedIn-Version and X-Restli-Protocol-Version headers.
    ``extra_headers`` covers Rest.li specifics such as
    ``X-RestLi-Method: PARTIAL_UPDATE`` for post edits.
    """
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if rest:
        headers["LinkedIn-Version"] = LINKEDIN_VERSION
        headers["X-Restli-Protocol-Version"] = "2.0.0"
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
            # Post/comment creation returns 201 with the new object's URN
            # only in the x-restli-id header; surface it for callers.
            restli_id = resp.headers.get("x-restli-id")
            if restli_id and isinstance(result, dict):
                result.setdefault("_restliId", restli_id)
                result.setdefault("_postUrn", restli_id)
            return result
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LinkedInError(exc.code, detail) from exc


def get_userinfo(token: str) -> dict:
    """Fetch the authenticated member's profile (OpenID Connect)."""
    return request("GET", "/v2/userinfo", token=token)


def author_urn(token: str) -> str:
    """Resolve the member's author URN (urn:li:person:<id>) for posting."""
    info = get_userinfo(token)
    sub = info.get("sub")
    if not sub:
        raise LinkedInError(0, "userinfo response did not include 'sub'")
    return f"urn:li:person:{sub}"


def _enc(urn: str) -> str:
    return urllib.parse.quote(urn, safe="")


# ---------------------------------------------------------------------------
# Post management (same w_member_social scope as creating posts)
# ---------------------------------------------------------------------------

def update_post_commentary(token: str, post_urn: str, commentary: str) -> dict:
    """Edit a post's text via a Rest.li partial update (returns 204)."""
    return request(
        "POST",
        f"/rest/posts/{_enc(post_urn)}",
        token=token,
        body={"patch": {"$set": {"commentary": commentary}}},
        rest=True,
        extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
    )


def delete_post(token: str, post_urn: str) -> dict:
    """Delete a post (idempotent; returns 204)."""
    return request(
        "DELETE",
        f"/rest/posts/{_enc(post_urn)}",
        token=token,
        rest=True,
        extra_headers={"X-RestLi-Method": "DELETE"},
    )


# ---------------------------------------------------------------------------
# Engagement: comments and reactions.
#
# Implemented, but gated by LinkedIn: creating comments and reactions
# requires the ``w_member_social_feed`` scope, which LinkedIn grants
# only through the vetted Community Management API product. With a
# standard "Share on LinkedIn" token these calls return 403 — the CLI
# (linkedin_engage.py) explains that instead of failing obscurely.
# ---------------------------------------------------------------------------

def create_comment(token: str, actor_urn: str, target_urn: str, text: str) -> dict:
    """Comment on a post: POST /rest/socialActions/<target>/comments."""
    return request(
        "POST",
        f"/rest/socialActions/{_enc(target_urn)}/comments",
        token=token,
        body={
            "actor": actor_urn,
            "object": target_urn,
            "message": {"text": text},
        },
        rest=True,
    )


def delete_comment(token: str, target_urn: str, comment_id: str, actor_urn: str) -> dict:
    """Delete one of the member's comments (returns 204)."""
    return request(
        "DELETE",
        f"/rest/socialActions/{_enc(target_urn)}/comments/{comment_id}"
        f"?actor={_enc(actor_urn)}",
        token=token,
        rest=True,
        extra_headers={"X-RestLi-Method": "DELETE"},
    )


def create_reaction(token: str, actor_urn: str, root_urn: str, reaction_type: str) -> dict:
    """React to a post: POST /rest/reactions?actor=<actor> (201)."""
    return request(
        "POST",
        f"/rest/reactions?actor={_enc(actor_urn)}",
        token=token,
        body={"root": root_urn, "reactionType": reaction_type},
        rest=True,
    )


def delete_reaction(token: str, actor_urn: str, root_urn: str) -> dict:
    """Remove the member's reaction from a post (returns 204)."""
    compound = f"(actor:{_enc(actor_urn)},entity:{_enc(root_urn)})"
    return request(
        "DELETE",
        f"/rest/reactions/{compound}",
        token=token,
        rest=True,
        extra_headers={"X-RestLi-Method": "DELETE"},
    )


# ---------------------------------------------------------------------------
# Asset uploads. Images and documents use a single PUT after initialize;
# videos use LinkedIn's chunked upload with per-part ETags and a finalize
# call, then processing must finish (status AVAILABLE) before posting.
# ---------------------------------------------------------------------------

def _initialize_upload(token: str, asset: str, owner_urn: str, extra: dict) -> dict:
    """POST /rest/<asset>?action=initializeUpload and return value dict."""
    payload = {"initializeUploadRequest": {"owner": owner_urn, **extra}}
    result = request(
        "POST",
        f"/rest/{asset}?action=initializeUpload",
        token=token,
        body=payload,
        rest=True,
    )
    value = result.get("value", {})
    if not value:
        raise LinkedInError(0, f"initializeUpload for {asset} returned: {result}")
    return value


def _put_bytes(
    upload_url: str,
    data: bytes,
    *,
    token: str | None = None,
    content_type: str | None = None,
) -> dict:
    """PUT raw bytes to an initializeUpload URL; return response headers.

    LinkedIn's dms-uploads front end rejects a bare Python PUT with an
    HTML 400 page (verified live 2026-10-01). The working shape is:
    the Authorization header attached, the file's real Content-Type,
    and a browser-like User-Agent — that returns 201.

    In Muse mode the token passed here is a placeholder: the Muse
    integration point (see get_access_token) substitutes the stored
    credential for requests to www.linkedin.com, exactly as it does
    for api.linkedin.com calls.
    """
    req = urllib.request.Request(upload_url, data=data, method="PUT")
    req.add_header("Content-Type", content_type or "application/octet-stream")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status not in (200, 201):
                raise LinkedInError(resp.status, "asset upload failed")
            return dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LinkedInError(exc.code, f"asset upload failed: {detail}") from exc


def upload_image(token: str, owner_urn: str, image_path: str) -> str:
    """Upload an image; return its urn:li:image identifier."""
    value = _initialize_upload(token, "images", owner_urn, {})
    with open(image_path, "rb") as fh:
        _put_bytes(
            value["uploadUrl"],
            fh.read(),
            token=token,
            content_type=mimetypes.guess_type(image_path)[0],
        )
    return value["image"]


def upload_document(token: str, owner_urn: str, doc_path: str) -> str:
    """Upload a document (PDF/PPT/DOC...); return urn:li:document id."""
    value = _initialize_upload(token, "documents", owner_urn, {})
    with open(doc_path, "rb") as fh:
        _put_bytes(
            value["uploadUrl"],
            fh.read(),
            token=token,
            content_type=mimetypes.guess_type(doc_path)[0],
        )
    return value["document"]


def _upload_extra_asset(value: dict, keyword: str, file_path: str, label: str, token: str) -> None:
    """Upload a video's caption/thumbnail file to its returned URL.

    When initializeUpload is called with uploadCaptions/uploadThumbnail
    set, the response carries extra upload instructions for those
    assets; their exact field names have varied across API versions,
    so scan the response for an uploadUrl under a key mentioning
    ``keyword``. If none is present, fail loudly rather than silently
    dropping the file.
    """
    found: list[str] = []

    def scan(node: object, key_hint: str) -> None:
        if isinstance(node, dict):
            url = node.get("uploadUrl")
            if url and keyword in key_hint.lower():
                found.append(url)
            for key, child in node.items():
                scan(child, f"{key_hint} {key}")
        elif isinstance(node, list):
            for child in node:
                scan(child, key_hint)

    scan(value, "")
    if not found:
        raise LinkedInError(
            0,
            f"initializeUpload returned no {label} upload URL; the "
            f"{label} file was NOT uploaded. Response keys: {list(value)}",
        )
    with open(file_path, "rb") as fh:
        _put_bytes(
            found[0],
            fh.read(),
            token=token,
            content_type=mimetypes.guess_type(file_path)[0],
        )


def upload_video(
    token: str,
    owner_urn: str,
    video_path: str,
    captions_path: str | None = None,
    thumbnail_path: str | None = None,
) -> str:
    """Upload a video (chunked); return urn:li:video id once AVAILABLE.

    Optional caption (.srt/.vtt) and thumbnail files are uploaded to
    the extra URLs LinkedIn returns when the corresponding flags are
    set at initialize time.
    """
    size = os.path.getsize(video_path)
    value = _initialize_upload(
        token,
        "videos",
        owner_urn,
        {
            "fileSizeBytes": size,
            "uploadCaptions": bool(captions_path),
            "uploadThumbnail": bool(thumbnail_path),
        },
    )
    video_urn = value["video"]
    upload_token = value.get("uploadToken", "")
    if captions_path:
        _upload_extra_asset(value, "caption", captions_path, "caption", token)
    if thumbnail_path:
        _upload_extra_asset(value, "thumbnail", thumbnail_path, "thumbnail", token)
    etags: list[str] = []
    with open(video_path, "rb") as fh:
        for part in value.get("uploadInstructions", []):
            length = part["lastByte"] - part["firstByte"] + 1
            fh.seek(part["firstByte"])
            headers = _put_bytes(part["uploadUrl"], fh.read(length), token=token)
            etag = headers.get("ETag") or headers.get("etag")
            if etag:
                etags.append(etag.strip('"'))

    request(
        "POST",
        "/rest/videos?action=finalizeUpload",
        token=token,
        body={
            "finalizeUploadRequest": {
                "video": video_urn,
                "uploadToken": upload_token,
                "uploadedPartIds": etags,
            }
        },
        rest=True,
    )

    # Video processing is asynchronous; the post would fail until the
    # asset reports AVAILABLE.
    encoded = urllib.parse.quote(video_urn, safe="")
    for _ in range(120):  # up to ~10 minutes
        info = request("GET", f"/rest/videos/{encoded}", token=token, rest=True)
        if info.get("status") == "AVAILABLE":
            return video_urn
        time.sleep(5)
    raise LinkedInError(0, f"video {video_urn} did not become AVAILABLE in time")
