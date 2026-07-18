# "Hey Siri, Log Catch" — Shortcut Setup

Voice-triggered catch logging via the iOS Shortcuts app. Siri opens the real
camera (full quality, photos land in your camera roll), you shoot the fish and
the Garmin, and the Shortcut posts everything straight to Wheelhouse — no web
page involved. The catch goes through the normal pipeline: EXIF timestamp,
background sonar read, conditions snapshot, crew notifications.

## One-time setup (~3 minutes)

**Get your token:** in Wheelhouse, open the Log Catch tab and tap **🎤 Siri
setup**. Copy the token shown. Treat it like a password — it lets its holder
log catches as you (and nothing else). Anyone on the crew can do this same
setup with their own token.

**Build the Shortcut** (Shortcuts app → + → rename it **"Log Catch"** — the
name is the Siri phrase):

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

## Notes

- Skipping the Garmin: just cancel the second camera — the POST still works
  without `instrument_photo` (remove the field or let it send empty).
- Species/size aren't asked; edit the catch in the app afterward if you want
  them. The sonar temp/depth/GPS merge in automatically ~30 s after the save.
- Regenerate a leaked token: POST to `/api/shortcut-token` while logged in
  (the 🎤 button fetches; regeneration can be added to the UI if ever needed).
- Endpoint auth: `X-Catch-Token` header → `shortcut_tokens` table →
  delegates to the standard `/log-catch-photo` handler as that user.
