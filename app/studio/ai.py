"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import json
import os
import re
import threading

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-3.8-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "openai/gpt-oss-20b"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", "claude-haiku-5-5"),
    "cerebras": ("Cerebras", "https://api.cerebras.ai/v1", "openai", "https://cloud.cerebras.ai", "gpt-oss-120b"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1", "openai", "https://console.mistral.ai/api-keys", "mistral-small-latest"),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


_local = {"at": 0.0, "models": []}


def default_models(p: str, base: str = "") -> list:
    """The model used when none was chosen: a fast, cheap one per service. For a model on this machine, the
    first one that is installed."""
    if p in ("ollama", "custom"):
        import time
        if time.time() - _local["at"] > 60:
            _local["at"] = time.time()
            try:
                r = httpx.get(f"{base or PROVIDERS[p][1]}/models", timeout=4)
                _local["models"] = [m["id"] for m in r.json().get("data", []) if m.get("id")][:1]
            except Exception:  # noqa: BLE001
                _local["models"] = []
        return _local["models"] or ([PROVIDERS[p][4]] if PROVIDERS[p][4] else [])
    return [PROVIDERS[p][4]] if p in PROVIDERS and PROVIDERS[p][4] else []


import threading as _threading
import contextlib as _contextlib

_over = _threading.local()


@_contextlib.contextmanager
def using(cfg: dict):
    """Run AI calls on this thread with another provider (the backup key) for a moment."""
    old = getattr(_over, "c", None)
    _over.c = cfg
    try:
        yield
    finally:
        _over.c = old


def _backup_file():
    return config.DATA_DIR / "ai_backup.json"


def backup() -> dict:
    """The backup AI (a Gemini key the person pasted when the main AI ran out), or {}."""
    import json as _json
    try:
        d = _json.loads(_backup_file().read_text(encoding="utf-8"))
        return d if d.get("key") and d.get("models") else {}
    except (OSError, ValueError):
        return {}


_picked: dict = {}


def gemini_models(key: str, fresh: bool = False) -> list:
    """The Gemini models this key can really use right now, best first. A free key gets zero quota on some new
    models (429), old ones are retired (404) and busy ones say 503, so each candidate is asked once and only the
    ones that answer are kept. Remembered on disk for a day, so this runs when a key is saved, not on every call."""
    import time as _time
    cache = config.DATA_DIR / "gemini_models.json"
    tag = key[-6:]
    if not fresh:
        if key in _picked:
            return _picked[key]
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("k") == tag and _time.time() - d.get("at", 0) < 86400 and d.get("models"):
                _picked[key] = d["models"]
                return d["models"]
        except (OSError, ValueError):
            pass
    r = httpx.get(PROVIDERS["gemini"][1], params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=15)
    if r.status_code in (400, 401, 403):
        raise ValueError("Google says that key isn't valid. Copy it again from aistudio.google.com/apikey.")
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    cands = [n for n in names if "flash" in n and not any(x in n for x in ("tts", "image", "live", "audio", "omni", "embed", "thinking"))]

    def ver(n):
        m = re.search(r"(\d+(?:\.\d+)?)", n)
        return (float(m.group(1)) if m else 0.5, "preview" not in n, "lite" not in n)
    cands.sort(key=ver, reverse=True)
    ok, busy = [], []
    for n in cands[:14]:
        try:
            t = httpx.post(f"{PROVIDERS['gemini'][1]}/{n}:generateContent", headers={"x-goog-api-key": key}, timeout=25,
                           json={"contents": [{"role": "user", "parts": [{"text": "Say ok"}]}],
                                 "generationConfig": {"maxOutputTokens": 20}})
        except httpx.HTTPError:
            continue
        if t.status_code == 200:
            ok.append(n)
        elif t.status_code in (500, 503):
            busy.append(n)            # works, just busy now: kept after the ones that answered
        if len(ok) >= 3:
            break
    models = (ok + busy)[:5]
    if not models:
        raise ValueError("This Gemini key has no free quota left on any model today. Try again tomorrow, or use another key.")
    _picked[key] = models
    try:
        cache.write_text(json.dumps({"k": tag, "at": _time.time(), "models": models}), encoding="utf-8")
    except OSError:
        pass
    return models


# ------------------------------------------------------------------ every key ever saved, by service
def _keys_file():
    return config.DATA_DIR / "ai_keys.json"


def keys() -> dict:
    """{"groq": key, "gemini": key, ...}: switching the main AI never loses the other key."""
    try:
        d = json.loads(_keys_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_keys(data):
    f = _keys_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(f)


def remember_key(provider_id: str, k: str):
    if not k:
        return
    with _lock:
        d = keys()
        d[provider_id] = k
        _write_keys(d)


def remove_key(provider_id: str, hint: str):
    """Remove the selected saved key without touching a replacement added since the UI loaded."""
    with _lock:
        dashboard = dict(_from_dashboard())
        remembered = keys()
        saved = dashboard.get("key") if dashboard.get("provider") == provider_id else ""
        backup = remembered.get(provider_id, "")
        selected = next((k for k in (saved, backup) if k and len(hint) == 4 and k.endswith(hint)), None)
        if not selected:
            raise ValueError("That saved key changed or was already removed. Refresh the list.")
        if backup == selected:
            del remembered[provider_id]
            _write_keys(remembered)
        if saved == selected:
            dashboard["key"] = ""
            f = _file()
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(dashboard), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(f)
            _saved["at"] = None


# free services worth pooling, in the order they are tried after the main AI (biggest free allowance first)
POOL_ORDER = ["cerebras", "groq", "gemini", "openrouter", "mistral"]
FREE_KEYS = {
    "cerebras": ("Cerebras", "https://cloud.cerebras.ai", "About 1 million free tokens a day. Sign in, API Keys, copy."),
    "groq": ("Groq", "https://console.groq.com/keys", "Free daily allowance per model; also runs Hinglish captions (Whisper)."),
    "gemini": ("Gemini", "https://aistudio.google.com/apikey", "Google's free tier. Create API key, copy."),
    "openrouter": ("OpenRouter", "https://openrouter.ai/keys", "Uses only its free models (no card needed)."),
    "mistral": ("Mistral", "https://console.mistral.ai/api-keys", "Free 'Experiment' plan. Create new key, copy."),
}
_models_cache: dict = {}


def pool_models(p: str, k: str) -> list:
    """The models to use for one service in the pool, best first (found from the service, kept for a day)."""
    import time as _time
    hit = _models_cache.get((p, k[-6:]))
    if hit and _time.time() - hit[0] < 86400:
        return hit[1]
    try:
        if p == "gemini":
            ms = gemini_models(k)
        elif p == "groq":
            ms = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
        else:
            r = httpx.get(PROVIDERS[p][1] + "/models", headers={"Authorization": "Bearer " + k}, timeout=15)
            r.raise_for_status()
            ids = [m.get("id", "") for m in r.json().get("data", [])]
            if p == "openrouter":       # only the free ones, the bigger the better
                free = [m for m in r.json().get("data", []) if str(m.get("id", "")).endswith(":free")]
                free.sort(key=lambda m: -(m.get("context_length") or 0))
                ms = [m["id"] for m in free[:4]]
            else:
                skip = ("embed", "whisper", "tts", "image", "vision", "guard", "moderation", "ocr", "audio")
                ids = [i for i in ids if i and not any(x in i.lower() for x in skip)]
                pref = {"cerebras": ["gpt-oss-120b", "qwen", "llama"], "mistral": ["mistral-small", "mistral-medium", "ministral"]}.get(p, [])
                ids.sort(key=lambda i: next((n for n, w in enumerate(pref) if w in i.lower()), 99))
                ms = ids[:3]
    except Exception:  # noqa: BLE001
        ms = [PROVIDERS[p][4]] if PROVIDERS[p][4] else []
    ms = ms or ([PROVIDERS[p][4]] if PROVIDERS[p][4] else [])
    _models_cache[(p, k[-6:])] = (_time.time(), ms)
    return ms


def pool() -> list:
    """Every AI to try, in order: the main one first, then each other saved free key."""
    main = current()
    out = [dict(main, main=True)] if main["provider"] and (main["key"] or main["provider"] in NO_KEY) else []
    ks = keys()
    for p in POOL_ORDER:
        if p != main["provider"] and ks.get(p):
            out.append({"provider": p, "key": ks[p], "models": pool_models(p, ks[p]), "base_url": "", "main": False})
    return out


def check_key(p: str, k: str) -> list:
    """Is this key good? Returns the models it will use. Raises ValueError with a plain reason if not."""
    if p not in FREE_KEYS:
        raise ValueError("Pick one of the free AI services.")
    k = str(k or "").strip()
    if len(k) < 16:
        raise ValueError("That key looks too short. Copy the whole key.")
    if p == "gemini":
        return gemini_models(k, fresh=True)
    r = httpx.get(PROVIDERS[p][1] + "/models", headers={"Authorization": "Bearer " + k}, timeout=20)
    if r.status_code in (401, 403):
        raise ValueError(f"{FREE_KEYS[p][0]} says that key isn't valid. Copy it again.")
    r.raise_for_status()
    _models_cache.pop((p, k[-6:]), None)
    return pool_models(p, k)


def groq_key() -> str:
    """For Whisper captions: the Groq key, whether or not Groq is the main AI right now."""
    c = current()
    return c["key"] if c["provider"] == "groq" and c["key"] else keys().get("groq", "")


def save_backup(key: str) -> dict:
    """Check a Gemini key, pick its newest fast models, keep it as the backup. Raises ValueError if it doesn't work."""
    key = str(key or "").strip()
    if len(key) < 20:
        raise ValueError("That doesn't look like a Gemini key. It starts with AIza and is about 39 characters.")
    models = gemini_models(key)
    remember_key("gemini", key)
    return {"models": models}


def backup() -> dict:
    """The other service to use when the main AI is out for the day: a remembered Gemini or Groq key, or {}."""
    main = current()["provider"]
    ks = keys()
    if main != "gemini" and ks.get("gemini"):
        try:
            return {"provider": "gemini", "key": ks["gemini"], "models": gemini_models(ks["gemini"]), "base_url": ""}
        except Exception:  # noqa: BLE001
            return {}
    if main != "groq" and ks.get("groq"):
        return {"provider": "groq", "key": ks["groq"], "base_url": "",
                "models": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]}
    return {}


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    if getattr(_over, "c", None):
        return dict(_over.c)
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    p = ALIASES.get(p, p)
    out = {"provider": p,
            "models": (list(d.get("models") or []) if d.get("provider") else list(config.AI_MODELS))
            or default_models(p, str(d.get("base_url") or "").rstrip("/")),
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": str(d["key"]) if d.get("provider") and "key" in d else config.ai_key()}
    if out["provider"] == "gemini" and out["key"] and (not out["models"] or out["models"] == [PROVIDERS["gemini"][4]]):
        try:
            out["models"] = gemini_models(out["key"])
        except Exception:  # noqa: BLE001
            pass
    return out


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return current()["key"]


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see. The key itself never leaves the server: only whether there is one, and
    its last four characters so the owner can tell which key it is."""
    c = current()
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(c["key"]),
            "key_hint": c["key"][-4:] if len(c["key"]) >= 12 else "", "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "can_remove_key": bool(_from_dashboard().get("key")),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None) -> dict:
    """Save the choice made in the dashboard. `new_key` None keeps the key that is already there (the page never
    has it to send back); an empty string removes it."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms and current()["provider"] == p:        # same service as before: keep the model it was using
        ms = list(_from_dashboard().get("models") or [])
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current()
    k = old["key"] if new_key is None and old["provider"] == p else str(new_key or "").strip()[:400]
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service.")
    if p == "gemini":          # only models this key can really use (free keys get zero quota on some)
        try:
            good = gemini_models(k, fresh=True)
            ms = [m for m in ms if m in good] or good
        except Exception:  # noqa: BLE001 - the test right after saving says what's wrong with the key
            pass
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"provider": p, "models": ms, "base_url": address, "key": k}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
    remember_key(p, k)
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k = str(with_key or "").strip() or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and the model all work."""
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    out, sent, wrote = call(ms[0], 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
    return {"ok": True, "model": ms[0], "tokens": sent + wrote, "answer": str(out)[:80]}


class AIError(RuntimeError):
    pass


def _fail(r, model):
    text = r.text[:300].replace(key() or "\0", "<key>")
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise AIError("The AI did not answer with JSON.")
        out = json.loads(m.group(0))
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict, max_tokens: int = None):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back)."""
    import time
    how = PROVIDERS[provider()][2]
    for attempt in range(3):
        try:
            if how == "openai":
                return _openai(model, prompt, schema, max_tokens)
            return {"gemini": _gemini, "anthropic": _anthropic}[how](model, prompt, schema)
        except AIError as e:
            # a free plan with a small allowance for the answer: ask again for a shorter one instead of failing
            if "reduce max_tokens" in str(e) and how == "openai" and attempt < 2:
                m = re.search(r"Limit (\d+)", str(e))
                max_tokens = max(300, int((int(m.group(1)) if m else 1000) * 0.9))
                continue
            # "too many requests": the service says how long to wait, so wait and ask again instead of giving up
            if "HTTP 429" not in str(e) or attempt == 2:
                raise
            m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))
            wait = (int(m.group(1) or 0) * 60 + float(m.group(2)) + 1.5) if m else 20.0 * (attempt + 1)
            if wait > 10:                  # a long wait: let the caller try another model or the backup key instead
                raise
            time.sleep(wait)


def _gemini(model, prompt, schema):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    import re as _re
    v = _re.search(r"gemini-(\d+)", model)
    if v and int(v.group(1)) >= 3:       # Gemini 3+ thinks for a long time by default: keep it quick for these jobs
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
    elif v:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": key()}, timeout=120)
    if r.status_code == 400 and "thinking" in r.text.lower():        # this model doesn't take that setting
        body["generationConfig"].pop("thinkingConfig", None)
        r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": key()}, timeout=120)
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai(model, prompt, schema, max_tokens=None):
    headers = {"Authorization": f"Bearer {key()}"} if key() else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{base_url()}/chat/completions", json={**body, **extra}, headers=headers, timeout=180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema):
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base_url()}/messages", json=body, timeout=180,
                   headers={"x-api-key": key(), "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
