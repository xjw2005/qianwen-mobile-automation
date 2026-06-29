# Qianwen Mobile Automation Migration README

This reference explains how to move the entire Qianwen mobile automation integration to another agent or computer and run it with minimal context loss.

The integration has **two cooperating modules**:

1. **千问来源提取 (Qianwen Source Extractor)** — a Node.js script that extracts real reference source URLs from a Qianwen share page by replaying the share/info API through Chrome DevTools Protocol (CDP).
2. **跑移动端 (Qianwen Mobile Runner)** — a Python package that drives the Qianwen Android app via ADB, captures answers / thinking content / share links, and (optionally) invokes the JS extractor to write sources back to Feishu.

The Python runner is the orchestrator. When `--extract-sources` is enabled, it captures the answer share link on the phone, hands it to the JS extractor, and the JS extractor writes the sources to Feishu.

## Changelog / 变更说明

### 2026-06-24 — `--link-only` + 飞书表 ID 外置

- **New `--link-only` mode** (mirrors the DeepSeek runner). Skips mobile-side thinking capture; the phone only asks the question and grabs the share link, then answer + 深度思考 + sources are pulled from the share page in one shot. Requires `--extract-sources`. The share-page content overrides the mobile-captured answer/thinking.
- **`extract-sources.js` now also returns `answer`, `thinkingContent`, and `searchEnabled`.** They come from the same `share/info` API response: `response_messages[]` are split by `mime_type` (`plan_cot/post` → 深度思考; `multi_load/iframe` etc. → answer with inline markers like `[(deep_think)]` / `[source_group_web_N]` / `[(video_note_list_1)]` stripped). Verified end-to-end against a real share link (18 sources + 1786-char answer + 268-char thinking, 0 residual markers). The source-extraction path is unchanged.
- **Externalized Feishu table IDs**: new `--feishu-config` (alias `--writeback-config`) loads a JSON with `input.baseUrl`/`baseToken`/`tableId`/`viewId`, `writeback.answerTableId`, `writeback.sourceTableId`, and `collectAccount`; applied as defaults with **CLI overriding JSON**. New `--answer-table-id` flag. Template: `mobile-auto-qianwen/configs/feishu-qianwen-example.json`. Only table IDs change between environments — field names/column structure stay fixed (`feishu_base.py` `ANSWER_WRITEBACK_FIELDS` / `SOURCE_WRITEBACK_FIELDS`).

## What This Skill Contains

```text
references/
  mobile-auto-qianwen/              # full Python project workspace (module 2)
    mobile_auto_qianwen/            # Python package
      __init__.py
      adb_client.py                 # ADB wrapper
      app.py                        # UI automation (tap, swipe, share, thinking capture)
      artifacts.py                  # state snapshots
      constants.py                  # package name, IME, UI text constants
      feishu_base.py                # Feishu Base read/write via lark-cli
      ocr.py                        # Windows Media.Ocr wrapper for screenshots
      result_writer.py              # result JSON writer
      runner.py                     # CLI entry point (python -m mobile_auto_qianwen.runner)
      source_extractor_bridge.py    # bridge: invokes qianwen-source-extractor/run.js
      source_links.py               # legacy in-app source link probing
      task_schema.py                # task JSON loading and normalization
      thinking_capture.py           # thinking-detail page capture
      time_utils.py                 # ISO timestamps and stamps
      ui_xml.py                     # uiautomator XML parsing
      命令大全.txt                   # ready-to-paste command examples
    configs/                        # externalized Feishu table-ID config
      feishu-qianwen-example.json   # --feishu-config template (only table IDs change per env)
    qianwen-source-extractor/       # JS extractor (module 1, integrated copy)
      run.js                        # main entry: extract + write to Feishu
      extract-sources.js            # CDP-based source URL extraction
      write-feishu.js               # Feishu Bitable writeback
      package.json
      README.md
  qianwen-source-extractor/         # standalone JS extractor (module 1, top-level reference)
    run.js
    extract-sources.js
    write-feishu.js
    package.json
    package-lock.json
    README.md
  tasks/                            # task JSON examples
  docs/                             # design notes
```

The runnable Python project snapshot is under `references/mobile-auto-qianwen/`.
The standalone JS extractor is under `references/qianwen-source-extractor/` for agents that only need web-side source extraction.

## Restore On A New Computer

1. Create a workspace folder, for example `D:\CursorProjects\mobile-auto-qianwen`.
2. Copy everything from `references/mobile-auto-qianwen/` into that workspace. The workspace must contain:
   - `mobile_auto_qianwen/` (the Python package)
   - `qianwen-source-extractor/` (the JS scripts, sibling of the Python package)
3. Create runtime folders if they are missing:

```powershell
New-Item -ItemType Directory -Force -Path tasks, results, outputs
```

4. Copy task examples:

```powershell
Copy-Item <skill>\references\tasks\*.json .\tasks\
```

5. Install Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

6. Install Node.js dependencies for the JS extractor:

```powershell
cd qianwen-source-extractor
npm install
cd ..
```

7. Confirm `lark-cli` is on PATH (Feishu read/write). If not, pass `--lark-cli <path-to-lark-cli.cmd>`.

## Android And ADB Setup

Install Android platform tools and locate `adb`. Common paths:

- Windows: `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`
- macOS: `~/Library/Android/sdk/platform-tools/adb`
- Linux: `~/Android/Sdk/platform-tools/adb`

Check devices:

```powershell
adb devices
```

Expected output contains at least one `device` row, for example:

```text
100.76.50.7:6666    device
emulator-5556       device
```

Use the exact serial in task JSON or pass it with `--serial` / `--device`.

For network ADB devices, connect first:

```powershell
adb connect 100.76.50.7:6666
adb devices
```

If the device shows `unauthorized`, unlock the device and accept the USB/network debugging prompt.

## ADB Keyboard Setup

Chinese question input depends on ADB Keyboard (`com.android.adbkeyboard/.AdbIME`).

1. Install `keyboardservice-debug.apk` (shipped at `references/tools/keyboardservice-debug.apk`).
2. Open the Android input-method settings page and enable `ADB Keyboard`.
3. Switch the current input method to `com.android.adbkeyboard/.AdbIME`.
4. Confirm it with:

```powershell
adb shell ime list -s
adb shell settings get secure default_input_method
```

If live runs fail with `adb_keyboard_not_installed`, install the APK and set the IME again.

On some emulator ROMs, `ime enable` and `ime set` are blocked by policy.
In that case, use the Settings UI plus `adb shell dumpsys input_method` to confirm the service is active.

## Chrome CDP Setup (For JS Source Extractor)

The JS extractor needs a Chrome instance running with remote debugging so it can replay the share/info API from the page context (cookies + origin).

1. Close all Chrome windows.
2. Launch Chrome with remote debugging:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

3. In that Chrome instance, open the Qianwen share page that the phone captured (`https://www.qianwen.com/share/chat/<id>?biz_id=ai_qwen`).
4. Confirm the CDP endpoint is reachable:

```powershell
Invoke-RestMethod http://127.0.0.1:9222/json/version
```

The default CDP URL is `http://127.0.0.1:9222`. Override with `--cdp-url` on the Python runner or `--cdp` on the JS script.

## Runner Commands (Python Mobile Automation)

The runner is invoked as a module from the workspace root:

```powershell
python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json --dry-run
python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json
```

Override ADB / device / output:

```powershell
python -m mobile_auto_qianwen.runner `
  --task tasks\qianwen_real_device_thinking_smoke.json `
  --output results\qianwen-real-device-thinking-fast.json `
  --serial 100.76.50.7:6666 `
  --source-limit 2
```

Feishu Base mode (read questions from Feishu, write answers back):

```powershell
python -m mobile_auto_qianwen.runner `
  --base-url "https://yuoukuajing.feishu.cn/base/UiE3bhcHRaCE01sh5Anc1AZanKd?table=tblXZ8vq7SouTIuu&view=vewZsJsX7y" `
  --base-limit 3 `
  --source-limit 99 `
  --writeback `
  --lark-cli "<lark-cli-path>" `
  --adb "<adb-path>" `
  --device <device-serial>
```

Full pipeline with JS source extraction + Feishu writeback + mark-collected:

```powershell
python -m mobile_auto_qianwen.runner `
  --base-url "https://yuoukuajing.feishu.cn/base/UiE3bhcHRaCE01sh5Anc1AZanKd?table=tblXZ8vq7SouTIuu&view=vewZsJsX7y" `
  --base-limit 1 `
  --source-limit 99 `
  --writeback `
  --mark-collected `
  --extract-sources `
  --lark-cli "<lark-cli-path>" `
  --adb "<adb-path>" `
  --device <device-serial>
```

### Key CLI Flags

| Flag | Purpose |
|------|---------|
| `--task <path>` | Run from a task JSON file. |
| `--base-url <url>` | Run from Feishu Base (alternative to `--task`). |
| `--base-token`, `--table-id`, `--view-id` | Feishu Base location (alternative to `--base-url`). |
| `--base-start`, `--base-end`, `--base-limit` | Row range to read from Feishu. Input question text comes from `问题文本` (legacy `问题` fallback); missing `是否本次采集` defaults to selected. |
| `--serial` / `--device` | ADB device serial. Required when multiple devices are online. |
| `--adb` | Path to `adb.exe`. |
| `--output` | Result JSON path. Use a unique value per parallel process. |
| `--writeback` | Write answers back to Feishu after each question. |
| `--mark-collected` | With `--writeback`, set `是否本次采集` to `否` after a successful answer writeback. |
| `--collect-account` | Override the `采集账号` field written to Feishu. |
| `--extract-sources` | Enable the JS source extractor. After the phone captures the share link, invoke `qianwen-source-extractor/run.js` to extract sources and write them to the Feishu source table. |
| `--cdp-url` | CDP endpoint for the JS extractor. Default: `http://127.0.0.1:9222`. |
| `--extractor-script` | Explicit path to `qianwen-source-extractor/run.js`. Auto-located if omitted. |
| `--extractor-timeout` | Per-attempt timeout (seconds) for the JS extractor. Default: 120. |
| `--extractor-retries` | Max retries for the JS extractor. Default: 2. |
| `--source-base-token`, `--source-table-id` | Feishu source table location. Defaults to the input base token and the built-in Qianwen source table id. |
| `--force-quick` | Disable deep-thinking for every question. |
| `--debug` | Keep screenshots, currentFocus, and XML artifacts. |
| `--dry-run` | Print the planned task and writeback without running. |

## JS Source Extractor Commands (Standalone)

The JS extractor can be run on its own when you already have a Qianwen share URL.

Full pipeline (extract + write to Feishu):

```powershell
node qianwen-source-extractor\run.js `
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" `
  --question-id NQ-001 `
  --base-token <feishu_app_token> `
  --table-id <feishu_table_id>
```

Extract only (save to JSON):

```powershell
node qianwen-source-extractor\run.js `
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" `
  --extract-only `
  --output sources.json
```

Write only (from existing JSON):

```powershell
node qianwen-source-extractor\run.js `
  --write-only `
  --sources sources.json `
  --question-id NQ-001 `
  --base-token <feishu_app_token> `
  --table-id <feishu_table_id>
```

### JS Extractor Flags

| Flag | Purpose |
|------|---------|
| `--url <url>` | Qianwen share URL (required for extract). |
| `--question-id <id>` | Question ID to associate (required for write). |
| `--natural-question <id>` | Backward-compatible alias for `--question-id`. |
| `--base-token <token>` | Feishu Bitable app_token (required for write). |
| `--table-id <id>` | Feishu Bitable table_id (required for write). |
| `--cdp <url>` | CDP endpoint. Default: `http://127.0.0.1:9222`. |
| `--timeout <ms>` | Page readiness wait. Default: 15000. |
| `--output <file>` | Save extracted sources JSON to a file. |
| `--sources <file>` | Load sources from JSON (for `--write-only`). |
| `--extract-only` | Only extract, don't write to Feishu. |
| `--write-only` | Only write to Feishu from `--sources`. |
| `--dry-run` | Preview rows without writing. |
| `--ai-platform <name>` | AI platform name. Default: `千问`. |

## Task JSON Contract

A task JSON describes sessions and questions. The runner accepts either a top-level array of sessions or an object with a `sessions` field.

```json
{
  "taskName": "qianwen-thinking-source-extraction-smoke",
  "mode": "separate",
  "thinking": true,
  "device": {
    "adb": "C:\\Users\\Administrator\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe",
    "serial": "100.76.50.7:6666"
  },
  "sessions": [
    {
      "sessionName": "q1",
      "newChat": true,
      "thinking": true,
      "questions": [
        "请用两句话解释为什么天空是蓝色的。"
      ]
    }
  ],
  "options": {
    "sourceLimit": 2,
    "waitStableSeconds": 2,
    "expertAnswerMaxScrolls": 8,
    "sourceExtractor": {
      "enabled": true,
      "cdpUrl": "http://127.0.0.1:9222",
      "timeoutSeconds": 120,
      "maxRetries": 2,
      "retryBackoffBase": 2.0
    }
  },
  "output": "results/qianwen-thinking-source-extraction-smoke.json"
}
```

### Task Fields

| Field | Purpose |
|-------|---------|
| `taskName` | Optional label written into the result JSON. |
| `mode` | Optional. `separate` (default) opens a new chat per question when `newChat` is true. |
| `thinking` | Global default for deep-thinking mode. Overridden per session / question. |
| `device.adb` | Path to `adb.exe`. Overridable with `--adb`. |
| `device.serial` | ADB serial. Overridable with `--serial` / `--device`. |
| `sessions[].sessionName` | Label for the session. |
| `sessions[].newChat` | Whether to open a fresh chat before each question in this session. |
| `sessions[].thinking` | Per-session deep-thinking override. |
| `sessions[].questions[]` | Either a string or `{ "text": "...", "newChat": bool, "thinking": bool }`. |
| `options.sourceLimit` | How many sources to collect per question (legacy in-app probing). |
| `options.waitStableSeconds` | How long the answer must stay stable before capture. |
| `options.expertAnswerMaxScrolls` | Max scrolls when capturing thinking-detail content. |
| `options.sourceExtractor.enabled` | Enable the JS source extractor bridge. |
| `options.sourceExtractor.cdpUrl` | CDP endpoint for the JS extractor. |
| `options.sourceExtractor.timeoutSeconds` | Per-attempt timeout. |
| `options.sourceExtractor.maxRetries` | Max retries on failure. |
| `options.sourceExtractor.retryBackoffBase` | Exponential backoff base (seconds). |
| `output` | Result JSON path. |

## Output Contract

The runner writes a result JSON to `task["output"]` (or `--output`). Per-question results include:

| Field | Meaning |
|-------|---------|
| `index` | 1-based question index within the session. |
| `question` | The exact question text. |
| `askedAt` / `finishedAt` | ISO timestamps. |
| `answer` | Captured answer text. |
| `thinkingContent` | Captured deep-thinking text (empty if quick mode). |
| `sources` | Source list. When the JS extractor runs, this is a summary of what it wrote to Feishu. |
| `answerShareUrl` | The Qianwen share URL captured on the phone. |
| `sourceExtraction` | JS extractor status: `status`, `ok`, `sourceCount`, `extractOk`, `feishuWriteOk`, `attempts`, `outputFile`, `error`. |
| `status` | `success` (answer + thinking + sources + share all ok), `partial` (answer exists but something is missing), `failed` (no answer). |
| `error` | Error reason when status is not `success`. |
| `debug` | Timing, snapshots, and notes. Includes `timing` (ms per phase) and `artifactsDir`. |

## Feishu Writeback

When `--writeback` is enabled (Feishu Base mode), the runner writes one row per question to the Feishu answer table with these fields:

| Feishu Field | Source |
|--------------|--------|
| 采集账号 | `--collect-account` or `options.collectAccount` |
| 问题文本 | The question text |
| 问题ID | The `问题ID` field from the Feishu input row; legacy input field `关联自然问句` is still accepted during migration |
| 是否开启深度思考 | Whether deep-thinking was requested |
| AI回答 | Captured answer text |
| 深度思考 | Captured thinking content |
| 是否触发联网 | Whether the answer used web sources |
| 对话链接 | The captured share URL |
| AI平台 | `千问移动端` |

When `--extract-sources` is also enabled, the JS extractor writes one row per source to the Feishu source table with these fields:

| Feishu Field | Source |
|--------------|--------|
| 来源标题 | `source.title` |
| 来源URL | `source.url` (real article URL, not just the domain) |
| 引用来源类型 | `视频` or `图文` (inferred) |
| 引用来源平台 | `source.platform` (cleaned) |
| 问题ID | The question ID passed to the extractor |

Built-in Feishu table ids (in `feishu_base.py`):

```python
FEISHU_ANSWER_TABLE_ID = "tblaV1deA4L9hzze"
FEISHU_SOURCE_TABLE_ID = "tblF1LsniY1BnOt3"
```

## How The Two Modules Cooperate

```text
[Python runner]
  1. ensure_app (open Qianwen, accept privacy)
  2. create_new_chat (if newChat)
  3. enter_thinking_mode (if thinking)
  4. send_question (ADB Keyboard broadcast)
  5. wait_for_answer (poll UI until stable)
  6. click_view_all + capture_thinking_content (scroll thinking-detail page)
  7. extract_answer_share_link (tap share, copy link, read clipboard)
  8. run_source_extractor  <-- bridge to JS module
        |
        v
[JS extractor]
  a. connectOverCDP (Chrome 9222)
  b. find the Qianwen share tab by share_id
  c. fetch share/info API from page context (cookies + origin)
  d. walk data.session.record_list[].response_messages[].meta_data.sources[].content.list[]
  e. map each source to { title, url, platform, summary, publishTime, type, ... }
  f. buildRows (infer 视频/图文, clean platform)
  g. lark-cli base +record-batch-create  --> Feishu source table
        |
        v
[Python runner]
  9. record sourceExtraction status in result JSON
 10. write Feishu answer row (if --writeback)
 11. mark 是否本次采集 = 否 (if --mark-collected)
```

The bridge lives in `mobile_auto_qianwen/source_extractor_bridge.py`. It validates the share URL, locates `qianwen-source-extractor/run.js`, invokes it with `subprocess`, parses the output JSON, and retries with exponential backoff on failure.

## Troubleshooting

### `adb_keyboard_not_installed`
Install `keyboardservice-debug.apk`, enable `ADB Keyboard` in Android Settings, and set `com.android.adbkeyboard/.AdbIME` as the current IME.

### `input_not_found` / `send_failed`
The Qianwen app may be on a nested page. The runner tries to back out automatically. If it still fails, run `--debug` and inspect the XML snapshots under `results/snapshots/`.

### `answer_not_found`
The answer may still be generating. Increase `options.waitStableSeconds` or the `wait_for_answer` timeout (currently 180s).

### `view_all_not_found`
The thinking-detail page may not have a `查看全部` control. The runner falls back to capturing thinking content from the answer page directly.

### `bottom_toolbar_share_failed` / `copy_link_failed`
The share flow may have changed. The runner falls back to a long-press context menu. If both fail, inspect `answerShare` in the result JSON.

### JS extractor: `api-request-failed`
The share/info API returned a non-2xx status. The share page may not be fully loaded. Increase `--timeout` (JS side) or `--extractor-timeout` (Python side). Confirm the share URL is still valid.

### JS extractor: `sources-not-found-in-api-response`
The API response did not contain a sources list. The share may be a quick-mode answer without web sources, or the response format changed. Inspect the `outputFile` JSON to see the raw API structure.

### JS extractor: `Could not locate qianwen-source-extractor/run.js`
The bridge could not find the JS script. Pass `--extractor-script <path>` explicitly, or ensure `qianwen-source-extractor/run.js` is a sibling of the `mobile_auto_qianwen/` package.

### JS extractor: `Node.js binary not found`
Install Node.js and ensure `node` is on PATH, or set `options.sourceExtractor.nodeBinary`.

### JS extractor: `lark-cli not found`
Install `lark-cli` and ensure it is on PATH, or pass `--lark-cli <path>`.

### Feishu writeback: `Feishu Base mode requires --base-url or both --base-token and --table-id`
Provide either `--base-url` (parsed) or both `--base-token` and `--table-id`.

### Feishu writeback: `--base-end must be greater than or equal to --base-start`
Fix the row range. `--base-start` and `--base-end` are 1-based and inclusive.

## Fast Debug Path

1. `adb devices` — confirm the device is online.
2. `adb shell settings get secure default_input_method` — confirm `com.android.adbkeyboard/.AdbIME`.
3. `Invoke-RestMethod http://127.0.0.1:9222/json/version` — confirm Chrome CDP is running.
4. `python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json --dry-run` — validate the task.
5. `python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json --debug` — live run with artifacts.
6. If source extraction fails, run the JS extractor standalone with `--extract-only --output sources.json` and inspect the JSON.
7. If UI selectors fail, inspect `results/snapshots/<session>-<index>-<stamp>/*.xml` and compare resource ids in `mobile_auto_qianwen/constants.py`.
8. For parallel runs, give each process a distinct `--serial` and `--output`.

## Operating Rules

- Preserve question text exactly. Do not paraphrase.
- Prefer one fresh Qianwen chat per question unless a task explicitly requests reuse.
- Do not automate login or captcha. Report those cases as `blocked`.
- Do not fabricate source URLs from source titles. Real source URLs come from the JS extractor (via the share/info API) or, legacy, from in-app share/copy/paste.
- Treat `partial` as useful output: answer text may exist even when some source links or share links failed.
- Keep generated `results/`, `outputs/`, screenshots, XML dumps, and logs outside the skill unless the user explicitly asks to archive evidence.
- The JS extractor writes sources to Feishu directly. The Python runner only records a summary in the result JSON.
- The JS extractor needs Chrome with `--remote-debugging-port=9222` and the Qianwen share page already open. The Python runner does not launch Chrome.
