# Wheelhouse — App Improvements Brief

**Author:** Mike Giorgio (captured via Claude sessions, 2026-07-17 / 2026-07-18)
**Contains:** two projects. Project 1 (Catch Reliability) is the foundation; Project 2 (Quick Catch) builds on it.

## Rollback & coexistence policy (applies to every project)

- **Every project ships as its own commit** (or small commit series) to `main`. Going back to the previous behavior is always `git revert <sha>` → push → autodeploy picks it up within 2 minutes. No project may be entangled with unrelated changes in a way that makes it hard to revert alone.
- **The original photo catch-logging flow is never removed.** New capture flows (Quick Catch) are additive: a normal (short) tap on Log Catch always keeps the existing full logging flow, exactly as it works today. If a new flow doesn't earn its keep on the water, reverting its commit — or simply not using the gesture — restores the status quo.
- Quick Catch additionally gets a **Settings toggle ("Enable Quick Catch long-press")** so it can be turned off per-user without a deploy.

---

# Project 1: Catch Reliability — Never Lose a Catch

**Status:** shipped to production 2026-07-18
**Motivation:** three catch-save attempts failed in July (Jul 11, 12, 17). Root causes found and fixed: (a) slow work (Claude sonar read + live NOAA/ERDDAP conditions fetches) ran inside the save request and blew gunicorn's worker timeout, killing the save after the photo was stored but before the catch entry was written; (b) an upstream NOAA outage on Jul 17 pinned the app's workers so the save never landed at all; (c) a failed save left the catch only in fragile page memory — when the page was recycled, the catch and its photos (taken via the in-app camera, so never in the camera roll) were unrecoverable.

## 1.1 Instant saves + background enrichment (shipped)

`/log-catch-photo` writes the catch entry and responds immediately (~2 s). The two slow jobs — Claude Vision read of the Garmin/instrument photo and the conditions snapshot (ERDDAP/buoy/tides) — run in a daemon thread afterward (`_enrich_catch_async`) and merge into the saved catch file atomically. A failure or timeout there can cost those fields, never the catch. Anthropic client calls are capped (45 s fish ID / 60 s instrument, limited retries).

## 1.2 Stash-on-failure queue + duplicate guard (shipped)

The existing offline IndexedDB catch queue (survives the app closing) now also catches **server-side failures**: any 5xx, killed worker, or non-JSON response (login page, gateway error) stashes the catch — photos included — instead of dead-ending at "Error — Try Again". UI shows "Saved to Phone / will retry automatically". Only 4xx validation errors (photo too large, etc.) still surface as errors, since retrying identical bytes can't succeed.

Duplicate guard: each catch gets a `client_id` (generated at first Save, stable across retries, cleared on form reset). The server scans recent catch entries for the id and acknowledges a re-send with `saved: true, duplicate: true` instead of logging the fish twice. Retries from the queue or a re-tap are therefore always safe.

## 1.3 Queue-drain hardening (shipped)

Draining the queue previously deleted a record on any 2xx response. A logged-out session receives the login page with status 200 — which would have silently discarded a queued catch. The drain now deletes a record only on a **confirmed JSON `saved` response**; anything else keeps the record for the next drain.

## 1.4 Server capacity (shipped, ops-side)

gunicorn moved to explicit threaded workers (`-w 2 --threads 8 --worker-class gthread --timeout 90`) via a systemd drop-in, so stalled external feeds (NOAA outages) can't pin the whole app.

## 1.5 Remaining / feeds into Project 2

- **Draft persistence at capture time:** photos should be written to the IndexedDB stash as soon as they're captured (on the file input's `change` event), not only when Save is tapped — so a backgrounded or recycled page mid-flow can't lose an already-taken photo. This is a Quick Catch requirement (§6 of Project 2) but is equally valuable in the classic flow.
- Optional: surface a small "catch waiting to sync" indicator more prominently (badge exists today).

---

# Project 2: Quick Catch (Long-Press Log Catch)

**Status:** v1 shipped 2026-07-18 — tune hold threshold (400–600 ms) in field testing
**Depends on:** Project 1 (queue + duplicate guard are prerequisites for §6's guarantees; drafts created by Quick Catch use the same stash and `client_id` idempotency).
**Platform context:** Wheelhouse runs as a web app in iOS Safari / installed as a home-screen PWA. All constraints below assume that environment.

## 2.1 Problem

Logging a catch on the boat currently takes too many taps at exactly the moment when the angler's hands are full and wet, the boat is moving, and there may be another fish on. Today's flow after landing a fish:

1. Tap **Log Catch**
2. Tap the **camera** button
3. Take the fish picture
4. Tap the **photo/confirm** button
5. Tap the **Garmin photo** button
6. Take the Garmin (sonar/chartplotter) picture
7. Confirm again

That is 6–7 deliberate taps on small targets. It's hectic and error-prone in real conditions.

## 2.2 Proposed Feature: Quick Catch

**Long-press the Log Catch button** to jump straight into a camera-first capture flow:

1. Angler **long-presses Log Catch** (~500 ms hold)
2. Native camera opens immediately for the **fish photo**
3. Angler shoots, taps iOS's **Use Photo**
4. App shows a single **giant full-screen "📷 Garmin" button** (one tap — see §2.4 for why this tap is required on iOS)
5. Native camera opens for the **Garmin screen photo**
6. Angler shoots, taps **Use Photo**
7. Catch is logged automatically with both photos, timestamped, GPS position captured at the moment of the long-press

Result: **2 in-app taps** (long-press + Garmin button) instead of 6–7, with the rest being the camera shutter/confirm the angler has to do anyway.

A normal (short) tap on Log Catch keeps the existing full logging flow unchanged. **This is a hard requirement (see Rollback & coexistence policy): the original flow must remain fully intact and reachable, and a Settings toggle must allow disabling the long-press gesture entirely.**

## 2.3 Interaction Details

### Long-press gesture
- Trigger threshold: ~500 ms hold (tune between 400–600 ms in field testing).
- Implement with Pointer Events (`pointerdown` / `pointerup` / `pointercancel`) and a timer. Cancel if the finger moves more than ~10 px (it's a scroll/drag, not a hold).
- **Visual feedback is mandatory** — iOS gives web apps no haptics (no Vibration API). Recommended: the button fills radially or pulses during the hold, then snaps/flashes when the threshold is crossed, so the angler can *see* the hold registered even with wet fingers. Optionally play a short sound (audio is allowed since it follows a user gesture).
- Suppress iOS system long-press behaviors on the button:
  ```css
  .log-catch-btn {
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
    touch-action: manipulation;
  }
  ```
  Also call `preventDefault()` on `contextmenu` for the button.

### Camera capture (v1 — native camera)
- Use a hidden file input:
  ```html
  <input type="file" accept="image/*" capture="environment" hidden>
  ```
- iOS requires a **user gesture** to programmatically `.click()` a file input. The long-press **release** (`pointerup`) counts as that gesture — trigger the first `.click()` synchronously in the `pointerup` handler (not after an `await` or `setTimeout`, which can break the gesture chain).
- After the fish photo's `change` event fires, do **not** attempt to auto-open the camera again — iOS will not reliably honor a chained programmatic open (see §2.4). Instead render the full-screen Garmin button; its tap is the user gesture for the second `.click()`.
- Handle the "user cancelled the camera" case: `change` never fires / fires with empty `files`. Detect via a `focus`-return + empty-files check and fall back gracefully (see §2.6).

### Data captured automatically
- Timestamp: at the moment the long-press triggers.
- GPS: fire `navigator.geolocation.getCurrentPosition()` immediately on long-press trigger (in parallel with the camera), so position reflects where the fish was caught, not where the boat is 30 seconds later. Cache last-known position as fallback if the fix times out.
- The catch record is created immediately in a "draft/quick" state and finalized when the second photo lands (or when the flow is abandoned — see §2.6). Drafts live in the Project 1 stash (IndexedDB) with a `client_id` from creation, so retries/finalization are duplicate-safe.
- Species, length, weight, notes, etc. are **not** asked for during Quick Catch. The catch is saved with photos + time + position, and the detail fields can be filled in later from the catch's detail view ("Complete this catch" affordance / badge on catches missing details).

## 2.4 iOS / PWA Constraints (why the flow is shaped this way)

- **Long-press:** fully supported in Safari and standalone PWAs. No platform blocker.
- **First camera open:** allowed, because the long-press release is a genuine user activation.
- **Second camera open cannot be automatic:** iOS Safari requires user activation for each file-input `.click()`; the `change` event from the first photo does not reliably count. Hence the one-tap giant Garmin button. This is the only extra tap in the flow and it should be full-screen and unmissable.
- **No haptics:** Vibration API is unsupported on iOS — visual (and optional audio) feedback only.
- **PWA vs Safari tab:** the native-camera file input behaves the same in both. No difference for v1.
- **In-app camera photos never reach the iOS camera roll.** This is a platform limitation — which is exactly why §2.6's draft-persistence rule (write the photo to the stash the moment `change` fires) is non-negotiable: the stash is the only durable copy.

### v2 option (not for initial build): in-app camera via getUserMedia
A custom viewfinder would allow a true zero-tap chain (snap fish → prompt flips to "now the Garmin" → snap → done). Supported in Safari and home-screen PWAs since iOS 14.3, **but**: standalone PWAs are known to re-prompt for camera permission more often than they should, and capture quality comes from the video stream rather than the full native camera pipeline (worse in low light/glare — bad for fish photos). Revisit only if the v1 flow still feels slow on the water.

## 2.5 UI Notes

- **Garmin button screen:** one button filling essentially the whole viewport, high-contrast, sunlight-readable, labeled with an icon + "Snap the Garmin". Include a small secondary "Skip" text button (bottom) for catches where no sonar shot is wanted.
- Show a thumbnail of the just-taken fish photo in a corner of the Garmin screen as confirmation it saved.
- After the second photo: brief full-screen confirmation ("Catch logged ✓" with both thumbnails, auto-dismiss ~2 s) then return to wherever the user was.
- Consider a subtle hint the first few times ("Tip: hold Log Catch for Quick Catch") so the feature is discoverable.
- **Settings:** "Enable Quick Catch long-press" toggle (default on after rollout; turning it off restores a plain button with only the original flow).

## 2.6 Edge Cases

- **Camera cancelled on fish photo:** abandon quick flow, discard draft, return to previous screen (or offer "Log without photo?").
- **Camera cancelled on Garmin photo:** save the catch with fish photo only; catch detail view shows an "Add Garmin photo" affordance.
- **Skip tapped on Garmin screen:** same as above — save with fish photo only.
- **GPS unavailable/slow:** save with last-known position and flag it; never block the flow on a GPS fix.
- **App backgrounded mid-flow** (call comes in, etc.): persist the draft (photo already captured) so the flow can resume or the partial catch is recoverable — write the fish photo to storage (IndexedDB/upload queue) as soon as `change` fires, not at the end.
- **Offline on the water:** photos and catch data queue locally and sync when connectivity returns — use the Project 1 stash + `client_id`; do not build a second queue.
- **Storage/upload failure:** never silently drop a photo; retry queue + visible error state on the catch.
- **Long-press on a scroll:** movement threshold cancels the hold so scrolling past the button doesn't misfire.

## 2.7 Acceptance Criteria

1. Holding Log Catch ≥ threshold opens the native camera with no intermediate screens; a short tap still opens the normal logging flow.
2. Visual hold feedback is visible before the camera opens; no iOS text-selection/magnifier artifacts appear.
3. After Use Photo on the fish shot, the full-screen Garmin button appears; one tap opens the camera again.
4. After Use Photo on the Garmin shot, the catch exists with both photos, timestamp, and GPS from the moment of the long-press — with zero further input.
5. Cancelling or skipping at any point never crashes and never loses an already-taken photo.
6. Works identically in Safari tab and installed home-screen PWA on current iOS.
7. Works offline; catch syncs later.
8. With the Settings toggle off, the button behaves exactly as before the feature existed.
9. Reverting the Quick Catch commit restores the pre-feature app with no data loss (drafts already in the stash still sync).

## 2.8 Out of Scope (v1)

- getUserMedia custom camera / zero-tap two-shot chain (v2 candidate).
- Auto-pulling data from the Garmin over network/API — the Garmin "photo of the screen" remains the capture method.
- Species recognition or any AI processing of the photos.
- Android/Chrome-specific tuning (flow should still work, but iOS is the target).
