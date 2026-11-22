# langchain-google-genai (Gemini through LangChain)

- **Package:** `langchain-google-genai` (4.x; needs Python ≥ 3.10, `langchain-core` ≥ 1.6.1, `google-genai` ≥ 2.20)
- **Used in:** `server/Chatbot/chat_bot.py` (`build_default_llm`)
- **Docs:** https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai
- **Gemini audio docs:** https://ai.google.dev/gemini-api/docs/audio

## Why

The chatbot used to send raw `requests` calls to Gemini's OpenAI-compatible endpoint, with a single prompt. It is now a **tool-calling agent**: Gemini decides which read-only SQLite lookups to run (`find_teacher`, `get_teacher_profile`, `search_faculty_knowledge`, `find_route`, `find_route_between_buildings`) and answers only from their results. `ChatGoogleGenerativeAI` gives us LangChain's standard `bind_tools` / `tool_calls` interface. It also accepts audio content blocks, which is how voice questions are transcribed.

## Configuration

| Setting | Source | Notes |
|---|---|---|
| API key | `GEMINI_API_KEY` in `server/.env` (via `env_variables["gemini_api_key"]`) | Passed explicitly as `api_key`. The integration would otherwise read `GOOGLE_API_KEY`, then `GEMINI_API_KEY`. |
| Model | `GEMINI_MODEL` (default `gemini-3.5-flash-lite`) | |
| `temperature` | not set | Gemini 3 models (e.g. `gemini-3.5-flash-lite`) use fixed sampling defaults. A `temperature` is ignored and triggers a `UserWarning`, so it isn't sent. The system prompt keeps answers grounded instead. |
| `max_tokens` / `timeout` / `max_retries` | `400` / `30` s / `2` | |

With no API key, `Chatbot` runs without an LLM: every question goes to the deterministic `fallback` node, which uses the same SQLite search and route engine.

## Best practices followed

- **Tools** are defined with `@tool(parse_docstring=True)`, so each argument's description reaches the model. Their docstrings say when to use each tool.
- **Route tools** use `response_format="content_and_artifact"`. The model sees a short text summary, while the map links travel as the `ToolMessage.artifact` and never go through the model.
- **Prompting:** the system prompt tells the model to always use the tools and to answer only from tool results, in 1–2 sentences. If the tools don't have the answer, it says so.
- **Error handling:** only the client's own failures are caught (`google.genai.errors.APIError`, `ChatGoogleGenerativeAIError`, `httpx.HTTPError`), and they lead to the fallback node. Tool failures (`ToolInvocationError` for bad arguments, `ValueError`, `LookupError`, `sqlite3.Error`) go back to the model as error `ToolMessage`s through `ToolNode(handle_tool_errors=...)`.
- **Audio:** sent inline as `{"type": "audio", "base64": ..., "mime_type": ...}`. Gemini accepts wav, mp3, aiff, aac, ogg, flac, m4a, opus and webm, up to 20 MB per request; the API caps uploads at 10 MB.
- **Privacy:** phone numbers are never included in tool results.

## Install

```powershell
# from backend/
uv add langchain-google-genai
uv export --no-dev --no-hashes -o requirements.txt
```
