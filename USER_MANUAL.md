# PSEditor User Manual

PSEditor is a code editor that runs in your web browser, similar to Cursor or VS Code. It has a file explorer,
search, a real terminal, and an **AI chat** panel where an AI agent (OpenRouter by default) can read, create and edit
code in your project. You approve every change before it happens.

PSEditor runs **only on your own PC** (`http://127.0.0.1:8800/`). It is not meant to be opened from other computers.

---

## Contents

1. [Requirements](#1-requirements)
2. [Installing on a PC](#2-installing-on-a-pc)
3. [Starting and stopping](#3-starting-and-stopping)
4. [The screen](#4-the-screen)
5. [Opening folders (the workspace)](#5-opening-folders-the-workspace)
6. [Editing files](#6-editing-files)
7. [Searching](#7-searching)
8. [Terminal](#8-terminal)
9. [Setting up the AI agent (OpenRouter)](#9-setting-up-the-ai-agent-openrouter)
10. [Using the AI chat](#10-using-the-ai-chat)
11. [Approving and undoing agent changes](#11-approving-and-undoing-agent-changes)
12. [Previewing HTML pages](#12-previewing-html-pages)
13. [Other agent types](#13-other-agent-types)
14. [Moving PSEditor to another PC](#14-moving-pseditor-to-another-pc)
15. [Settings](#15-settings)
16. [Troubleshooting](#16-troubleshooting)
17. [Safety notes](#17-safety-notes)
18. [Keyboard shortcuts](#18-keyboard-shortcuts)

---

## 1. Requirements

| Item | Requirement |
|------|-------------|
| Operating system | Windows 10 or 11, 64-bit |
| Python | 3.10 or newer (3.13 recommended). The installer can install it for you with `winget`. |
| Browser | Chrome, Edge or Firefox (recent version) |
| Internet | Needed for the AI agent (OpenRouter) and the first time the editor page loads (see [Troubleshooting](#16-troubleshooting)). **Not** needed to install the Python packages. |
| AI account | An OpenRouter API key from <https://openrouter.ai/keys> |

---

## 2. Installing on a PC

There are two installation packages. Both are built in the `dist` folder.

| Package | Built with | Contains | Use it when |
|---------|-----------|----------|-------------|
| `PSEditor-install.zip` (~27 MB) | `package.bat` | The app and offline Python packages (`wheels` folder). **No** personal data. | Giving PSEditor to someone else, or a clean install. |
| `PSEditor-full.zip` (~49 MB) | `package_full.bat` | Everything above **plus** `.venv` and `db.sqlite3` (your workspaces, chats, agents **and API keys**). | Moving your own setup to another PC you trust. |

### Steps

1. Copy the zip file to the other PC.
2. Right-click the zip and choose **Extract All…**, for example to `C:\PSEditor` or `F:\pseditor`.
   Use a folder you can write to (not `C:\Program Files`).
3. Open the extracted folder and double-click **`install.bat`**.
4. The installer will:
   - find Python 3.10 or newer. If none is found it offers to install Python 3.13 with `winget` (answer `Y`);
   - create the `.venv` virtual environment, or reuse the bundled one if it still works on this PC;
   - install the Python packages from the bundled `wheels` folder, **without internet**;
   - create or update the database (`db.sqlite3`);
   - ask for your **OpenRouter API key** if the default agent does not have one yet. What you type is hidden.
     Press **Enter** to skip and add it later (see [section 9](#9-setting-up-the-ai-agent-openrouter));
   - create a **PSEditor** shortcut on the Desktop and in the Start menu.
5. At the end, answer `Y` to start PSEditor now.

Everything the installer does is logged in `install.log` in the same folder. Read it if something goes wrong.

You can run `install.bat` again at any time. It is safe and keeps your data.

### Installing on a Mac (macOS)

Use **`PSEditor-mac.zip`** (built on Windows with `package_mac.bat`). It works on Apple silicon (M1/M2/M3/M4)
and Intel Macs, and contains offline Python packages in the `wheels_mac` folder.

1. You need Python 3.10 or newer: install it from <https://www.python.org/downloads/macos/>, or run
   `brew install python@3.13` if you use Homebrew. The installer offers to run Homebrew for you.
2. Double-click `PSEditor-mac.zip` in Finder. It unzips to a **PSEditor** folder. Move it somewhere permanent,
   for example your home folder.
3. Open the folder, **right-click `install_mac.command` → Open**, then click **Open** in the warning.
   The first time, macOS blocks files from the internet unless you open them this way. If you still see
   *"cannot be opened"*, go to **System Settings → Privacy & Security** and click **Open Anyway**,
   or in Terminal run `bash ~/PSEditor/install_mac.command`.
4. A Terminal window runs the same steps as on Windows: virtual environment, offline packages, database, and the
   OpenRouter API key prompt. It creates **PSEditor.command** on the Desktop.
5. To start PSEditor, double-click **PSEditor.command** on the Desktop (or `run_mac.command` in the folder).
   To stop it, press **Ctrl+C** in its Terminal window.

On a Mac the terminal panel runs **bash** instead of PowerShell, and activating a virtual environment is
`source .venv/bin/activate`. PSEditor's shortcuts work with **Cmd** as well as **Ctrl** (for example Cmd+S, Cmd+P).

---

## 3. Starting and stopping

**Start:** double-click the **PSEditor** shortcut on the Desktop, or `run.bat` in the PSEditor folder.

A black console window opens, then your browser opens <http://127.0.0.1:8800/> after about 2 seconds.
If the browser does not open by itself, type that address in the browser.

**Stop:** go to the black console window and press **Ctrl+C**, or close the window.

> Keep the console window open while you use PSEditor. Closing it stops the editor.

---

## 4. The screen

```
+-----------------------------------------------------------------------------------+
| PSEditor | Open folder... | Go to file | Terminal |              | AI Chat | Agents|  <- top bar
+--------------+---------------------------------------------+----------------------+
| Explorer     |  tabs: app.py  index.html                   | Agent v  Chat v  + 🗑 |
| Search       |                                             | model ...   Models v |
|              |                                             | Workspace: [folders] |
|  file tree   |              code editor                    |                      |
|              |                                             |   chat messages      |
|              +---------------------------------------------+                      |
|              |  Terminal (Ctrl+`)                          | [ask the agent...]   |
|              |                                             |        Stop   Send   |
+--------------+---------------------------------------------+----------------------+
| status bar                                                                        |
+-----------------------------------------------------------------------------------+
```

| Area | What it does |
|------|--------------|
| **Top bar** | Open a folder, go to a file by name, show/hide the terminal, show/hide the AI chat, set up agents. |
| **Left sidebar** | **Explorer** (file tree) and **Search** (search inside files). |
| **Center** | Code editor with tabs. The terminal opens below it. |
| **Right panel** | AI chat: choose agent, conversation and model; see which folders the agent can use; type requests. |
| **Status bar** | Current state and messages. |

Drag the thin borders between the areas to resize them.

---

## 5. Opening folders (the workspace)

The **workspace** is the set of folders you are working on. The AI agent can read, create, edit and delete files
**only inside the workspace folders**.

### Open a folder

1. Click **Open folder…** (top left) or press **Ctrl+Shift+O**.
2. Either type a path (for example `D:\projects\myapp`) and click **Open**, or click **Browse** and click through
   the folders, then click **Open**.
3. To start a new project, browse to the parent folder and click **+ New folder here…**.

Folders you opened before are listed under **Recent**. Click one to reopen it, or click **remove** to take it off
the list. Removing it from the list does not delete any files.

### Add more folders to the same workspace

- In the Explorer, click **📁 Add to workspace**, or
- in the chat panel, click **+ Add folder** in the *Workspace* row.

With several folders, file paths are shown as `FolderName/path/to/file`.

### Remove a folder from the workspace

Click the **✕** on the folder's chip in the chat panel, or right-click the folder in the Explorer and choose
**Remove from workspace**. Files on disk are **never** deleted by this.

> Unsaved edits are saved automatically before the folder list changes.

---

## 6. Editing files

- Click a file in the **Explorer** to open it in a tab. The editor is Monaco, the same editor used in VS Code,
  with syntax colouring, find/replace (**Ctrl+F** / **Ctrl+H**), multiple cursors and so on.
- A dot on a tab means the file has unsaved changes. Press **Ctrl+S** to save.
- **Ctrl+P** (or **Go to file**) opens a quick list. Type part of a file name and press **Enter**.
- If you try to leave the page with unsaved changes, the browser warns you.

### File and folder actions

Right-click in the Explorer:

| On | Menu items |
|----|-----------|
| Empty space | New file, New folder, Add folder to workspace… |
| A file or folder | New file, New folder, Rename, Delete, Copy relative path |
| An HTML file | also Open preview, Edit source |
| A workspace folder (top level) | Remove from workspace, Copy path |

The buttons at the top of the Explorer do the same: **+ File**, **+ New folder**, **📁 Add to workspace**,
**↻** (refresh the tree).

---

## 7. Searching

1. Click the **Search** tab in the left sidebar, or press **Ctrl+Shift+F**.
2. Type the text and press **Enter**.
3. Options:
   - **regex** treats the text as a regular expression;
   - **case** makes the search case-sensitive;
   - **files** limits the search to matching file names, for example `*.py` or `*.html`.
4. Click a result to open the file at that line.

Folders such as `.git`, `node_modules`, `__pycache__`, `.venv` and `venv` are skipped.

---

## 8. Terminal

Press **Ctrl+`** (the key above Tab) or click **Terminal** in the top bar.

This is a **real PowerShell terminal**, one per workspace, that starts in your project folder. You can use it as
you would a normal terminal:

- run `python`, `pip`, `npm`, `git`, `python manage.py runserver`, and so on;
- interactive programs work: password prompts, `input()`, the Python REPL, `createsuperuser`;
- `cd` works anywhere on the PC, and the terminal remembers the folder, variables and activated virtual environment;
- the terminal keeps running when you reload the page or hide the panel.

| Action | How |
|--------|-----|
| Stop a running program | **Ctrl+C** (with no text selected) |
| Copy text | Select it, then **Ctrl+C** |
| Paste | **Ctrl+Shift+V** |
| End the shell | type `exit`. Pressing a key afterwards starts a new one. |
| Activate a virtual environment | `venv\Scripts\activate` (or `.venv\Scripts\activate`). The prompt shows `(venv)`. |

> Inside the terminal, **Ctrl+P / Ctrl+L / Ctrl+S** go to the shell, not to PSEditor. Only **Ctrl+`** is caught
> by PSEditor (to hide the terminal).

If the full terminal cannot load (no internet for xterm.js, or `pywinpty` missing), PSEditor uses a simpler
line-based terminal. It still runs commands and remembers `cd` and variables, but interactive programs do not work.

---

## 9. Setting up the AI agent (OpenRouter)

A new install already contains an agent called **OpenRouter**, set as the **default agent**:

| Setting | Value |
|---------|-------|
| Type | OpenAI-compatible API |
| Base URL | `https://openrouter.ai/api/v1` |
| Model | `anthropic/claude-sonnet-5.5` |

It only needs your API key.

### Get an OpenRouter API key

1. Go to <https://openrouter.ai/> and sign in or create an account.
2. Add credit (**Settings → Credits**) if you want to use paid models.
3. Go to <https://openrouter.ai/keys>, click **Create Key**, and copy the key (it starts with `sk-or-`).

### Enter the key in PSEditor

1. Click **⚙ Agents** (top right).
2. Click **OpenRouter** in the list on the left.
3. Paste the key into **API key**.
4. Click **Save**, then **Test connection**. A success message means it works.

If you skip the key and send a chat message, PSEditor shows:
*"Agent 'OpenRouter' has no OpenRouter API key. Click Agents and paste your key…"*

### Choose a different model

- **From the chat panel:** click the model box or **Models ▾** and pick a model. The choice is saved on the agent
  straight away and is still selected the next time you open PSEditor.
- **From the settings:** in **Agents**, click **Load models**, pick one, then **Save**.

The model must support **tool calling**, because the agent uses tools to read and edit files. Good choices on
OpenRouter include Anthropic Claude, OpenAI GPT and Google Gemini models. For a free option, use the preset
**OpenRouter (free model)**. Free models are slower and less capable.

### Add another agent

In **Agents**, pick a preset from **+ Add agent from preset…**, fill in the key, model or URL, and click **Save**.
The agent you pick in the chat panel is saved straight away: it becomes the **Default agent** (★ in the list) and
is still selected the next time you open PSEditor. Ticking **Default agent** in **Agents** does the same.

---

## 10. Using the AI chat

1. Open a workspace folder ([section 5](#5-opening-folders-the-workspace)).
2. In the chat panel, check the **agent** (first drop-down) and the **conversation** (second drop-down).
   Click **+** to start a new conversation, or **🗑** to delete the current one.
3. Type what you want in the box at the bottom, for example:
   - *"Create a Flask app with a /health endpoint and a test for it."*
   - *"Explain what `views.py` does."*
   - *"Fix the error in the terminal output: …"*
   - *"Rename the function `calc` to `calculate_total` everywhere."*
4. Press **Enter** to send (**Shift+Enter** for a new line), or click **Send**.

**Include open file / selection** (ticked by default) sends the file you have open, and any text you selected, so
the agent knows what you are looking at. Untick it if the request has nothing to do with the open file.

While the agent works, the status shows *"Agent is working…"*. The agent can:

| Without asking | Only after you approve |
|----------------|------------------------|
| list files, read files, search | create/write a file, edit a file, delete a file, run a command |

Files the agent changes are reloaded in the editor automatically.

Click **Stop** at any time to cancel the request.

**Tips for good results**
- Be specific: name the files, functions and the expected result.
- Ask for one thing at a time for large changes.
- Ask the agent to run the tests after a change, for example *"then run `python manage.py test`"*.

---

## 11. Approving and undoing agent changes

### Approve or deny

Before the agent writes, edits or deletes a file, or runs a command, the chat shows exactly what it wants to do:
the new file content, a before/after of the edit, or the command. The status shows *"Waiting for your approval…"*.

- **Approve** runs the action.
- **Deny** refuses it. You can type a reason or *"do this instead"*; the agent reads it and adapts.
- **Stop** cancels the whole request.
- An action you never answer is **not** run. It times out after 10 minutes.

Command-line agents (see [section 13](#13-other-agent-types)) are approved once, before they start.

### Undo (Revert changes)

Every request that changed files has a **Revert changes** button under it. Click it and confirm to put all files
changed by that request back to how they were before. Each request can be reverted once.

> Approved changes are saved to disk straight away. Use **Revert changes**, or git, to undo them.

---

## 12. Previewing HTML pages

Clicking an `.html` file in the Explorer opens it **as a web page** in a new browser tab. This preview can write
to disk, for HTML apps that save files.

To edit the HTML code instead, right-click the file and choose **Edit source**.

---

## 13. Other agent types

| Type | Use for | What to fill in |
|------|---------|-----------------|
| **OpenAI-compatible** | OpenRouter (default), OpenAI, Ollama, LM Studio, Groq, vLLM… | Base URL, API key, model |
| **Anthropic** | Claude directly from Anthropic | API key, model |
| **Custom HTTP (webhook)** | Your own agent service | Webhook URL (and key if needed). See `README.md` for the JSON format. |
| **Command-line** | `claude -p`, `codex exec`, `aider`, or any script | The command. The prompt is sent on stdin, or replaces `{prompt}`. |

Local models (no internet, no cost):

- **Ollama:** install Ollama, run `ollama pull qwen2.5-coder:7b`, then use the **Ollama (local)** preset.
- **LM Studio:** start the local server in LM Studio, then use the **LM Studio (local)** preset.

Local models must support tool calling. Small models often make mistakes with file edits.

---

## 14. Moving PSEditor to another PC

**On the old PC**, in the PSEditor folder:

| To move | Double-click | Then copy |
|---------|-------------|-----------|
| The app only (clean start) | `package.bat` | `dist\PSEditor-install.zip` |
| The app **with** your data and keys | `package_full.bat` | `dist\PSEditor-full.zip` |

- Close PSEditor (`run.bat`) before running `package_full.bat`.
- `package.bat` needs internet on the old PC, because it downloads the offline Python packages.

**On the new PC:** extract the zip and double-click `install.bat` ([section 2](#2-installing-on-a-pc)).

The bundled `.venv` in the full package works as-is only if the new PC has the same Python at the same location
(`C:\Python313`). Otherwise `install.bat` rebuilds it automatically from the `wheels` folder. Your data is kept
either way.

### Backup

All your settings, workspaces, chats and agents are stored in **`db.sqlite3`** in the PSEditor folder. To back up,
close PSEditor and copy that file. Your project files stay in their own folders and are not inside PSEditor.

---

## 15. Settings

Advanced settings are in `pseditor\settings.py`. Restart PSEditor after changing them.

| Setting | Default | Meaning |
|---------|---------|---------|
| `PSEDITOR_ALLOW_TERMINAL` | `True` | `False` turns off the terminal **and** the agent's ability to run commands. |
| `PSEDITOR_REQUIRE_APPROVAL` | `True` | Agent writes, edits, deletes and commands wait for your approval. Keep it `True`. |
| `PSEDITOR_APPROVE_READS` | `False` | `True` also asks before the agent lists, reads or searches files. |
| `PSEDITOR_APPROVAL_TIMEOUT` | `600` | Seconds to wait for your answer. Unanswered actions are not run. |
| `PSEDITOR_COMMAND_TIMEOUT` | `60` | Maximum seconds for a command run by the agent. |
| `PSEDITOR_MAX_AGENT_STEPS` | `25` | Maximum tool steps the agent may take for one request. |
| `PSEDITOR_MAX_FILE_BYTES` | 2 MB | Largest file the editor and agent will open. |
| `PSEDITOR_IGNORED_DIRS` | `.git`, `node_modules`, … | Folders hidden from the tree, search and the agent. |
| `PSEDITOR_OCR_ENDPOINT` | `https://api.opentyphoon.ai/v1/ocr` | Thai OCR service. **Read text** sends the image to Typhoon chat completions (`/v1/chat/completions`, model `typhoon-ocr`). A URL ending in `/ocr` is rewritten to that path. Empty shows the picture only. |
| `PSEDITOR_OCR_API_KEY` | empty | Bearer token for the OCR endpoint. Set it with the OCR gear, or with the `PSEDITOR_OCR_API_KEY` environment variable. |
| `PSEDITOR_OCR_MODEL` | `typhoon-ocr` | Model field sent with the image. |

To use a different port, change `8800` in `run.bat` in **both** places.

---

## 16. Troubleshooting

| Problem | Cause and fix |
|---------|---------------|
| `install.bat`: *"Python 3.10 or newer is required"* | Install Python from <https://www.python.org/downloads/windows/>, tick **Add python.exe to PATH**, then run `install.bat` again. |
| `install.bat`: *"Could not download the Python packages… cannot reach pypi.org"* | The zip has no `wheels` folder and the PC has no internet. Build the zip again with `package.bat` on a PC with internet. Behind a company proxy, run `set HTTPS_PROXY=http://proxy:port` in a Command Prompt, then `install.bat` from the same window. |
| Browser shows *"This site can't be reached"* | PSEditor is not running. Start it with the shortcut or `run.bat` and keep the console window open. |
| Port 8800 already in use | Another copy is running. Close it, or change the port in `run.bat`. |
| The editor is a plain text box, with no colours | The PC had no internet when the page loaded, so Monaco could not load from the CDN. Connect to the internet and reload the page (**F5**). |
| Terminal does not run interactive programs | xterm.js (internet) or `pywinpty` is missing, so the simple terminal is used. Connect to the internet and reload, or run `install.bat` again. |
| *"Agent 'OpenRouter' has no OpenRouter API key"* | Add the key: **Agents → OpenRouter → API key → Save**. |
| Test connection fails with 401 | The API key is wrong or was deleted. Create a new key on OpenRouter. |
| Error 402 or *"insufficient credits"* | Add credit on OpenRouter, or use a free model. |
| The agent answers but does not change files | The model does not support tool calling. Choose another model. |
| *"No agent configured"* | Click **Agents**, pick a preset, fill it in and click **Save**. |
| An action shows *"Timed out — not run"* | It was not approved within 10 minutes. Ask again. |
| Anything else during install | Open `install.log` in the PSEditor folder. |

---

## 17. Safety notes

- PSEditor can read and write files and run commands on your PC. Use it **only on your own PC**, and never expose
  port 8800 to a network.
- The agent can only touch files **inside the workspace folders**. You add the folders; the agent cannot add more.
- The **terminal is not limited to the workspace**. It can do anything your Windows user can.
- **API keys are stored in plain text** in `db.sqlite3`. Do not share that file or `PSEditor-full.zip`.
- Read each action before you click **Approve**, especially delete actions and commands.
- Keep your projects in git, so you can always go back.

---

## 18. Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| **Ctrl+S** | Save the current file |
| **Ctrl+P** | Go to file |
| **Ctrl+Shift+O** | Open folder |
| **Ctrl+Shift+F** | Search in files |
| **Ctrl+L** | Focus the AI chat box |
| **Ctrl+`** | Show/hide the terminal |
| **Enter** / **Shift+Enter** | In the chat box: send / new line |
| **Ctrl+F** / **Ctrl+H** | In the editor: find / replace |
| **Ctrl+C** | In the terminal: copy the selection, or stop the running program |
| **Ctrl+Shift+V** | In the terminal: paste |
