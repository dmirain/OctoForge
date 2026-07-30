# Images and voice

The conversation model reads text. Pictures and recordings are turned into text by separate models behind
their own ports before they ever reach a dialog, which keeps the main model free of multimodal
requirements and keeps the cost of each modality visible.

## Images

### Two tiers, on purpose

| Tier | When it runs | Configured by |
|---|---|---|
| Ingestion | Every incoming image is described as it arrives (~4–8 s) | `OF_VISION_MODEL` |
| `image_look` | Only when the user asks something specific about a picture (~15–30 s, several times the tokens) | `OF_VISION_DEEP_MODEL` |

The gap between the two is measured, not assumed, which is why the expensive model is not used for
ingestion. An empty `OF_VISION_MODEL` turns ingestion off and images arrive as text placeholders; an empty
`OF_VISION_DEEP_MODEL` hides the `image_look` tool.

### How an image travels

1. The transport downloads the picture and asks the ingestion tier to describe it.
2. The description enters the dialog as text, with the image kept as an `Attachment` on the message.
3. If the user later asks about it, `image_look(question)` re-fetches the bytes through the
   `ImageResolver` port (transport-owned, because only the transport can turn a reference back into
   bytes) and asks the strong tier.

`image_look` is bound into `ToolContext.image_inspector` only when both a vision client and a resolver are
present for that dialog; otherwise the tool hides itself. In Telegram an album arrives as one message with
every page kept, rather than one message per photo.

### Material, not a question

A picture with no caption is *material*: the user shared something without asking anything. It joins the
dialog's material collection instead of opening an obligation — see [exchanges.md](exchanges.md).

## Voice

A voice message is the user speaking, so it becomes **the user's own words**: the transcript enters the
dialog as their message, not as material and not as a quote.

`TranscriptionClient.transcribe(audio, config)` is the port; the shipped implementation posts to an
OpenAI-compatible `/audio/transcriptions` endpoint. One tier is enough — unlike a picture, a recording has
no "look closer": the transcript *is* the message.

Two details that come from the real world rather than from the design:

- **File names matter.** These APIs pick a decoder by extension, so the file name travels with the bytes.
  Telegram hands out voice notes as `.oga`, which the API rejects outright, so names are normalized
  (`.oga → .ogg`) before upload.
- **No fallback to the LLM endpoint.** `/audio/transcriptions` is a different endpoint kind, and a
  chat-only gateway answers 404 for it. Both `OF_STT_BASE_URL` and `OF_STT_MODEL` must be set explicitly,
  or the feature stays off and a recording gets a "text only" notice.

Recordings longer than `OF_VOICE_MAX_SECONDS` are refused before download — a guard on both latency and
the provider's daily quota.

## Invariants

- **The main model never receives image bytes or audio.** Only text derived from them.
- **Vision and speech are separate ports**, each independently switchable; either being off leaves the
  rest of the system working.
- **`image_look` is invisible when it cannot work** (no vision client, or no resolver for that dialog).
- **A voice transcript is the user's message**; a caption-less picture is material. The transport decides,
  because it knows who produced what.
- **Attachment bytes are never stored** by the core — the transport keeps the reference and can re-fetch.
- **Duration is checked before download.**

## Configuration

| Variable | Effect |
|---|---|
| `OF_VISION_MODEL` | Ingestion tier; empty disables image understanding |
| `OF_VISION_DEEP_MODEL` | Strong tier for `image_look`; empty hides the tool |
| `OF_VISION_BASE_URL`, `OF_VISION_API_KEY` | Vision endpoint; inherit the LLM's when empty |
| `OF_STT_BASE_URL`, `OF_STT_MODEL` | Both required for transcription; no inheritance |
| `OF_STT_API_KEY`, `OF_STT_LANGUAGE` | Key, and an optional language hint |
| `OF_VOICE_MAX_SECONDS` | Longest accepted recording (default 600) |

## Failure modes

| Situation | Outcome |
|---|---|
| Vision not configured | Images arrive as placeholders; the dialog continues in text |
| Vision provider fails | The failure is reported into the dialog as text; the message still arrives |
| `image_look` called with no image in the dialog | `VisionUnavailableError`, explained to the model |
| Image reference can no longer be fetched | Same — the tool reports it instead of guessing |
| Transcription endpoint missing (`/audio/transcriptions` 404) | Feature reported off rather than silently failing; both URL and model are required |
| Recording longer than the cap | Refused with a message before download |
| Unsupported audio extension | Normalized where known (`.oga`), otherwise rejected by the provider and reported |

## Code anchors

- `core/src/octoforge_core/vision/api.py` — `VisionClient`, `ImageResolver`, `ImageData`,
  `VisionUnavailableError`
- `core/src/octoforge_core/vision/client.py` — the OpenAI-compatible vision client
- `core/src/octoforge_core/vision/tools.py` — the `image_look` tool and its visibility rule
- `core/src/octoforge_core/speech/api.py` — `TranscriptionClient`, `AudioData`, accepted extensions
- `core/src/octoforge_core/speech/client.py` — the transcription client
- `web/src/octoforge_web/telegram/images.py`, `web/src/octoforge_web/telegram/poller.py` — ingestion,
  albums, resolvers, voice handling
- `core/tests/test_vision_tools.py`, `core/tests/test_speech_client.py`,
  `web/tests/test_telegram_images.py`
