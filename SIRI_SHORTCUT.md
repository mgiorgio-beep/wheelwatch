# "Hey Siri, Log Catch" — Shortcut Setup

Voice-triggered catch logging via the iOS Shortcuts app. Siri opens the real
camera (full quality, photos land in your camera roll), you shoot the fish and
the Garmin, and the Shortcut posts everything straight to Wheelhouse — no web
page involved. The catch goes through the normal pipeline: EXIF timestamp,
background sonar read, conditions snapshot, crew notifications.

## One-time setup (~1 minute, pre-built shortcut)

1. **Install the shortcut:** open
   https://www.icloud.com/shortcuts/932c3d2875dd4c3295d4ded3fd844575
   on your iPhone (also linked from the **⬇️ Get the Shortcut** button on the
   Log Catch tab) and tap **Add Shortcut**.
2. **Get your token:** in Wheelhouse, Log Catch tab → **🎤 Siri setup** →
   copy the token. Treat it like a password — it lets its holder log catches
   as you (and nothing else).
3. **Paste it in:** open the shortcut in the Shortcuts app → Get Contents of
   URL → Headers → replace `PASTE-YOUR-TOKEN-HERE` with your token.
4. Run it once with ▶ to approve the permission prompts (choose **Always
   Allow** for wheelhouse.rednun.com). Done — "Hey Siri, log catch".

The shared link contains a placeholder token, never a real one. If the
shortcut is ever rebuilt from scratch, re-share a cleaned copy the same way
and update the link above and in `static/fishing.html`.

## Building it manually (reference — what the shared shortcut contains)

(Shortcuts app → + → rename it **"Log Catch"** — the name is the Siri phrase):

1. **Take Photo** — Show Camera Preview: On. *(the fish)*
2. **Save to Photo Album** — input: Photo from step 1. *(permanent copy in your roll)*
3. **Take Photo** again. *(the Garmin screen)*
4. **Save to Photo Album** — input: Photo from step 3.
5. **Get Current Location**.
6. **Get Contents of URL** — `https://wheelhouse.rednun.com/api/shortcut/log-catch`
   - Method: **POST**
   - Headers: add `X-Catch-Token` = *your token*
   - Request Body: **Form**, with fields:
     - `photo` = *Photo* (step 1, as File)
     - `instrument_photo` = *Photo* (step 3, as File)
     - `lat` = *Current Location ▸ Latitude*
     - `lon` = *Current Location ▸ Longitude*
7. *(Optional)* **Show Notification** — input: Contents of URL, so you see the
   `saved: true` confirmation.

Say **"Hey Siri, log catch"** to run it. Add it to the Home Screen or an
Action-Button/Back-Tap binding for a no-voice trigger too.

## Troubleshooting

- **413 Request Entity Too Large**: nginx on the Beelink is missing
  `client_max_body_size 25m;` in the `http {}` block of `/etc/nginx/nginx.conf`
  (its 1MB default blocks full-resolution Shortcut uploads). Re-add it, then
  `sudo nginx -t && sudo systemctl reload nginx`. See the App Improvements
  Brief §1.4 for all Beelink-only config that lives outside this repo.
- **Garmin values missing from a catch**: check the Shortcut's second form
  field — key must be exactly `instrument_photo` with the GarminPhoto variable
  attached; Shortcuts silently drops a File field with no value.

## Notes

- Skipping the Garmin: just cancel the second camera — the POST still works
  without `instrument_photo` (remove the field or let it send empty).
- Species/size aren't asked; edit the catch in the app afterward if you want
  them. The sonar temp/depth/GPS merge in automatically ~30 s after the save.
- Regenerate a leaked token: POST to `/api/shortcut-token` while logged in
  (the 🎤 button fetches; regeneration can be added to the UI if ever needed).
- Endpoint auth: `X-Catch-Token` header → `shortcut_tokens` table →
  delegates to the standard `/log-catch-photo` handler as that user.
