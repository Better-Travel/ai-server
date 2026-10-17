# Betty chat API for the mobile app

Status: proposed contract, 2026-10-18. The server side is not implemented yet (see
[mobile-integration-plan.md](mobile-integration-plan.md)). Fields marked **(new)** do not exist on
the server today; everything else does. Please review and send back anything that doesn't fit
the app before we build it.

The server is the `ai-server` FastAPI service. It is **stateless**: every request carries the
whole conversation, and the app owns the history.

## Base URL

| Where the app runs | Base URL |
|---|---|
| Android emulator | `http://10.0.2.2:8000` |
| iOS simulator | `http://localhost:8000` |
| Device on the same Wi-Fi | `http://<laptop IP>:8000` (start the server with `--host 0.0.0.0`) |

Interactive docs of the running server: `{baseUrl}/docs`.

## Endpoints

| Method and path | Use |
|---|---|
| `POST /api/v1/chat/stream` **(new)** | Main chat endpoint. Streams the reply as Server-Sent Events. |
| `POST /api/v1/chat` | Same turn, one blocking JSON response. Fallback and tests. |
| `GET /health` | `{"status": "ok"}` |

## Request

Both chat endpoints take the same body.

```json
{
  "messages": [
    {"role": "user", "content": "Warsaw for 3 days, I love castles and coffee"},
    {"role": "assistant", "content": "Lovely! When are you going?"},
    {"role": "user", "content": "From 2 November"}
  ],
  "language": "en",
  "mode": "voice",
  "today": "2026-10-18",
  "last_search": null
}
```

| Field | Type | Notes |
|---|---|---|
| `messages` | list, 1-50 items | Oldest first. `role` is `"user"` or `"assistant"`, `content` is non-empty text, at most 4,000 characters **(new limit)**. The last message must be from the user. The field is `content`, not `text`. |
| `language` **(new)** | string, optional | App language as an ISO 639-1 code (`"en"`, `"fr"`). Betty replies in it, so it matches the TTS voice. Without it she follows the traveller's language. |
| `mode` **(new)** | `"text"` or `"voice"`, default `"text"` | In `"voice"` Betty answers in 2-3 short plain sentences: no markdown, lists, links or emoji, so the text can go straight to TTS. |
| `today` **(new)** | `YYYY-MM-DD`, optional | The device's local date. Used to check that trip dates are not in the past and to resolve dates without a year. |
| `last_search` **(new)** | object, optional | The `search` object from the most recent turn that had one, sent back unchanged. Lets Betty answer follow-ups about the places she suggested. |

History rules:

- After a turn completes, append the assistant message with `content` = the `reply` from the
  `done` event (not the concatenated deltas, in case they differ).
- If a turn fails, do not add a partial assistant message to the history. Retry by sending the
  same history again.
- If the history grows past 50 messages, drop the oldest ones (keep the first user message if you
  can: it usually names the destination).

## Streaming: `POST /api/v1/chat/stream`

Request headers: `Content-Type: application/json`, `Accept: text/event-stream`.
The response is `text/event-stream`. Each event is:

```
event: <type>
data: <one line of JSON>

```

Lines starting with `:` are keep-alive comments (`: ping`, every 15 s while the server works).
Ignore them; they only keep the connection from timing out.

### Events

Events always arrive in this order: one `start`, then any number of `status`, `delta` and
`search` events, then exactly one `done` **or** one `error`, then the server closes the stream.

| Event | Data | What the app does |
|---|---|---|
| `start` | `{"turn_id": "6f1c..."}` | Show an empty assistant bubble with a typing indicator. Keep `turn_id` for bug reports. |
| `status` | `{"stage": "searching_places"}` | Show a localized hint ("Looking for places…"). Stages: `searching_places`. More may be added; ignore unknown ones. |
| `delta` | `{"text": "Lovely, "}` | Append to the bubble. In voice mode, also feed the sentence splitter (below). |
| `search` | the `search` object (below) | Places are ready: put pins on the map, keep it as the latest search. Arrives before Betty's text about the places. |
| `done` | `{"reply": "...", "search": {...} or null, "mentioned_place_ids": ["way/123", ...]}` | Replace the bubble text with `reply`, add it to the history, finish TTS. |
| `error` | `{"code": "upstream_unavailable", "message": "...", "retryable": true}` | Mark the bubble as failed (keep any partial text visible, greyed), offer retry if `retryable`. |

Notes:

- `delta` text is plain text in voice mode and may contain light markdown in text mode.
- Betty may write a short line before she searches ("Let me look for places…"); that text stays
  part of the reply. The server never takes back text it has streamed.
- `search` is sent at most once per turn. When the trip is accepted but the search fails,
  `search.error` is set and `places` is empty; Betty explains it in her text.
- `mentioned_place_ids` lists the `osm_id`s of places from `search.places` that Betty's reply
  names, in the order she names them. Empty when she named none or there was no search.

### Example

```
event: start
data: {"turn_id": "6f1c2a0e"}

event: delta
data: {"text": "Great, three days in Warsaw from 2 November! Let me find some spots."}

event: status
data: {"stage": "searching_places"}

: ping

event: search
data: {"trip": {...}, "summary": "...", "total": 57, "places": [...], "error": null}

event: delta
data: {"text": "\n\nStart at the Royal Castle, "}

event: delta
data: {"text": "then have coffee at ..."}

event: done
data: {"reply": "Great, three days ...", "search": {...}, "mentioned_place_ids": ["way/24601", "node/1357"]}

```

### Errors

Before the stream starts, problems with the request are plain HTTP errors with a JSON body
`{"detail": ...}`:

| Status | When |
|---|---|
| 422 | Invalid body (missing fields, last message not from the user, too many or too long messages). |
| 503 | Server is not configured (no model API key). |

After the stream has started the status is always 200 and failures arrive as an `error` event:

| `code` | `retryable` | Meaning |
|---|---|---|
| `upstream_unavailable` | true | The language model failed or timed out. |
| `internal` | true | Unexpected server error. |

A failed place search is **not** an error event: the turn completes and `search.error` says what
happened.

### Cancelling and timeouts

- To cancel a turn (the user sends another message, starts listening, or leaves the screen),
  close the connection. The server stops the model call.
- A turn with a place search can take a minute or more. Configure the client for that: socket
  (read) timeout of at least 30 s (keep-alives arrive every 15 s) and an overall request timeout
  of about 180 s. Engine defaults can be as short as 10 s (OkHttp), which is too short for the
  blocking endpoint.

## Blocking: `POST /api/v1/chat`

Same request. Response:

```json
{
  "turn_id": "6f1c2a0e",
  "reply": "Great, three days in Warsaw ...",
  "search": null,
  "mentioned_place_ids": []
}
```

`turn_id` and `mentioned_place_ids` are **(new)**. `search` is set only on a turn where Betty had
enough information and searched.

## The `search` object

```json
{
  "trip": {
    "city": "Warsaw",
    "district": null,
    "country": "PL",
    "scope": "city",
    "start_date": "2026-11-02",
    "end_date": null,
    "duration_days": 3
  },
  "summary": "Destination: Warsaw, country PL. Dates: from 2026-11-02, 3 days. ...",
  "total": 57,
  "places": [
    {
      "osm_id": "way/24601",
      "name": "Zamek Królewski w Warszawie",
      "type": "way",
      "category": "historic=castle",
      "lat": 52.2479,
      "lon": 21.0152,
      "tags": {
        "historic": "castle",
        "tourism": "museum",
        "name:en": "Royal Castle in Warsaw",
        "addr:street": "plac Zamkowy",
        "addr:housenumber": "4",
        "opening_hours": "Tu-Su 10:00-18:00",
        "website": "https://www.zamek-krolewski.pl",
        "wheelchair": "yes",
        "wikidata": "Q1074447"
      },
      "wikipedia": {
        "pl": {"title": "Zamek Królewski w Warszawie", "url": "https://pl.wikipedia.org/wiki/...", "extract": "..."},
        "en": {"title": "Royal Castle, Warsaw", "url": "https://en.wikipedia.org/wiki/...", "extract": "..."}
      }
    }
  ],
  "error": null
}
```

(Values in the example are illustrative.)

| Field | Notes |
|---|---|
| `trip` | What Betty confirmed. Dates are `YYYY-MM-DD`; any of `district`, `start_date`, `end_date`, `duration_days` can be null. |
| `summary` | The text the search was built from. Debugging only; don't show it. |
| `total` | Distinct places OpenStreetMap found (duplicates removed). |
| `places` | At most 20, a mix of kinds, notable ones first. |
| `places[].osm_id` | Stable id, `"node/…"`, `"way/…"` or `"relation/…"`. Use it as the place id for bookmarks, deduping and refreshing. |
| `places[].name` | Local (usually Polish) name; may be null. Prefer `tags["name:en"]` in an English UI when present. |
| `places[].category` | One tag saying what the place is (`"tourism=museum"`, `"amenity=cafe"`) or `"other"`. Good for icons. |
| `places[].lat`, `lon` | Can be null (rare). Skip such places on the map. |
| `places[].tags` | Only these keys, each optional: `amenity`, `tourism`, `historic`, `leisure`, `cuisine`, `opening_hours`, `website`, `contact:website`, `phone`, `contact:phone`, `name:en`, `addr:street`, `addr:housenumber`, `addr:postcode`, `wikidata`, `wheelchair`, `diet:vegetarian`, `diet:vegan`, `fee`. `opening_hours` uses the [OSM syntax](https://wiki.openstreetmap.org/wiki/Key:opening_hours). |
| `places[].wikipedia` | Optional, keyed by `"pl"` / `"en"`: a title, URL and a short intro (at most about 300 characters). Only Polish and English exist; a French UI can show the English one. |
| `error` | Set when the trip was accepted but the place search failed; `places` is then empty. |

Unknown fields may be added later: keep `ignoreUnknownKeys = true`.

## Mapping to the app's domain

- **Map pins**: every place in `search.places` with coordinates. Highlight the ones in
  `mentioned_place_ids`.
- **Route card** (`SuggestedRoute`): the server does not plan routes yet, so `done` has no
  `route`. Build one on the device when `mentioned_place_ids` has at least `MIN_ROUTE_STOPS`
  places: stops in that order (`RouteStop(name, GeoPoint(lat, lon))`), the path from
  `WalkingRouting.walkingPath`, distance and walking time from that path, and a title such as
  the trip city. When the server learns to plan routes, `done` will gain a `route` field in the
  app's current shape and this step goes away.
- **Bookmarks**: use `osm_id` as `BookmarkedPlace.id` instead of `name@lat,lon`.
- **Last search**: keep the most recent non-null `search` with the conversation and send it back
  as `last_search`.

## Voice mode

The app keeps doing the audio: STT on the device, TTS through Google Cloud. What changes is that
Betty can start speaking while the reply is still streaming.

1. Send `mode: "voice"` and `language` = the app language (the same one used for the TTS voice).
2. Split the deltas into sentences: append each delta to a buffer; whenever the buffer contains
   `.`, `!`, `?` or `…` followed by whitespace, cut there. Merge pieces shorter than about 40
   characters with the next one to save TTS requests. On `done`, flush what is left.
3. Speak the sentences one after another through a queue. To avoid gaps, synthesize the next
   sentence while the current one plays (split today's `speak` into "synthesize to MP3" and
   "play").
4. The first `delta` may be a short line before a search ("Let me look for places…"). Speak it:
   it covers the wait. While `status: searching_places` is the latest event and nothing is
   playing, the UI can show a "searching" state.
5. Stopping (the user taps the mic, sends a message or closes voice mode): close the stream,
   clear the queue, stop playback, exactly as today.

## Kotlin sketch

Domain (replaces `AIChatting.reply` returning one `Single`):

```kotlin
sealed interface ChatEvent {
    data class Started(val turnId: String) : ChatEvent
    data class Status(val stage: String) : ChatEvent
    data class Delta(val text: String) : ChatEvent
    data class PlacesFound(val search: PlaceSearch) : ChatEvent
    data class Done(val reply: String, val search: PlaceSearch?, val mentionedPlaceIds: List<String>) : ChatEvent
}

interface AIChatting {
    /** Cold: subscribing starts the turn, disposing cancels it. Errors arrive as ChatException. */
    fun replyStream(history: List<ChatMessage>, options: ChatOptions): Observable<ChatEvent>
}

data class ChatOptions(
    val language: String,
    val voice: Boolean,
    val today: String,
    val lastSearch: PlaceSearch?,
)
```

Networking with Ktor 3 (`ktor-client-core` includes the SSE plugin; `install(SSE)` in
`HttpClientFactory`, plus `HttpTimeout`):

```kotlin
fun replyStream(history: List<ChatMessage>, options: ChatOptions): Observable<ChatEvent> =
    channelFlow {
        client.sse(
            urlString = "$baseUrl/api/v1/chat/stream",
            request = {
                method = HttpMethod.Post
                contentType(ContentType.Application.Json)
                setBody(ChatRequestDto.from(history, options))
            },
        ) {
            incoming.collect { event ->
                val data = event.data ?: return@collect
                when (event.event) {
                    "start" -> send(ChatEvent.Started(json.decodeFromString<StartDto>(data).turnId))
                    "status" -> send(ChatEvent.Status(json.decodeFromString<StatusDto>(data).stage))
                    "delta" -> send(ChatEvent.Delta(json.decodeFromString<DeltaDto>(data).text))
                    "search" -> send(ChatEvent.PlacesFound(json.decodeFromString<PlaceSearchDto>(data).toDomain()))
                    "done" -> send(json.decodeFromString<DoneDto>(data).toDomain())
                    "error" -> throw json.decodeFromString<ErrorDto>(data).toException()
                    else -> Unit // unknown events: ignore
                }
            }
        }
    }.asObservable() // reaktive coroutines-interop
```

If POST with a body turns out awkward with the SSE plugin on one platform, the stream is plain
text: `client.preparePost(...).execute { response -> response.bodyAsChannel() }` and read it line
by line (`event:` line, `data:` line, blank line ends an event).

Mock server (Ktor server), so UI work goes on without the real backend:

```kotlin
post("/api/v1/chat/stream") {
    val request = call.receive<ChatRequest>()
    call.respondTextWriter(ContentType.Text.EventStream) {
        fun event(type: String, json: String) { write("event: $type\ndata: $json\n\n"); flush() }
        event("start", """{"turn_id":"mock-1"}""")
        for (word in replyTo(request).split(" ")) {
            event("delta", """{"text":${Json.encodeToString("$word ")}}""")
            delay(60)
        }
        event("done", """{"reply":"…","search":null,"mentioned_place_ids":[]}""")
    }
}
```

## Checklist for the mobile app

- [ ] DTOs: `content` instead of `text`; request fields `language`, `mode`, `today`, `last_search`.
- [ ] `AIChatting.replyStream` over SSE, with `HttpTimeout` (socket ≥ 30 s, request ≈ 180 s).
- [ ] Chat UI: assistant bubble created on `start`, filled by `delta`, finalized by `done`;
      "searching places" hint on `status`; failed state with retry on `error`.
- [ ] Cancel the turn (dispose) on a new message, on starting to listen, and on leaving the screen.
- [ ] History: append `done.reply`; never append partial text; cap at 50 messages.
- [ ] Voice mode: `mode: "voice"`, sentence splitter, TTS queue with prefetch, stop on interrupt.
- [ ] Places: pins from `search.places`, highlight `mentioned_place_ids`, details from tags and
      Wikipedia, bookmarks keyed by `osm_id`.
- [ ] Route card built on the device from `mentioned_place_ids` + `WalkingRouting`.
- [ ] Keep `last_search` and send it back.
- [ ] Mock server: `POST /api/v1/chat/stream` and `POST /api/v1/chat` with the new shapes.
- [ ] Base URL per platform; port 8000.
