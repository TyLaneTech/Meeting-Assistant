"""AI speaker detection: who the meeting app showed as speaking, read off the
screen recording by a vision model, turned into named speakers.

The pieces, in the order a run uses them:

- ``planner``      picks the moments to look at: a few full-frame scouts
                   that learn the meeting window's layout and roster, reads
                   anchored in each speaker key's turns, and audit reads that
                   ignore the keys, so a voice the diarizer merged or split
                   is caught rather than assumed.
- ``vision``       sends frames to the configured provider in parallel
                   batches with structured output and adaptive concurrency.
- ``prompts``      the instructions and the output schema (versioned).
- ``observations`` turns answers into time-stamped observations: the visual
                   timeline, stored against meeting time, never against
                   speaker keys, so it survives a reanalysis.
- ``names``        maps the labels read on screen to people.
- ``resolver``     fuses the timeline with voice-library and transcript
                   evidence into a change set: names, overrides of the voice
                   library, merges, moved lines, profile links and training.
- ``instructions`` turns the user's own words into a run spec (scope,
                   autonomy, voice-library policy, constraints).
- ``runs``         the orchestrator: lifecycle, progress, cancellation,
                   budget, and applying the change set through the app's own
                   speaker functions, journaled (core.speaker_journal) so
                   every change can be undone.

AGENT.md ("AI speaker detection") describes how it fits the rest of the app.
"""
