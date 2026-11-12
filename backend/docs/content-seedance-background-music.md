# Seedance background music

The `seedance` music provider uses a separate Ark video-generation request with
`generate_audio=true`, then extracts MP3/WAV locally. It is not an audio-only API:
video-generation billing applies even though only audio is retained.

Only the music adapter is switched. The normal video model, video task queue,
reference assets, timeline, and image generation are not changed. Both video
entry points use the existing `generate-music` route and receive music assets.

Configuration (restart the content service when other work permits):

```dotenv
CONTENT_MUSIC_PROVIDER=seedance
CONTENT_MUSIC_MODEL=doubao-seedance-2-0-fast-260128
CONTENT_MUSIC_MIN_DURATION_SECONDS=5
CONTENT_MUSIC_MAX_DURATION_SECONDS=15
CONTENT_MUSIC_TIMEOUT_SECONDS=360
CONTENT_MUSIC_POLL_INTERVAL_SECONDS=5
```

Credentials resolve in this order: dedicated `CONTENT_MUSIC_API_KEY` (or secret
file), `CONTENT_AI_VIDEO_API_KEY` (or secret file), shared `CONTENT_ARK_API_KEY`.
No music AK/SK is required for this adapter. Model visibility in Ark's catalog
does not itself prove the account can submit paid tasks for that model.

This configuration uses the same Seedance 2.0 Fast model as the existing video
service, without changing that service's model or credentials.
Requested audio duration is 1–15 integer seconds. Requests below 5 seconds submit
5 seconds of video, then trim actual decoded audio. There is no automatic
resubmission, looping, padding, mock-music fallback, or fallback to another model.
The prompt requests instrumental music without voices or sound effects; model
compliance still requires listening to the result. No vocal-isolation guarantee
is made. Missing/corrupt/too-short audio fails instead of producing a fake result.

Only allowlisted HTTPS CDN outputs are downloaded, without forwarding Ark
credentials. Video intermediate files are temporary; the music artifact records
the provider task ID and video-billing provenance, never the signed media URL.

Verification uses mocked HTTP responses and real local FFmpeg extraction. It
does not submit paid model jobs or mutate existing user projects.
