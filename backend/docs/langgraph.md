# LangGraph (campus assistant agent)

- **Package:** `langgraph>=1.2.14`, including `langgraph-prebuilt` (`ToolNode`, `tools_condition`)
- **Used in:** `server/Chatbot/chat_bot.py` (`Chatbot`); the tools live in `server/Chatbot/teacher_tools.py` and `server/Chatbot/navigation_tools.py`
- **Official docs:** https://docs.langchain.com/oss/python/langgraph/graph-api and https://docs.langchain.com/oss/python/langgraph/workflows-agents
- **Gemini integration:** see `docs/langchain-google-genai.md`

## Why

The chatbot used to fuzzy-match the whole question against the Q&A questions. Free-form questions ("how many years of experience does ravi verma have") often matched the wrong teacher and fell through to a canned reply; this behaviour was already present in the original code.

It is now a **tool-calling agent**. Gemini decides which read-only SQLite lookups to make, and answers only from what those lookups return. Voice questions and directions go through the same loop.

## Graph

```
START --(audio?)--> transcribe --(heard something)--> agent
  \--(text)-----------------------------------------> agent
                                                      |  ^
                                    (tool calls)      v  |
                                                     tools
agent --(final answer)--> END
agent --(Gemini unavailable / too many tool rounds)--> fallback --> END
transcribe --(nothing intelligible)--> END   ("Sorry, I couldn't understand that audio...")
```

| Node | What it does |
|---|---|
| `transcribe` | Voice only. Sends the audio to Gemini as an `audio` content block and asks for an exact transcript. If Gemini is unavailable, WAV audio falls back to Google `speech_recognition`. Adds the transcript as the user's message. |
| `agent` | `ChatGoogleGenerativeAI.bind_tools(tools)` with the system prompt. It either requests tool calls or gives the final answer. Gemini errors set `llm_failed`. |
| `tools` | `ToolNode(tools, handle_tool_errors=(ToolInvocationError, ValueError, LookupError, sqlite3.Error))`. Bad arguments come back to the model as an error message instead of crashing. |
| `fallback` | Deterministic answer without Gemini, in this order: a room-to-room route ("from 301 to 405"); the named teacher's best-matching fact; their cabin and room; the top search hit, if it contains at least half the question's words; otherwise a canned reply. It is also used when Gemini returns an empty answer. |

## Tools

| Tool | Source | Returns |
|---|---|---|
| `find_teacher(name)` | `teachers` and `qa` (name index) | Up to 5 matches with name, cabin, room and `has_profile`. No phone numbers. |
| `get_teacher_profile(name)` | `qa` rows for that teacher | Bio, background and experience, position, research, awards, cabin and room. An ambiguous name ("verma") returns candidates instead. |
| `search_faculty_knowledge(query)` | BM25 over all `qa` text | The top 5 facts for topic questions ("who works on machine learning"). |
| `find_route(start, end, building, preference)` | the existing route engine (`svg_manipulator.main`) | A text summary for the model, plus the map links as the `ToolMessage` artifact. |
| `find_route_between_buildings(...)` | `svg_manipulator.multi_building_nav` | The same, for two buildings. |

The database is opened read-only (`file:...?mode=ro`), so the tools cannot modify data.

**Name matching** (`TeacherKnowledge.resolve`):
- Names are normalized: lowercased, with the possessive "'s", punctuation and titles like "Dr." or "Prof." removed.
- Initials are ignored ("Dr. C. P. Koushik" is found by "koushik").
- Every remaining part of the name must appear in the question, allowing small typos (`fuzz.ratio ≥ 85`).
- The longest matching name wins, and a tie counts as ambiguous.
- When a tool receives a *name* (`exact_name=True`), every word the caller wrote must also belong to that teacher's name. So "Suresh Patel" never resolves to "Dr. M. Suresh"; the model gets the candidate list and asks the user instead.

## State

`AgentState(MessagesState)` adds:
- `user_input`
- `audio` (`AudioInput`: `base64`, `mime_type`)
- `transcript`
- `tool_rounds`
- `llm_failed`
- `files`: the fallback route's map links. Map links from Gemini's tool calls travel on the tool messages instead.

Nodes return partial updates (`AgentUpdate`). `Chatbot.chat()` and `chat_audio()` return a `ChatResult` (`response`, plus `files` and `transcript` when present).

## Caching

All layers are cleared by `Chatbot.reload_knowledge_base()` (the `POST /reload_knowledge` route, which needs a cpanel login).

1. **Response cache:** an LRU with a TTL, keyed by the normalized question. It skips caching:
   - route answers (the SVG files are shared and may be redrawn by later requests)
   - answers produced while Gemini was down, or by the fallback node
   - canned replies
2. **LangGraph node cache on `agent`:** `CachePolicy(key_func=conversation_key)`. The key is the conversation (message types, content, tool calls and their IDs), without the per-run message IDs, so a repeated conversation replays Gemini's replies instead of calling it again.
3. **Lookup memo** in `TeacherKnowledge.get_profile`: repeated profile lookups skip SQLite until the next reload.

| Env var | Default | Meaning |
|---|---|---|
| `CHAT_CACHE_TTL` | `3600` | Seconds an entry stays valid (response cache and node cache) |
| `CHAT_CACHE_MAX_SIZE` | `256` | Maximum number of response cache entries |

The caches live in process memory, one copy per worker.

## Limits

- `MAX_TOOL_ROUNDS = 4`: after that many tool rounds, the question goes to the fallback node.
- `recursion_limit = 25`: a hard safety net. A `GraphRecursionError` also leads to the fallback answer.

## API

- `POST /chat {message}` returns `{response, status}`, plus `files` (`{start_floor, end_floor?}`) when the answer is a route.
- `POST /chat/audio` (multipart `audio_file`) returns `{response, status, transcript, files?}`. It responds with 415 for an unsupported type, 413 above 10 MB, and 400 for an empty file.
- Both run the agent in the threadpool, because the Gemini and SQLite calls block.

## Best practices followed (from the official docs)

- The agent loop is built from `StateGraph(MessagesState)`, `ToolNode` and `tools_condition`, with a custom router that adds the fallback branch.
- Edges use `START`/`END`. Routers return `Literal`s with explicit `path_map`s.
- Node caching uses `CachePolicy` and `compile(cache=InMemoryCache())`, and is cleared with `graph.clear_cache()`.
- Tool artifacts (`response_format="content_and_artifact"`) keep UI data out of the model's context.

## Tests

- `Test/test_teacher_tools.py`: name handling, resolution, profiles, search, read-only access, memoization.
- `Test/test_navigation_tools.py`: routes, artifacts, room-number parsing.
- `Test/test_chatbot_langgraph.py`: the agent ⇄ tools loop, fallback, recursion limit, caches, voice, and the `/chat`, `/chat/audio` and `/reload_knowledge` contracts.

Gemini is replaced by `Test/chat_fakes.py::ScriptedChatModel`, so no test makes network calls.
