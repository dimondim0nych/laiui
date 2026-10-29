import atexit
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
LLAMA_DIR = BASE_DIR / "llama"
MODELS_DIR = BASE_DIR / "models"
CHATS_DIR = BASE_DIR / "chats"
SETTINGS_FILE = BASE_DIR / "settings.json"
LOG_FILE = BASE_DIR / "llama-server.log"

APP_VERSION = "1.1.0"

LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8081
LLAMA_URL = f"http://{LLAMA_HOST}:{LLAMA_PORT}"
CONTEXT_SIZE = 4096
REPLY_RESERVE = 512
LOAD_TIMEOUT = 180

DEFAULT_TITLE = "Новый чат"
TITLE_MAX = 40
RENAME_MAX = 100
CHAT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

app = Flask(__name__)

llama_lock = threading.Lock()
llama_proc = None
current_model = None


# ---------- JSON storage ----------

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_settings():
    data = read_json(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    return {"system_prompt": str(data.get("system_prompt", ""))}


def load_chat(chat_id):
    if not CHAT_ID_RE.match(chat_id):
        return None
    path = CHATS_DIR / f"{chat_id}.json"
    if not path.is_file():
        return None
    chat = read_json(path, None)
    return chat if isinstance(chat, dict) else None


def save_chat(chat, touch=True):
    if touch:
        chat["updated_at"] = time.time()
    write_json_atomic(CHATS_DIR / f"{chat['id']}.json", chat)


def list_chats():
    items = []
    for path in CHATS_DIR.glob("*.json"):
        chat = read_json(path, None)
        if isinstance(chat, dict) and "id" in chat:
            items.append({
                "id": chat["id"],
                "title": chat.get("title", DEFAULT_TITLE),
                "updated_at": chat.get("updated_at", 0),
            })
    items.sort(key=lambda c: c["updated_at"], reverse=True)
    return items


def make_title(text):
    flat = " ".join(text.split())
    return flat if len(flat) <= TITLE_MAX else flat[:TITLE_MAX].rstrip() + "…"


# ---------- llama-server process ----------

def find_llama_server():
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    direct = LLAMA_DIR / name
    if direct.is_file():
        return direct
    for path in LLAMA_DIR.rglob(name):
        if path.is_file():
            return path
    return None


def list_models():
    return sorted(
        p.name for p in MODELS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() == ".gguf"
    )


def is_ready():
    return llama_proc is not None and llama_proc.poll() is None and current_model is not None


def stop_llama():
    global llama_proc, current_model
    proc = llama_proc
    llama_proc = None
    current_model = None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def start_llama(model_name):
    global llama_proc, current_model
    exe = find_llama_server()
    if exe is None:
        raise RuntimeError("llama-server executable not found in llama/")

    stop_llama()
    command = [
        str(exe),
        "-m", str(MODELS_DIR / model_name),
        "--host", LLAMA_HOST,
        "--port", str(LLAMA_PORT),
        "-c", str(CONTEXT_SIZE),
    ]
    with open(LOG_FILE, "wb") as log:
        proc = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, cwd=str(exe.parent)
        )
    llama_proc = proc

    deadline = time.time() + LOAD_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            code = proc.returncode
            stop_llama()
            raise RuntimeError(f"llama-server exited with code {code}, see {LOG_FILE.name}")
        try:
            with urllib.request.urlopen(LLAMA_URL + "/health", timeout=2) as resp:
                if resp.status == 200:
                    current_model = model_name
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)

    stop_llama()
    raise RuntimeError("llama-server did not become ready in time")


atexit.register(stop_llama)


# ---------- Prompt building and streaming ----------

def estimate_tokens(text):
    # Rough, deliberately pessimistic estimate: about 2 characters per token
    return len(text) // 2 + 4


def build_messages(history, system_prompt):
    budget = CONTEXT_SIZE - REPLY_RESERVE
    system = [{"role": "system", "content": system_prompt}] if system_prompt else []
    used = sum(estimate_tokens(m["content"]) for m in system)

    kept = []
    for message in reversed(history):
        if not message["content"]:
            continue
        cost = estimate_tokens(message["content"])
        if kept and used + cost > budget:
            break
        kept.append({"role": message["role"], "content": message["content"]})
        used += cost
    kept.reverse()

    while kept and kept[0]["role"] == "assistant":
        kept.pop(0)
    return system + kept


class ThinkSplitter:
    """Splits streamed text into thinking and content parts by <think> tags.

    Tags may be cut between chunks, so a possible partial tag is held back.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self):
        self.buffer = ""
        self.in_think = False

    def feed(self, text):
        self.buffer += text
        out = []
        while True:
            kind = "thinking" if self.in_think else "content"
            tag = self.CLOSE if self.in_think else self.OPEN
            index = self.buffer.find(tag)
            if index != -1:
                if index:
                    out.append((kind, self.buffer[:index]))
                self.buffer = self.buffer[index + len(tag):]
                self.in_think = not self.in_think
                continue

            keep = 0
            for n in range(min(len(tag) - 1, len(self.buffer)), 0, -1):
                if tag.startswith(self.buffer[-n:]):
                    keep = n
                    break
            cut = len(self.buffer) - keep
            if cut:
                out.append((kind, self.buffer[:cut]))
            self.buffer = self.buffer[cut:]
            return out

    def flush(self):
        kind = "thinking" if self.in_think else "content"
        rest, self.buffer = self.buffer, ""
        return [(kind, rest)] if rest else []


def sse_line(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def extract_stats(timings):
    stats = {}
    for key in ("prompt_per_second", "predicted_per_second"):
        value = timings.get(key)
        if isinstance(value, (int, float)) and value > 0:
            stats[key] = round(float(value), 1)
    return stats


def stream_reply(chat, payload_messages):
    splitter = ThinkSplitter()
    parts = {"thinking": "", "content": ""}
    resp = None
    stopped = False
    timings = None
    stats = {}
    started = time.monotonic()
    duration = None

    def make_event(kind, text):
        if not parts[kind]:
            text = text.lstrip("\n")
        if not text:
            return None
        parts[kind] += text
        return sse_line({"type": kind, "text": text})

    try:
        body = json.dumps({"messages": payload_messages, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            LLAMA_URL + "/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=300)

        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("timings"), dict):
                timings = chunk["timings"]
            try:
                delta = chunk["choices"][0]["delta"]
            except (KeyError, IndexError, TypeError):
                continue

            pieces = []
            if delta.get("reasoning_content"):
                pieces.append(("thinking", delta["reasoning_content"]))
            if delta.get("content"):
                pieces.extend(splitter.feed(delta["content"]))

            for kind, text in pieces:
                event = make_event(kind, text)
                if event:
                    yield event

        for kind, text in splitter.flush():
            event = make_event(kind, text)
            if event:
                yield event
        if timings:
            stats = extract_stats(timings)
        if stats:
            yield sse_line({"type": "stats", "stats": stats})
        duration = round(time.monotonic() - started, 1)
        yield sse_line({"type": "done", "duration": duration})
    except GeneratorExit:
        # The client closed the connection (stop button or closed tab)
        stopped = True
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        yield sse_line({"type": "error", "message": f"llama-server request failed: {exc}"})
    finally:
        if resp is not None:
            resp.close()
        if duration is None:
            duration = round(time.monotonic() - started, 1)
        content = parts["content"].strip()
        thinking = parts["thinking"].strip()
        if content or thinking:
            # Reload from disk: the chat may have been renamed or deleted meanwhile
            fresh = load_chat(chat["id"])
            if fresh is not None:
                reply = {"role": "assistant", "content": content, "thinking": thinking}
                if stopped:
                    reply["stopped"] = True
                if stats:
                    reply["stats"] = stats
                reply["duration"] = duration
                fresh.setdefault("messages", []).append(reply)
                save_chat(fresh)


# ---------- Routes ----------

def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.get("/")
def index():
    return render_template("index.html", version=APP_VERSION)


@app.get("/api/models")
def api_models():
    return jsonify({"models": list_models(), "current": current_model if is_ready() else None})


@app.post("/api/models/select")
def api_select_model():
    name = json_body().get("name", "")
    if name not in list_models():
        return jsonify({"error": "Unknown model"}), 400
    with llama_lock:
        try:
            start_llama(name)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500
    return jsonify({"current": current_model})


@app.get("/api/settings")
def api_get_settings():
    return jsonify(load_settings())


@app.put("/api/settings")
def api_put_settings():
    prompt = str(json_body().get("system_prompt", ""))
    write_json_atomic(SETTINGS_FILE, {"system_prompt": prompt})
    return jsonify({"system_prompt": prompt})


@app.get("/api/chats")
def api_list_chats():
    return jsonify(list_chats())


@app.post("/api/chats")
def api_create_chat():
    now = time.time()
    chat = {
        "id": uuid.uuid4().hex,
        "title": DEFAULT_TITLE,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    save_chat(chat)
    return jsonify(chat), 201


@app.get("/api/chats/<chat_id>")
def api_get_chat(chat_id):
    chat = load_chat(chat_id)
    if chat is None:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify(chat)


@app.patch("/api/chats/<chat_id>")
def api_rename_chat(chat_id):
    chat = load_chat(chat_id)
    if chat is None:
        return jsonify({"error": "Chat not found"}), 404
    title = " ".join(str(json_body().get("title", "")).split())
    if not title:
        return jsonify({"error": "Empty title"}), 400
    chat["title"] = title[:RENAME_MAX]
    save_chat(chat, touch=False)
    return jsonify({"id": chat_id, "title": chat["title"]})


@app.delete("/api/chats/<chat_id>")
def api_delete_chat(chat_id):
    if load_chat(chat_id) is None:
        return jsonify({"error": "Chat not found"}), 404
    (CHATS_DIR / f"{chat_id}.json").unlink()
    return jsonify({"ok": True})


@app.post("/api/chats/<chat_id>/messages")
def api_send_message(chat_id):
    chat = load_chat(chat_id)
    if chat is None:
        return jsonify({"error": "Chat not found"}), 404
    text = str(json_body().get("content", "")).strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400
    if not is_ready():
        return jsonify({"error": "No model is loaded"}), 409

    chat.setdefault("messages", []).append({"role": "user", "content": text})
    if chat.get("title", DEFAULT_TITLE) == DEFAULT_TITLE:
        chat["title"] = make_title(text)
    save_chat(chat)

    payload = build_messages(chat["messages"], load_settings()["system_prompt"])
    return Response(
        stream_reply(chat, payload),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    for folder in (LLAMA_DIR, MODELS_DIR, CHATS_DIR):
        folder.mkdir(exist_ok=True)
    if not SETTINGS_FILE.exists():
        write_json_atomic(SETTINGS_FILE, {"system_prompt": ""})
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
