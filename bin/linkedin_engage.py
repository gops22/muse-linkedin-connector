#!/usr/bin/env python3
"""Comment on, or react to, a LinkedIn post as the authenticated member.

Usage:
    python3 linkedin_engage.py comment --target urn:li:share:123 --text "Great post"
    python3 linkedin_engage.py react   --target urn:li:share:123 --reaction LIKE
    python3 linkedin_engage.py unreact --target urn:li:share:123
    python3 linkedin_engage.py delete-comment --target urn:li:share:123 \
        --comment-id 6643206422739898368
    (add --dry-run to any of these to preview the request offline)

IMPORTANT — scope gate: creating comments and reactions requires the
``w_member_social_feed`` scope, which LinkedIn grants only through its
vetted **Community Management API** product. A standard "Share on
LinkedIn" token (``w_member_social``) can publish posts but gets a 403
here. The commands are implemented so the connector is complete the
day that product is approved; until then the 403 is reported with this
explanation rather than as a mystery failure. Do NOT add the scope to
the OAuth request before the product is approved — LinkedIn rejects
an authorization that names an unapproved scope, breaking sign-in
entirely.

Commenting and reacting act publicly as the member, so the skill's
operating rules require the same explicit approval as posting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from linkedin_api import (
    LinkedInError,
    author_urn,
    create_comment,
    create_reaction,
    delete_comment,
    delete_reaction,
    get_access_token,
)

REACTION_TYPES = (
    "LIKE",
    "PRAISE",        # Celebrate
    "EMPATHY",       # Love
    "INTEREST",      # Insightful
    "APPRECIATION",  # Support
    "ENTERTAINMENT", # Funny
)

SCOPE_HINT = (
    "This action needs the w_member_social_feed scope, granted only "
    "through LinkedIn's vetted Community Management API product. A "
    "'Share on LinkedIn' token cannot comment or react. Apply for "
    "Community Management access in the LinkedIn developer portal; "
    "once approved, reconnect so the new scope is granted."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Comment/react on LinkedIn posts")
    parser.add_argument("action", choices=("comment", "react", "unreact", "delete-comment"))
    parser.add_argument("--target", required=True, help="Post URN (urn:li:share/ugcPost/activity)")
    parser.add_argument("--text", help="Comment text (comment action)")
    parser.add_argument("--reaction", default="LIKE", choices=REACTION_TYPES)
    parser.add_argument("--comment-id", help="Comment id (delete-comment action)")
    parser.add_argument("--dry-run", action="store_true", help="Preview request only")
    args = parser.parse_args()

    if args.action == "comment" and not args.text:
        print("The comment action needs the comment text.", file=sys.stderr)
        return 2
    if args.action == "delete-comment" and not args.comment_id:
        print("delete-comment needs --comment-id.", file=sys.stderr)
        return 2

    try:
        if args.dry_run:
            actor = "urn:li:person:YOU"
            preview: dict = {"actor": actor, "target": args.target}
            if args.action == "comment":
                preview["request"] = {
                    "method": "POST",
                    "path": f"/rest/socialActions/<encoded target>/comments",
                    "body": {
                        "actor": actor,
                        "object": args.target,
                        "message": {"text": args.text},
                    },
                }
            elif args.action == "react":
                preview["request"] = {
                    "method": "POST",
                    "path": "/rest/reactions?actor=<encoded actor>",
                    "body": {"root": args.target, "reactionType": args.reaction},
                }
            elif args.action == "unreact":
                preview["request"] = {
                    "method": "DELETE",
                    "path": "/rest/reactions/(actor:<actor>,entity:<target>)",
                }
            else:
                preview["request"] = {
                    "method": "DELETE",
                    "path": "/rest/socialActions/<encoded target>/comments/<id>",
                    "commentId": args.comment_id,
                }
            preview["requiresScope"] = "w_member_social_feed"
            print(json.dumps(preview, indent=2))
            return 0

        token = get_access_token()
        actor = author_urn(token)
        if args.action == "comment":
            result = create_comment(token, actor, args.target, args.text)
            print(json.dumps(result, indent=2) if result else "Comment posted.")
        elif args.action == "react":
            result = create_reaction(token, actor, args.target, args.reaction)
            print(json.dumps(result, indent=2) if result else "Reaction added.")
        elif args.action == "unreact":
            delete_reaction(token, actor, args.target)
            print("Reaction removed.")
        else:
            delete_comment(token, args.target, args.comment_id, actor)
            print("Comment deleted.")
    except LinkedInError as exc:
        print(str(exc), file=sys.stderr)
        if exc.status == 403:
            print(SCOPE_HINT, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
