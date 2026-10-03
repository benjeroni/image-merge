# image-merge

Self-hosted personalized email images (a NiftyImages replacement).
Staff design templates in a web UI. Emails load `https://IMG_HOST/<template>.jpg?name=Ben`.

## For staff (the /admin site)

1. **New template:** give it a name (becomes the link, e.g. `fall-orientation`) and upload a background
   at twice the size it should show in the email.
2. **Edit:** drag on the image to draw the text area, then type the message with `{name}` where the first
   name goes and a version with no name. Pick font, colour and alignment. The preview updates as you go.
   Try the long-name and "(no name)" buttons.
3. **Publish:** it's live immediately. Live templates are locked because emails already sent keep loading
   them. To change one, **Copy** it (drafts can be copied too) and edit the copy.
4. **Constant Contact:** copy the snippet from the live template's page into a Custom code block in the email.
   Constant Contact fills in `[[FIRSTNAME]]` for each contact when it sends.

Names stay in Constant Contact; nobody uploads contact lists here. Keep the First Name field tidy there.
Missing, junk, non-alphabetic or >30-character names get the "no name" message.

**Archive** hides a draft or live template from the lists; **Restore** brings it back. Archiving never
breaks anything: an archived live template keeps serving its image to emails already sent.

**Admins** can also add/remove staff and reset passwords (Users), install fonts (Fonts), and take a live template
offline (this breaks its image in emails already sent).

## Rules the editor enforces

- Background: PNG/JPEG, at least 400px wide (larger is scaled to 1200), 120–1600px tall. Metadata is stripped.
  Anything under 1200px wide gets a warning, because it will show narrower than a full-width (600px) email.
- Message: ≤ 80 characters, `{name}` exactly once, every character must exist in the font.
- Text area: at least 16px from every edge. Text sizes 20–160, line spacing 1.0–1.6.
- Publishing is refused unless the no-name message **and** the message with "Christopher" fit the text area.

## Privacy

The server stores staff usernames and password hashes (scrypt), and nothing else about people.
A recipient's first name arrives in the image request, is used to draw the image in memory, and is never written
to disk or logged (gunicorn access logging is off, and Flask's error log records the path, not the query).
Note that Cloudflare (or any CDN in front) will see and cache image URLs, which include first names.

## Run (Unraid + Traefik)

From the VM:

```bash
./deploy-to-unraid.sh
```

This builds the image on the VM, streams it to Unraid over `ssh unraid`, copies `compose.yml` to
`/mnt/user/appdata/image-merge`, and runs `docker compose up -d` there. `data/` is copied only on the first
deploy (it seeded the admin account); after that, Unraid's `data/` is the real one and is never overwritten.

`compose.yml` joins the `arr` network, and Traefik serves it at https://image-merge.biel.casa
(letsencrypt resolver, `secure-headers@file`, same as unraid-stats). No ports are published.

- `/mnt/user/appdata/image-merge/data` holds everything: `images/`, `fonts/`, `drafts/`, `live/`, `users.db`, `secret_key`. Back it up.
- More admins: `docker exec -it image-merge python admin.py add-user NAME --admin`, or use the Users page.
- `PUBLIC_URL` is the address emails use; it goes into the Constant Contact snippet.
- Cookies are HTTPS-only. For plain-http testing add `COOKIE_SECURE: "0"`.
- `/healthz` returns 500 if a live template can't be served.
- Consider Cloudflare Access on `/admin` so the login page isn't public.

## Develop

```bash
uv run python test_app.py                                          # self-check (throwaway data dir)
COOKIE_SECURE=0 uv run flask --app app run --port 8087             # local dev server
```

Code: `core.py` (templates, validation, drawing; no web code), `admin.py` (staff UI + accounts),
`app.py` (public image route), `pages/` (HTML), `sample/` (seeded as the first live template on a fresh install).
