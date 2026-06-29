---
name: qianwen-mobile-automation
description: Drives Qianwen Android via ADB to capture answers/share links, extracts real source URLs via a Node.js CDP script, and writes to Feishu. Invoke for Qianwen mobile runs or share-page source extraction. NEW: --link-only mode skips mobile thinking capture and pulls answer + 深度思考 + sources from the share-page share/info API in one shot; --feishu-config externalizes the Feishu answer/source table IDs into a JSON (switch environments without source edits).
---

# Qianwen Mobile Automation

Use this skill to run, debug, or migrate the Qianwen mobile automation integration on another machine.
It covers two cooperating modules: a Python runner that drives the Qianwen Android app, and a Node.js extractor that pulls real source URLs from Qianwen share pages via Chrome DevTools Protocol.

## Two Modules

1. **千问来源提取 (Qianwen Source Extractor)** — Node.js script under `references/qianwen-source-extractor/` (and an integrated copy at `references/mobile-auto-qianwen/qianwen-source-extractor/`). It connects to Chrome via CDP, replays the share/info API from the page context, and extracts the real article URLs (the DOM only shows platform + domain). It can write sources directly to a Feishu Bitable.
2. **跑移动端 (Qianwen Mobile Runner)** — Python package under `references/mobile-auto-qianwen/mobile_auto_qianwen/`. It drives the Qianwen Android app via ADB: opens chats, types questions with ADB Keyboard, waits for answers, captures deep-thinking content, taps the share button to copy the share link, and (optionally) invokes the JS extractor to write sources to Feishu.

The Python runner is the orchestrator. With `--extract-sources`, it hands the captured share URL to the JS extractor; the JS extractor writes sources to Feishu directly.

## Core Workflow

1. Read `references/README.md` before setup, ADB/device changes, Chrome CDP setup, or Feishu Base changes.
2. Restore `references/mobile-auto-qianwen/` into a workspace if the project is not already present. The workspace must contain `mobile_auto_qianwen/` (Python package) and `qianwen-source-extractor/` (JS scripts) as siblings.
3. Confirm prerequisites: Android device online, ADB Keyboard set as IME, Chrome running with `--remote-debugging-port=9222`, `lark-cli` on PATH, Node.js installed.
4. Run `python -m mobile_auto_qianwen.runner --task <task.json> --dry-run` before any live run.
5. Use a unique `--output` for each parallel process.
6. Use live runs only when an Android device is online, the Qianwen app is already logged in, and (for source extraction) Chrome has the target share page open.
7. After each run, report the output path, status counts, answer text, `thinkingContent`, `answerShareUrl`, source titles, source URLs, and any blocked/partial/failed reasons.

## Runner Commands (Python Mobile Automation)

Task JSON mode:

```powershell
python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json --dry-run
python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json
python -m mobile_auto_qianwen.runner --task tasks\qianwen_real_device_thinking_smoke.json --serial 100.76.50.7:6666 --output results\qianwen-real-device-thinking-fast.json --source-limit 2
```

Feishu Base mode (read questions from Feishu, write answers back):

```powershell
python -m mobile_auto_qianwen.runner `
  --base-url "https://yuoukuajing.feishu.cn/base/UiE3bhcHRaCE01sh5Anc1AZanKd?table=tblXZ8vq7SouTIuu&view=vewZsJsX7y" `
  --base-start 1 --base-end 10 --source-limit 99 --writeback `
  --lark-cli "<lark-cli-path>" `
  --adb "<adb-path>" `
  --serial <device-serial>
```

Full pipeline with JS source extraction + Feishu writeback + mark-collected:

```powershell
python -m mobile_auto_qianwen.runner `
  --base-url "<feishu-base-url>" `
  --base-start 1 --base-end 10 --source-limit 99 --writeback --mark-collected --collect-account 18870501682 --extract-sources `
  --cdp-url http://127.0.0.1:9222 `
  --lark-cli "<lark-cli-path>" `
  --adb "<adb-path>" `
  --serial <device-serial>
```

For parallel runs, pass a unique `--serial` and a unique `--output` per process.
The runner refuses to guess when multiple adb devices are online, which prevents cross-device runs.

Externalized Feishu table config + link-only (recommended for switching environments):

```powershell
# Dry-run: JSON supplies input base + answer/source table IDs; CLI still overrides
python -m mobile_auto_qianwen.runner --feishu-config configs\feishu-qianwen-example.json --base-start 1 --base-end 10 --dry-run

# Live: answer/思考/来源 all come from the share-page share/info API (link-only)
python -m mobile_auto_qianwen.runner `
  --feishu-config configs\feishu-qianwen-example.json `
  --base-start 1 --base-end 10 --writeback --mark-collected `
  --extract-sources --link-only --cdp-url http://127.0.0.1:9222 `
  --lark-cli "<lark-cli-path>" --adb "<adb-path>" --serial <device-serial>
```

When switching to a different Feishu Base, usually only the table IDs in the JSON change — no source edits, no column-name changes. In `--link-only` mode the phone only asks the question and grabs the share link; answer/深度思考/来源 are pulled from the share page in one shot.

## CLI Parameters

### Task Configuration

- `--task` — Task JSON file path.
- `--adb` — ADB executable path (required when not in PATH).
- `--serial` / `--device` — Android device serial (required when multiple devices connected).
- `--output` — Override result JSON path.
- `--dry-run` — Preview mode, validate task without execution.

### Feishu Integration

- `--base-url` — Feishu Base URL containing `/base/{baseToken}?table=...&view=...`.
- `--base-token` — Feishu Base token.
- `--table-id` — Feishu table ID.
- `--view-id` — Feishu view ID.
- `--base-start` — 1-based start row in Feishu Base (inclusive). **Recommended instead of --base-limit**.
- `--base-end` — 1-based end row in Feishu Base (inclusive). **Recommended instead of --base-limit**.
- `--base-limit` — Deprecated fallback for old top-N mode.
- `--writeback` — Write results back to Feishu.
- `--mark-collected` — Mark Feishu rows "是否本次采集" as "否" after writeback.
- `--collect-account` — Override "采集账号" field in Feishu.
- `--lark-cli` — lark-cli executable path.
- `--feishu-config` / `--writeback-config` — JSON file that externalizes the Feishu input base + answer/source writeback table IDs. Loaded at startup and applied as defaults; **CLI flags always override JSON**. Template: `references/mobile-auto-qianwen/configs/feishu-qianwen-example.json`. Only table IDs change between environments — field names and column structure stay fixed (see `feishu_base.py` `ANSWER_WRITEBACK_FIELDS` / `SOURCE_WRITEBACK_FIELDS`).
- `--answer-table-id` — Feishu table_id for the answer writeback table (defaults to the built-in Qianwen answer table).

### Source Extraction

- `--extract-sources` — Enable JS source extractor (requires Chrome CDP).
- `--cdp-url` — Chrome DevTools Protocol endpoint (default: `http://127.0.0.1:9222`).
- `--extractor-script` — Path to `qianwen-source-extractor/run.js` (auto-located if omitted).
- `--extractor-timeout` — Per-attempt timeout in seconds (default: 120).
- `--extractor-retries` — Max retries on failure (default: 2).
- `--source-base-token` — Feishu base_token for source table (defaults to input base_token).
- `--source-table-id` — Feishu table_id for source table (defaults to built-in Qianwen source table).
- `--source-limit` — How many sources to collect per question (default: 2).
- `--link-only` — Skip mobile-side thinking capture; take **answer + 深度思考 + sources** from the share-page `share/info` API instead. **Requires `--extract-sources`.** Faster and more complete: the API content is the full conversation, not limited to the mobile viewport. The API answer/thinking override the mobile-captured values.

Current Feishu input rows use `问题文本`, `问题ID`, and `是否开启深度思考`; `是否本次采集` is optional and defaults to `是` when absent. Legacy input fields `问题` and `关联自然问句` are still tolerated during migration.

### Other Options

- `--platform` — Platform name (default: "千问").
- `--force-quick` — Force quick mode (disable thinking).
- `--debug` — Enable debug mode (keep screenshots, XML, etc.).

## JS Source Extractor Commands (Standalone)

Use the JS extractor on its own when you already have a Qianwen share URL.

```powershell
# Extract + write to Feishu
node qianwen-source-extractor\run.js `
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" `
  --question-id NQ-001 `
  --base-token <feishu_app_token> --table-id <feishu_table_id>

# Extract only
node qianwen-source-extractor\run.js `
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" `
  --extract-only --output sources.json

# Write only from existing JSON
node qianwen-source-extractor\run.js `
  --write-only --sources sources.json `
  --question-id NQ-001 `
  --base-token <feishu_app_token> --table-id <feishu_table_id>
```

The JS extractor needs Chrome running with `--remote-debugging-port=9222` and the target Qianwen share page already open in that Chrome.

## Required References

- `references/README.md`: migration, environment setup, ADB checks, ADB Keyboard setup, Chrome CDP setup, task JSON contract, Feishu Base mode, output contract, troubleshooting, and the cooperation diagram between the two modules.
- `references/mobile-auto-qianwen/`: runnable Python project snapshot containing `mobile_auto_qianwen/` (the package) and `configs/feishu-qianwen-example.json` (externalized Feishu table-ID template).
- `references/qianwen-source-extractor/`: standalone JS extractor reference (top-level) for agents that only need web-side source extraction.
- `references/tasks/`: task JSON examples for the Python runner.
- `references/tools/keyboardservice-debug.apk`: ADB Keyboard APK for reliable Chinese text input.
- `references/docs/`: design notes.

## Key Source Files

### Python package (`references/mobile-auto-qianwen/mobile_auto_qianwen/`)

- `runner.py` — CLI entry point. Parse args, build task, run sessions, write results, invoke JS extractor bridge.
- `app.py` — UI automation: ensure_app, create_new_chat, enter_thinking_mode, send_question, wait_for_answer, click_view_all, capture_thinking_content, extract_answer_share_link.
- `source_extractor_bridge.py` — Bridge to the JS extractor. Validates share URL, locates run.js, invokes with subprocess, retries with exponential backoff.
- `feishu_base.py` — Feishu Base read/write via lark-cli. Builds tasks from Feishu rows, writes answer rows back. Writeback table IDs are read from `writeback_context` (supplied by `--feishu-config`/CLI) and fall back to the `FEISHU_ANSWER_TABLE_ID` / `FEISHU_SOURCE_TABLE_ID` constants; column structure is fixed.
- `adb_client.py` — ADB wrapper (tap, keyevent, text broadcast, dump_xml, screenshot, ime).
- `constants.py` — Qianwen package name, ADB Keyboard IME, UI text constants.
- `ocr.py` — Windows Media.Ocr wrapper for screenshot-based OCR fallback.
- `task_schema.py` — Task JSON loading and normalization.
- `ui_xml.py` — uiautomator XML parsing.
- `thinking_capture.py` — Thinking-detail page capture helpers.
- `source_links.py` — Legacy in-app source link probing (largely superseded by the JS extractor).
- `result_writer.py` — Result JSON writer.
- `artifacts.py` — State snapshots (XML + nodes).
- `time_utils.py` — ISO timestamps and stamps.
- `命令大全.txt` — Ready-to-paste command examples.

### JS extractor (`references/qianwen-source-extractor/` and `references/mobile-auto-qianwen/qianwen-source-extractor/`)

- `run.js` — Main entry. Parses args, calls extract-sources.js, then write-feishu.js. Supports `--extract-only`, `--write-only`, `--dry-run`.
- `extract-sources.js` — Connects to Chrome via CDP (`connectOverCDP`), finds the Qianwen share tab by share_id, replays the share/info API from the page context, walks `data.session.record_list[].response_messages[].meta_data.sources[].content.list[]` (and the `multi_load[].content.docs[]` variant), and returns clean source objects with real URLs. It also returns `answer`, `thinkingContent`, and `searchEnabled` extracted from the same response (`response_messages[]` split by `mime_type`: `plan_cot/post` → 深度思考, `multi_load/iframe` etc. → answer with inline markers like `[(deep_think)]` / `[source_group_web_N]` stripped). These feed `--link-only`.
- `write-feishu.js` — Builds Feishu rows (来源标题, 来源URL, 引用来源类型, 引用来源平台, 问题ID) and creates records via `lark-cli base +record-batch-create`.
- `package.json` — Declares `playwright-core` dependency.

## Operating Rules

- Preserve question text exactly. Do not paraphrase.
- Prefer one fresh Qianwen chat per question unless a task explicitly requests reuse.
- Do not automate login or captcha. Report those cases as `blocked`.
- Do not fabricate source URLs from source titles. Real source URLs come from the JS extractor (via the share/info API) or, legacy, from in-app share/copy/paste.
- Treat `partial` as useful output: answer text may exist even when some source links or share links failed.
- Keep generated `results/`, `outputs/`, screenshots, XML dumps, and logs outside the skill unless the user explicitly asks to archive evidence.
- The JS extractor writes sources to Feishu directly. The Python runner only records a summary in the result JSON.
- The JS extractor needs Chrome with `--remote-debugging-port=9222` and the Qianwen share page already open. The Python runner does not launch Chrome.

## ADB Keyboard Notes

Use `com.android.adbkeyboard/.AdbIME` for Chinese input.

1. Install `keyboardservice-debug.apk` on the target device (shipped at `references/tools/keyboardservice-debug.apk`).
2. Open Android input-method settings and enable `ADB Keyboard`.
3. Switch the current input method to `com.android.adbkeyboard/.AdbIME`.
4. Confirm with `adb shell ime list -s` and `adb shell settings get secure default_input_method`.

If live runs fail with `adb_keyboard_not_installed`, install the APK and set the IME again.

On some emulator ROMs, `ime enable` and `ime set` are blocked by policy. Use the Settings UI plus `adb shell dumpsys input_method` to confirm the service is active.

## Chrome CDP Notes (For JS Source Extractor)

The JS extractor replays the share/info API from the page context so it carries the correct cookies and origin. This requires:

1. Close all Chrome windows.
2. Launch Chrome with `--remote-debugging-port=9222`.
3. Open the Qianwen share page in that Chrome.
4. Confirm `Invoke-RestMethod http://127.0.0.1:9222/json/version` returns a version payload.

The default CDP URL is `http://127.0.0.1:9222`. Override with `--cdp-url` (Python) or `--cdp` (JS).

## Fast Debug Path

1. `adb devices` — confirm the device is online.
2. `adb shell settings get secure default_input_method` — confirm `com.android.adbkeyboard/.AdbIME`.
3. `Invoke-RestMethod http://127.0.0.1:9222/json/version` — confirm Chrome CDP is running.
4. `python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json --dry-run` — validate the task.
5. `python -m mobile_auto_qianwen.runner --task tasks\qianwen_sample.json --debug` — live run with artifacts.
6. If source extraction fails, run the JS extractor standalone with `--extract-only --output sources.json` and inspect the JSON.
7. If UI selectors fail, inspect `results/snapshots/<session>-<index>-<stamp>/*.xml` and compare resource ids in `mobile_auto_qianwen/constants.py`.
8. For parallel runs, give each process a distinct `--serial` and `--output`.
