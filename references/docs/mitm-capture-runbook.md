# 千问 MITM 抓包 Runbook —— 绕过「带货回答无分享链接」反 GEO

## 为什么需要它

现有流水线命门是分享链接：手机点分享 → `chat2-api.qianwen.com/api/v1/share/info`
→ 一次性拿到 `record_list[].response_messages[].meta_data.sources[]` 与 `plan_cot` 思考块。

千问对「带货回答」（淘宝商品卡）**刻意不渲染分享按钮** → 没有 share_id → share/info 进不去
→ answer / 深度思考 / 来源 全断。这是阿里保护电商变现护城河、不让带货回答被 GEO 抓取再分发。

但 App 渲染这条回答时，本来就从后端拉过一次同构数据。**在网络层把那次响应截下来，
就和有没有分享按钮无关了。** 这是最鲁棒、面向未来的解法（阿里再砍更多 share 也不影响）。

本 runbook 的目标：**先定位 App 渲染回答时调用的那个会话接口**（URL + 请求体字段 + 响应结构）。
定位到之后，就能像 `extract-sources.js` 重放 share/info 那样直连重放它。

---

## 一、前置

- 跑自动化那台机器（带 Android 设备、constants.py 里指向 `Administrator` 的那台）。
- 设备能装 CA 证书（你已确认可行）。
- 设备与电脑在**同一局域网**，或用 USB + adb 反向代理。

## 二、装 mitmproxy

```powershell
pip install mitmproxy
mitmdump --version   # 确认安装
```

## 三、启动抓包插件

```powershell
# 在 references/mitm/ 目录下
mitmdump -s qianwen_capture.py -p 8080
```

命中含 `record_list/sources/plan_cot/...` 的响应会打印一行摘要，并 dump 到
`outputs/mitm/<时间戳>-<host><path>.json`（`outputs/` 已被 .gitignore 忽略）。

排查时若想看全部千问相关流量：

```powershell
mitmdump -s qianwen_capture.py -p 8080 --set qw_capture_all=true
```

## 四、让设备走代理

电脑的局域网 IP 记为 `<HOST_IP>`（`ipconfig` 查 IPv4）。

**方式 A — Wi-Fi 手动代理（简单）**
设备 设置 → WLAN → 当前网络 → 代理 → 手动 → 主机 `<HOST_IP>`、端口 `8080`。

**方式 B — adb 全局代理（免动 UI，推荐自动化）**
```powershell
adb shell settings put global http_proxy <HOST_IP>:8080
# 用完务必还原，否则设备断网：
adb shell settings put global http_proxy :0
```

## 五、安装 mitmproxy CA 证书

设备走代理后，用设备浏览器打开 **http://mitm.it** → 下载 Android 证书（`.cer`）。

- **Android 7+ 关键点**：App 默认只信任**系统级**用户证书，普通「用户证书」只有 WebView/部分场景信任。
  - 千问回答区是 WebView，**多数情况用户证书即可**抓到。先按用户证书装：
    设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书 → 选刚下载的文件。
  - 若抓不到 App 原生接口（只看到 TLS 握手失败 / 无 JSON）：说明该接口走原生网络栈、不认用户 CA。
    需要 **root + 把证书装成系统证书**（`/system/etc/security/cacerts/`，按 mitmproxy 官方
    "Install System CA Certificate on Android Emulator/Device" 步骤），或用已 root 的设备 + Magisk 的
    "Move Certificates" 模块。
  - 若遇到**证书绑定（pinning）**导致始终失败：通常需 frida + objection 反 pinning。千问当前一般无强 pin，
    先不预设这一步，抓不到再上。

验证证书生效：设备浏览器访问任意 https 网站不报错，且 `mitmdump` 终端能看到该域名流量。

## 六、复现并抓取

1. 千问 App 已登录。
2. 发一条**会触发淘宝商品卡的带货问题**（就是你现在拿不到分享链接的那类）。
3. 观察 `mitmdump` 终端：会打印若干 `[qw-capture] ...` 行。
4. 回答渲染完成后停止复现。

## 七、定位目标接口（关键产出）

打开 `outputs/mitm/` 下的 dump，按以下顺序找：

1. **非** `share/info`、`status=200`、`markersFound` 含 `record_list` 或 `meta_data` 的 JSON 响应。
   - 看 `responseBody.data.session.record_list[...]` 是否和 share/info 同构（有 `response_messages[].meta_data.sources` / `plan_cot`）。
   - 这就是要直连重放的**会话接口**。
2. 记录它的：
   - `url`（host + path + query，如 `…/api/v1/conversation/info?…`）；
   - `method` 与 `requestBody` 字段（哪个是 `session_id` / `conversation_id` / `msg_id`）；
   - `requestHeaders` 里登录态靠什么（cookie / authorization，已脱敏但能看到长度，确认存在）。
3. 若回答接口是 **SSE**（`isEventStream=true`）：`responseBody` 是原始事件流文本。
   思考块/来源以增量出现，需要按 `data:` 行拼接解析。优先找**非流式的「会话回放/历史」接口**
   （重开会话时调用，一次返回完整 record_list），它最接近 share/info、最好复用。

## 八、把信息给我，进入集成

把第七步记录的 endpoint 三件套（URL、请求体字段、登录态字段）贴给我。我会：

1. 把 `extract-sources.js` 里 `extractQianwenSourcesViaApi` 的字段走法抽成**共享解析器**
   （输入 `{data:{session:{record_list}}}`，输出 `{sources, answer, thinkingContent}`），
   share/info 与新接口共用一套；
2. 给 runner 增加一条**不依赖分享链接**的来源路径（直连重放会话接口，或读 MITM dump），
   作为带货回答的兜底，与现有 `--link-only` 并存。

## 善后

```powershell
adb shell settings put global http_proxy :0   # 还原代理，恢复设备正常联网
```
抓包 dump 含请求头与会话数据，留在本机 `outputs/` 即可，**勿提交进库**（已 gitignore）。
