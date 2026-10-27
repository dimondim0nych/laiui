# LaiUI

A lightweight web interface for local GGUF models. It runs in the browser and launches models through llama.cpp (`llama-server`). It is similar in idea to Open WebUI, but with a smaller feature set.

## Features

- Pick a GGUF model from the `models/` folder and launch it through `llama-server`
- Streamed replies: text appears as it is generated
- A "Thinking" block for models that produce reasoning (for example, Qwen3). The block stays open during generation and collapses once the reply is done
- Multiple chats that persist across restarts
- System instructions shared by all chats, kept across restarts
- Chat deletion only after confirmation
- Dark theme that follows the system setting, and a sidebar that adapts to narrow screens

## Requirements

- Python 3.9 or newer
- Flask
- A llama.cpp build that includes the `llama-server` executable (`llama-server.exe` on Windows)
- Models in GGUF format

## Project structure

```
LaiUI/
├── app.py            # Flask server, llama-server management, chats and streaming
├── settings.json     # system instructions (created on first run)
├── llama/            # llama.cpp build containing llama-server
├── models/           # GGUF models
├── chats/            # one JSON file per chat
├── templates/
│   └── index.html    # interface markup
└── static/
    ├── app.js        # interface logic
    └── style.css     # styles
```

## Installation

1. Clone the repository.
2. Install Flask: `pip install flask`
3. Put the llama.cpp build into the `llama/` folder. The script will find `llama-server` even in a nested folder.
4. Put your GGUF models into the `models/` folder.

## Running

From the project root:

```
python app.py
```

Open `http://127.0.0.1:5000` in your browser.

Select a model with the "Select model" button at the bottom. Loading can take up to three minutes before the model is ready. Press Enter to send a message and Shift+Enter for a new line.

Set system instructions with the "Set your own system instructions" button in the sidebar.

## Configuration

The main settings are at the top of `app.py`:

| Constant | Default | Purpose |
|---|---|---|
| `LLAMA_PORT` | 8081 | port used by `llama-server` |
| `CONTEXT_SIZE` | 4096 | context size, passed to `llama-server` as `-c` |
| `REPLY_RESERVE` | 512 | tokens reserved for the reply when trimming history |
| `LOAD_TIMEOUT` | 180 | seconds to wait for a model to load |

## Data files

- `chats/` contains one JSON file per chat
- `settings.json` stores the system instructions
- `llama-server.log` contains the `llama-server` log and helps find the cause if a model fails to load

Do not commit these files and folders to Git.

## Known limitations

- There is no stop button for generation. To interrupt a reply, close the tab.
- Replies are shown as plain text, without Markdown rendering or code highlighting.
- Chats cannot be renamed. The title comes from the first message.
- The model is not loaded automatically on startup. You need to select it again.
- Token counting is approximate, so history trimming in long chats may kick in earlier than necessary.
- The context size is fixed by `CONTEXT_SIZE`, even if the model supports more.

## Security

The Flask server runs in development mode and is meant for local use only. `llama-server` listens on `127.0.0.1` only, so it is not reachable from outside. Do not expose the ports to the network without a proper WSGI server and authentication.
