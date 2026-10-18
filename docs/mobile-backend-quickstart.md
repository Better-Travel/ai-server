# Run the Betty backend locally (for the mobile app)

How to start the AI server in Docker on your machine and point the app at it. The API itself is
described in [mobile-chat-api.md](mobile-chat-api.md).

## 1. Start it

You need Docker (with compose) and a clone of `https://github.com/Better-Travel/ai-server`.

```bash
git clone https://github.com/Better-Travel/ai-server
cd ai-server
cp .env.example .env
```

In `.env`, set `GEMINI_API_KEY` (the Google Gemini key). Ask the AI-server
team for the keys and never commit `.env`. The other settings can stay as they are.

```bash
docker compose up -d --build
curl http://localhost:8000/health        # {"status":"ok"}
```

The first build takes a few minutes; later starts take seconds. Interactive API docs:
http://localhost:8000/docs.

Optional: `docker compose --profile ui up -d --build` also starts a web chat at
http://localhost:8501, handy for seeing what Betty answers without the app.

## 2. Point the app at it

| Where the app runs | Base URL |
|---|---|
| Android emulator | `http://10.0.2.2:8000` |
| iOS simulator | `http://localhost:8000` |
| Phone on the same Wi-Fi | `http://<your computer's IP>:8000` (allow port 8000 in the firewall) |

Main endpoint: `POST {baseUrl}/api/v1/chat/stream` (Server-Sent Events). Try it from a terminal:

```bash
curl -N http://localhost:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hi! I want to visit Warsaw."}]}'
```

You should see `event: start`, several `event: delta` and one `event: done`. A message with a
city, dates and interests (for example "Warsaw from 17 to 19 November, I love castles and
museums") also triggers a place search: expect a `status`, then after a minute or two `search`
with `routes[0].stops`, the ordered coordinates of the route.

## 3. Day to day

| Task | Command |
|---|---|
| Logs (every turn is logged with its `turn_id`) | `docker compose logs -f api` |
| Stop | `docker compose --profile ui down` |
| Update to the latest backend | `git pull && docker compose up -d --build` |

## Troubleshooting

- **Port 8000 is already in use**: stop whatever uses it, or change `"8000:8000"` to for example
  `"8001:8000"` in `compose.yaml` and use port 8001 in the app.
- **`error` event with `upstream_unavailable`**: the model API key in `.env` is missing or wrong
  (check `docker compose logs api`), or the model API is slow. Run `docker compose up -d` again after
  changing `.env`.
- **Searches are slow**: normal for now, a turn that searches takes 1-3 minutes. Keep the app's
  HTTP socket timeout at 30 s or more and don't set an overall request timeout (see "Timing" in
  [mobile-chat-api.md](mobile-chat-api.md)).
- **No events in the answer**: city events come from the BetterTravel backend on port 8080 of
  your machine. Without it the chat still works and just leaves events out. If the app's mock
  server runs on 8080 instead, events are also left out, which is fine.
- **Places**: by default the container uses a local Overpass server on port 12345 if one runs
  (the AI-server team's setup), otherwise the public `overpass-api.de`. You don't need to set
  anything up for it.
