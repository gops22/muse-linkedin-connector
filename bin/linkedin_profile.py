#!/usr/bin/env python3
"""Print the authenticated LinkedIn member's profile.

Usage:
    python3 linkedin_profile.py           # full userinfo JSON
    python3 linkedin_profile.py --status  # compact health check

Uses the OpenID Connect ``/v2/userinfo`` endpoint, which is available to
any app with the "Sign In with LinkedIn using OpenID Connect" product and
the ``openid profile email`` scopes. The --status mode doubles as the
connector's health check: it proves the stored token works and reports
the member URN. It cannot introspect granted scopes — posting scope is
proven by the first successful post (or a --dry-run plus app Products
tab), and engagement scope (w_member_social_feed) only by a live call.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from linkedin_api import LinkedInError, get_access_token, get_userinfo


def main() -> int:
    status_mode = "--status" in sys.argv
    token = get_access_token()
    try:
        info = get_userinfo(token)
    except LinkedInError as exc:
        print(str(exc), file=sys.stderr)
        if exc.status in (401, 403):
            print(
                "Hint: check that the token is still valid (~60-day life) "
                "and that the grant included the openid/profile scopes.",
                file=sys.stderr,
            )
        return 1

    if status_mode:
        print("LinkedIn connector: OK")
        print(f"  member : {info.get('name')} <{info.get('email')}>")
        print(f"  urn    : urn:li:person:{info.get('sub')}")
        print("  profile endpoint: reachable (openid profile email)")
        print("  posting scope (w_member_social): granted by the app's")
        print("    'Share on LinkedIn' product; proven by the first post.")
        return 0

    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
