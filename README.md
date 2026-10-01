# LinkedIn Connector for Muse

A custom connector that lets [Muse](https://muse.ai) — Meta's personal AI
agent — work with **your own LinkedIn account through LinkedIn's official
API**: read your profile; publish posts in every organic format; schedule
them; and edit or delete them afterwards.

No browser automation, no session cookies, no scraping. Authentication is
OAuth 2.0, and your credentials are stored in Muse's Secure Credentials
Store — never in this repo, never in chat.

---

## What it can do

| Capability | How | Scope needed |
| --- | --- | --- |
| Fetch your profile (name, email, photo) | `GET /v2/userinfo` | `openid profile email` |
| Text post | `POST /rest/posts` | `w_member_social` |
| Image post / multi-image post (2–20 images) | Images API upload + `POST /rest/posts` | `w_member_social` |
| Video post (chunked upload, optional captions + thumbnail) | Videos API upload + `POST /rest/posts` | `w_member_social` |
| Document post (PDF/PPT/DOC carousel, ≤100 MB / 300 pages) | Documents API upload + `POST /rest/posts` | `w_member_social` |
| Article / link post (custom title, description, thumbnail) | `content.article` in `POST /rest/posts` | `w_member_social` |
| Poll (2–4 options, 1 day – 2 weeks) | `content.poll` in `POST /rest/posts` | `w_member_social` |
| Celebration post (job change, kudos, launch, …) | `content.celebration` in `POST /rest/posts` | `w_member_social` |
| Reshare / repost a post | `reshareContext` in `POST /rest/posts` | `w_member_social` |
| @mention members in a post | `--mention 'Name=URN'` rewrites `@Name` to mention syntax | `w_member_social` |
| Audience control per post: Anyone / Connections / Logged-in members | `visibility` field on every post | — |
| Disable resharing per post | `isReshareDisabledByAuthor` | — |
| Edit a published post's text | Partial update on `POST /rest/posts/{URN}` | `w_member_social` |
| Delete a published post | `DELETE /rest/posts/{URN}` | `w_member_social` |
| Post history (URN + URL of everything you published) | Recorded locally at publish time | — |
| Schedule a post | Local queue + Muse publishes via the API at the chosen time (see below) | — |
| Comment on / react to a post | `socialActions` + Reactions APIs — **implemented but scope-gated, see below** | `w_member_social_feed` ⚠️ |

### Three honest limitations

- **Comment control** (Anyone / Connections only / No one) exists in
  LinkedIn's composer UI, but the public Posts API exposes **no field**
  for it — an API-created post takes LinkedIn's default, which is whatever
  you last chose in the UI. The connector accepts `--comments` to record
  your intent, shows it in the approval summary, and reminds you to set it
  on the post in LinkedIn after publishing. It never pretends the setting
  was applied.
- **Scheduling:** LinkedIn's UI schedules member posts natively (10
  minutes to 3 months ahead), but the public API has no schedule field
  for member posts. So `--schedule-at` queues the *approved* post in a
  local JSON queue, and Muse runs `--publish-due` at that time to post
  it through the API. If Muse isn't running at that moment, the post
  publishes on the next `--publish-due` run and reports the delay. The
  queue supports `--cancel-queued <id>`, and a post that fails three
  times is marked failed instead of retrying forever.
- **Comments and reactions need a vetted scope.** Creating them
  requires `w_member_social_feed`, granted only through LinkedIn's
  **Community Management API** product (an application/review process —
  not self-serve). The commands are implemented in
  `bin/linkedin_engage.py`; with a standard token they return `403` and
  the CLI explains exactly that. Don't add the scope to your OAuth
  request before the product is approved: LinkedIn rejects an
  authorization naming an unapproved scope and sign-in breaks entirely.
  Relatedly, *reading back* your own posts and their analytics needs
  the restricted `r_member_social` scope — which is why published posts
  are recorded locally (`--history`) instead.

## What it deliberately cannot do

LinkedIn restricts the following to approved **partner programs** — they
return `403` for ordinary developer apps and have no self-serve path, so
this connector does not attempt them:

- Job search and job applications
- Posting jobs (a separate employer-side Jobs API, client-credentials
  flow — not member posting)
- Creating events (a separate partner Events API)
- Reading or sending inbox messages
- People search
- Sending connection invitations

Automating the LinkedIn website instead would violate LinkedIn's User
Agreement (§8.2) and risks account restriction, so it is not a fallback.

---

## How it works

```
You ──approve on LinkedIn──► Muse secure connect page
                                │  stores Client ID/Secret + tokens
                                ▼
                        Secure Credentials Store
                                │  token supplied at call time
                                ▼
   Muse skill (SKILL.md) ──► bin/*.py ──► https://api.linkedin.com
```

- **Connector** (`custom.linkedin`) — the OAuth registration Muse holds:
  endpoints, scopes, and your credential.
- **Skill** (`SKILL.md` + `bin/`) — the instructions and small Python CLIs
  Muse actually runs. The scripts resolve the token at call time; nothing
  secret is ever written to a file.

---

## Part 1 — Create your LinkedIn developer app

LinkedIn issues API credentials per **app**, so you create one first
(two minutes, free):

1. Go to [linkedin.com/developers/apps](https://www.linkedin.com/developers/apps)
   and click **Create app**.
2. Fill in the app name and details. LinkedIn requires every app to be
   associated with a LinkedIn **Page**, which acts as the app's publisher.
   **Which Page?** You're an *individual developer* using self-serve
   products, so LinkedIn provides one: in the Page field, type and select
   **"Default Company Page for Individual Developer"**. Per LinkedIn's
   Help article on app–Page association, "API Products available to
   individual developers must have a default page associated with them
   and you must select that default page to proceed." It's LinkedIn's own
   placeholder page — not yours, not public in your name, nothing to
   create, and nobody can end up associated with a page of yours because
   there isn't one. Posts still publish to your personal profile.
   *(Fallback, rarely needed: if the default page isn't offered in your
   form, create a Page for yourself operating in your individual
   capacity — LinkedIn's Page Terms allow a sole-proprietorship Page —
   and select that instead. Never select an employer's Page, and never
   invent a fake company; LinkedIn bans fake entities.)*
3. **Verify the app — read this carefully.** The app's **Settings**
   tab will likely show: *"This app is not verified as being associated
   with this company."* That banner means the Page's **super admin**
   hasn't approved the app–Page association.
   - With the default individual-developer Page, you are **not** that
     super admin, so you can't approve the verification URL yourself —
     it would have to go to that Page's (unknown) admin.
   - The banner alone doesn't always block the two self-serve
     products. **Acid test:** go to the **Products** tab (Step 4) and
     try to add both products. If they add instantly, ignore the
     banner and proceed.
   - If the Products tab blocks you pending verification, the
     default-Page route has failed for this app. Fallback: create
     your own Page in your individual capacity (Step 2 fallback),
     associate a new app with it — as its super admin you can click
     **Settings → Verify → Generate URL**, open that URL yourself,
     and approve it, then add the products.
4. Open the **Products** tab and add both:
   - **Sign In with LinkedIn using OpenID Connect** → grants
     `openid profile email`
   - **Share on LinkedIn** → grants `w_member_social`
5. Open the **Auth** tab and, under **Authorized redirect URLs**, add this
   exactly:

   ```
   https://agent.meta.ai/api/hatch/oauth/callback
   ```

6. Keep the Auth tab open — it shows your **Client ID** and
   **Client Secret**, which you'll need in Part 2.

## Part 2 — Connect LinkedIn to Muse

1. In Muse chat, ask Muse to build/connect the LinkedIn connector (or use
   the connect link Muse gives you). Muse opens a **secure connect page**
   for a custom OAuth connector with these settings:

   | Setting | Value |
   | --- | --- |
   | Authorization URL | `https://www.linkedin.com/oauth/v2/authorization` |
   | Token URL | `https://www.linkedin.com/oauth/v2/accessToken` |
   | Token auth method | `client_secret_post` |
   | API hosts | `api.linkedin.com`, `www.linkedin.com` (media uploads) |
   | Scopes | `openid profile email w_member_social` |

2. On that page, enter the **Client ID** and **Client Secret** from your
   app's Auth tab. They go directly into secure storage — Muse never sees
   them in chat.
3. You'll be redirected to LinkedIn to **approve** the requested
   permissions. Approve with the same account the app belongs to.
4. Done — the connector is registered as `custom.linkedin`. The first real
   API call is what verifies the credential; if it returns `401/403`,
   Muse checks the token, its expiry, and the granted scopes before
   changing anything.

> **Token lifetimes:** LinkedIn access tokens last ~60 days. A refresh
> token (~1 year) is issued only to eligible/approved apps — if yours
> didn't get one, reconnect the connector when posting starts failing
> with `401` rather than debugging the code.
>
> **API versioning:** the scripts pin `LinkedIn-Version: 202608`
> (override with the `LINKEDIN_API_VERSION` env var). LinkedIn retires
> each monthly version after roughly a year and then rejects it with
> `426` — bump the pin yearly.

## Part 3 — Install the skill

Once the connector is connected, Muse scaffolds the skill from it:

```bash
/opt/hatch/skills/skill-creator/bin/scaffold-connector-skill --provider linkedin
```

This generates `~/workspace/skills/linkedin/` with a `SKILL.md` whose
Tooling/Auth sections already describe the live credential mechanics. Then:

1. Copy this repo's `bin/*.py` into the scaffolded skill's `bin/` folder
   (the token-resolution integration point is marked in
   `bin/linkedin_api.py`).
2. Merge this repo's `SKILL.md` **Operating Rules** into the scaffolded
   one — most importantly: *posting always requires the user's approval of
   the exact final text first.*
3. Compile-check the scripts:

```bash
python3 -m py_compile ~/workspace/skills/linkedin/bin/*.py
```

4. Test with a read before any write: ask Muse *"show my LinkedIn
   profile"* — it runs `linkedin_profile.py` and should print your
   `userinfo` JSON.

## Usage

Ask Muse in plain language — *"post this to LinkedIn"*, *"fetch my
LinkedIn profile"* — or run the CLIs directly:

```bash
# Profile (+ compact health check)
python3 bin/linkedin_profile.py
python3 bin/linkedin_profile.py --status

# Preview any post payload without posting (works offline)
python3 bin/linkedin_post.py "Hello from my Muse connector" --dry-run

# Text post, connections-only audience
python3 bin/linkedin_post.py "For my network" --visibility connections

# Image / multi-image / video / document posts
python3 bin/linkedin_post.py "Demo day!" --image ./demo.png
python3 bin/linkedin_post.py "Trip recap" --image a.jpg --image b.jpg
python3 bin/linkedin_post.py "Watch this" --video ./demo.mp4
python3 bin/linkedin_post.py "My slides" --document ./deck.pdf

# Article (link) post with a custom card
python3 bin/linkedin_post.py "Worth reading" \
  --article-url https://example.com/post --article-title "Title"

# Poll (options 2-4, each <=30 chars; duration ONE_DAY..TWO_WEEKS)
python3 bin/linkedin_post.py "Quick question" \
  --poll-question "Best agent framework?" \
  --poll-option LangGraph --poll-option CrewAI --poll-duration ONE_WEEK

# Celebration post
python3 bin/linkedin_post.py "New chapter!" \
  --celebration-type CELEBRATE_JOB_CHANGE --celebration-image ./party.png

# Reshare a post, and @mention a member (rewrites @Name in the text)
python3 bin/linkedin_post.py "Worth your time" --reshare urn:li:share:123456
python3 bin/linkedin_post.py "Thanks @Ada Lovelace!" \
  --mention "Ada Lovelace=urn:li:person:XXXX"

# Manage published posts (URNs/URLs from --history)
python3 bin/linkedin_post.py --history
python3 bin/linkedin_post.py --edit-post urn:li:share:123456 "Corrected text"
python3 bin/linkedin_post.py --delete-post urn:li:share:123456

# Schedule a post for later (queued locally; Muse publishes it then)
python3 bin/linkedin_post.py "Tuesday post" --schedule-at 2026-10-06T09:00:00-05:00
python3 bin/linkedin_post.py --list-queue
python3 bin/linkedin_post.py --cancel-queued <id>

# Comment / react (implemented; needs the vetted w_member_social_feed
# scope — with a standard token these return 403 with an explanation)
python3 bin/linkedin_engage.py comment --target urn:li:share:123456 --text "Great post" --dry-run
python3 bin/linkedin_engage.py react --target urn:li:share:123456 --reaction PRAISE
```

## Standalone mode (without Muse)

The same scripts run outside Muse if you supply your own token:

1. In your LinkedIn app's **Auth** tab → **OAuth 2.0 tools**, generate a
   token with scopes `openid profile w_member_social`.
2. Copy `.env.example` to `.env`, paste the token, and export it:

```bash
export LINKEDIN_ACCESS_TOKEN=<your-token>
```

Never commit `.env` — it's in `.gitignore` for a reason.

---

## Repo layout

```
.
├── README.md              ← you are here
├── SKILL.md               ← Muse skill definition (draft; see Part 3)
├── bin/
│   ├── linkedin_api.py    ← shared client: auth, headers, uploads, errors
│   ├── linkedin_profile.py← GET /v2/userinfo (+ --status health check)
│   ├── linkedin_post.py   ← publish / schedule / edit / delete posts
│   └── linkedin_engage.py ← comment / react (scope-gated, see above)
├── .env.example           ← standalone-mode token placeholder
├── LICENSE                ← MIT
└── .gitignore
```

## Security notes

- Credentials are entered only on Muse's secure connect page and held in
  the Secure Credentials Store; the scripts fetch the token at call time
  and never print, log, or persist it.
- The connector only ever calls `api.linkedin.com`, plus the media-upload
  URLs LinkedIn itself returns from `initializeUpload` (on
  `www.linkedin.com`). Those uploads carry the same Authorization header
  as API calls: LinkedIn's upload front end rejects a bare,
  unauthenticated PUT with an HTML 400 page (verified live 2026-10-01).
- Posting is approval-gated by the skill's operating rules: Muse must show
  you the exact post and get your OK before publishing.
- Meta does not review custom connectors — you are trusting this code,
  which is why it's short, dependency-free (Python standard library only),
  and readable end to end.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `401` on any call | Token expired (~60 days) or not attached | Reconnect the connector; verify the request carries the token |
| `403` on profile | App lacks the OpenID Connect product | Add **Sign In with LinkedIn using OpenID Connect** in the Products tab |
| `403` on posting | App lacks `w_member_social` | Add **Share on LinkedIn** in the Products tab, then reconnect |
| `403` on comment/react | Needs `w_member_social_feed` | Apply for the **Community Management API** product; reconnect after approval |
| `426 NONEXISTENT_VERSION` | The pinned `LinkedIn-Version` was retired | Bump `LINKEDIN_VERSION` in `bin/linkedin_api.py` (or set `LINKEDIN_API_VERSION`) to a current YYYYMM |
| Media upload fails in Muse mode | Upload host not declared on the connector | Make sure the connector's API hosts include `www.linkedin.com` |
| `400` (HTML page) on the media-upload PUT | Upload sent without Authorization, or with a default form Content-Type / script User-Agent | Use the current code: it attaches the Authorization header, the file's real MIME type, and a browser-like User-Agent (verified live 2026-10-01) |
| `redirect_uri` mismatch during connect | Redirect URL differs from the app's Auth tab | Set it to `https://agent.meta.ai/api/hatch/oauth/callback` exactly |

## License

MIT
