# Changelog

Release notes for everyone who uses Meeting Assistant. The app shows this file in
Settings > Changelog and, after an update, in the What's new card. Edit it freely;
nothing else feeds those views.

How an entry is read:

- Every `## ` heading is one entry: the title, then the date in parentheses.
  Newest first.
- The first word of the title picks the icon: Added, Fixed, Improved, Removed,
  Reworked, and so on. Past tense, user language, no module names.
- Everything below the heading, until the next `## `, is that entry's notes:
  `### ` sub-headings for areas (Recording, Speakers, Settings), `- ` bullets,
  paragraphs. Plain markdown.
- Infrastructure, docs, CI and tooling changes get no entry.


## Added a Home dashboard, a Calendar view, and a redesigned Speakers workflow (2026-09-05)

### Home

- Moving between Home, Calendar, Needs attention, Speakers, and a recording no longer reloads the page
- The Home dashboard shows headline stats, a 12-week meeting load chart, a when-you-meet heatmap, a 14-day activity chart, and the people you meet with most, sorted by how much they speak
- Charts fill the space available and repaint as the window changes size
- Ask your meetings lives in a rail beside the views: docked on wide windows, overlaid on narrow ones

### Calendar

- The Calendar view shows scheduled meetings next to their recordings, fed by a published Outlook calendar link (Settings > Calendar)
- Recordings are matched to calendar events by time, the attendee count caps how many speakers a reanalysis looks for, and attendee names are offered when you clean up speakers
- The calendar link is stored like a password and only ever shown masked

### Speakers

- The Speakers dialog opens on Cleanup, which lists the calendar invite's attendees ahead of the Voice Library; the separate Resolve tab is gone
- A Needs attention page lists every recording that still has unnamed speakers, one click from cleanup
- Ask your meetings can plan, confirm, and apply bulk speaker renames across recordings

### Sidebar and settings

- Page links sit at the top of the sidebar; pick which ones show with the small edit button, and click the line under them to fold them into icons beside the app name
- Settings > Icons swaps the app, tray, and shortcut icons for your own set
- Settings > System gains recording reliability options: follow call audio to its output device, and an external freeze watchdog that restarts a hung app
- Optional Obsidian export writes finished meetings into a vault
- In-app dialogs and toasts replace browser alerts

### Recording

- A recording that captures only silence raises an alert instead of failing quietly
- Auto-detected meetings start in the window that is already open instead of opening a second one
- Idle models unload to free memory and reload on the next recording
- Fixed the desktop device: the device you select is always the device captured, even when Windows reports a different default output
- Fixed pausing and resuming a recording corrupting the per-speaker audio tracks
- Fixed a preference saved in one tab being undone by another open tab
- Fixed restarting or updating from the tray sometimes not bringing the app back

### Launching
- Opening Meeting Assistant from the Start Menu no longer shows a console window: the app starts in the system tray and its window opens once it is ready
- The sign-in (start with Windows) shortcut starts the app in the tray the same way, and an existing one is moved over on the next launch
- A first run still shows the installer in a console so its progress is visible
- If a hidden start fails, a message points at the launch log instead of waiting forever for a key press

### Changelog
- Release notes now come from CHANGELOG.md in the project, so they can be written and corrected freely; the notes for the previous update read properly again

## Improved how quickly recordings stop and sidebar items move (2026-08-28)

### Stopping a recording

- Stopping a meeting now finishes almost immediately. The button used to sit on "Stopping" while the app converted the separate you and everyone-else audio tracks; on a 34 minute meeting that was about 28 seconds, and 27 of them were that conversion alone.
- The conversion now runs quietly in the background once the meeting is already closed out, and both tracks convert at the same time instead of one after the other, so it also finishes in about half the time.
- Starting or resuming a recording right after stopping one no longer waits on that background work, and cannot collide with it either.
- The log now records how long the conversion took, so a slow save is easy to spot.

### Sidebar drag and drop

- Dragging meetings and folders in the sidebar now moves them the moment you drop, instead of snapping back until the save and a full sidebar reload had finished. Covers reordering, moving into a folder, and moving back out.
- If the save fails, the sidebar reloads from your library and says the new order could not be saved, so it never leaves an arrangement on screen that was not actually stored.

## Made manual speaker corrections stick during a recording (2026-08-28)

### Speaker names during a meeting

- When the app puts the wrong name on someone and you correct one of their recent lines, the correction now sticks: everything that voice says from then on carries the name you chose, instead of coming back wrong segment after segment for the rest of the meeting.
- The status bar confirms when a correction starts sticking. To undo it, reassign any new line back to the original speaker. Corrections last until the recording stops.
- Correcting older lines still changes only those lines, so tidying up earlier parts of a transcript never changes who new speech is credited to.
- Once you have corrected a speaker by hand, voice matching stops suggesting or applying names for them and any pending "sounds like" prompt for them clears. Their speech can still strengthen the voice profile of the person you picked, but only on a confident match.

### Fixes

- Fixed the weekly Voice Library cleanup giving up for the rest of the session if a run ever hit an error.
- Cleared leftover speaker-matching state that could carry into a new recording or survive a re-analysis.

## Improved speaker naming in long meetings, added Voice Library cleanup (2026-08-28)

### Speaker naming during meetings

- The rule that stopped one person from being matched to more than one detected speaker is gone. Long meetings often re-detect the same voice as a "new" speaker partway through; those now pick up the right name automatically instead of piling up as unnamed Speaker 12, 13, 14.
- Two smarter auto-naming checks: a match that clearly beats every other candidate now applies on its own, and so does a profile that keeps winning several checks in a row. Replayed against ten hand-corrected meetings, correct automatic naming rose from 24% to 69% of speech time (second halves of meetings: 13% to 64%) and manual speaker fixes per meeting dropped from about 15 to 6.
- Voice profiles now only learn from high-confidence matches, so a borderline match can no longer slowly pull a profile toward someone else's voice.
- Set speaker_link_v2 to false in settings.json to restore the old matching behavior exactly.

### Voice Library health

- New Health tab in the Voice Library shows duplicate profiles, voice samples filed under the wrong person, profiles that contain two different voices, and pairs of people whose voices sound alike.
- Run Cleanup Now merges same-name duplicates, removes misfiled samples, strips out second-voice pollution it can attribute, and re-tunes every profile from its remaining samples. Anything ambiguous is reported for review, never auto-fixed.
- The same cleanup also runs automatically about once a week while the app is idle (toggle in the Health tab, or the library_maintenance settings). It never runs during a recording.

## Added an Agent API so AI assistants can work with your meetings (2026-08-27)

### Agent API

- Claude Desktop, Claude Code, Codex, and your own scripts can now read your library directly: search, transcripts, summaries, notes, chapters, speakers, video frames, and audio clips.
- Set it up in Settings > Agent API. One click writes the config for Claude Desktop, Claude Code, or Codex, and copyable snippets are there for anything else.
- Optional access token, plus an off-by-default switch for letting agents start and stop recordings. Everything stays on this machine and nothing can be deleted.

### App logs

- The app now keeps a rolling log file, so problems can be traced after the fact.

### Speed and accuracy

- Faster transcript and speaker lookups on large libraries.
- Speaker session counts now follow segments you have reassigned.
- The dashboard counts named participants instead of raw voice clusters.

## Added folder, date, and speaker filters to Global Chat search (2026-08-25)

### Global Chat search

- Ask about a project, team, or client and the chat searches just that folder, including anything nested inside it.
- Narrow by date ("last week", or an explicit range) or by who took part in the meeting, and combine all three filters in one search.
- New List Folders tool lets the chat match your approximate wording to a real folder; if it matches more than one it asks which you meant instead of guessing.
- A search that comes back empty gets loosened one step at a time (any term instead of all, a looser topic match, a wider date range) and the chat says what it had to broaden.

### Quote attribution

- Search results now show who said each matching line and how far into the meeting it was, so the chat can attribute quotes to the right person.
- Results carry the folder's full path, so answers keep project context.

### Chat panel

- Tool chips spell out the scope of a search, e.g. "kickoff" in Engineering, last 7 days.
- Code blocks in replies wrap instead of running off the side.

### Search index

- Fixed the index being thrown away and rebuilt from scratch on every launch; it now persists between runs.
- Leftover entries from re-analysed or deleted meetings are cleared at startup, and clearing a transcript or deleting a folder now removes its entries too. These used to show up as hits that led nowhere.
- One busy meeting can no longer crowd every other meeting out of the results.

### Speed

- Describing a page of results takes a handful of queries instead of loading the full transcript of every hit.

## Improved audio device persistence and chapter headings (2026-07-19)

## Added AI Chapters that mark the key topics across a meeting (2026-07-06)

### Chapters

- Chapters mark the high-level talking points across a meeting and can auto-generate as the recording goes, spaced out so they don't pile up.
- They show as hoverable ticks on the playback bar, as bold headings inline in the transcript, and as markers on the transcript timeline. Click any of them to jump straight to that moment.
- The Summary and Chat now receive the chapter outline as extra context.

### Chapters manager

- A new Chapters button in the transcript header (between Speakers and Auto) opens a place to review and edit them: rename a title, add one at the current playback point, delete, or regenerate the whole set.
- A Tuning tab controls whether chapters auto-generate, how coarse or fine they are, and their system prompt, just like Summary and Chat.

### Export

- Chapters are included when you export a meeting (on by default) and restored when you import one.

### Notes

- Rich-text toolbar icons now use the accent color on hover and when active.

## Added chat folder context, voice-ranked speakers, and recording updates (2026-06-30)

Chat grounding

- Replaced the chat paperclip and folder buttons with one toolbox menu for file attachments and local folders.
- Persisted selected folders per session and restored them after refresh while keeping folder access revalidated.
- Added multi-folder chat tools for codebase overview, file listing, targeted reads, globbed search, file info, and bounded read-only shell inspection.
- Improved search and large-file reads with context windows, file-only results, per-file limits, and clearer tool errors.

Speaker workflow

- Ranked speaker pickers and fingerprint suggestions by voice similarity and showed match percentages.
- Used the ranked picker for bulk reassignment and kept it open while selecting more speaker segments.
- Added a No action that suppressed bad fingerprint suggestions for the meeting.
- Fixed suggestion popouts so they stayed unclipped, flipped within the viewport, and closed cleanly.
- Tidied the split Apply menu so the chevron and main action read as one control.

Sidebar and recording

- Kept folder navigation pinned while searching the sidebar, including drop targets for filing meetings.
- Added smooth auto-scroll while dragging meetings or folders near the top or bottom of the sidebar.
- Added optional auto-start recording for detected meetings, with a confirmation toast and Stop recording action.

## Stop the speaker picker popout being clipped and tidy its dropdown button (2026-06-30)

### Popout no longer cut off

- The candidate picker in the suggestions panel opened as an absolutely positioned layer inside the panel, which has overflow:hidden and a scrollable list, so the menu was clipped at the panel edge.
- It now opens as a viewport-fixed layer positioned against its toggle button, so it escapes the clipping. It prefers opening upward and flips below when there isn't room, and stays clamped inside the viewport.
- It closes on an outside click, on scroll, on resize, when a candidate is picked, and whenever the panel re-renders, with its listeners cleaned up.

### Dropdown button

- Apply and the chevron now read as one split button: Apply keeps its left radius, the chevron sits flush against it in matching green with a thin divider, and the chevron is centered instead of floating off to one side.
- The chevron rotates to point up while the picker is open.

## Keep folders pinned at the top during search and auto-scroll the sidebar on drag (2026-06-30)

### Folders stay visible while searching

- Searching no longer replaces the whole sidebar with a flat result list. The folder tree now stays pinned at the top as a compact, headers-only strip, so folder navigation is never lost mid-search.
- The strip is sticky, so the folders stay put even as you scroll down through the results below them.
- Each pinned folder still accepts dropped sessions (drop a result onto a folder to file it). Clicking a pinned folder leaves search and reveals that folder, expanded, in the normal tree.
- Filtering and sorting already keep folders at the top (the folder tree renders above the loose sessions); that behavior is unchanged.

### Auto-scroll while dragging near an edge

- Dragging a meeting (or folder) up toward the top of the session list now smoothly auto-scrolls it, so you can reach folders and drop positions that are scrolled out of view. The bottom edge scrolls the same way.
- Speed eases with how far into the edge zone the pointer is, and a requestAnimationFrame loop keeps it scrolling even when you hold the pointer still in the zone.

## Add a "No" reject and a ranked speaker picker to fingerprint suggestions (2026-06-30)

### Suggestion picker

- The suggestion toast and notification cards now expand into a similarity-ranked list of candidate profiles (top match first), reusing the transcript speaker-picker look with a percentage badge per row.
- The backend sends a fuller ranked candidate list with each suggestion (top 8, including sub-threshold), so the dropdown shows real alternatives instead of only the one or two that crossed the suggest threshold.

### "No" / reject

- Added a "No" action to both surfaces. It marks the profile as not in this meeting and suppresses it as a candidate for every speaker, not just the one shown, so it stops re-suggesting on other diarizer fragments of the same voice. Any pending cards pointing at that profile are cleared.
- The reject is session-scoped on the server (matching only runs live, so it covers the meeting it was raised in). It does not delete the saved voice profile, which would degrade that person for future meetings; use the Voice Library panel to prune individual samples if needed.

Also drops the em dashes from comments/labels added in the prior two commits.

## Show a voice-similarity badge on each speaker-picker suggestion (2026-06-30)

- Each ranked option now carries a small percentage on the right showing how close that voice is to the clicked speaker, so the ordering is self-explanatory.
- Color tiers match the cleanup picker: muted below the suggest threshold, accent above it, green once it crosses the auto-match threshold.
- The badge only appears after voice data loads and only on entries with a comparable voice sample; everything else just shows the name as before.

## Rank speaker-picker suggestions by voice similarity and use it for bulk reassignment (2026-06-30)

### Speaker picker

- Meeting speakers and Voice Library suggestions are now ordered by how close each voice is to the clicked speaker (most-alike first). Meeting speakers stay grouped above the library; both groups are sorted.
- Ordering snaps in once voice data loads and falls back to the previous order when no voice samples are available (model not ready, etc.).

### Ctrl-click reassignment

- Ctrl/Cmd/Shift-clicking speaker pills now opens the same speaker-picker widget used for renaming, instead of the bottom selection bar.
- The picker pops out to the right of the clicked pill so the pills below it stay visible and clickable for adding more segments to the selection.
- Picking a name (or Mark as Noise) reassigns every selected segment, with the same backend behavior as before. The picker stays open while you keep ctrl-clicking; a plain click elsewhere or Escape clears the selection.

Voice data for ranking comes from the same per-session analysis the cleanup tab uses, cached so repeat opens don't re-fetch.

## Added a macOS-styled app icon (squircle tile with the standard grid margin, a dark gradient, rim light, and the brand mark with a soft drop shadow) and used it for the Applications bundle instead of the bare full-bleed web logo (2026-06-27)

## Installed the macOS launcher app into /Applications when it is writable (falling back to ~/Applications), so it shows up in Launchpad and the Apps grid where users expect, and removed the stale copy from the other location - Built the app icon as a proper multi-resolution icns via an iconset and iconutil, since the previous single sips conversion produced a blank icon - Registered the bundle with Launch Services after creating it so it is indexed without a manual refresh (2026-06-27)

## Cleared any x86_64-poisoned uv interpreter cache once per venv on macOS, so a tag set cached by a prior Rosetta run no longer forces x86_64 wheels (which left mlx unsatisfiable and pinned torch to its last x86_64 build) even on a native arm64 launch (2026-06-27)

## Re-executed the macOS launcher as native arm64 when it is running under Rosetta on Apple Silicon, so uv resolves arm64 wheels instead of x86_64 (which left mlx unsatisfiable and pinned torch to its last x86_64 macOS build) - Added the same arm64 guard to launch.py as a safety net for the direct python launch.py and in-app relaunch paths (2026-06-27)

## Added a Meeting Assistant launcher to the macOS Applications folder (2026-06-27)

- On macOS the launcher now creates a Meeting Assistant.app in ~/Applications, mirroring the Windows Start Menu shortcut, so the app shows up in Launchpad, Spotlight, and Finder.
- The bundle also gives macOS a stable app identity for the Screen Recording and Microphone permissions, so they are attributed to Meeting Assistant instead of to Terminal. It is ad-hoc signed and self-heals if the project folder is moved.

## Added a one-line installer that pulls from GitHub, sets up, and launches (2026-06-27)

- New install.sh (macOS/Linux) and install.ps1 (Windows) clone the repo from GitHub and hand off to the launcher, which installs uv, Python, the virtual environment, and the models, then starts the app with no further steps.
- macOS:  curl -fsSL https://raw.githubusercontent.com/TyLaneTech/Meeting-Assistant/main/install.sh | bash
- Windows (PowerShell):  irm https://raw.githubusercontent.com/TyLaneTech/Meeting-Assistant/main/install.ps1 | iex
- The only prerequisite is git (plus Homebrew on macOS, which the installer uses to add a native ffmpeg). The install location defaults to the home folder and can be changed with MEETING_ASSISTANT_DIR. Documented in the README, and a .gitattributes keeps the shell scripts LF so they run on macOS.

## Shortened the ffmpeg line in the startup log so it does not crowd the output (2026-06-27)

- The startup log now shows just the ffmpeg version token and a project-relative path, instead of the full version banner ("Copyright ... the FFmpeg developers") followed by the full absolute path. A system ffmpeg found on PATH still shows its full path so you can tell where it came from.

## Fixed GPU detection on newer NVIDIA drivers so CUDA isn't mistaken for CPU (2026-06-27)

- A recent NVIDIA driver (610+) renamed the line that nvidia-smi prints from "CUDA Version" to "CUDA UMD Version", which made the launcher miss the GPU and report "No accelerator detected -- CPU mode". The launcher now recognizes both labels, so your GPU is detected again and the CUDA build of PyTorch is kept instead of being treated as CPU.

## Added macOS support, plus cross-platform audio cleanup and sharper transcription (2026-06-27)

### macOS support

- Meeting Assistant now runs on Apple Silicon Macs (macOS 13+). System audio is captured with Apple's ScreenCaptureKit, so there is no driver to install: just grant Screen & System Audio Recording the first time you record.
- The menu-bar icon, in-app Restart and Update, screen recording, microphone capture, and live transcription all work on macOS now, matching the Windows feature set right down to "mic is you" speaker labeling.

### Cleaner microphone audio (Windows and macOS)

- Echo cancellation now runs on the raw microphone before auto-gain, so other people's voices coming from your speakers are removed instead of being amplified back in.
- Added a separate Noise Suppression toggle that can quiet steady background noise on its own. Both live in Settings under Echo Cancellation.
- When echo cancellation or noise suppression is on, the mic auto-gain steps aside so the cleaned signal is not re-boosted. Use the Mic gain slider to set your level.

### Sharper speaker attribution

- Desktop audio that leaks into your microphone is no longer transcribed as you. A self-calibrating gate drops microphone segments that are just bleed from your speakers, with adjustable strength in Settings.

### Cleaner transcripts

- Whisper's repeated stuck-character runs and stray closed-caption credits are now filtered out, so they no longer show up in your transcript.

### Smaller fixes

- The spacebar no longer ends a recording.
- A microphone that was renamed or reconnected is matched again automatically instead of silently recording nothing.
- The audio visualizer fades out smoothly when a recording stops instead of freezing on the last frame.

## Added a playback volume control, plus a startup fix for transcription not loading on some machines (2026-06-25)

### Volume control for playback

- A speaker button now sits next to the playback scrubber. Hover or focus it and a volume slider slides out, so you can set how loud a recording plays without nudging the other controls out of place.
- Click the speaker to mute, and click again to jump back to your previous level. The icon shows at a glance whether you are muted, quiet, or loud.
- Playback opens at half volume (recordings are normalized fairly hot, so full scale is louder than you usually want), and whatever level you pick is remembered for next time.

### Fixed transcription failing to start on some machines

- A few setups finished setup saying "All models cached" but then could not start transcription, showing an error about not finding an appropriate cached snapshot folder.
- That happened when the speech model was only half downloaded, or was present only in a different cache than the one the app reads while running offline. Startup now confirms each model is fully present in the exact place the app loads it from, and re-downloads it if anything is missing.
- It also catches models left as cloud-only placeholders, such as OneDrive "files on demand", which look present but are not actually on the disk yet.

## Added a saved-speaker picker to the import name prompt (2026-06-24)

### Naming an imported recording's speaker

- When you import a recording whose microphone is still labeled "You", the name prompt now also offers a "pick from your saved speakers" dropdown, so you can choose someone already in your voice library instead of retyping their name.
- Your own "You" profile is left out of the list, since whoever recorded a meeting you received is never you.
- Picking a speaker fills in their name, which you can still edit before saving; the meeting is then labeled with that name.

## Added name prompts on export and import so a shared recording never just says "You" (2026-06-24)

### Naming the microphone speaker when sharing

- When you export a recording whose microphone speaker is still the default "You", the app now asks for your name first, so whoever you send it to can see who was actually talking. You can still choose to export it as "You" if you prefer.
- Naming yourself here applies everywhere, the same as renaming your "You" speaker from the transcript, so you only have to do it once and all of your recordings update.
- When you import someone else's recording and their microphone is still labeled "You", the app now asks who recorded it and labels the transcript with their name. This only changes the imported meeting, never your own "You" identity.
- The prompt only appears when there is actually mic audio still under the default label; recordings you have already named, and meetings with no microphone audio, are left alone.
- Skipping either prompt leaves the recording labeled "You", exactly as before.

## Added "this is you" mic labeling and a reworked Speaker Cleanup, plus speed and smoothness gains (2026-06-23)

### Your microphone is now "you"

- Your own microphone audio is always attributed to a single "You" speaker and tagged with a "(You)" badge in the transcript, instead of being split into anonymous "Speaker 1/2" labels or mixed up with the other people on the call. (Windows)
- Only the meeting's other audio is split apart by speaker now. Your voice is kept out of speaker matching, clustering, and the Speaker Cleanup tab entirely, so you can never be mistakenly matched to someone else.
- A first-run prompt lets you say who you are: type your name, or pick an existing saved speaker as you. It is optional, dismissable, and remembered so it does not ask again.
- You can rename your "You" speaker any time by clicking your name in the transcript, and the new name applies everywhere, even to segments already captured mid-recording.
- A new Settings option, "Microphone is you," turns the behavior on or off and shows who you are currently set as.
- Re-running speaker analysis on a past recording keeps your mic attributed to you, and older recordings made before this feature still work by falling back to the previous combined-audio method.
- Importing someone else's shared recording keeps their microphone as their own identity instead of folding it into your "You."

### Speaker Cleanup, reworked

- Select several voices at once (click, Ctrl-click, or Shift for a range) and assign, group, play, or mark them as noise together from a floating action bar.
- Assign a group to a saved voice with a searchable picker that ranks your library by voice similarity, shows how many voiceprints each profile has, and lets you create a new named voice on the spot.
- Unnamed groups show a one-tap "Sounds like..." suggestion with a confidence score you can accept or dismiss.
- A new voice-similarity heatmap compares every group against the others; hotter cells flag likely same-person voices, and clicking one selects both groups so you can merge them.
- Expand a voice to see a mini timeline of when they spoke plus their actual transcript snippets, each with a play button, and a "play all" that plays their clips back to back (with the screen-recording video following along when it is open).
- Dragging now auto-scrolls at the top and bottom edges and shows how many voices you are moving; the grid lays speakers out in multiple columns and the panel widens for more room.
- Clearer on-screen guidance, and the auto-match button is renamed "Auto-assign" with a tooltip explaining it assigns every group whose best library match is highly confident.
- The app now warns you before applying a merged group that has no name or profile, instead of quietly splitting it back apart.
- Fixed the Cleanup tab sometimes showing the previous session's speakers after switching sessions, and the floating video preview opening off-screen when its saved position no longer fit the window. The preview now shows a friendly prompt when idle and a clear message when a session has no screen recording.

### Smoother and faster

- Opening a session now shows the Summary and Chat right away instead of waiting for the whole transcript to draw first; the transcript streams in behind them with a gentle loading shimmer.
- Live AI summaries and chat replies stream in much more smoothly, without the stutter that came from redrawing the whole text on every word.
- Dragging the transcript time-range filter, scrolling the transcript, and resizing the window are all smoother, especially on long meetings.
- The audio visualizers now rest when there is no sound, and when the app is in a background tab, instead of animating non-stop, so the app uses less CPU and battery while it is just sitting open.
- The home page appears a little sooner.

### Reliability

- During live recording, silent stretches on your microphone are skipped instead of being run through transcription, cutting down spurious or empty lines when you are not speaking.
- After an update or restart, the app stops waiting after about two minutes and tells you to refresh once the server is back, instead of waiting forever; a background search-readiness check on the home screen no longer runs indefinitely.
- Speakers marked as noise no longer clutter the speaker initials shown next to a session in the list.

## Added meeting auto-detect and smoother video playback, plus copy, notification, and network refinements (2026-06-17)

### Auto-detect meetings

- New opt-in setting: when a Zoom or Teams meeting starts and nothing is recording, the app offers to record it with a notification, so you never forget to hit record. (Windows)
- A meeting is spotted by the microphone being held by Zoom or Teams (this works even while you're muted), or by a Zoom meeting window being open.
- It nudges you only once per meeting, never interrupts while you're already recording, and waits out an adjustable cooldown between prompts.
- Off by default. Turn it on under Settings → Reminders, where the cooldown control lives too.

### Smoother video playback

- The screen-recording video now stays locked to the audio as it plays, gently matching speed instead of constantly re-seeking, so it no longer drifts out of sync, stutters, or loops a short clip on its own.
- Seeking is quicker and lands closer to where you click: new recordings save frequent keyframes, so the browser no longer has to decode from far away.
- Fixed playback getting stuck after you drag and release the scrub bar.
- The floating preview player in Speaker Cleanup uses the same locked sync, so it stays glued to the clip you're auditioning.

### Copy summary

- The summary Copy button now opens a small menu so you can copy with or without the [M:SS] timestamps.
- A quick green check confirms the copy.

### Notifications

- "Not now" on the meeting-detected alert and "Keep recording" on the quiet-recording alert now simply close the notification instead of opening a page.
- "Stop recording" still stops the recording and opens the session.

### Corporate networks

- Automatic Cloudflare WARP disconnecting is now off by default. Now that the app verifies against your computer's certificate store (added last update), it no longer needs to toggle your VPN around AI replies, model downloads, or update checks, so your WARP connection is left untouched.
- A new "Auto-toggle Cloudflare WARP" switch under Settings → System → Network brings the old behavior back if you ever need it.

## Added a Speaker Cleanup tab, plus mic, notification, and network fixes (2026-05-29)

### Speaker Cleanup

- New Cleanup tab in the speaker editor (next to Manage) for sorting out who said what after a meeting.
- Speakers show up as cards: people already matched to your voice library, plus unrecognized voices grouped by how alike they sound.
- Drag a speaker onto another card to merge them, or drop on "+ New cluster" to split one off on its own.
- "Sounds like..." suggestions come from your saved voices; Assign accepts a match, "Not this" dismisses it, and Apply confident takes every high-confidence match at once.
- Click a speaker to hear sample clips, and mark stray or non-speech voices as Noise to tuck them out of the way.
- Nothing changes until you click Apply; Reset and an unsaved-changes prompt protect your edits, and applying teaches your voice library so future meetings recognize people better. Works on older recordings too.

### Recording preview

- Optional floating video player while you clean up, so you can see the moment a speaker was talking.
- Drag it anywhere, resize from the corner, and scroll to zoom or drag to pan; it remembers where you left it.

### Microphone reliability

- Recordings are much more dependable: before each recording the app re-checks your selected mic against the devices currently connected, so a renamed or unplugged mic fails with a clear error (or retargets automatically) instead of silently recording nothing.
- Fixed a clash where the audio test could still be holding the mic when a recording started, which could leave the recording with no sound.

### Audio mixing

- Reworked how your microphone and the meeting audio are combined so your voice is always captured, even when the other side is loud or the desktop is silent (both could previously drop your mic).
- Fixed a bug that was throwing away most of the microphone audio.

### Playback

- After a stop and resume, the playback bar now shows the correct length right away instead of needing a page refresh.

### Notifications

- Rebuilt Windows toasts so they reliably show up in Windows 11 and stay in the Action Center.
- The "things have gone quiet" alert now has working Stop recording and Keep recording buttons.
- Added a Test Toast item to the tray menu to confirm notifications work on your machine.

### Corporate networks

- AI replies and model downloads now work behind Cloudflare WARP and similar corporate SSL inspection, verifying against your computer's certificate store instead of switching certificate checks off.
- No .env tweaking needed: it works whether WARP is connected or not, and connections stay properly secured.

### Faster startup

- The app loads its transcription and speaker models straight from the local cache, skipping online checks at launch for a quicker, more reliable (and offline-friendly) start.

## Added Notes pane, Changelog tab, and folder-aware sidebar filtering (2026-05-05)

### Notes pane

- New rich-text Notes column alongside Transcript, Summary, and Chat — with formatting (bold, italic, lists, quotes, code, colors, alignment).
- Drop or paste images and files anywhere in the pane. Files render as inline chips; images embed and can be resized via corner handles.
- Drag images and chips between paragraphs to rearrange.
- Drag any image or file from Notes into the Chat panel to attach it as context for the AI.
- Notes auto-save and travel with sessions — included in export zips and restored on import with their attachments intact.
- Toolbar collapses the alignment buttons into a dropdown when the column is narrow and re-expands when there's room.

### Settings: System Prompts

- New tab housing the global Chat, Summary, and Session Title prompts as collapsible sections (collapsed by default). A "Custom" chip appears on each header you've edited.
- Session Title prompt is now editable — the AI follows your wording when naming new recordings and Global Chat conversations.
- One Save button commits all three; prompts that match the built-in verbatim are stored as empty so future built-in updates flow through.

### Settings: Changelog tab

- New tab pinned to the bottom of the settings nav. Lists recent updates parsed from git history, grouped by date with sub-headings and bulleted summaries.
- Cached locally; only rebuilt when you click Refresh or after an update is applied — no startup or polling cost.
- Each entry gets a category icon (feature / fix / improvement / refactor / removal) for quick scanning.
- Header redesigned with a logo accent bar, mono short-hash chip, and a status dot showing whether the list is cached or just-refreshed.

### What's New popup

- After every update, the next page load shows a one-time popup featuring the latest commit. Clean modal with the app logo, a soft category-tinted hero, the headline, and the same parsed bullets used in the Changelog tab.
- "Got it" dismisses; "View full changelog" jumps straight to the Changelog tab.
- Suppressed during recording so it never interrupts a live meeting.
- Settings → Changelog has a "Preview popup" button to demo the popup on demand.

### Sidebar

- Filtering by date, duration, speaker, etc. now preserves your folder hierarchy. Folders with at least one matching session stay visible with their children nested inside; non-matching folders are hidden until the filter clears.
- Folder expand/collapse state is preserved across filter changes.
- "Clear filters" + closing the popover now also drops the saved- default filter, so the sidebar opens unfiltered next launch.
- OS files dropped anywhere on the page while Notes has focus go straight to Notes — the session-import overlay no longer steals the drag.

### Pane toggles

- The topbar pane toggle group reorders correctly when you drag columns. The three positional icons always toggle whichever non- Notes column is at that visual position; the Notes button slots in based on where the Notes column sits. Tooltips update accordingly.

### OpenAI Responses API for summarization

- OpenAI summaries now use the Responses API with structured output, replacing the legacy Chat Completions path. Anthropic models are unchanged. The model picker no longer lists OpenAI "Pro" variants.

### Whisper transcription

- Detect and clean the per-word-period failure mode where Whisper starts emitting "Word. Word. Word." indefinitely; previously the polluted text fed back as prompt context and snowballed.
- Per-speaker prompt context — one speaker's bad output can no longer poison another's transcription. Speaker merges in the diarizer mirror through to the transcriber.

### Other

- Status-pill messages also log to the JS console (with [status] prefix), so debug messages aren't lost when the pill flashes by.
- Global Chat tool widget renders saved tool calls correctly when loading a past conversation, matching parallel results to their calls by id.
- Both AI provider API key fields are always visible in Settings; legacy per-tool override controls removed in favor of inline column pickers.

## Live-summary customization, OpenAI Responses API, settings dedup (2026-05-04)

### Summary system prompt

- Per-session summary system prompt override (new sessions table column, /api/sessions/<id>/summary-prompt routes, AIAssistant.summarize accepts system_prompt that overrides the built-in _SYSTEM_SUMMARY).
- Resolution at run time: session > saved global > built-in. Stored empty global means "follow the built-in", so future built-in updates flow through transparently.

### Custom Instructions

- Always honor _state["custom_prompt"] in /api/summarize and _queue_speaker_summary_refresh, even when summarizing a non-active session (textarea POSTs apply to whichever session is being viewed).
- Append custom_prompt to patch_summary's system prompt as well as the user prompt, so instructions appear consistently across initial and incremental runs.
- "Use as default for new sessions" toggle persisted via new summary_default_instructions pref; loadSummaryPrompt seeds new sessions from it. newSession() now re-runs the seed instead of clearing the textarea.

### OpenAI Responses API for summarization

- summarize() routes OpenAI through new _stream_openai_responses (responses.stream with instructions=, input=); Anthropic stays on messages.stream.
- _complete_structured (patch_summary) uses responses.create with a strict json_schema text format mirroring _PATCH_TOOL.
- Filter out -pro OpenAI variants from the picker.

### Settings widget

- Renamed "AI Assistant" tab to "AI Providers"; "Provider" label to "Default Provider"; description notes per-tool overrides exist below.
- New "System Prompts" tab housing the global Chat and Summary defaults with a single Save button (no auto-save). Pre-populates the textarea with the built-in text so users always see the live default; Save stores empty when textarea matches built-in verbatim.
- Both API key fields are always visible (removed provider-toggle hide).
- Source chip in the inline summary widget collapses to "Default" / "Session override" — no more "Global" vs "Built-in" distinction.

### Template dedup

- Extracted shared sidebar markup into ui_web/templates/_sidebar.html and the settings overlay into _settings.html. home.html and index.html now {% include %} both. Home picks up Reminders tab + System Prompts tab + model refresh button; legacy tool-overrides-group dropped.

### CSS fixes

- Added missing --yellow to the dark theme; removed broken "--red-muted: var();" line; added --red-muted to the light theme for parity.
- Styling for the redesigned summary-prompt-area (tabs, panes, source chip, default-toggle, ghost/primary buttons, save bar).

## Self-heal Start Menu shortcut when target/args/wd/icon drift (2026-05-01)

## Fix Whisper TypeError on TranscriptionInfo and improve error logging (2026-05-01)

faster-whisper changed TranscriptionInfo from a NamedTuple to a dataclass, so _asdict() is missing and dict(info) raises TypeError, killing every live transcription. Fall back to info.__dict__ for dataclass-style objects.

Also surface Whisper exceptions inline (type, message, audio duration, VAD flag, device/compute_type) and route the traceback through the same logger, so failures aren't silent when stderr is hidden by the launcher.

## Add macOS support, reorganize into packages, consolidate storage layout (2026-04-30)

macOS Apple Silicon port:
- Platform-dispatched audio capture (capture_audio: WASAPI on Windows, BlackHole + AVFoundation on macOS via mac_bootstrap)
- Platform-dispatched screen recording (capture_video: gdigrab on Windows, AVFoundation on macOS)
- mlx-whisper engine backend for Metal/MPS (transcriber_engine factory selects faster-whisper or mlx-whisper per platform)
- compute_device.best_torch_device() as the single source of truth for cuda/mps/cpu selection across diarizer, transcriber, and batch transcriber
- Mac-aware notifications (osascript) and tray (pystray)
- requirements-macos.txt for arm64 torch + pyobjc + mlx-whisper; launch.py picks the right requirements file per sys.platform
- launch.command for macOS

Folder reorganization (flat -> 7 packages):
- ai/ (assistant.py)
- capture_audio/ (windows.py, mac.py, mac_bootstrap.py, wav_writer.py, params.py, audio/ for bundled MP3s)
- capture_video/ (windows.py, mac.py, ffmpeg_util.py, media_edit.py)
- core/ (log, config, paths, settings, network, compute_device, storage)
- ml/ (transcriber, transcriber_engine, batch_transcriber, diarizer, speaker_db, text_embeddings, eval/optimize scripts)
- ui_desktop/ (tray, notifications)
- ui_web/ (templates/, static/) - Flask configured with template_folder/static_folder
- app.py and launch.py stay at project root as entry points

Storage consolidation:
- data/, models/, tools/ moved under storage/
- launch._migrate_legacy_layout() auto-migrates on first run after update; idempotent and silent on no-op runs
- data/ migration is pointer-aware: only moved when at default location. Custom .data_location targets (and the data they reference) are left untouched. Migration mirrors core/paths._read_pointer's logic for relative/empty/corrupt pointer files.
- **/storage/ in .gitignore; legacy /data/, /tools/, /models/, /audio/ also ignored as a defensive layer

Settings hygiene:
- video_offset_<session_id> flat keys folded into a single video_offsets dict; settings.load/put/update auto-migrate any legacy keys

Bug fixes:
- batch_transcriber._run_diarization referenced an out-of-scope `device` during reanalysis; now uses torch_device.type
- Video playback now follows audio jumps during speaker-filter mode (previously looped a short snippet because _videoSeekPending blocked drift-detection sync calls)

Documentation:
- README and AGENT.md rewritten for the new layout, dual-platform install paths, and the engine factory
- AGENT.md gains a Platform Notes section consolidating macOS-specific gotchas (CoreAudio API, PyObjC 12.1 ctypes workaround, BlackHole + aggregate device routing)

## Global Chat list_recent_meetings tool, sidebar polish, split + healing fixes (2026-04-28)

### Global Chat

- New list_recent_meetings tool: browse the meeting library by date range (within_days, start_date/end_date, limit). Returns titles, IDs, dates, speakers, folder, segment_count, and truncated summaries; system prompt updated to use it for date-bounded queries and combine with get_session_detail for follow-up.
- System prompt now requires every cited session to be a markdown link ([Title](/session?id=<id>)), with example, so responses include clickable references.
- marked.js postprocess hook adds target="_blank" rel="noopener noreferrer" to every rendered <a> that doesn't already declare a target or onclick (timestamp pills are skipped because they have onclick handlers).

### OpenAI tool diagnostics

- _execute_tool_openai no longer swallows tool-executor exceptions: full traceback is logged AND a 'Tool X failed: <error>' message is sent back to the model so it can recover or report meaningfully.
- _tool_loop_openai catches responses.stream rejection (e.g. model doesn't support tools), logs the model + tool list at start, logs each round's outcome, and warns explicitly when the first round produces text without invoking any tool — the smoking-gun pattern for a model that's ignoring the provided tools.

### Sidebar

- _revealSessionInSidebar walks the active session's folder ancestry and uncollapses every parent on session open / popstate / bootstrap race. Cycle-safe.
- Two highlight classes on .sidebar-folder: folder-active (immediate folder of the active session) and folder-active-ancestor (every folder in the chain).
- collapsed/expanded class added to every .sidebar-folder for CSS hooks.
- Active class snaps onto the clicked session item before loadSession's async fetch completes — instant feedback instead of waiting for the fetch to land.

### Stale "In progress" sessions

- storage.heal_stale_in_progress: finds rows with ended_at IS NULL that aren't actively recording and computes a sensible ended_at from last_segment_time. Called once at app startup; logs how many were fixed.
- formatSessionMeta only says "In progress" when state.sessionId === s.id && state.isRecording — defensive against any future code path that forgets to call end_session.

### Split logic

- split_session resolves a single base_dt for the whole split before the per-part loop; each part's started_at = base_dt + start_sec and ended_at = base_dt + end_sec, so part N+1 always lands exactly when part N ended. No possibility of all parts clustering at _now() ms-apart.
- create_split_session accepts explicit started_at / ended_at; logs a warning instead of failing silently when source has no parseable started_at.
- If the source has no parseable started_at, base_dt anchors at now() − total_duration so parts at least sit in correct relative order rather than stacking.

## Guard playback-video seeked listener for pages without the element (2026-04-28)

Home page has no <video id="playback-video"> so _playbackVideo is null; the unconditional addEventListener I added in 3e11a07 crashed the JS load and prevented the home sidebar from populating.

## Configurable data folder, preset resolver, OpenAI Responses API, diarizer & video fixes (2026-04-28)

- paths.py: single source of truth for data folder; user-configurable via System tab. Pointer file (.data_location) at project root; SQLite online backup API for safe DB migration; rejects same/nested destinations; rolls back partial copies on failure. Native folder picker via tkinter subprocess. /api/data_folder GET/pick/migrate/reset endpoints.
- settings.py / storage.py / media_edit.py / optimize_diarization.py / app.py: route every data path through paths.py. Module-level constants (DB_PATH, DATA_DIR, AUDIO_DIR, ...) re-exposed via __getattr__ for back-compat with live re-resolution.
- default_audio_params.py: resolve_audio_params() — when a preset is selected, preset values are the source of truth (so source-code preset updates auto-propagate); per-key audio_params overrides only apply on "custom". Auto-flip to custom when a slider edits a preset-controlled key, snapshotting effective values first so untouched sliders keep their preset values. Optimizer recommendations capped to delta_new <= 0.65 with rationale.
- diarizer.py: each speaker now keeps an immutable anchor + drifting centroid; matching uses max(sim_to_centroid, sim_to_anchor) so centroid drift can't lock in misclassifications. Hysteresis margin on centroid updates (only confident matches update). 1.0s minimum embedding to spawn a new speaker (0.5s still allowed to match existing). delta_new / rho_update / tau_active live-applicable via apply_params (no session restart needed).
- ai_assistant.py: OpenAI tool loop migrated from chat.completions to the Responses API. Web search preview tool re-enabled (was being rejected by chat.completions). Function-call output items + image follow-up messages for vision-grounded screenshots. previous_response_id for stateless follow-ups.
- speaker_db.py: find_matches gains min_similarity= for diagnostic queries; SUGGEST/AUTO_APPLY thresholds exposed as class attributes.
- app.py: per-unlabeled-speaker fingerprint debug logging — on every embedding extraction, log the top 3 closest profiles with similarity scores plus the active thresholds. Localhost URLs in tray + browser launch (Flask still binds to 127.0.0.1 for security).
- static/app.js: video preview seek freeze fixed — single persistent 'seeked' listener clears a _videoSeekPending flag and resumes playback after the decoder lands on the new frame. Drift correction and call-sites that previously raced .play() against an in-flight seek now defer until the seeked event. Data Folder UI handlers and preset-dropdown sync from /api/audio_params response.
- templates/{home,index}.html + style.css: Storage settings group with Data Folder display, Choose…, and Reset-to-default controls.

## Replace diart with direct pyannote streaming diarizer; independent per-tool model pickers (2026-04-24)

- Rewrite diarizer.py: StreamingDiarizer uses pyannote segmentation-3.0 + wespeaker embeddings directly (drops diart dep), with overlap-aware detection and online centroid clustering
- transcriber.py: wire up on_diarizer_error callback; add large-v3-turbo whisper preset; raise minimum Whisper clip from 0.2s to 0.5s
- app.py: concat video parts across pause/resume cycles so resumed recordings keep a single merged MP4; surface diarizer errors in console; separate global_chat model settings from session chat
- static/app.js: Summary / Session Chat / Global Chat pickers each track their own per-tool override instead of sharing one global choice
- default_audio_params.py: retune diarization defaults and presets (delta_new 0.5->0.8, rho_update 0.422->0.25, larger merge gaps) plus reanalysis defaults based on optimizer sweeps
- Add eval_diarization.py (synthetic audio + DER harness) and optimize_diarization.py (auto-tune from corrected sessions)

## Add Global Chat model picker, unify model selection, and fix Claude label regex (2026-04-23)

- Inline model picker on Global Chat prompt bar, mirroring the session chat widget (mic button + popup panel, left of the textarea)
- Unify Summary / Session Chat / Global Chat pickers to drive a single global AI provider/model; per-tool overrides cleared on every global pick so the chosen model actually takes effect everywhere
- Reparent picker panel to <body> so position:fixed resolves against the viewport (animated .home-chat-panel was acting as the containing block), then compute absolute top/left with viewport clamping to keep the panel on-screen regardless of where the button sits
- App Theme section was missing from the home-page Settings pane; copied it in alongside Hardware & Updates groups
- Fix a JS crash in home.js caused by querying a selector that no longer existed (.home-topbar-right → now guarded against null)
- Anthropic picker labels: tighten _ANTHROPIC_CLASS_RE so the greedy version group no longer swallows trailing 8-digit dates; IDs like claude-haiku-4-5-20251001 now render as "Haiku 4.5" instead of "Haiku 4 5.20251001", and the class bucketing collapses to the right latest-per-family behavior
- /api/audio/gain returns 200 with applied:false when there's no active capture, and home.js polls the correct /api/search/semantic/status URL

## Add live model refresh, per-session chat prompts, split restore, and custom theme accents (2026-04-23)

- Fetch AI model lists live per provider with on-disk cache and manual refresh endpoint
- Per-session chat prompt override with default prompt API
- Split-meeting backups with snapshot/restore flow for audio, video, and metadata
- Bulk retitle helper
- Custom theme accent palette derived from hex with blend strength

## Refresh Start Menu shortcut when logo.ico is updated (2026-04-23)

The shortcut creator short-circuited once the .lnk pointed at the right launch.bat, so swapping logo.ico never propagated (Windows keys its icon cache off the .lnk's mtime). Re-save the shortcut whenever the icon file is newer than the .lnk.

## Add meeting import/export, TSB voice library, right-click menus, and tray shortcuts (2026-04-22)

Export: session data (transcript, summary, chat, speakers, voice fingerprints, screenshots, attachments) packaged as .zip with Opus-compressed audio. Selective export via modal with per-category checkboxes and animated progress steps.

Import: drag-and-drop .zip onto session or home page, or file picker. Handles Opus/FLAC/WAV audio conversion, screenshot URL rewriting, speaker embedding ingestion into voice library, and title dedup with (1)/(2) suffix.

TSB autocomplete now shows Voice Library speakers (async-fetched, cached) in a separate section below meeting speakers, plus a "Mark as Noise" option.

Ad-hoc speaker labels (label_override, source_override) now feed into AI transcript builder so summaries and chat reflect manual speaker corrections.

Session and folder right-click context menus in sidebar. Export option in session three-dot menu.

Tray menu simplified: removed model/diarizer info lines, added Settings, Check for Updates (deep-links to system settings tab), and Restart Server.

## Add audio/video editing tools and quiet-recording toast reminder (2026-04-22)

Introduce media_edit helpers plus trim/split/restore and audio-profile endpoints for post-session cleanup, and a Windows toast that nudges the user when a recording has gone silent.

## Add per-tool AI provider/model selection, prompt caching, and ellipsis cleanup (2026-04-14)

- Independent provider and model selection for Summary and Chat tools in settings modal and inline model picker dropdowns on the session page
- Anthropic prompt caching with two cache breakpoints (system+tools prefix and rolling message history) for reduced token costs in multi-turn chat
- Filter Anthropic models to only show latest version per class (opus/sonnet/haiku)
- Clean excessive Whisper ellipsis artifacts from transcription output
- Fix API key reload to clear cached clients for both providers

## Add duplicate instance detection and reduce FFmpeg mic capture latency (2026-04-13)

- New instance handshake: probes port for existing instance before starting. If recording is active, aborts with clear message. If idle, requests shutdown and waits for port to free up.
- Dedicated /api/instance-handshake endpoint logs takeover attempts on the existing server side for transparency.
- FFmpeg dshow mic: add -rtbufsize 32k, -audio_buffer_size 40ms, nobuffer and low_delay flags to reduce visualization latency.

## Add audio/video upload, home page recording redirect, speaker auto-linking, and UI polish (2026-04-13)

- Upload button in session header: accepts any audio/video file, FFmpeg extracts audio to WAV, then runs the standard reanalysis pipeline
- Recording from home page and system tray now redirects to /session?autostart=1 so all recordings go through the session page's proven audio init path (fixes DirectShow echo issue)
- Speaker auto-linking: renaming a speaker auto-creates/links a global voice profile and syncs the global profile's color to the session speaker
- Shutdown/restart/update dialogs skip confirmation when no recording is active
- Restart/shutdown/update screens use a minimal CSS EQ-bar animation
- Recording cleanup synchronization prevents start/stop race conditions
- Backend falls back to saved device preferences when no device IDs are provided

## Tweak AGC debug panel layout spacing (2026-04-10)

## Add FFmpeg mic capture, AGC, auto-detect devices, and visualizer improvements (2026-04-10)

- Replace browser/WASAPI mic capture with FFmpeg subprocess via DirectShow for reliable, distortion-free microphone input
- Add automatic gain control (AGC) with noise gate, transient protection, and per-source enable toggles (desktop + mic), enabled by default
- Add auto-detect device feature with magic wand button that tests all audio devices simultaneously, plays a test sample, and selects the best
- Fix dead low-frequency spectrum bars by zero-padding FFT to full window
- Add gradient shading to spectrum bars and level meter tracks
- Auto-stop orphaned audio tests on page refresh
- Add AGC debug panel showing live gain, envelope, and gate status
- Fuzzy-migrate legacy WASAPI/browser mic preferences to FFmpeg equivalents

## Add native web search, typing cursor animation, and auto-scroll improvements (2026-04-10)

- Add native web search tool (Anthropic web_search_20250305 + OpenAI web_search_preview) to both session and global chat, with system prompt instructions to use sparingly
- Anthropic: event-based streaming detects server_tool_use in real-time, emits tool_call/tool_result events for the chat-tool-widget
- OpenAI: tracks url_citation annotations during streaming for widget
- Session chat now always uses tool loop (web search available even without screen recording)
- Replace blinking cursor with inline typing-cursor span that glows on chunk arrival and fades to invisible when chunks stop
- Cursor is reused across renders (not recreated), resilient to morphdom updates and void elements
- Auto-scroll to bottom on message submit in both chats, with scroll- away suppression for global chat
- UI polish: session meta separators, icon updates, power menu tweaks

## Stop clearing summary and chat on reanalysis start (frontend) (2026-04-09)

The reanalysis_start SSE handler was clearing both the summary and chat panels. Now only the transcript is cleared for retranscription.

## Remove summary regeneration from reanalysis, set segment break to 0.75s (2026-04-09)

Reanalysis now only retranscribes - summary and chat are preserved. Lowered segment_break_silence default from 1.5s to 0.75s for more frequent paragraph breaks in monologues.

## Break long same-speaker segments at silence gaps (2026-04-09)

Added segment_break_silence parameter (default 1.5s) that forces a new transcript segment when the same speaker continues after a pause longer than the threshold. Previously all consecutive same-speaker diarization segments were merged into one massive batch regardless of silence gaps, producing unreadably long monologue blocks.

Now a 1.5s+ pause within a monologue creates a visual paragraph break in the transcript while shorter pauses (normal speech rhythm) still merge as before. Configurable in Settings > Diarization.

## Add reanalyze button to transcript header, preserve chat/summary (2026-04-09)

- Added rotate-right icon button to transcript header right side that triggers reanalysis on the current session
- Removed confirmation dialog before reanalysis
- Reanalysis no longer clears chat history or summary (frontend or backend) - only the transcript is cleared and retranscribed
- Users can still manually clear chat/summary if desired

## Split summary header into left/right divs for proper justify layout (2026-04-09)

Uses the same col-actions left/right pattern as the transcript header so space-between pushes Auto+badge to the left and action buttons to the right.

## Move Auto button to left side of summary header, next to title (2026-04-09)

Matches the transcript header layout where action buttons appear to the right of the column title.

## Reduce fragment period stripping threshold from 4 to 2 words (2026-04-09)

The 4-word threshold was stripping trailing periods from most diarized segments (which are typically 3-5 words each), causing long monologues to render as massive run-on sentences with no punctuation. Lowered to 2 words so only truly tiny fragments ('Yeah.' or 'Okay.') get stripped.

## Use selected recording monitor for live screenshot fallback (2026-04-09)

capture_live_frame() was defaulting to display 0 instead of using the monitor selected for screen recording in settings.

## Only show display names (not raw keys) to the chat model (2026-04-09)

Speaker metadata no longer exposes raw keys like 'Speaker 1' to the model. If the user labels Speaker 1 and Speaker 3 both as 'Bob', the model sees a single 'Bob' in the speaker roster. Raw keys are never included in the metadata block.

## Disconnect WARP before every AI API call (2026-04-09)

Added warp_disconnect() to all four AI entry points:
- _stream (chat streaming)
- _stream_with_tools (chat with tool use)
- _complete (title generation)
- _complete_structured (summary patching)

This ensures WARP is off whenever the app communicates with Anthropic or OpenAI APIs, since WARP's TLS inspection breaks the connection regardless of SSL verification settings.

## Disable SSL verification for AI API clients (Anthropic/OpenAI) (2026-04-09)

Cloudflare WARP's TLS inspection also breaks the Anthropic and OpenAI SDKs which use httpx. Now creates an httpx.Client(verify=False) and passes it to both SDK constructors. Also set SSL_CERT_FILE and SSL_VERIFY env vars as belt-and-suspenders for any other HTTP libs.

## Styling adjustments (2026-04-09)

## Summary prompt panel respects user's collapsed state, not text content (2026-04-09)

_applyPromptText was forcing the panel open whenever the prompt had text, overriding the user's manual collapse. Now only uses the localStorage 'summary-prompt-open' flag.

## Fix streaming timestamp pills using wrong class name (2026-04-09)

_linkifyTimestampsInMd was creating <span class='ts-pill'> but the CSS styles <a class='timestamp-link'>. Now uses the same element type and class as the DOM-based linkifyTimestamps that runs on page refresh.

## Fix real-time timestamp pill rendering during streaming (2026-04-09)

marked.js was interpreting [M:SS] brackets as link reference syntax, so the regex replacement on the HTML output never matched. Now the replacement runs on the raw markdown text BEFORE marked.parse(), so timestamps become HTML spans that marked passes through as-is.

Applied to both chat streaming (via _morphChatBody) and summary streaming.

## Fix chat input height not updating after paste (2026-04-09)

_autogrowChatInput() was not called after the paste trim, so the textarea kept its pre-paste height until the next input event.

## Strongly enforce square bracket syntax for timestamps in system prompt (2026-04-09)

Explicitly states that bare timestamps like 2:50 won't render as clickable pills, and that [2:50] is required. Explains that timestamps are rendered as interactive pills to motivate the correct format.

## Add scroll-wheel zoom and cursor-aware panning to image lightbox (2026-04-09)

- Mouse wheel zooms in/out (0.5x to 10x), zooming toward cursor position
- Drag to pan when zoomed in (scale > 1)
- Double-click toggles between fit-to-screen and 2x zoom
- Properly cleans up event listeners on close
- Escape and backdrop click still close the lightbox

## Render timestamp pills in real-time during summary streaming (2026-04-09)

## No-tilde timestamps, live timestamp pills, fix image flashing (2026-04-09)

System prompt:
- Added explicit instruction to never use approximate timestamps like [~17:30] - always use exact timestamps from the transcript

Timestamp pills during streaming:
- Moved timestamp linkification into the HTML string transform (_linkifyTimestampsHtml) that runs before morphdom, so pills render as soon as a valid [M:SS] bracket appears in the stream

Image flashing fix:
- Before morphdom runs, detach already-loaded images from the DOM
- After morphdom recreates the structure, replace fresh unloaded img clones with the cached loaded originals
- This prevents the browser from re-requesting/re-decoding images that haven't changed when surrounding text updates

## Morphdom for smooth chat streaming, image scroll fix, lightbox preview (2026-04-09)

Streaming:
- Chat body now uses morphdom to diff-update instead of innerHTML replace, preventing images from flashing/reloading on each chunk
- Images with unchanged src are skipped entirely during the diff

Auto-scroll:
- New images get an onload listener that triggers scrollChatToBottom so the chat scrolls down when images finish loading asynchronously

Lightbox:
- Clicking any inline screenshot opens a full-screen overlay
- Click image to toggle zoom, drag to pan when zoomed
- Click backdrop or press Escape to close
- Close button in top-right corner

## Inline screenshots in chat responses + text/tool separator (2026-04-09)

Screenshots:
- Screenshots are saved to data/screenshots/{session_id}/ and served via /api/sessions/{session_id}/screenshots/{filename}
- Tool result now includes markdown embed URL so the model can reference screenshots inline with ![description](url)
- System prompt instructs the model to embed screenshots at relevant points in its response
- Images render in chat via marked.js markdown, styled with rounded corners and full width
- Screenshots persist on disk so they survive page refreshes

Text/tool separator:
- When the model emits text, then calls tools, then emits more text, a horizontal rule (---) is inserted between the two text sections
- Separator is emitted from the backend via on_token so it's included in the saved message and persists through refreshes

## Global scrollbar styling for consistent thin dark scrollbars everywhere (2026-04-09)

Applied on * selector so all elements (including code fence overflow, textareas, panels, etc.) get the same thin scrollbar. Individual elements can still override if needed.

## Made bell icon visibility dynamic (2026-04-09)

## Remove CUDA smoke test, move torchaudio shims to config.py (2026-04-09)

Removed the CUDA diarizer smoke test entirely and restored the original _load_diarizer logic.

Moved torchaudio compatibility shims (AudioMetaData, list_audio_backends, set_audio_backend) to config.py so they're applied before any pyannote import regardless of whether diarizer.py or speaker_db.py loads first.

## Revert "Add torchaudio shims to speaker_db, fix CUDA smoke test import" (2026-04-09)

This reverts commit a39905a76c9abd70d9185bf2362778e689280ec6.

## Add torchaudio shims to speaker_db, fix CUDA smoke test import (2026-04-09)

speaker_db.py imports pyannote independently of diarizer.py, so it needs its own torchaudio shims (AudioMetaData, list_audio_backends, set_audio_backend) for torchaudio 2.x compatibility.

CUDA smoke test was importing speechbrain.inference.speaker which doesn't exist in speechbrain 1.0.3. Replaced with a basic CUDA conv1d operation that exercises cuDNN without speechbrain imports.

## CUDA smoke test before loading diarizer to prevent native crashes (2026-04-09)

Exit code 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN) on test machine was a cuDNN version mismatch causing a native crash during speechbrain model loading on GPU. Since native crashes kill the entire process and can't be caught in Python, we now run a subprocess smoke test that imports speechbrain with CUDA. If the subprocess crashes, the diarizer falls back to CPU instead of taking down the server.

## Enable faulthandler for native crash diagnostics (2026-04-09)

The diarizer crash on the test machine produced no error output.
- Added faulthandler.enable() in app.py to dump tracebacks on native crashes (SIGSEGV, SIGABRT, etc.)
- Launch.py now passes -X faulthandler to Python
- Shows exit code and "crashed in native code" message when no stderr
- Increased error output capture to 40 lines

## Show video playback button after reanalysis completes (2026-04-09)

The reanalysis_done handler now checks for a screen recording and calls initVideo() if one exists, matching the recording-stop behavior.

## Auto-collapse notification panel when all suggestions are processed (2026-04-09)

After applying, ignoring, or clearing all suggestions, the panel auto-collapses after 1.2s (enough time to see "No pending suggestions"). The panel can still be manually reopened via the bell button.

## Fix broken JS regex from em-dash replacement in timestamp pattern (2026-04-09)

Used Unicode escapes (\u2013, \u2014) for en/em-dash in the character class so they survive text-level dash normalization.

## Fix broken regex from em-dash replacement in noise pattern (2026-04-09)

The character class [.…!?\-–-\s] had an invalid range after the em-dash was replaced with a regular dash. Simplified to [.…!?\-\s].

## Replace em-dashes with standard dashes across codebase (2026-04-09)

## Show full API key in concealed/revealed field, not masked abbreviation (2026-04-09)

Server now returns the full key value (local-only app on 127.0.0.1). The input field shows the full key as password dots, revealable via the eye toggle. Only keys that were actually changed are sent on save.

## Show concealed API keys with reveal toggle, provided/required badges (2026-04-09)

- When an API key is set, show its masked value in the password field (dots by default, revealable via eye button)
- Badge changes from red "required" to green "provided" when key is set
- "Get a key/token" link is hidden when key is already provided
- Clicking into a masked field clears it for new input; blurring without typing restores the masked value
- Save only sends keys that were actually edited (not masked values)

## Match home page header layout to sessions page (2026-04-09)

Reorder: [Record] [Status] | [Update] [Settings] [Power] | [Search] Added topbar-sep dividers matching the sessions page layout.

## Merge AI Assistant and API Keys into a single settings pane (2026-04-09)

Provider/model selection at the top, API key fields below. The active provider's key field is still dynamically shown/hidden. Removed the separate API Keys nav item from both index.html and home.html.

## Add power menu dropdown to home page (2026-04-09)

Same Shut Down / Restart / Update & Restart dropdown as the session page. All JS functions are already in app.js which home.html loads.

## Add 'Update & Restart' option to power menu when updates are available (2026-04-09)

Hidden by default, shown when _showTopbarUpdate detects commits behind. Calls /api/update/apply (same as the topbar Update button), then shows a waiting screen that auto-reloads when the server comes back.

## Replace Shut Down button with power icon dropdown (Shut Down / Restart) (2026-04-09)

- Power icon button opens a dropdown with Shut Down and Restart options
- Both show confirmation dialogs before acting
- Added /api/restart endpoint that stops everything, relaunches via Start Menu shortcut, then exits
- Restart shows a "Restarting..." screen that auto-reloads when the server comes back (polls /api/status every 2s)

## Live screenshot seeking via fragmented MP4 + fallback to screen capture (2026-04-09)

Screen recordings now write as fragmented MP4 (frag_keyframe+empty_moov) which is seekable mid-recording. On stop, the file is remuxed to a standard faststart MP4 for broad compatibility.

The chat frame extractor now works during live recording: 1. Try seeking into the fragmented MP4 at the requested timestamp 2. Fall back to a live screen capture if seeking fails 3. After recording stops, use the finalized MP4 as before

## Rich text copy for chat response copy buttons (2026-04-09)

Chat message copy now writes both text/html and text/plain to the clipboard, matching the summary copy behavior. Pasting into rich editors preserves headings, lists, code blocks, and formatting.

## Recursive cache search for models with depth limit (2026-04-09)

Instead of hardcoding cache paths per library, recursively search ~/.cache/ and ./models/ for the models--org--name directory pattern up to depth 4. Finds models cached by any library (huggingface_hub, pyannote, torch, etc.) regardless of exact directory structure.

## Check all cache locations including torch/pyannote and default HF cache (2026-04-09)

Pyannote models cache under ~/.cache/torch/pyannote/, not HF_HOME. Other models may be in the default ~/.cache/huggingface/hub/ from before HF_HOME was pinned. Now checks project-local, default HF, and torch/pyannote cache dirs before spawning download subprocesses.

## Skip model pre-download when all models are cached (2026-04-09)

The pre-download step was spawning 6 subprocesses that each imported PyTorch/speechbrain/transformers just to check if models existed, adding ~40s to startup even when everything was cached.

Now does a fast filesystem check first (is the snapshot dir non-empty?) and only spawns the heavy download subprocess for models that are actually missing. Typical startup with cached models: <1s for this step.

## Fix tool widget: keep streaming class until first chunk, no scroll (2026-04-09)

The streaming class was being removed when tools completed (allDone), before the first chat_chunk arrived. This meant the collapse on first chunk couldn't find .streaming and never fired.

Now: streaming class persists after tools complete and is only removed on first chat_chunk, which also collapses the widget. The streaming CSS state disables max-height, overflow scroll, and transitions so the widget expands freely without scrolling during tool use.

## Cancel in-flight responses when chat is cleared (2026-04-09)

Both session and global chat now stop active generation, reset busy state, and clear cursor/buffer before wiping the DOM. Prevents orphaned SSE events from rendering into a cleared chat.

## Allow tool widget to be manually expanded during response streaming (2026-04-09)

Only collapse the tool widget once (on first chunk) by targeting .streaming class in the selector. After that, the user can freely toggle it open/closed and subsequent chunks won't force it shut.

## Trim leading/trailing whitespace on paste in chat inputs (2026-04-09)

## Unlimited height for tool widget during streaming, scrollable after (2026-04-09)

During active tool use (.streaming class), the tool details expand without height limit so all tool calls are visible. Once the model starts its text response, the widget collapses. When manually reopened later, it's capped at 300px with internal scrolling.

## Auto-expand tool widget during tool use, collapse on response stream (2026-04-09)

Tool widget now auto-opens while tools are in progress so users can watch results arrive in real time. When the model starts streaming its text response, the widget auto-collapses to make room for the answer. Manual toggle still works after tools complete.

Also styles the tool widget scrollbar to match the rest of the site.

## Make tool widget scrollable, fix OpenAI parallel screenshot error (2026-04-09)

Tool widget: cap at 300px with overflow-y scroll instead of clipping.

OpenAI error: screenshot tool was inserting user messages (with images) between tool result messages during parallel tool calls, violating OpenAI's protocol that all tool results must be contiguous. Now embeds the image directly in the tool result content using multi-content format.

## Add copy button to Summary tab with rich text support (2026-04-09)

Copies the summary as both HTML and plain text using the Clipboard API, so headings, lists, and formatting are preserved when pasting into Word, Outlook, Notion, etc. Shows a checkmark for visual feedback.

## Tighten hallucination filtering: sentence dedup + lower threshold (2026-04-09)

- Raise repetition threshold from 0.35 to 0.50 to catch more loops
- Add _dedup_sentences() that splits on sentence boundaries and removes repeated sentences/clauses (catches "QUESTION. SO I THINK THAT'S A GOOD QUESTION. QUESTION." patterns that slip through n-gram checks)
- If >60% of sentences are duplicates, discard the entire segment and clear the context to prevent contaminating the next Whisper call
- Applied to both streaming and batch transcription paths

## Filter Whisper hallucination phrases (Subtitles by, etc.) (2026-04-09)

Whisper hallucinates subtitle credits and YouTube outros when processing silent or noisy audio. Added a pattern-based filter that strips known phrases like "Subtitles by Subtitle Workshop", "Amara.org community", "Thanks for watching", etc. Applied to both streaming and batch paths.

If the entire segment is a hallucination, it's discarded. If real speech is mixed with artifacts, the artifacts are stripped and the speech is kept.

## Hide empty chat bubble until model starts responding (2026-04-09)

The chat-msg-body and actions are now hidden (display:none) when created, and revealed when the first text chunk arrives. This eliminates the awkward empty bubble that appeared while the model was thinking. Applied to both session chat and global chat.

## Differentiate session vs global chat system prompts (2026-04-09)

Session chat: explicitly scoped to the current meeting only, emphasizes timestamps for jumping to specific moments in the recording.

Global chat: explicitly cross-session, references meetings by title and speakers by name, no timestamps (not a single-recording player).

## Enhance global chat: speaker history, folder context, summaries in search (2026-04-09)

- Add get_speaker_history tool: find all meetings a person appeared in with summaries, folder info, and segment counts
- Enrich search_transcripts and semantic_search results with session summaries (truncated to 500 chars) and folder names
- Increase transcript truncation limit from 50K to 200K chars
- Increase tool loop limit from 5 to 50 rounds
- Update system prompt with new tool and context guidance

## Truncate long status pill errors, show full message on hover (2026-04-09)

Cap the status pill at 360px with text-overflow ellipsis. On hover, display the full error message in a styled tooltip below the pill.

## Disable SSL verification to bypass WARP's TLS inspection (2026-04-09)

Corporate Cloudflare WARP injects a self-signed CA that breaks SSL for HuggingFace and other HTTPS downloads. Set HF_HUB_DISABLE_SSL_VERIFY, clear CA bundle env vars, and override Python's default SSL context so model downloads work regardless of WARP state.

## Retry model downloads up to 3 times on transient failures (2026-04-09)

## Pin HF cache to project-local models/ directory (2026-04-09)

Set HF_HOME to ./models/ in config.py so pre-downloaded and runtime models always use the same cache location. Simplifies cache checking to a direct filesystem lookup. Added models/ to .gitignore.

## Fix false cache miss: check HF cache directly, force offline load (2026-04-09)

pyannote's Pipeline.from_pretrained ignores local_files_only, so the cache-first attempt always hit the network. Now we check the HF cache via try_to_load_from_cache and temporarily set HF_HUB_OFFLINE=1 during load to prevent pyannote from making any network calls.

## Pre-download all HF models during launch setup while WARP is off (2026-04-09)

Models are downloaded during launch.py's setup phase (WARP disconnected) so they're always cached before the app starts. At runtime, batch reanalysis uses local_files_only first, eliminating network dependency.

Models pre-cached: faster-whisper large-v3, pyannote segmentation/embedding/ pipeline, whisper-large-v3-turbo, sentence-transformers. Each downloads in a subprocess so failures don't block others. Already-cached models return instantly.

## Cache-first model loading, remove WARP toggling from runtime code (2026-04-09)

Multiple threads toggling WARP created race conditions. Now:
- launch.py is the only place that toggles WARP (off for pip, on after)
- app.py reconnects WARP before git fetch for update checks
- Batch reanalysis uses _load_hf_pipeline() which tries local cache first, then attempts download with WARP off, then with WARP on as fallback
- All other model loads (whisper, diarizer, embeddings) no longer touch WARP since their models are cached after first run

## Fix WARP toggle: disconnect for model downloads, reconnect only for git (2026-04-09)

WARP's TLS inspection breaks both pip and HuggingFace downloads. Only git operations need WARP connected for corporate network routing.

## Toggle WARP off for pip, back on for git/HF model downloads (2026-04-09)

WARP's TLS inspection breaks pip (untrusted CA) but the corporate network requires WARP for routing to GitHub and HuggingFace. Disconnect before uv/pip installs, reconnect after. Ensure WARP is connected before every model download and git fetch in case IT's script hasn't re-enabled it.

## Disconnect Cloudflare WARP before every model download (2026-04-09)

IT periodically reconnects WARP, so a single disconnect at launch isn't enough. Added shared ensure_warp_disconnected() utility and call it before every model download point: whisper, diarizer, batch reanalysis, speaker embeddings, and sentence-transformers. Removed HF_HUB_OFFLINE in favor of this approach.

## Disconnect Cloudflare WARP before downloads, enable HF offline mode at runtime (2026-04-09)

WARP's TLS inspection blocks HuggingFace model downloads and pip index requests. Disconnect it via warp-cli at launch before any network ops. After all packages/models are downloaded, set HF_HUB_OFFLINE=1 so runtime reanalysis uses cached models without network calls.

## Pin speechbrain==1.0.3, broaden LazyModule scan, show crash tracebacks (2026-04-09)

- Pin speechbrain to 1.0.3 in requirements.txt to avoid LazyModule crashes in >=1.1 that break inspect.stack() during model loading
- Broaden post-import scan to replace ALL speechbrain LazyModules, not just ones under the integrations prefix
- Show actual traceback in launch.py when app exits with an error

## Fall back to GitHub when Azure DevOps update check fails (2026-04-09)

## Suppress speechbrain deprecation and torchaudio backend warnings (2026-04-09)

## Bulletproof speechbrain lazy-module shims and auto-retry corrupted whisper cache (2026-04-09)

Replace static speechbrain integration stubs with a meta-path finder that intercepts any import under speechbrain.integrations.*, plus a post-import scan that replaces LazyModule entries already in sys.modules with inert stubs. This prevents inspect.stack() from triggering ImportError on optional deps.

Add auto-recovery for corrupted faster-whisper model caches — if WhisperModel fails to load (e.g. missing model.bin), the HF hub cache is cleared and the download is retried automatically.

## Harden diarizer init, unify overlays, and fix home page integration (2026-04-08)

- Stub speechbrain.integrations.k2_fsa lazy modules to prevent crash on newer speechbrain versions; add granular try/catch with diagnostic messages around segmentation and embedding model loads
- Add diarizer_failed state flag so recording isn't permanently blocked when diarizer fails to load (was stuck on "Loading speaker diarization..." forever)
- Make Settings and Voice Library overlays universal — both panels now open natively on the home page instead of redirecting to /session
- Add Settings and Update Available buttons to home page topbar
- Fix sidebar session clicks on home page to navigate to /session?id=...
- Fix SSE connection leaks on page navigation via beforeunload cleanup
- Unify brand-icon/brand-name classes between home and session pages
- Move periodic update check to shared init so it runs on both pages
- Default global chat sidebar to collapsed, persist state in localStorage

## Add home page with global chat, dashboard widgets, and shared sidebar (2026-04-08)

Introduces a landing page at / with a global AI chat interface for querying across all meetings, and a redesigned dashboard featuring hero stats, a 14-day activity bar chart, recent sessions with metadata, and top speakers with avatars and talk-time bars. The full session sidebar (devices, models, folders, voice library, recording controls) is shared between both pages via app.js with home-page guards. Includes enhanced analytics API with word counts, activity heatmap data, and speaker talk-time. Fixes SSE connection leaks on page navigation via beforeunload cleanup.

## Fix manual summary refresh for non-active sessions (2026-04-08)

_run_summary unconditionally bailed when the requested session ID didn't match the active in-memory session, silently dropping manual refresh requests for past sessions. Scope the early exit to auto- summaries only so the refresh button works when viewing any session.

## Add chat tool UI, fix diarization periods, minimap perf, and UI polish (2026-04-08)

- Fix diarized transcription adding periods after every word by batching consecutive same-speaker segments before Whisper, with fallback fragment-period stripping for short outputs
- Add collapsible tool-call widget and thinking animation to chat UI, with tool result persistence through refreshes via new DB column
- Make chat clear actually delete messages from DB, not just DOM
- Debounce/cache minimap redraws during live recording for performance
- Add dynamic bottom border-radius to transcript and summary panel stacks so only the bottom-most visible element is rounded
- Add date to session meta (Today/Yesterday/Mon Day), HH:MM:SS duration support, minimap icon update, fp-bell-btn open state, and summary prompt open-state persistence in localStorage
- Clamp screen display selection to valid range on load
- Screenshot thumbnails in chat tool-call widget

## Add batch reanalysis pipeline, speaker notification system, and UI improvements (2026-03-30)

- Batch reanalysis using HuggingFace transformers + pyannote full-file diarization with independent settings panel (model, batch size, speaker count, clustering)
- Speaker suggestion notification bell with persistent queue and review panel
- Inline fingerprint identify icons on unlinked speaker badges
- Auto-apply flash feedback with status bar message
- Anti-hallucination guards for batch transcription (compression ratio, n-gram filter)
- Session embedding replacement on reanalysis for cleaner Speaker Library centroids
- Brand visualizer redesigned: horizontal EQ bars extending from logo sides
- Updated green accent to #00b464 throughout
- Browser mic properly released on test stop and page unload
- Transcript segments populate live during reanalysis (isViewingPast fix)
- Collapse grouping uses resolved display names, removes interstitial merge bug
- Expanded group segments indented with playback state on group summaries
- Minimap renders after CSS transition completes
- Per-session pane toggle persistence in localStorage
- Reanalysis respects auto-summary toggle
- Exclude already-linked speakers from fingerprint suggestions

## Fix audio pipeline: WAV sample rate mismatch, mic capture timing, and browser mic cleanup (2026-03-27)

- WavWriter: automatically resample audio when appending to a WAV with a different sample rate (fixes chipmunk/double-speed playback on resume when the loopback device changes between recordings)
- AudioCapture: fix mic capture reading oversized chunks (_mic_buf_size) instead of CHUNK_SIZE, causing bursty delivery and choppy/slowed mic audio
- AudioCapture: fix race condition where stop_wav() was called while the mixer thread was still writing; WAV is now finalized after thread join
- Transcriber: guard against orphaned threads on rapid stop/start
- Frontend: call syncBrowserMic() on device selector change so switching away from browser mic immediately releases the getUserMedia lock (prevents WASAPI shared-mode contention on the physical mic device)
- Kill stale ffmpeg processes on startup, column reorder UI, style tweaks

## Refine minimap viewport, sidebar styling, and minor spacing tweaks (2026-03-25)

## Add transcript minimap, collapse groups, presets, video sync, and UI polish (2026-03-25)

- Speaker minimap: VS Code-style canvas strip with colored segment blocks, viewport indicator, playhead tracking, click/drag navigation
- Collapse consecutive speaker runs with merge logic for uniform grouping
- Transcription and diarization presets with reset-all buttons
- Fix video playback freezing on scrub: debounced seeks, pause-during-scrub, wait for video seeked event before resuming audio
- WAV append mode for resumed sessions with timestamp continuation
- Speaker label collision fix on session resume (next_label passthrough)
- Live preview: hide broken image until first frame loads
- Session title in topbar, resume without refresh, delete-active-session flow
- Smooth scroll replaced with 150ms ease-out animation
- Copy button green checkmark feedback
- Pane toggle buttons with layout-aware resize handle repurposing
- Speaker similarity toast moved to bottom center
- Session dot pulse fix on page refresh
- Video viewer visibility persisted between refreshes

## Add screen recorder, text embeddings, and major UI/backend enhancements (2026-03-25)

## Add sidebar subfolders, drag-and-drop reordering, and folder deletion with contents (2026-03-23)

- Folders can now nest inside other folders (parent_id column + recursive rendering)
- Sessions within folders can be manually reordered via drag-and-drop
- Folders can be reordered and dragged into other folders as subfolders
- New subfolder creation via folder context menu
- Deleting a folder with contents shows item count and permanently removes everything
- Drop indicator line shows between items during reorder drags
- Dragging items onto the ungrouped session list removes them from folders
- Compact session item styling to fit more in the sidebar
- Fix: prevent self/descendant folder drops that orphaned folders
- New POST /api/reorder endpoint for atomic batch sort_order updates

## Replace broken spectral subtraction with WebRTC AEC3 for echo cancellation (2026-03-23)

The custom spectral subtraction implementation corrupted mic audio due to feeding processed output back as input, no delay compensation, and phase-blind magnitude subtraction. Replaced with aec-audio-processing (WebRTC AEC3) which handles adaptive filtering, delay estimation, and double-talk automatically.

Removed all manual tuning params (gate_ratio, silence_floor, spectral_sub, hold_ms, crossfade_ms, mic_suppress_db) and UI presets — only the on/off toggle remains. Also fixed a race condition in WavWriter.write() when close() runs concurrently with the mixer loop.

## Overhaul speaker management: original-key toggle, bulk reassign, analytics noise, and persistence (2026-03-23)

- Add original speaker key toggle that shows diarizer keys with display name aliases on both filter pills and transcript badges
- Fix bulk reassign (transcript-selection-bar) to do per-segment reassignment instead of global rename, with proper speaker key attribution
- Replace datalist with free-text input with autocomplete suggestions
- Create custom speaker keys for new names during bulk reassign
- Add source_override column to persist speaker key reassignments across reload
- Show noise segments in analytics as subtle rows at bottom of each chart
- Add Voice Library entries to speaker-picker with live filtering
- Center fingerprint toast with scale fly-in/fly-out animations
- Fix shift+click range selection to skip filtered-out segments
- Noise segments respect original-key toggle for filtering and display
- Deduplicate noise reassignment picker options
- Add scrollbar styling to speaker-picker and autocomplete dropdowns

## Add configurable echo cancellation with presets for speaker+mic setups (2026-03-23)

Implements spectral subtraction, configurable source gating, gate hold, crossfade, and partial mic suppression in the audio mixer loop. Adds an Echo Cancellation settings section with a master toggle (disabled by default) and 4 quick-start presets (Mild, Moderate, Aggressive, Maximum).

## Add tunable audio parameter settings with custom sliders and tooltips (2026-03-22)

- New default_audio_params.py with 9 transcription + 6 diarization params
- Settings pane: Transcription and Diarization tabs with custom range sliders, synced numeric inputs, per-param reset buttons, and rich info tooltips (fixed-position, overflow-safe)
- API endpoints: GET/PUT /api/audio_params, POST /api/audio_params/reset
- transcriber.py: class constants → instance attributes loaded from settings
- diarizer.py: hardcoded config → settings-driven with apply_params()
- Diarizer step reduced from 0.5s to 0.25s for faster speaker transitions

## Migrate package management from pip to uv; revert nvidia DLL changes (2026-03-22)

- Replace pip with uv for all package installs (10-100x faster)
- launch.bat: auto-install uv, use `uv venv --python 3.12` (auto-downloads Python)
- launch.py: uv pip install with streaming output, targeted --reinstall-package
- Revert nvidia-cublas-cu12/nvidia-cudnn-cu12 removal and transcriber.py DLL registration to fix broken GPU transcription
- Suppress pyannote SyntaxWarning for Python 3.12 escape sequences
- Skip fingerprint toast when match equals current speaker name
- Add analytics button active color

## Revamp settings pane with sidebar layout and Launch at Startup toggle (2026-03-22)

- Settings dialog widened to 640px with a sidebar nav (AI Assistant, API Keys, System) and section panels that switch without page reload
- API key fields now use card-style groups with stacked layout and inline Get-a-key links; only the active provider's card is shown
- System section: GPU status, Launch at Startup toggle (Windows only, creates/removes a shortcut in the Startup folder), and Update row
- Toggle switch component added (CSS + HTML)
- Added /api/settings/startup GET+POST routes and import sys to app.py

## Poll server after update restart to reset the update button (2026-03-22)

After applyUpdate triggers a restart the button was stuck on 'Restarting...' indefinitely. Now polls /api/settings/status every 1.5s until the server responds, then resets the button and shows 'Updated successfully'.

## Default AI provider to OpenAI; show only active provider's key field (2026-03-22)

- Default provider changed from Anthropic to OpenAI across settings.py, app.py fallbacks, and AI assistant initialisation
- Settings panel now hides the inactive provider's API key field instead of dimming it; toggling the provider toggles field visibility

## Move onnxruntime-gpu cleanup to after requirements install (2026-03-22)

diart pulls onnxruntime-gpu and nvidia-cudnn-cu12 back in as transitive dependencies, so uninstalling before requirements.txt had no effect. Move the cleanup step to after requirements install so the conflicting packages are removed regardless of what diart's dependency tree installs.

## Force CPU onnxruntime to fix cudnnGetLibConfig crash on CUDA 12 machines (2026-03-22)

pyannote.audio pulls onnxruntime-gpu which was built against cuDNN 8; cudnnGetLibConfig was removed in cuDNN 9 (bundled with torch cu12x), causing a fatal C-level crash. Swapping to CPU onnxruntime avoids the conflict — torch still handles all GPU compute for the diarizer.

## Remove standalone nvidia CUDA packages; use torch's bundled cuDNN instead (2026-03-22)

nvidia-cublas-cu12 and nvidia-cudnn-cu12 install a third copy of cuDNN alongside the ones already bundled in the torch and ctranslate2 Windows wheels. The version mismatch causes onnxruntime (via pyannote/diart) to crash with "Could not load symbol cudnnGetLibConfig. Error code 127".

Fix: register torch/lib via add_dll_directory so ctranslate2 finds the same cuDNN that torch uses, with no separate packages needed.

## Add HF_HUB_DISABLE_SSL_VERIFICATION support for corporate proxy environments (2026-03-22)

Pre-load .env at config module level so the flag is applied before any HuggingFace Hub imports happen. Documented as a commented-out option in the .env template and .env.example for users on networks with SSL inspection.

## Pre-install matplotlib binary wheel before diart to avoid MSVC requirement (2026-03-22)

diart pulls matplotlib transitively and can trigger a source build on machines without MSVC. Installing with --only-binary first ensures pip uses a prebuilt wheel regardless of diart's dependency resolution order.

## Pin matplotlib>=3.8.0 to avoid build failure on Python 3.12 (2026-03-22)

diart -> pyannote.audio pulls matplotlib transitively. Older versions have no prebuilt Windows wheel for Python 3.12 and fail to compile without MSVC Build Tools. 3.8.0+ ships cp312 wheels on PyPI.

## Stream pip progress for PyTorch and dependency installs (2026-03-22)

Shows download filenames/sizes and package names in real time so the user can see activity during the multi-minute PyTorch download.

## Accept Python 3.10 or 3.11 as valid; only winget-install if none found (2026-03-22)

## Add manual noise labeling, noise filter/solo, and Python 3.12 auto-install (2026-03-22)

- Noise badges are now clickable — opens a picker to reassign the segment to a real speaker (un-noise), wired up for both auto-detected and manually-marked noise segments
- Noise pill merges auto-detected and manually-marked groups into one count
- Noise pill now supports solo mode (click to show only noise, click again to reset), matching speaker pill behavior; right-click jumps to next
- Noise segments are exempt from the speaker filter so they stay visible in solo mode regardless of which speaker keys are filtered
- Add "Mark as Noise" button to the badge rename picker (global or one-off)
- Add [Noise] option to the reassign-to dropdown in the navigator
- Skip voice library training when the label override is [Noise]
- launch.bat: auto-install Python 3.12 via winget if not present, falling back through py launcher → known paths → winget install → re-check

## Updated README (2026-03-21)

## Rewrite README with comprehensive technical documentation (2026-03-21)

Covers architecture, models, pipeline details, voice library, analytics, configuration, troubleshooting, and project structure.

## Add analytics panel, diverse color palette, and speaker UX improvements (2026-03-21)

- Analytics panel with donut chart, KPI cards, animated timeline swimlanes, and horizontal bar charts for speaking time & segments
- Timeline rows animate in with staggered slide+scale on scroll
- Bar charts animate via IntersectionObserver as they enter viewport
- Expanded speaker color palette from 10 to 16 diverse colors
- Color clicks in Speaker Manager auto-save without needing Save button
- Clicking an already-selected speaker now deselects them
- Moved stats out of transcript navigator into dedicated analytics dropdown

## Added FontAwesome icons (2026-03-21)

## Make noise detection speaker-aware: reclaim segments when speaker confirmed (2026-03-21)

- Only label segments as [Noise] if the speaker hasn't produced any substantive (non-noise) segments yet
- Track confirmed speakers per session (_confirmed_speakers set)
- When a speaker's first real segment arrives, retroactively reclaim any earlier noise segments from that speaker back to their label
- Push transcript_update SSE events with source changes for reclaimed segments so the frontend re-renders badges in real time
- Add storage.update_segment_source() helper for DB updates
- Frontend handles source changes in transcript_update events

## Add voice library bulk management, noise detection, and UX improvements (2026-03-21)

- Voice Library: bulk select/delete/merge/optimize with search filter
- Auto-detect noise/filler segments (short fillers, laughter, fragments) and label as [Noise], hidden by default with toggle in transcript navigator
- Playback auto-scroll: use counter-based guard instead of timeout flag, center current segment, don't disable on programmatic scrolls
- Transcript navigator: noise pill with show/hide toggle
- Storage: add get_segment() and get_segments_by_speaker() helpers, busy_timeout
- Speaker DB: add extract_embedding_from_wav() helper, busy_timeout
- System tray: fix always-yellow icon, add dynamic tooltips
- SSE: fix reconnection race condition on page refresh
- Various sidebar, filter, and styling improvements

## Merge consecutive speech segments from same speaker (2026-03-21)

When two speech segments from the same speaker are separated by a short gap (< 2s) and the previous segment doesn't end with sentence-ending punctuation, merge them into a single segment. This reduces fragmentation from Whisper's pause-based flushing and improves readability of the transcript display.

Also implement source-gated mixing in the audio pipeline: only mix with the dominant audio source to prevent speaker-to-mic bleed from duplicating remote speech, and add CUDA kernel warmup to eliminate the first-inference latency spike.

## Added update mechanism (2026-03-20)

## Disabled HuggingFace symlink warning (2026-03-20)

## Updated .gitignore to ignore all .env files (2026-03-20)

## Added Hugging Face token with public model download permissions (2026-03-20)

## Fixed invalid .env generation (2026-03-20)

## Added logo PSD file (2026-03-20)

## Updated README.md (2026-03-20)
