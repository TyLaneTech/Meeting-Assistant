# Changelog

Release notes for people who use Meeting Assistant. The app shows this file in `Settings > Changelog`, and the newest entry in the What's new card after an update. Nothing else feeds those views.

How an entry is read:

- Each `## ` heading is one entry: the title, then the date in parentheses. Newest first.
- The title's first word picks the icon: Added, Fixed, Improved, Removed, Reworked, and so on.
- One entry per update. Everything that ships together goes under one heading, with `### ` sub-headings by area (Recording, Speakers, Settings) when it covers more than one.
- Infrastructure, docs, CI and tooling changes get no entry.

How to write one:

- Keep it tight. One bullet per change, one short sentence: what the user will notice. Not how it works, not why it was hard, not the history of the bug.
- Each bullet is a single line in this file. The page wraps text on its own, and a line break inside a sentence shows up on screen as a break.
- `**bold**` on the thing that changed, usually the bullet's first few words. Never a whole sentence.
- `` `code` `` for text the user sees or types in the app: a button (`Apply`), a path (`Settings > Calendar`), a value it shows. Never for a module, a function or a file path; users do not have those.
- Nested bullets and a `> ` note are for the rare caveat a user needs to act on. Most entries need neither.
- No filler, no marketing words, and no restating the title in the first bullet.


## Added the app's own notifications, plus fixes for Record and Speakers (2026-10-01)

### Notifications

- **Notifications are drawn by Meeting Assistant**, not Windows, so Focus Assist no longer hides them during calls.
- **They follow your app theme**, stay on top without taking focus, and wait while you hover.
- **They close themselves** once they no longer apply, like a meeting prompt after recording starts.
- **Each kind has its own sound**, from one of five packs. `Felt` is the default.

### Recording

- **`Record` shows `Starting…` immediately**, so you know the click registered. Extra presses are ignored.
- **If the app hangs while starting**, `Record` comes back after a minute.
- **The "transcription is behind" warning** is now a small note in the top bar, and stays closed once dismissed.
- **The call audio warning** no longer covers the top bar or its `Stop` button.

### Speakers

- **`Apply` closes the Speakers window** after saving. If saving fails, it stays open.
- **The recording preview** plays along with whatever is playing, instead of staying frozen.
- **Space pauses playback** in the Speakers window without it restarting a few seconds later.
- **Selecting a speaker** no longer shifts the window, so a click and drag stays on the speaker you picked.

### Settings

- **`Settings > Reminders > Notifications`** sets the position, sound pack, volume and `Stay Until Dismissed`, with a test button.
- **`Test Notification`** in the tray menu replaces `Test Toast`.

> macOS still uses Notification Center.


## Fixed recordings that would not start after an audio device changed (2026-09-24)

- **`Record` works again after a device change**, like unplugging a headset or undocking. It used to fail with `Insufficient memory` until a restart.
- **Newly connected devices** appear without restarting the app.
- **A device that cannot be opened** is named in the error message.
- **Stopping is about two seconds faster** when nothing is playing.


## Fixed Codex setup and made large Speakers groups easier to scan (2026-09-22)

### Speakers

- **Groups show their first three speakers**, with `Show all` for the rest.
- **The arrow on a group folds it** to one line; a toolbar button cycles every group through these views.
- **Groups no longer stretch** to match the tallest one beside them.

### Agent API

- **Re-running Codex setup** no longer breaks Codex. If it already did, press `Run setup` again.


## Reworked the Speakers window into one screen (2026-09-17)

- **`Apply` is always visible**; the selection bar no longer covers it.
- **The `Manage` tab is gone**; everything it did is on the main screen.
- **Click the coloured square** beside a name to change a speaker's colour.
- **`Voice Library`** moved into the toolbar.
- **`Add participant` is gone**; it only added a name with nothing behind it.


## Fixed long meetings losing the end of their transcript (2026-09-11)

### Recording

- **`Stop` keeps the transcription backlog** instead of discarding it; the transcript finishes after the recording ends.
- **The title, chapters and vault export wait** for the transcript to finish.
- **A warning shows** while transcription falls behind.

### Reanalyzing

- **An interrupted reanalysis puts the old transcript back** instead of leaving the meeting empty.
- **Opening the app again** no longer kills a reanalysis in progress.
- **Reanalyze older meetings that stop early** to recover their ending.


## Fixed AI assistant connections left running after a crash (2026-09-10)

- **A crashed or force-quit AI assistant** no longer leaves its background helper running until you restart.


## Added Storage, Free up space and a Join button, and sped up startup (2026-09-08)

### Home

- **Activity is one chart**, with choices for what it measures, how far back, and how it groups.
- **New Storage card** shows disk use by type, meeting, month or folder.
- **Next shows today and the next two days**, with a countdown to the next meeting.

### Free up space

- **`Free up space`** on the Storage card shrinks recordings: audio to Opus, video to AV1, HEVC or H.264.
- **It shows the savings first**, checks every file it writes, and skips meetings in use.
- **Transcripts, notes, speakers and undo** are unaffected.

### Calendar

- **`Join` button** for Teams, Zoom, Meet and Webex meetings, on the Calendar and on Home.

### Recording

- **The red recording bar is gone**; the top bar turns red and shows the level meters instead.
- **The elapsed timer** survives a page refresh.
- **`Call/desktop audio went silent`** no longer fires during normal pauses in conversation.
- **`Regenerate chapters after meeting`** (on by default) rebuilds chapters once the recording stops.

### App

- **The tray, notifications and Start Menu** bring up your open window instead of a new one.
- **The tray icon appears in about half a second**, not seven.
- **`Launch at Startup`** now shows when Windows has switched it off, and fixes that when you turn it on.
- **New `Open on Launch` and `Open from Start Menu`** settings control when the window opens.
- **Folders start closed**, and the sidebar names each of the last seven days.
- **`Name recordings after the meeting`** in `Settings > Calendar` titles recordings from your calendar.

### Agent API

- **AI assistants can name unidentified speakers** from transcript, calendar and voice evidence. Your own mic is never relabeled.
- **More assistant tools**: fix one line's speaker, rename or merge voice profiles, and file meetings into folders.


## Added a Home dashboard, a Calendar view, and a redesigned Speakers workflow (2026-09-05)

### Home

- **New Home dashboard** with headline stats, meeting load, a when-you-meet heatmap and the people you meet most.
- **Switching views** no longer reloads the page.
- **`Ask your meetings`** sits in a rail beside every view.

### Calendar

- **New Calendar view** shows scheduled meetings beside their recordings, from a published Outlook calendar link.
- **Recordings are matched to calendar events**, which offers attendee names and caps the speaker count for reanalysis.

### Speakers

- **The Speakers window opens on Cleanup**, offering the invite's attendees first.
- **New `Needs attention` page** lists recordings with unnamed speakers.
- **`Ask your meetings`** can rename speakers across recordings.

### Recording

- **A recording that captures only silence** raises an alert.
- **Auto-detected meetings record in the open window** instead of a new one.
- **Idle models unload** to free memory.
- **The desktop device you select** is always the one recorded.
- **Pausing and resuming** no longer corrupts per-speaker audio.

### App

- **`Settings > Icons`** swaps in your own app, tray and shortcut icons.
- **Optional Obsidian export**, and a freeze watchdog that restarts a hung app.
- **The Start Menu and sign-in shortcuts** start the app in the tray without a console window.


## Improved speaker naming, stopping and sidebar dragging (2026-08-28)

### Speakers

- **Returning voices in long meetings** are named automatically instead of piling up as new `Speaker` numbers.
- **A correction you make mid-meeting** sticks for the rest of the recording.
- **Voice profiles** only learn from confident matches.
- **New Voice Library `Health` tab** finds duplicates and misfiled samples; `Run Cleanup Now` fixes the clear cases.

### Recording and sidebar

- **`Stop` finishes almost immediately**; audio track conversion runs in the background.
- **Dragging meetings and folders** moves them the moment you drop.


## Added an Agent API so AI assistants can work with your meetings (2026-08-27)

- **Claude Desktop, Claude Code, Codex and your own scripts** can search and read your meetings.
- **Set it up in `Settings > Agent API`**, with one click per assistant.
- **Optional access token**; recording control stays off unless you turn it on. Nothing can be deleted.
- **Faster lookups** in large libraries.


## Added folder, date and speaker filters to Global Chat search (2026-08-25)

- **Ask about a project or client** and the chat searches that folder and its subfolders.
- **Narrow by date or by who attended**, alone or together.
- **Results show who said each line**, so quotes are attributed correctly.
- **An empty search widens step by step**, and the chat says what it widened.
- **The search index persists** between launches, and deleted meetings no longer appear as hits.


## Improved audio device persistence and chapter headings (2026-07-19)


## Added AI Chapters that mark the key topics in a meeting (2026-07-06)

- **Chapters mark the main topics** and can generate as the meeting goes.
- **They show on the playback bar, in the transcript and on the timeline**; click one to jump there.
- **The `Chapters` button** lets you rename, add, delete or regenerate them, and tune how they are made.
- **Exports include chapters**, and imports restore them.


## Added chat folder context, voice-ranked speakers and meeting auto-start (2026-06-30)

### Chat

- **One toolbox menu** attaches files or local folders, and remembers your folders per meeting.
- **The chat can explore attached folders**: list, search and read files.

### Speakers

- **Speaker pickers rank names by voice similarity**, with a match percentage on each.
- **`No` on a suggestion** stops that profile being suggested again in the meeting.
- **Ctrl-click speaker labels** to reassign several lines at once.

### Sidebar and recording

- **Folders stay pinned at the top while you search**, and still accept dropped meetings.
- **The sidebar scrolls** when you drag near its edge.
- **Optional auto-start** records detected meetings without asking, with a notification to confirm.


## Added macOS support, a one-line installer and cleaner mic audio (2026-06-27)

### macOS

- **Meeting Assistant runs on Apple Silicon Macs** (macOS 13+), with no audio driver to install.
- **A Meeting Assistant app** is installed in Applications, so it shows in Launchpad and Spotlight.
- **Apple Silicon installs** no longer pick up Intel-only packages.

### Install

- **One-line installer** for macOS and Windows; see the README.

### Audio

- **Echo cancellation runs before auto-gain**, so voices from your speakers are removed, not amplified.
- **New `Noise Suppression` toggle** under Echo Cancellation.
- **Speaker sound leaking into your mic** is no longer transcribed as you.
- **Whisper's stuck characters and stray caption credits** are filtered out.

### Fixes

- **NVIDIA GPUs are detected again** on newer drivers.
- **The spacebar** no longer ends a recording.
- **A renamed or reconnected mic** is found again automatically.


## Added a playback volume control and fixed transcription not loading on some machines (2026-06-25)

- **Volume slider** beside the playback bar; click the speaker icon to mute. Your level is remembered.
- **Startup checks each model is fully downloaded** and fetches what is missing, fixing transcription that would not start.


## Added name prompts when sharing recordings (2026-06-24)

- **Exporting asks for your name** while your mic speaker is still `You`, so others see who spoke.
- **Importing asks who recorded it** when their mic is still `You`, with your saved speakers to pick from.


## Added "this is you" mic labeling and reworked Speaker Cleanup (2026-06-23)

### Your microphone

- **Your mic is always one speaker, `You`**, and is never matched to anyone else.
- **Tell the app your name once** and it applies to every recording.
- **`Microphone is you`** in Settings turns this on or off.

### Speaker Cleanup

- **Select several voices** to assign, group, play or mark as noise together.
- **Assign a group from a searchable picker** ranked by voice similarity, or create a new voice.
- **`Sounds like...` suggestions** show a confidence score.
- **A similarity heatmap** flags groups that are likely the same person.
- **Expand a voice** to see when they spoke and play their lines.

### Speed

- **Summary and chat appear immediately** when you open a meeting; the transcript loads behind them.
- **Streaming replies, scrolling and resizing** are smoother.
- **Visualizers rest when idle**, saving CPU and battery.
- **Silence on your mic** is skipped instead of producing empty lines.


## Added meeting auto-detect and smoother video playback (2026-06-17)

- **Auto-detect meetings** (Windows, opt-in): the app offers to record when a Zoom or Teams meeting starts. Turn it on in `Settings > Reminders`.
- **Screen recordings stay in sync** with the audio, and seeking is quicker.
- **`Copy` on the summary** can include or leave out timestamps.
- **`Not now` and `Keep recording`** just close the notification.
- **Cloudflare WARP is left alone** by default; `Settings > System` can turn the old toggling back on.


## Added a Speaker Cleanup tab, plus mic, notification and network fixes (2026-05-29)

### Speaker Cleanup

- **New Cleanup tab** groups unrecognised voices by similarity; drag to merge or split them.
- **`Sounds like...` suggestions** come from your voice library, and `Apply confident` takes every strong match.
- **Nothing changes until you press `Apply`**, which also teaches your voice library.
- **Optional floating video player** shows who was talking.

### Fixes

- **Each recording re-checks your mic**, so an unplugged one fails clearly instead of recording silence.
- **Your voice is always captured**, even when the other side is loud; a bug was discarding most mic audio.
- **Windows notifications were rebuilt** with working buttons, plus a `Test Toast` tray item.
- **AI replies and downloads work behind Cloudflare WARP** without turning off security checks.
- **Startup is faster**, loading models straight from the cache.


## Added a Notes pane, a Changelog tab and folder-aware sidebar filters (2026-05-05)

- **New Notes column** with rich text, images and files, saved with each meeting and included in exports.
- **Drag images or files from Notes into Chat** to give the AI context.
- **`Settings > System Prompts`** now includes the prompt used to title new recordings.
- **New Changelog tab**, and a What's new card after each update.
- **Sidebar filters keep your folders**, showing the ones with matching meetings.
- **Whisper's repeated "Word. Word. Word." output** is detected and cleaned up.


## Added per-meeting summary instructions (2026-05-04)

- **Each meeting can have its own summary instructions**, overriding the default.
- **`Use as default for new sessions`** carries your instructions into new recordings.
- **`AI Assistant` settings are now `AI Providers`**, and default prompts have their own `System Prompts` tab.


## Fixed live transcription failing after a speech engine update (2026-05-01)

- **Live transcription works again**; a change in the speech engine had stopped it.
- **The Start Menu shortcut repairs itself** when it points at the wrong place.


## Added early macOS support and moved app data into one folder (2026-04-30)

- **First macOS support** for Apple Silicon, with transcription on the Mac's GPU.
- **Data, models and tools move into one storage folder** automatically on first launch; a custom data folder stays put.
- **Video follows the audio** when you filter by speaker.


## Added a movable data folder and date browsing in Global Chat (2026-04-28)

- **Choose where your data lives** in `Settings > System`; the app copies it and switches over.
- **Global Chat can browse meetings by date**, and links every meeting it mentions.
- **Opening a meeting expands its folder** in the sidebar.
- **Meetings stuck "In progress"** after a crash are fixed at startup.
- **Split meetings** keep their real start times.
- **Live speaker tracking** drifts less in long meetings.
- **Video preview no longer freezes** after seeking.


## Improved live speaker detection and gave each AI tool its own model (2026-04-24)

- **A new live speaker detector** handles overlapping speech better.
- **Summary, Chat and Global Chat** each keep their own model choice.
- **Resumed recordings** keep a single video file.
- **New transcription preset**: `large-v3-turbo`.


## Added custom theme accents, live model lists and split restore (2026-04-23)

- **Custom accent colour** for the app theme, with a strength slider.
- **Model lists update live** from each AI provider, with a refresh button.
- **Global Chat has a model picker** like meeting chat.
- **Each meeting** can have its own chat prompt.
- **Split meetings can be restored** to the original.
- **Claude model names read correctly**, like `Haiku 4.5`.
- **The Start Menu icon updates** when the logo changes.


## Added meeting import and export, editing tools and a quiet-recording reminder (2026-04-22)

- **Export a meeting** as a .zip with its transcript, summary, chat, speakers and audio; drop one on the app to import it.
- **Trim, split and restore** a meeting's audio and video.
- **A notification nudges you** when a recording has gone quiet.
- **The speaker picker offers Voice Library names** and `Mark as Noise`.
- **Your speaker corrections** now reach summaries and chat.
- **Right-click menus** for meetings and folders.
- **The tray menu gained** `Settings`, `Check for Updates` and `Restart Server`.


## Added separate AI models for Summary and Chat (2026-04-14)

- **Summary and Chat** can use different providers and models.
- **Long Claude chats cost less** through prompt caching.
- **Model lists** show only the latest version of each Claude model.
- **Runs of ellipses** are cleaned out of transcripts.
- **Changing an API key** takes effect immediately.


## Added audio and video upload and automatic voice profiles (2026-04-13)

- **Upload any audio or video file** to transcribe it like a recording.
- **Naming a speaker** creates or links their voice profile.
- **Starting the app a second time** replaces the running copy, unless it is recording.
- **Recording from Home or the tray** opens the meeting page first, fixing an echo.
- **The mic visualizer** responds faster.
- **Restart and shut down** skip the confirmation when nothing is recording.


## Added automatic gain, device auto-detect and web search in chat (2026-04-10)

- **Microphone capture is more reliable**, without distortion.
- **Automatic gain control** for mic and desktop audio, on by default.
- **Auto-detect devices** tests every input and picks the best.
- **Chat can search the web** when it needs to.
- **A typing cursor** shows while replies stream, and chats scroll to your new message.


## Added inline screenshots in chat, clickable timestamps and a power menu (2026-04-09)

### Chat

- **Screenshots appear inline in replies**; click one to zoom and pan.
- **Timestamps are clickable**, even while a reply is streaming.
- **Copying a reply or the summary** keeps its formatting.
- **Global Chat knows each person's meeting history** and folders.
- **The tool activity panel** opens while tools run and folds away when the answer starts.
- **Clearing a chat** stops the reply in progress.

### Transcript

- **A `Reanalyze` button** in the transcript header; reanalysis keeps your chat and summary.
- **Long monologues break into paragraphs** at pauses.
- **Fewer invented lines**, like "Subtitles by…" or repeated sentences.

### App

- **New power menu** with `Shut Down`, `Restart`, and `Update & Restart` when an update is waiting.
- **API keys are hidden** behind a reveal button, and show whether they are set.
- **Startup is faster** when models are already downloaded.
- **Chat can capture your screen mid-recording**, from the monitor you record.


## Added a home page with Global Chat and a dashboard (2026-04-08)

- **New home page** with Global Chat across all your meetings, and a dashboard of stats, recent meetings and top speakers.
- **Settings and the Voice Library** open from the home page too.
- **Chat shows the tools it is using**, and clearing a chat deletes it for good.
- **No more periods after every word** in diarized transcripts.
- **Refreshing a past meeting's summary** works again.
- **Recording is no longer blocked** if speaker detection fails to load.


## Added full-file reanalysis and speaker suggestions (2026-03-30)

- **Reanalyze a meeting** with whole-file transcription and speaker detection, with its own settings.
- **A bell collects speaker suggestions** for you to review.
- **Identify buttons** on unnamed speakers, and a flash when a name is applied automatically.
- **The transcript fills in live** during reanalysis.
- **The browser mic is released** when a test stops or the page closes.


## Fixed audio problems on resume and with the browser mic (2026-03-27)

- **Resumed recordings** no longer play back at double speed after an output device change.
- **Mic audio** is no longer choppy or slowed.
- **Switching away from the browser mic** releases it immediately.


## Added screen recording, a transcript minimap and presets (2026-03-25)

- **Screen recording** alongside the audio.
- **A minimap beside the transcript** shows who spoke when; click or drag to move.
- **Runs of lines from one speaker** can be collapsed into a group.
- **Transcription and speaker detection presets**, with reset buttons.
- **Video no longer freezes** when you scrub.
- **Resuming a meeting** continues its audio and timestamps.


## Added subfolders and better echo cancellation, and reworked speaker reassignment (2026-03-23)

- **Folders can nest**, and meetings and folders can be reordered by dragging.
- **Deleting a folder** says how many items it holds first.
- **Echo cancellation** uses WebRTC's canceller, with a single on/off switch.
- **Reassigning selected lines** changes only those lines, and survives a reload.
- **A toggle shows the original speaker labels** beside the names.
- **Voice Library names** are offered in the speaker picker.


## Added tunable audio settings, noise labeling and a redesigned Settings (2026-03-22)

- **Transcription and speaker detection settings** with sliders, explanations and reset buttons.
- **Settings has a sidebar** and a `Launch at Startup` switch.
- **Mark any line as noise**, or click a noise label to give it back to a speaker.
- **The noise filter** can show only noise.
- **OpenAI is the default provider**, and only its key field is shown.
- **Installs are faster** and show download progress.
- **A startup crash on some NVIDIA machines** is fixed.


## Added an analytics panel, noise detection and Voice Library bulk tools (2026-03-21)

- **Analytics panel** with talk time, a speaker timeline and charts.
- **Fillers and noise are labeled `[Noise]`** and hidden by default, until that speaker really talks.
- **Voice Library bulk tools**: select, delete, merge and search.
- **Consecutive lines from one speaker** are joined when they belong together.
- **More speaker colours**, and colour changes save immediately.
- **The tray icon** shows the right state, with a tooltip.


## Added in-app updates (2026-03-20)

- **Updates** can be installed from inside the app.
- **Speaker detection models download** without your own Hugging Face token.
- **First-run setup** no longer writes a broken configuration file.
