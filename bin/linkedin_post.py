#!/usr/bin/env python3
"""Publish (or schedule) a post on the authenticated member's LinkedIn feed.

Content types (LinkedIn Posts API, organic member posts):
    text         commentary only
    image        --image FILE            (repeatable: 2+ files = multi-image)
    video        --video FILE            (chunked upload, waits for AVAILABLE)
    document     --document FILE         (PDF/PPT/DOC carousel-style post)
    article      --article-url URL [--article-title --article-description
                                     --article-thumbnail IMAGE]
    poll         --poll-question Q --poll-option X (2-4) [--poll-duration]
    celebration  --celebration-type TYPE --celebration-image FILE
                 [--celebration-recipient URN ...]
    reshare      --reshare <post URN>    (repost, with optional commentary)
    video extras --video-captions FILE.srt --video-thumbnail IMAGE

Mentions:
    --mention "Ada Lovelace=urn:li:person:XXX" rewrites "@Ada Lovelace"
    in the text to LinkedIn's mention syntax @[Ada Lovelace](URN).
    A plain "@Name" in the text does NOT tag anyone; the name match is
    case-sensitive.

Audience & interaction controls:
    --visibility public|connections|logged_in   (default: public)
        Maps to the Posts API `visibility` field: PUBLIC / CONNECTIONS /
        LOGGED_IN. (CONTAINER exists for group posts and is out of scope.)
    --comments anyone|connections|none          (default: anyone)
        IMPORTANT: LinkedIn's public Posts API has no comment-control
        field. The composer UI offers Anyone / Connections only / No one,
        but an API-created post always takes LinkedIn's default (your last
        UI choice). This flag records your intent: it is shown in the
        dry-run and approval summary, and after publishing the script
        reminds you to set it on the post in LinkedIn if it differs.
    --disable-reshare
        Sets isReshareDisabledByAuthor=true.

Scheduling:
    LinkedIn's own UI can schedule member posts (10 minutes to 3 months
    ahead), but the public API exposes no schedule field for member posts.
    --schedule-at 2026-10-05T14:00:00-05:00 therefore queues the approved
    post locally (JSON queue file) and Muse publishes it at that time
    with --publish-due (wired to a scheduled job). Approval is captured
    when the post is queued; nothing unapproved is ever published.

Other modes:
    --dry-run        print the exact API payload (offline, placeholder
                     URNs for uploads) without posting
    --list-queue     show scheduled posts waiting in the queue
    --cancel-queued ID
                     remove a queued post before it publishes
    --publish-due    publish every queued post whose time has come
                     (a post that fails 3 times is marked failed and
                     stops retrying, instead of looping forever)
    --history        list posts published through this connector
                     (URN + URL are recorded locally at publish time,
                     because reading them back needs a restricted scope)
    --edit-post URN "new text"
                     edit a published post's text (partial update)
    --delete-post URN
                     delete a published post

Requires the w_member_social scope ("Share on LinkedIn" product).
Commentary is limited to 3,000 characters; longer text is rejected here
rather than silently truncated by the API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from linkedin_api import (
    LinkedInError,
    author_urn,
    delete_post,
    get_access_token,
    request,
    update_post_commentary,
    upload_document,
    upload_image,
    upload_video,
)

VISIBILITY_MAP = {
    "public": "PUBLIC",
    "connections": "CONNECTIONS",
    "logged_in": "LOGGED_IN",
}
POLL_DURATIONS = ("ONE_DAY", "THREE_DAYS", "ONE_WEEK", "TWO_WEEKS")
CELEBRATION_TYPES = (
    "CELEBRATE_WELCOME",
    "CELEBRATE_ANNIVERSARY",
    "CELEBRATE_AWARD",
    "CELEBRATE_EVENT",
    "CELEBRATE_GRADUATION",
    "CELEBRATE_JOB_CHANGE",
    "CELEBRATE_KUDOS",
    "CELEBRATE_LAUNCH",
    "CELEBRATE_CAREER_BREAK",
    "CELEBRATE_CERTIFICATE",
    "CELEBRATE_EDUCATION",
    "CELEBRATE_MILESTONE",
)
QUEUE_PATH = os.environ.get(
    "LINKEDIN_POST_QUEUE", os.path.expanduser("~/.linkedin-post-queue.json")
)
HISTORY_PATH = os.environ.get(
    "LINKEDIN_POST_HISTORY", os.path.expanduser("~/.linkedin-post-history.json")
)
MAX_PUBLISH_ATTEMPTS = 3
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
DOC_EXTS = {".pdf", ".ppt", ".pptx", ".doc", ".docx"}
DOC_MAX_BYTES = 100 * 1024 * 1024  # Documents API: 100 MB / 300 pages max
CAPTION_EXTS = {".srt", ".vtt"}
COMMENT_NOTE = (
    "Comment control '{choice}' cannot be set through LinkedIn's public "
    "API. After the post is live, set it in LinkedIn: open the post, tap "
    "the ••• menu, choose 'Who can comment on your post?'."
)


# --------------------------------------------------------------------------
# Spec -> API body. A "spec" is the JSON-friendly description of the post
# (local file paths, not URNs); uploads happen when the body is built for
# real, or are shown as placeholders in a dry run.
# --------------------------------------------------------------------------

def validate_spec(spec: dict) -> None:
    text = spec.get("text", "")
    if len(text) > 3000:
        raise ValueError(
            f"Post text is {len(text)} characters; LinkedIn's limit is 3,000."
        )
    kinds = [
        bool(spec.get("images")),
        bool(spec.get("video")),
        bool(spec.get("document")),
        bool(spec.get("article")),
        bool(spec.get("poll")),
        bool(spec.get("celebration")),
        bool(spec.get("reshare")),
    ]
    if sum(kinds) > 1:
        raise ValueError("Choose only one content type per post.")
    if not text and not any(kinds):
        raise ValueError("A post needs text or a content type.")
    poll = spec.get("poll")
    if poll:
        if len(poll["question"]) > 140:
            raise ValueError("Poll question must be 140 characters or fewer.")
        options = poll["options"]
        if not 2 <= len(options) <= 4:
            raise ValueError("A poll needs 2 to 4 options.")
        if any(len(o) > 30 for o in options):
            raise ValueError("Poll options must be 30 characters or fewer.")
        if poll["duration"] not in POLL_DURATIONS:
            raise ValueError(f"Poll duration must be one of {POLL_DURATIONS}.")
    if spec.get("images") and len(spec["images"]) > 20:
        raise ValueError("Multi-image posts support at most 20 images.")
    celebration = spec.get("celebration")
    if celebration and not celebration.get("image"):
        raise ValueError(
            "A celebration post needs --celebration-image: LinkedIn "
            "requires a media (or template) URN for celebrations."
        )
    article = spec.get("article")
    if article and not article.get("title"):
        raise ValueError("Article posts need --article-title.")
    for mention in spec.get("mentions", []):
        if f"@{mention['name']}" not in text:
            raise ValueError(
                f"Mention '{mention['name']}' was given, but the text "
                f"contains no '@{mention['name']}' to attach it to "
                "(matching is case-sensitive)."
            )


def check_media_files(spec: dict) -> None:
    """Verify referenced files exist and fit LinkedIn's limits.

    Runs on the publish and queue paths (not dry runs, which are
    offline previews and may use not-yet-created files).
    """
    def need(path: str, exts: set | None = None, label: str = "file") -> None:
        if not os.path.isfile(path):
            raise ValueError(f"{label} not found: {path}")
        if exts and os.path.splitext(path)[1].lower() not in exts:
            raise ValueError(
                f"{label} {path}: extension must be one of {sorted(exts)}"
            )

    for path in spec.get("images", []):
        need(path, IMAGE_EXTS, "Image")
    if spec.get("video"):
        need(spec["video"], None, "Video")
    if spec.get("videoCaptions"):
        need(spec["videoCaptions"], CAPTION_EXTS, "Captions")
    if spec.get("videoThumbnail"):
        need(spec["videoThumbnail"], IMAGE_EXTS, "Thumbnail")
    doc = spec.get("document")
    if doc:
        need(doc, DOC_EXTS, "Document")
        if os.path.getsize(doc) > DOC_MAX_BYTES:
            raise ValueError("Documents are limited to 100 MB (and 300 pages).")
    celebration = spec.get("celebration")
    if celebration and celebration.get("image"):
        need(celebration["image"], IMAGE_EXTS, "Celebration image")
    article = spec.get("article")
    if article and article.get("thumbnail"):
        need(article["thumbnail"], IMAGE_EXTS, "Article thumbnail")


def apply_mentions(text: str, mentions: list) -> str:
    """Rewrite @Name placeholders into LinkedIn mention syntax."""
    for mention in mentions:
        text = text.replace(
            f"@{mention['name']}", f"@[{mention['name']}]" + f"({mention['urn']})"
        )
    return text


def build_body(spec: dict, author: str, token: str | None) -> dict:
    """Build the POST /rest/posts body.

    With token=None (dry run), uploads are represented by placeholder URNs.
    """
    dry = token is None

    def img(path: str) -> str:
        return "urn:li:image:UPLOADED_AT_PUBLISH" if dry else upload_image(token, author, path)

    body: dict = {
        "author": author,
        "commentary": apply_mentions(
            spec.get("text", ""), spec.get("mentions", [])
        ),
        "visibility": VISIBILITY_MAP[spec.get("visibility", "public")],
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": bool(spec.get("disableReshare", False)),
    }

    content: dict = {}
    if spec.get("images"):
        urns = [img(p) for p in spec["images"]]
        if len(urns) == 1:
            content["media"] = {"id": urns[0]}
        else:
            content["multiImage"] = {"images": [{"id": u} for u in urns]}
    elif spec.get("video"):
        video_urn = (
            "urn:li:video:UPLOADED_AT_PUBLISH"
            if dry
            else upload_video(
                token,
                author,
                spec["video"],
                captions_path=spec.get("videoCaptions"),
                thumbnail_path=spec.get("videoThumbnail"),
            )
        )
        media = {"id": video_urn}
        if spec.get("videoTitle"):
            media["title"] = spec["videoTitle"]
        content["media"] = media
    elif spec.get("document"):
        doc_urn = (
            "urn:li:document:UPLOADED_AT_PUBLISH"
            if dry
            else upload_document(token, author, spec["document"])
        )
        content["media"] = {
            "id": doc_urn,
            "title": os.path.basename(spec["document"]),
        }
    elif spec.get("article"):
        art = {"source": spec["article"]["url"]}
        if spec["article"].get("title"):
            art["title"] = spec["article"]["title"]
        if spec["article"].get("description"):
            art["description"] = spec["article"]["description"]
        if spec["article"].get("thumbnail"):
            art["thumbnail"] = img(spec["article"]["thumbnail"])
        content["article"] = art
    elif spec.get("poll"):
        poll = spec["poll"]
        content["poll"] = {
            "question": poll["question"],
            "options": [{"text": o} for o in poll["options"]],
            "settings": {"duration": poll["duration"]},
        }
    elif spec.get("celebration"):
        cel = spec["celebration"]
        celebration = {"type": cel["type"], "media": img(cel["image"])}
        if cel.get("recipients"):
            celebration["recipients"] = cel["recipients"]
        content["celebration"] = celebration

    if content:
        body["content"] = content
    if spec.get("reshare"):
        body["reshareContext"] = {"parent": spec["reshare"]}
    return body


def load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def record_history(post_urn: str, spec: dict) -> str:
    """Record a published post locally; returns its LinkedIn URL.

    Reading posts back through the API needs the restricted
    r_member_social scope, so the URN/URL captured at publish time is
    the connector's only handle for later edit/delete.
    """
    url = f"https://www.linkedin.com/feed/update/{post_urn}/"
    entries = load_history()
    kind = next(
        (
            k
            for k in ("images", "video", "document", "article", "poll",
                      "celebration", "reshare")
            if spec.get(k)
        ),
        "text",
    )
    entries.append(
        {
            "urn": post_urn,
            "url": url,
            "publishedAt": datetime.now(timezone.utc).isoformat(),
            "visibility": spec.get("visibility", "public"),
            "kind": kind,
        }
    )
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
    return url


def publish(spec: dict) -> dict:
    """Publish a spec now; returns the API result (with _postUrn/_postUrl).

    No automatic retry: a blind retry after an ambiguous failure could
    publish the post twice. Callers decide whether to try again.
    """
    validate_spec(spec)
    check_media_files(spec)
    token = get_access_token()
    author = author_urn(token)
    body = build_body(spec, author, token)
    result = request("POST", "/rest/posts", token=token, body=body, rest=True)
    post_urn = result.get("_postUrn")
    if post_urn:
        result["_postUrl"] = record_history(post_urn, spec)
    comments = spec.get("comments", "anyone")
    if comments != "anyone":
        print(COMMENT_NOTE.format(choice=comments), file=sys.stderr)
    return result


# --------------------------------------------------------------------------
# Local scheduling queue
# --------------------------------------------------------------------------

def load_queue() -> list:
    if not os.path.exists(QUEUE_PATH):
        return []
    with open(QUEUE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_queue(items: list) -> None:
    with open(QUEUE_PATH, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2)


def queue_post(spec: dict, when_iso: str) -> str:
    validate_spec(spec)
    check_media_files(spec)  # fail now, not at 9am on publish day
    when = datetime.fromisoformat(when_iso)
    if when.tzinfo is None:
        raise ValueError("--schedule-at needs a timezone, e.g. 2026-10-05T14:00:00-05:00")
    if when <= datetime.now(timezone.utc):
        raise ValueError("--schedule-at must be in the future.")
    items = load_queue()
    item_id = uuid.uuid4().hex[:12]
    items.append(
        {
            "id": item_id,
            "publishAt": when.isoformat(),
            "spec": spec,
            "attempts": 0,
        }
    )
    save_queue(items)
    return item_id


def cancel_queued(item_id: str) -> bool:
    items = load_queue()
    remaining = [i for i in items if i["id"] != item_id]
    if len(remaining) == len(items):
        return False
    save_queue(remaining)
    return True


def publish_due() -> int:
    now = datetime.now(timezone.utc)
    items, remaining, failures = load_queue(), [], 0
    for item in items:
        if item.get("failed"):
            remaining.append(item)  # kept for inspection; never retried
            continue
        when = datetime.fromisoformat(item["publishAt"])
        if when > now:
            remaining.append(item)
            continue
        item["attempts"] = item.get("attempts", 0) + 1
        try:
            result = publish(item["spec"])
            print(f"[{item['id']}] published: {result}")
        except Exception as exc:  # surface the failure; cap the retries
            print(f"[{item['id']}] FAILED: {exc}", file=sys.stderr)
            item["lastError"] = str(exc)
            if item["attempts"] >= MAX_PUBLISH_ATTEMPTS:
                item["failed"] = True
                print(
                    f"[{item['id']}] giving up after {item['attempts']} "
                    "attempts; fix the cause, then remove it with "
                    "--cancel-queued and re-queue if still wanted.",
                    file=sys.stderr,
                )
            remaining.append(item)
            failures += 1
    save_queue(remaining)
    return 1 if failures else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def spec_from_args(args: argparse.Namespace) -> dict:
    spec: dict = {
        "text": args.text or "",
        "visibility": args.visibility,
        "comments": args.comments,
        "disableReshare": args.disable_reshare,
    }
    if args.image:
        spec["images"] = args.image
    if args.video:
        spec["video"] = args.video
        if args.video_title:
            spec["videoTitle"] = args.video_title
        if args.video_captions:
            spec["videoCaptions"] = args.video_captions
        if args.video_thumbnail:
            spec["videoThumbnail"] = args.video_thumbnail
    if args.reshare:
        spec["reshare"] = args.reshare
    if args.mention:
        mentions = []
        for raw in args.mention:
            name, sep, urn = raw.partition("=")
            if not sep or not urn.startswith("urn:li:"):
                raise ValueError(
                    f"--mention expects 'Display Name=urn:li:...', got: {raw!r}"
                )
            mentions.append({"name": name.strip(), "urn": urn.strip()})
        spec["mentions"] = mentions
    if args.document:
        spec["document"] = args.document
    if args.article_url:
        spec["article"] = {
            "url": args.article_url,
            "title": args.article_title,
            "description": args.article_description,
            "thumbnail": args.article_thumbnail,
        }
    if args.poll_question:
        spec["poll"] = {
            "question": args.poll_question,
            "options": args.poll_option or [],
            "duration": args.poll_duration,
        }
    if args.celebration_type:
        spec["celebration"] = {
            "type": args.celebration_type,
            "image": args.celebration_image,
            "recipients": args.celebration_recipient or [],
        }
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Post to LinkedIn")
    parser.add_argument("text", nargs="?", help="Post text (commentary)")
    parser.add_argument("--image", action="append", help="Image file (repeatable)")
    parser.add_argument("--video", help="Video file")
    parser.add_argument("--video-title", help="Optional video title")
    parser.add_argument("--video-captions", help="Captions file (.srt/.vtt)")
    parser.add_argument("--video-thumbnail", help="Thumbnail image file")
    parser.add_argument("--reshare", metavar="URN", help="Reshare this post URN")
    parser.add_argument(
        "--mention",
        action="append",
        metavar="'Name=URN'",
        help="Tag a member: rewrites @Name in the text (repeatable)",
    )
    parser.add_argument("--document", help="Document file (PDF/PPT/DOC)")
    parser.add_argument("--article-url", help="Link to share as an article card")
    parser.add_argument("--article-title")
    parser.add_argument("--article-description")
    parser.add_argument("--article-thumbnail", help="Thumbnail image file")
    parser.add_argument("--poll-question")
    parser.add_argument("--poll-option", action="append", help="Poll option (2-4)")
    parser.add_argument("--poll-duration", default="THREE_DAYS", choices=POLL_DURATIONS)
    parser.add_argument("--celebration-type", choices=CELEBRATION_TYPES)
    parser.add_argument("--celebration-image", help="Celebration image file")
    parser.add_argument("--celebration-recipient", action="append", help="Member URN")
    parser.add_argument(
        "--visibility",
        default="public",
        choices=tuple(VISIBILITY_MAP),
        help="Who can see the post (default: public)",
    )
    parser.add_argument(
        "--comments",
        default="anyone",
        choices=("anyone", "connections", "none"),
        help="Who may comment — recorded intent; see module docstring",
    )
    parser.add_argument("--disable-reshare", action="store_true")
    parser.add_argument(
        "--schedule-at",
        metavar="ISO8601",
        help="Queue the post for this time instead of publishing now",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview payload only")
    parser.add_argument("--list-queue", action="store_true")
    parser.add_argument("--cancel-queued", metavar="ID", help="Remove a queued post")
    parser.add_argument("--publish-due", action="store_true")
    parser.add_argument("--history", action="store_true", help="List published posts")
    parser.add_argument("--edit-post", metavar="URN", help="Edit this post's text")
    parser.add_argument("--delete-post", metavar="URN", help="Delete this post")
    args = parser.parse_args()

    try:
        if args.list_queue:
            items = load_queue()
            print(json.dumps(items, indent=2) if items else "Queue is empty.")
            return 0
        if args.cancel_queued:
            if cancel_queued(args.cancel_queued):
                print(f"Removed queued post {args.cancel_queued}.")
                return 0
            print(f"No queued post with id {args.cancel_queued}.", file=sys.stderr)
            return 1
        if args.publish_due:
            return publish_due()
        if args.history:
            entries = load_history()
            print(json.dumps(entries, indent=2) if entries else "No posts recorded yet.")
            return 0
        if args.edit_post:
            new_text = args.text or ""
            if not new_text:
                raise ValueError("--edit-post needs the replacement text.")
            if len(new_text) > 3000:
                raise ValueError("Post text is limited to 3,000 characters.")
            encoded = urllib.parse.quote(args.edit_post, safe="")
            if args.dry_run:
                print(json.dumps({
                    "method": "POST",
                    "path": f"/rest/posts/{encoded}",
                    "headers": {"X-RestLi-Method": "PARTIAL_UPDATE"},
                    "body": {"patch": {"$set": {"commentary": new_text}}},
                }, indent=2))
                return 0
            token = get_access_token()
            update_post_commentary(token, args.edit_post, new_text)
            print(f"Edited post {args.edit_post}.")
            return 0
        if args.delete_post:
            if args.dry_run:
                print(json.dumps({
                    "method": "DELETE",
                    "path": f"/rest/posts/{urllib.parse.quote(args.delete_post, safe='')}",
                }, indent=2))
                return 0
            token = get_access_token()
            delete_post(token, args.delete_post)
            print(f"Deleted post {args.delete_post}.")
            return 0

        spec = spec_from_args(args)
        validate_spec(spec)

        if args.schedule_at:
            item_id = queue_post(spec, args.schedule_at)
            print(f"Queued as {item_id} for {args.schedule_at} -> {QUEUE_PATH}")
            if spec["comments"] != "anyone":
                print(COMMENT_NOTE.format(choice=spec["comments"]), file=sys.stderr)
            return 0

        if args.dry_run:
            body = build_body(spec, "urn:li:person:YOU", None)
            preview = {"post": body, "requestedComments": spec["comments"]}
            if spec["comments"] != "anyone":
                preview["note"] = COMMENT_NOTE.format(choice=spec["comments"])
            print(json.dumps(preview, indent=2))
            return 0

        result = publish(spec)
    except (ValueError, LinkedInError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2) if result else "Post published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
