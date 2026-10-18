# Betty chat API for the mobile app

Status, 2026-10-18: **implemented** on the `sofeeka` branch of `ai-server`. This page describes
what the server does today. Things that are planned but not built yet are listed in
[Not available yet](#not-available-yet); the wider plan is in
[mobile-integration-plan.md](mobile-integration-plan.md).

**The first priority for the app is the route**: an ordered list of coordinates, in
`search.routes[0].stops` (see [The route](#the-route)). Streaming is there so the app can show
Betty's text (and start TTS) while she is still writing.

The server is a FastAPI service. It is **stateless**: every request carries the whole
conversation, and the app owns the history.

## Base URL

| Where the app runs | Base URL |
|---|---|
| Android emulator | `http://10.0.2.2:8000` |
| iOS simulator | `http://localhost:8000` |
| Device on the same Wi-Fi | `http://<laptop IP>:8000` (start the server with `--host 0.0.0.0`) |

Start the server with `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`. Its interactive
docs are at `{baseUrl}/docs`.

## Endpoints

| Method and path | Use |
|---|---|
| `POST /api/v1/chat/stream` | Main chat endpoint. Betty's reply streams as Server-Sent Events. |
| `POST /api/v1/chat` | The same turn as one blocking JSON response (same body as the stream's `done` event). |
| `GET /health` | `{"status": "ok"}` |

## Request

Both chat endpoints take the same body:

```json
{
  "messages": [
    {"role": "user", "content": "Warsaw for 3 days, I love castles and museums"},
    {"role": "assistant", "content": "Lovely! Which dates?"},
    {"role": "user", "content": "17 to 19 November"}
  ]
}
```

- `messages`: oldest first, at least one. `role` is `"user"` or `"assistant"`, `content` is
  non-empty text. The field is **`content`** (the mock server currently uses `text`).
- The last message must be from the user, otherwise the server answers HTTP 422.
- Unknown fields are ignored, so the app can already send fields from
  [Not available yet](#not-available-yet) without breaking anything.

History rules for the app:

- When a turn completes, append `{"role": "assistant", "content": done.reply}`. Use the `reply`
  from `done`, not the concatenated deltas (they differ in surrounding whitespace).
- If a turn fails or is cancelled, don't add the partial assistant text to the history. Retry by
  sending the same history again.
- Keep the history to about 50 messages; drop the oldest ones first (the server doesn't enforce
  a limit yet, but long histories make every turn slower).

## Streaming: `POST /api/v1/chat/stream`

Request headers: `Content-Type: application/json`, `Accept: text/event-stream`. The response is
`text/event-stream` with status 200. Each event is two lines and a blank line:

```
event: <name>
data: <one line of JSON>

```

Lines starting with `:` are comments. The server sends `: ping` whenever it has sent nothing
for 15 seconds. Ignore them; they only keep the connection open.

### Events

A turn is always: one `start`, then any number of `status`, `delta`, `search` and `events` (in
the order things happen), then exactly one `done` **or** one `error`. Then the server closes the
stream.

| Event | Data | What the app does |
|---|---|---|
| `start` | `{"turn_id": "9c11e0a93bda"}` | Show an empty assistant bubble with a typing indicator. Keep `turn_id`: it is in the server logs, quote it in bug reports. |
| `status` | `{"stage": "searching_places"}` | Show a localized hint such as "Looking for places…". This can last minutes (see [Timing](#timing)). Ignore stages you don't know; more may be added. |
| `delta` | `{"text": " to 19"}` | Append `text` to the bubble. |
| `search` | the [search object](#the-search-object) | Places and the route are ready: draw them. Arrives before Betty's text about them. |
| `events` | the [events object](#the-events-object) | City events on the trip's dates (Warsaw only). |
| `done` | `{"reply": "...", "search": {...} or null, "events": {...} or null}` | Replace the bubble text with `reply`, add it to the history, stop the typing indicator. `search` and `events` are the same objects as sent before (or null when this turn had none). |
| `error` | `{"code": "upstream_unavailable", "message": "...", "retryable": true}` | Mark the bubble as failed (keep any partial text visible, e.g. greyed out), offer a retry. |

Details:

- `status` is sent each time Betty calls her search tool. If she called it with a trip that
  doesn't meet the minimum (for example a whole country), no `search` follows: she asks the
  traveller instead.
- `search` and `events` come at most once per search. In the rare case Betty searches twice in a
  turn, the later one replaces the earlier one; `done` carries the last.
- `events` comes only when the trip has specific dates and is in Warsaw.
- Betty may write a sentence before she searches; that text stays part of the reply. The server
  never takes back text it has streamed. A later part of the reply starts with `"\n\n"`.
- Text may contain light formatting: line breaks and arrows (`→`) between route stops. Markdown
  is not requested from the model, but it can appear.

### Example

Recorded from a real turn (shortened; times are seconds since the request):

```
0.0   event: start
      data: {"turn_id":"9c11e0a93bda"}

3.4   event: status
      data: {"stage":"searching_places"}

18.4  : ping
...   (a ping every 15 s while places, events and the route are found)

151.8 event: search
      data: {"trip":{"city":"Warsaw",...},"places":[...],"routes":[{"stops":[...],"distance_km":...}],...}

151.9 event: events
      data: {"city":"Warsaw","from_date":"2026-11-17","to_date":"2026-11-19","total":0,"events":[],"error":"..."}

166.8 : ping

177.8 event: delta
      data: {"text":"Your Warsaw trip from"}

177.8 event: delta
      data: {"text":" November 17"}

...   (about 40 more deltas within one second)

178.6 event: done
      data: {"reply":"Your Warsaw trip from November 17 to 19, 2026 is confirmed! Here's a suggested ...","events":{...},"search":{...}}

```

### Timing

A turn without a search usually starts streaming text after a few seconds. A turn with a search
takes much longer: the summary, the Overpass query, Wikipedia, the route and events all run
before Betty writes her answer, and the model also thinks before the first `delta` (about 25 s in
the example). The example above took **3 minutes**; about 2 of those were the route planner's
model call timing out, which is being looked into. Plan the UI for long waits:

- Show the `status` hint for as long as it lasts, and draw `search` as soon as it arrives:
  the route is useful before Betty's text.
- Configure the HTTP client with a **socket (read) timeout of at least 30 s** (pings arrive every
  15 s) and **no overall request timeout**, or one of at least 5 minutes. Ktor's engines default
  to much shorter timeouts.

### Errors

Problems with the request are plain HTTP errors, before any event is sent:

| Status | When | Body |
|---|---|---|
| 422 | Invalid body, or the last message is not from the user. | `{"detail": ...}` |
| 503 | The server has no model API key configured. | `{"detail": "..."}` |

After the stream has started the status is already 200, so failures arrive as an `error` event:

| `code` | Meaning |
|---|---|
| `upstream_unavailable` | The language model failed, timed out or returned nothing. |
| `internal` | Unexpected server error (`message` is generic; the details are in the server log under `turn_id`). |

`retryable` is `true` for both today. A failed **place search** is not an error event: the turn
completes, `search.error` says what happened, and Betty tells the traveller. The same goes for
events (`events.error`).

### Cancelling

Close the connection to cancel a turn: when the user sends another message, starts speaking or
leaves the screen. The server stops at its next step. A place search that is already running
finishes on the server first, but nothing more is sent.

## Blocking: `POST /api/v1/chat`

Same request. The response is the body of the `done` event:

```json
{"reply": "...", "events": null, "search": null}
```

Useful for tests and as a fallback. It sends nothing until the whole turn is finished, so it
needs the same long timeouts.

## The `search` object

Sent in the `search` event and in `done.search` on a turn where Betty had enough information and
searched.

```json
{
  "trip": {
    "city": "Warsaw",
    "district": null,
    "country": "PL",
    "scope": "city",
    "start_date": "2026-11-17",
    "end_date": "2026-11-19",
    "duration_days": null
  },
  "summary": "Destination: Warsaw, country PL. Dates: 2026-11-17 to 2026-11-19. The traveller loves castles, palaces and museums.",
  "query": "[out:json][timeout:25]; ...",
  "query_attempts": 1,
  "fetched": 142,
  "total": 140,
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
  "routes": [
    {
      "stops": [
        {"osm_id": "way/24601", "name": "Zamek Królewski w Warszawie", "category": "historic=castle",
         "lat": 52.2479, "lon": 21.0152, "role": "must", "why": "The royal castle, a must for history lovers."}
      ],
      "distance_km": 3.4,
      "steps": []
    }
  ],
  "error": null
}
```

(Values in the example are illustrative.)

| Field | Notes |
|---|---|
| `trip` | What Betty confirmed. Dates are `YYYY-MM-DD`; `district`, `start_date`, `end_date` and `duration_days` can each be null. |
| `summary`, `query`, `query_attempts`, `fetched` | Debugging only; don't show them. |
| `total` | Distinct places OpenStreetMap found (duplicates removed). |
| `places` | Sights and attractions only (no food), at most 30, notable ones first. |
| `places[].osm_id` | Stable id: `"node/…"`, `"way/…"` or `"relation/…"`. Use it as the place id for bookmarks, deduping and refreshing. |
| `places[].name` | Local (usually Polish) name; may be null. Prefer `tags["name:en"]` in an English UI when present. |
| `places[].category` | One tag saying what the place is (`"tourism=museum"`, `"historic=castle"`) or `"other"`. Good for icons. |
| `places[].lat`, `lon` | Can be null (rare). Skip such places on the map. |
| `places[].tags` | Only these keys, each optional: `amenity`, `tourism`, `historic`, `leisure`, `cuisine`, `opening_hours`, `website`, `contact:website`, `phone`, `contact:phone`, `name:en`, `addr:street`, `addr:housenumber`, `addr:postcode`, `wikidata`, `wheelchair`, `diet:vegetarian`, `diet:vegan`, `fee`. `opening_hours` uses the [OSM syntax](https://wiki.openstreetmap.org/wiki/Key:opening_hours). |
| `places[].wikipedia` | Optional, keyed by `"pl"` / `"en"`: title, URL and a short intro (about 300 characters at most). Only Polish and English exist. |
| `routes` | Suggested routes, best first. At most one today; empty when no place has coordinates. See below. |
| `error` | Set when the trip was accepted but the place search failed; `places` and `routes` are then empty. |

Unknown fields may be added later: keep `ignoreUnknownKeys = true`.

### The route

`search.routes[0]` is the walk Betty suggests, 5 to 10 stops. **Draw `stops` in the given order:
that order is the route.** The ordered coordinates are `stops[i].lat` and `stops[i].lon`.

| Field | Notes |
|---|---|
| `stops[].osm_id` | The same id as in `places`, where the full details are. |
| `stops[].name`, `category` | As in `places`. `name` may be null. |
| `stops[].lat`, `lon` | Always set. |
| `stops[].role` | `"must"`: a key place for this traveller. `"on_the_way"`: added because it is close to the path. |
| `stops[].why` | One short sentence for the traveller (English), or null when the planner fell back to a simple order. |
| `distance_km` | Straight-line length through the stops; real walking is longer. |
| `steps` | How the route was built, for debugging. Ignore it. |

The coordinates are the places themselves, not a walking path: for the line on the map, pass the
stops to `WalkingRouting.walkingPath` as today. Don't reorder or pick stops on the device.

## The `events` object

Sent in the `events` event and in `done.events`: events on the trip's dates, Warsaw only.

```json
{
  "city": "Warsaw",
  "from_date": "2026-11-17",
  "to_date": "2026-11-19",
  "total": 12,
  "events": [
    {
      "title": "...",
      "url": "https://...",
      "categories": ["Kultura"],
      "venues": ["..."],
      "addresses": ["ul. Prosta 1"],
      "lat": 52.23,
      "lon": 21.0,
      "description": "...",
      "repeatable": false,
      "occurrences": [{"start": "2026-11-18T19:00:00+01:00", "end": "2026-11-18T21:00:00+01:00"}],
      "occurrences_in_range": 1,
      "ongoing": false
    }
  ],
  "error": null
}
```

`events` holds at most 5, the ones most relevant to the traveller. Times are ISO 8601 in Warsaw
time with the offset. `ongoing` is true for things already running before the dates (for
example exhibitions). `error` is set when the lookup failed; then `events` is empty and Betty
doesn't mention events.

## Voice mode

The app keeps doing all the audio (speech recognition on the device, TTS through Google Cloud).
With streaming, Betty can start speaking before the reply is complete:

1. Split the deltas into sentences: append each `delta` to a buffer; whenever the buffer contains
   `.`, `!`, `?` or `…` followed by whitespace, cut there. Merge pieces shorter than about 40
   characters with the next one, to save TTS requests. On `done`, speak what is left.
2. Before sending a sentence to TTS, replace `→` with a comma and drop markdown characters
   (`*`, `#`, `_`): Betty doesn't have a voice-friendly mode yet.
3. Speak the sentences one after another from a queue. To avoid gaps, synthesize the next
   sentence while the current one plays (split today's `speak` into "synthesize to MP3" and
   "play").
4. While `status` is the latest event and nothing is playing, show the "searching" state. The
   wait can be long.
5. To stop (the user taps the mic, sends a message or closes voice mode), close the stream, clear
   the queue and stop playback, as today.

## Kotlin sketch

Domain: replaces `AIChatting.reply(history): Single<ChatReply>`.

```kotlin
sealed interface ChatEvent {
    data class Started(val turnId: String) : ChatEvent
    data class Status(val stage: String) : ChatEvent
    data class Delta(val text: String) : ChatEvent
    data class PlacesFound(val search: PlaceSearch) : ChatEvent      // places + route
    data class EventsFound(val events: CityEvents) : ChatEvent
    data class Done(val reply: String, val search: PlaceSearch?, val events: CityEvents?) : ChatEvent
}

interface AIChatting {
    /** Cold: subscribing starts the turn, disposing cancels it. Failures arrive as ChatException. */
    fun replyStream(history: List<ChatMessage>): Observable<ChatEvent>
}
```

Networking with Ktor 3: install `SSE` and `HttpTimeout` in `HttpClientFactory`.

```kotlin
install(SSE)
install(HttpTimeout) {
    socketTimeoutMillis = 60_000
    requestTimeoutMillis = HttpTimeoutConfig.INFINITE_TIMEOUT_MS
}
```

```kotlin
fun replyStream(history: List<ChatMessage>): Observable<ChatEvent> =
    channelFlow {
        client.sse(
            urlString = "$baseUrl/api/v1/chat/stream",
            request = {
                method = HttpMethod.Post
                contentType(ContentType.Application.Json)
                setBody(ChatRequestDto(messages = history.map(ChatMessageDto::from)))
            },
        ) {
            incoming.collect { event ->
                val data = event.data ?: return@collect // comments (pings) have no data
                when (event.event) {
                    "start" -> send(ChatEvent.Started(json.decodeFromString<StartDto>(data).turnId))
                    "status" -> send(ChatEvent.Status(json.decodeFromString<StatusDto>(data).stage))
                    "delta" -> send(ChatEvent.Delta(json.decodeFromString<DeltaDto>(data).text))
                    "search" -> send(ChatEvent.PlacesFound(json.decodeFromString<PlaceSearchDto>(data).toDomain()))
                    "events" -> send(ChatEvent.EventsFound(json.decodeFromString<EventSearchDto>(data).toDomain()))
                    "done" -> send(json.decodeFromString<DoneDto>(data).toDomain())
                    "error" -> throw json.decodeFromString<ErrorDto>(data).toException()
                    else -> Unit // unknown events: ignore
                }
            }
        }
    }.asObservable() // reaktive coroutines-interop
```

DTO field names follow the JSON (snake_case: `turn_id`, `distance_km`, `osm_id`, `from_date`,
...): use `@SerialName` or a `JsonNamingStrategy.SnakeCase` Json instance.

If POST with a body is awkward with the SSE plugin on one platform, the stream is plain text:
`client.preparePost(...).execute { response -> response.bodyAsChannel() }` and read it line by
line (an `event:` line, a `data:` line, and a blank line end each event; skip lines starting
with `:`).

Mock server (Ktor server), so UI work can continue without the real backend:

```kotlin
post("/api/v1/chat/stream") {
    val request = call.receive<ChatRequest>()
    val reply = replyTo(request)
    call.respondTextWriter(ContentType.Text.EventStream) {
        fun event(name: String, json: String) { write("event: $name\ndata: $json\n\n"); flush() }
        event("start", """{"turn_id":"mock-1"}""")
        for (word in reply.split(" ")) {
            event("delta", """{"text":${Json.encodeToString("$word ")}}""")
            delay(60)
        }
        event("done", """{"reply":${Json.encodeToString(reply)},"search":null,"events":null}""")
    }
}
```

## Not available yet

Planned in [mobile-integration-plan.md](mobile-integration-plan.md), not built. Sending these
fields is harmless (they are ignored), but don't rely on them:

| Field | What it will do |
|---|---|
| `language` (request) | Betty replies in the app language, matching the TTS voice. Today she follows the traveller's language. |
| `mode: "voice"` (request) | Short plain sentences for TTS, without arrows or markdown. |
| `today` (request) | The device's local date for checking trip dates. Today the server's date is used. |
| `last_search` (request) | Lets Betty answer follow-ups about the places she gave last turn. |
| `mentioned_place_ids` (`done`) | Which places Betty named in her text. |
| Limits, auth | At most 50 messages and 4,000 characters each; an API key header. |

## Checklist for the mobile app

- [ ] DTOs: messages use `content`; snake_case field names.
- [ ] Route: draw `search.routes[0].stops` in order (first priority).
- [ ] `AIChatting.replyStream` over SSE, with `HttpTimeout` (socket ≥ 30 s, no request timeout).
- [ ] Chat UI: bubble created on `start`, filled by `delta`, finalized by `done`; "searching"
      hint on `status`; places and route drawn on `search`; failed state with retry on `error`.
- [ ] Cancel the turn (dispose) on a new message, on starting to listen, and on leaving the screen.
- [ ] History: append `done.reply`; never append partial text; keep about 50 messages.
- [ ] Places: pins from `search.places`, details from tags and Wikipedia, bookmarks keyed by
      `osm_id`.
- [ ] Events: show `events.events` for trips with dates.
- [ ] Voice mode: sentence splitter, `→`/markdown clean-up, TTS queue with prefetch, stop on
      interrupt.
- [ ] Mock server: `POST /api/v1/chat/stream` and `POST /api/v1/chat` with these shapes.
- [ ] Base URL per platform; port 8000.
