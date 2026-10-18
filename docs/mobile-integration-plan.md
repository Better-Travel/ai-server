# Mobile integration: gap analysis and plan

> **Progress (2026-10-18):** done on the server: the route (`search.routes[0].stops`, item 7),
> streaming (`POST /api/v1/chat/stream`, item 2), socket keep-alives (item 5) and `turn_id`
> (item 15). The shared contract (item 1) is [mobile-chat-api.md](mobile-chat-api.md), which
> describes what is implemented. Still open: items 3, 4, 6 and 8-14.

Written as a proposal on 2026-10-18; the gap analysis below is kept as it was, with the progress
above.

## Where things stand

| | Mobile app (`mobileApplication`) | AI server (`ai-server`) |
|---|---|---|
| Endpoint | `POST {baseUrl}/chat` (Ktor mock server on port 8080) | `POST /api/v1/chat` (FastAPI, port 8000) |
| Request | `{"messages": [{"role", "text"}]}` | `{"messages": [{"role", "content"}]}` |
| Response | `{"text", "route"?}`, `route` = title, distanceKm, durationMinutes, stops[name, latitude, longitude] | `{"reply", "search"?}`, `search` = trip, summary, total, places[osm_id, name, category, lat, lon, tags, wikipedia], error |
| Delivery | One blocking `Single<ChatReply>` | One blocking JSON response |
| Voice | Native STT; Google Cloud TTS speaks the whole reply after it arrives | No knowledge of voice mode |
| Language | App in English or French; TTS voice picked from the app language | Betty answers in whatever language the conversation is in; nothing tells her the app language |

The two sides have never talked to each other: the mobile app only knows its own mock server.

## What is missing

### Must have for the first integration

1. **A shared contract.** Path, field names and the response shape differ (`text` vs `content`,
   `text`/`route` vs `reply`/`search`). The server's names win (the Streamlit UI and tests already
   use them); the mobile DTOs and the mock server change.
2. **Streaming.** A turn can take long: Betty's LLM call, then (when she searches) a summary call,
   the Overpass query LLM, Overpass itself and Wikipedia, then Betty's final answer. That can
   take tens of seconds, and the timeouts allow minutes. Today the app shows a spinner for
   all of it and only then starts TTS. Needed: `POST /api/v1/chat/stream` (Server-Sent Events)
   with text deltas, a "searching places" status, the search result as soon as it exists, and a
   final `done` event.
3. **Speakable text in voice mode.** Betty may use markdown or lists, which TTS reads badly. The
   request needs `mode: "voice"`, and in that mode the prompt asks for short, plain sentences with
   no markdown, lists, URLs or emoji.
4. **Reply language.** The request needs `language` (`en`, `fr`, ...) so Betty answers in the app
   language, which is also the TTS voice. Without it a French UI can get an English voice reading
   French, or the other way round.
5. **Socket timeouts.** HTTP engine defaults (OkHttp's read timeout is 10 s) can be shorter than
   a searching turn. The stream sends a keep-alive comment every 15 s; the client sets an overall
   timeout of about 3 minutes.
6. **Which places Betty actually suggested.** `search.places` has up to 20 places, but Betty names
   3-5 of them in the reply. The app can't tell which. The server adds `mentioned_place_ids`
   (the `osm_id`s of returned places whose name appears in the reply, in order of appearance).
   The app uses them to highlight pins or to build a walking route.

### Should have soon after

7. **Route.** The app needs an ordered list of coordinates; the server has none (Betty does not
   plan routes). This is the current priority and is being implemented on the server; the
   contract gets the field once it lands.
8. **Memory of earlier results.** The server is stateless and only sees message text, so on the
   next turn Betty no longer has the places she was given ("tell me more about the second one"
   fails). The request gets an optional `last_search` (the `search` object from the previous
   turn); the server gives it to Betty as context.
9. **Client-side date.** Trip dates are checked against the server's `date.today()`. Near
   midnight, or for a traveller in another time zone, "tomorrow" can be rejected as past. The
   request gets an optional `today` (the device's local date).
10. **Limits.** No limit on history length or message size today. Add: at most 50 messages and
    4,000 characters per message; older messages are dropped by the client, not the server.
11. **Bookmarks by OSM id.** `BookmarkedPlace.idOf` uses `name@lat,lon`. With `osm_id` the app
    can dedupe, save favourites and refresh a place reliably.

### Before release (not blocking development)

12. **Auth and abuse protection.** The API has no authentication. Add a per-app API key header
    (`X-Api-Key`) to start, then rate limiting per device. HTTPS only.
13. **Privacy.** The server logs user messages at `INFO`. Before real users, log only metadata
    (lengths, timings, statuses) at `INFO`.
14. **TTS key in the app binary.** The mobile docs already flag that the Google TTS key ships in
    the app. If the team wants a proxy, `POST /api/v1/speech` on this server is a small addition;
    the app would still own playback. Open question, not in this plan.
15. **Observability.** A `turn_id` per turn (returned in the stream and logged) so the mobile
    developer can quote it in bug reports.

## Implementation plan

### Server (ai-server)

Phase 1: streaming contract (unblocks the mobile developer). **Streaming, keep-alives and
`turn_id` are done; `language`, `mode`, `today`, limits and `mentioned_place_ids` are not.**

1. `app/schemas.py`: `ChatRequest` gains `language: str | None`, `mode: Literal["text", "voice"]`
   (default `"text"`), `today: date | None`, `last_search: PlaceSearch | None`, and limits on
   history length and message size. `ChatResponse` gains `turn_id` and `mentioned_place_ids`.
   Add one Pydantic model per stream event (`start`, `status`, `delta`, `search`, `done`,
   `error`) so the contract is in the OpenAPI schema and tested.
2. `app/prompts.py`: language rule ("always reply in {language}"), voice-mode rules (plain
   sentences, no markdown/lists/URLs/emoji, 2-3 sentences), `today` from the request when given.
3. `app/services/betty_service.py`: `chat_stream()`, a generator of events.
   - Call the model with `stream=True`. Forward content chunks as `delta`; collect tool-call
     chunks by index until the round ends.
   - On a `search_places` call: emit `status: searching_places`, run the existing `_search`, emit
     `search`, then stream the next round. Text streamed before a tool call stays part of the
     reply (the server never takes back text); rounds are joined with a blank line.
   - Keep the existing rules: tools are removed on the last round, an empty answer is retried
     once (nothing was streamed, so the retry is invisible), upstream errors become an `error`
     event.
   - The blocking `chat()` keeps working; it can become "run `chat_stream()` and collect".
   - Risk: NVIDIA's OpenAI-compatible endpoint must stream tool calls for the chosen model.
     Check this first with a short script. Fallback if it does not: run tool rounds without
     streaming and stream only the final answer round (the user then waits through the search
     with a status, which is still fine).
4. `mentioned_place_ids`: after the reply is complete, match each returned place's `name` (and
   `name:en`) against the reply, case-insensitively, keep the order of first appearance.
5. `app/main.py`: `POST /api/v1/chat/stream` returning `text/event-stream`
   (`Cache-Control: no-cache`, `X-Accel-Buffering: no`), a `: ping` comment every 15 s, stop
   the upstream stream when the client disconnects. Validation errors stay plain HTTP 422 before
   the stream starts; anything after the first byte is an `error` event.
6. Tests with a fake streaming OpenAI client: plain reply, reply after a search, upstream failure
   mid-stream, empty-answer retry, voice/language prompt rules, `mentioned_place_ids`. Update the
   README and the Streamlit UI (optional: show deltas live).

Phase 2: context and robustness

7. `last_search` in the prompt as a compact list (osm_id, name, category), so follow-up questions
   work.
8. `X-Api-Key` check (key from settings; disabled when unset, for local development), basic rate
   limit, metadata-only `INFO` logs, `turn_id` in all logs for a turn.

Route (current priority, being implemented separately): the server returns the route as an
ordered list of coordinates; the contract gets the field once it lands.

### Mobile (mobileApplication)

Tracked in detail in [mobile-chat-api.md](mobile-chat-api.md#checklist-for-the-mobile-app).
In short: new DTOs, an SSE-based `AIChatting` that emits events instead of one `Single`, the
in-progress assistant bubble filled by deltas, sentence-by-sentence TTS in voice mode, places
on the map from `search.places`, the route drawn from the server's ordered coordinates, bookmarks by
`osm_id`, and the mock server speaking the same stream so UI work continues offline.

### Order and hand-off

1. Agree on [mobile-chat-api.md](mobile-chat-api.md) with the mobile developer (this is the
   only thing that blocks the mobile side).
2. In parallel: server Phase 1, and the mobile app against its updated mock server.
3. Point the app at the real server (`http://10.0.2.2:8000` from the Android emulator,
   `http://localhost:8000` from the iOS simulator) and test the voice flow end to end.
4. Phase 2 before any external testers.
