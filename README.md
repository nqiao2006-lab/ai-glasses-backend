# AI 老人辅助眼镜后端

## Render 云端部署

见 [云端部署与前端/硬件接入说明](DEPLOY_RENDER.md)。统一启动命令为 `python -m app`，监听 `0.0.0.0`，端口读取 `PORT`（默认 8000），保持单 worker。下面的局域网 IP、Windows 路径和防火墙说明仅用于原有本地联调；云端调用应使用 Render 分配的 HTTPS 域名，无需开放电脑端口。

## 实时图像增强

中文首页默认显示增强图，可按需开启同帧原图对比，支持 A/B/C/D 模式切换，以及 JPEG 接收、处理、浏览器绘制 FPS 和处理耗时。默认 C：EMA 亮度稳定 + 适度 CLAHE，锐化可选。本机 1600×1200 合成回放四模式均处理约 9.5 FPS、无丢帧；尚未验证本轮真实 ESP32/浏览器 FPS。详见 [实时增强使用与验证报告](REALTIME_ENHANCEMENT.md)。原 `/upload`、`/analyze`、`/vision/analyze` 上传协议和模型配置保留；只增强实时预览，不自动调用模型，也不改变模型接收的原图。无需训练或下载数据集，默认验证只使用内存合成图。

## 上传图片自动过期（当前行为）

`/upload` 协议和成功响应不变，但磁盘图片现为临时保存：默认保留 30 秒，每 5 秒检查，通常在保存后 30–35 秒删除。可在 `.env` 配置 `UPLOAD_RETENTION_SECONDS=30`、`UPLOAD_CLEANUP_INTERVAL_SECONDS=5`，修改后重启；数值必须为有限正数。

服务启动后也会检查之前保存的图片，按文件修改时间计算期限。只删除 uploads 顶层由本服务命名的 32 位小写十六进制 UUID `.jpg` / `.png` 文件，不递归、不跟随文件符号链接，不删除 `.gitkeep` 或其他命名的文件。删除失败会记录简短日志并下轮重试。服务停止时不会执行清理，恢复运行后继续清理。自动删除不经过回收站，无法通过本服务恢复。当前 uploads 检查时仅有 `.gitkeep`，未发现旧图片。

磁盘文件过期不会清除内存中的最新预览帧，实时窗口仍正常显示，其他接口不新增磁盘保存。

## 硬件实时画面窗口

中文首页 `/` 顶部显示最近处理完成的增强图，勾选“原图对比”才并排显示同帧原图。一次绘制完成后约 15 ms 再检查更新，无重叠轮询、无显示帧队列；不保证视频帧率，也不会主动控制硬件拍摄。显示帧号、时间、来源接口；5 秒无更新提示显示最后一帧。浏览器默认通过 `/camera/preview/enhanced` 读取增强图，对比时额外请求带匹配帧号的 `/camera/preview/original`。原 `GET /camera/latest` 继续返回未增强的最新原图，未收到图片返回 204，ETag 相同返回 304。

原 `/upload`、旧 `/analyze` 和 `/vision/analyze` 校验通过的图片均会更新预览，不需要另传一份；模型失败也不影响预览。旧 `/analyze` 仍要求 intent，缺字段返回 422 不会更新。现有字段和保存约定不变。

连续传图推荐 `POST /camera/frame`，multipart 图片字段 `file`，无需 intent、不调用 AI、不保存磁盘：

```powershell
curl.exe -F "file=@C:\path\photo.jpg;type=image/jpeg" http://192.168.20.42:8000/camera/frame
```

内存仅保留最新接收帧及最近处理完成的一对原图/增强图，没有无界帧队列，重启即清空；所有设备和测试上传共用同一画面。此原型只使用一个 Uvicorn worker，不支持多进程共享帧或按设备隔离。在可信局域网使用，同网可访问者能看到预览。`/upload` 额外临时保存图片，过期自动删除。

现有 FastAPI 项目入口为 `app.main:app`，端口 8000。当前采用独立视觉感知线：`/vision/analyze` 只接收图片；语音采集与转写由未来 Android 前端负责。本项目不执行手机操作。旧 `/upload` 和 `/analyze` 兼容保留，后者是旧版联合意图演示，不是独立视觉线的必经步骤。

## 独立视觉分析（当前推荐）

新增 `app/vision_service.py`：视觉模型、Chat Completions 适配与错误处理；新增 `app/agent_coordinator.py`：仅保留上下文边界与建议，不生成或执行操作。模型观察（scene / visible_elements / current_state）、模型推测（possible_next_steps）、帮助建议（suggested_assistance）和用户明确需求（explicit_user_intent）分开保存于内存数据结构。模型观察也不等于已经核实的事实；没有用户授权不能推导执行权限。

### Swagger / 中文页面

启动后打开 `http://127.0.0.1:8000/` 的“视觉分析”，或 `http://127.0.0.1:8000/docs` 展开 **POST /vision/analyze → Try it out**，只选择 `image`，点击 Execute；不需要 intent，也不需要先请求 `/upload`。

```powershell
curl.exe -i -F "image=@C:\path\photo.jpg;type=image/jpeg" http://127.0.0.1:8000/vision/analyze
```

默认模拟响应（不识别画面，不虚构场景）：

```json
{"status":"mock","mode":"mock","analysis":null,"error":{"code":"mock_no_observation","message":"模拟模式：未调用真实模型，未识别图片，不提供虚构的画面内容。"}}
```

### 方舟 / 豆包真实调用配置

在已有本地 `.env` 增加或修改以下配置，不要覆盖原有密钥。`.env` 已被 `.gitignore` 忽略。

```dotenv
VISION_MODE=ark
VISION_PROVIDER=ark
VISION_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
VISION_MODEL=填写方舟控制台支持视觉的模型ID或推理接入点ID
ARK_API_KEY=填写自己的方舟密钥
VISION_OUTPUT_FORMAT=json_object
VISION_TIMEOUT_SECONDS=25
```

重启后用上述 curl 或 Swagger 上传真实图片。适配器实际向 Base URL 后的 `/chat/completions` 发 HTTP POST，Bearer 鉴权；原始 JPEG/PNG 作为 Base64 `image_url` 视觉输入传入。未引入新的厂商 SDK。必须在方舟账户中确认所选模型支持视觉输入、Data URL 和所选输出模式；不猜测账号内可用的模型 ID。

输出模式支持 `json_schema`（模型支持时优先）、`json_object`（默认）、`text`（不发送 response_format，但仍严格解析 JSON）。不同豆包型号能力不同；不支持指定模式时返回明确 HTTP 错误，不会悄悄转模拟或接受任意文本。`confidence` 是模型自报的估计值，不是经校准的概率。

新接口配置优先级：`VISION_MODE` → `AI_MODE` → mock；供应商 `VISION_PROVIDER` → `AI_PROVIDER`；地址 `VISION_BASE_URL` → `ARK_BASE_URL` → `AI_BASE_URL` → 方舟默认地址；模型 `VISION_MODEL` → `ARK_MODEL` → `AI_MODEL`；密钥 `VISION_API_KEY` → `ARK_API_KEY` → `AI_API_KEY`。原 AI_* 配置不改动；如果原 AI_BASE_URL 指向其他厂商，务必使用 VISION_BASE_URL 显式指定方舟地址。环境变量优先于 `.env`。

以下只是模型成功识别特定手机画面时的**结构示例，不是实测识别结果**：

```json
{
  "status": "ok",
  "mode": "cloud",
  "analysis": {
    "scene": "手机应用列表",
    "visible_elements": ["微信图标"],
    "current_state": "手机屏幕显示应用列表。",
    "possible_next_steps": ["用户可能想选择某个应用。"],
    "suggested_assistance": ["可以询问用户是否需要查找应用。"],
    "confidence": 0.8
  },
  "error": null
}
```

响应体及 `X-AI-Mode` 区分 mock/cloud。模型无法识别返回 HTTP 422、status=unrecognized；超时 504；密钥或配置缺失 503；鉴权/网络/上游 HTTP/无效 JSON 或字段校验错误 502。以上视觉失败都返回 analysis=null 和 error.code/message，不降级成虚构内容。上传本身校验失败仍沿用原接口的 HTTP 400/413/415 与 detail；缺 image 返回 FastAPI 422 校验错误。

### 隐私和硬件接入

`/vision/analyze` 默认不写入 uploads、不使用数据库。multipart 解析大文件时可能使用系统临时文件；读完或读取/校验失败时通过 finally 关闭，模型调用前已释放临时文件，因此模型失败也不会遗留上传临时文件。请求图片仅在内存中存活到请求结束（不是安全擦除保证）。真实模式将原图发往所配置的云服务，其数据留存规则需另行核实。日志不记录原图、模型正文、语音、密钥或用户意图。

**`/upload` 将图片临时保存到 uploads，默认 30 秒后自动清理。** 之前保存的服务生成图片也受相同期限管理；旧硬件协议保持 `POST /upload`、multipart 字段 `file`。如硬件需要直接获得视觉分析，应由队友明确切换到 `POST /vision/analyze`、图片字段 `image`，不带 intent；不能期望旧 `/upload` 自动触发 AI。地址为 `http://电脑局域网IPv4:8000/vision/analyze`，可信局域网启动用 `--host 0.0.0.0`，并允许局域网 TCP 8000；不要向公网开放。

当前没有真实密钥，真实模型网络、账户权限、输出模式兼容性和视觉质量须配置后验证。自动化测试使用模型 HTTP 替身，不能当作真实方舟测试。

本轮实际验证：`pytest -q --tb=short` 为 41 passed（另有现有 TestClient 依赖弃用警告）；旧上传保存回归、视觉无 intent、原始图片 Data URL、Pydantic 拒绝无效结果、不可识别/鉴权/HTTP/网络/超时、临时文件关闭均通过。实际启动 8000 后，用 uploads 中已有 JPEG 请求 `/vision/analyze` 得 HTTP 200、X-AI-Mode=mock、analysis=null；`/health`、`/docs` 和中文主页也正常。未调用真实方舟 API，未连接 ESP32。

## 项目结构

```text
app/main.py          FastAPI 路由、图片校验和保存
app/ai_service.py    Pydantic 返回模型、mock、OpenAI 兼容适配器
tests/test_api.py    上传回归、模型载荷及错误处理测试
uploads/             原有 /upload 保存位置
.env                 本地配置，禁止提交
.env.example         无密钥配置模板
requirements.txt     运行和测试依赖
```

## 安装与启动

在项目根目录运行（当前项目已具备 `.venv`）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

修改 `.env` 后需重启服务。环境变量优先于 `.env`；程序按项目根目录定位 `.env`。默认 `AI_MODE=mock`，不发出云端请求、不需要密钥。模拟规则只演示明确的打开应用意图，不分析图片内容。

## 接口约定

| 接口 | 表单字段 | 行为 |
| --- | --- | --- |
| `GET /health` | 无 | 返回 `{"status":"ok"}` |
| `POST /upload` | `file` 图片 | 保留原协议，保存到 `uploads/`，返回 success、filename、size |
| `POST /vision/analyze` | `image` 图片，无 intent | 独立视觉分析，返回状态及六字段 analysis，不永久保存 |
| `POST /analyze` | `image` 图片 + `intent` 文本 | 原图和意图一起分析，直接返回固定五字段 JSON，不保存图片 |

图片采用 `multipart/form-data`，图片部分 MIME 为 `image/jpeg` 或 `image/png`；校验文件头与 MIME 是否匹配，空文件返回 400、类型错误 415、超出 10 MiB（10,485,760 字节）返回 413。文件头检查不等同完整图片解码，损坏图片仍可能被云模型拒绝。`intent` 必填、不可为空白，最多 1000 字符。

一次 `/analyze` 已包含原图，不需要先上传 `/upload` 再上传同一图片。旧硬件保存接口完全保留，原来只发送 `file` 到 `/upload` 的固件无需修改。

### Swagger 和本地图片测试

打开 `http://127.0.0.1:8000/docs`，展开 `POST /analyze`，点 **Try it out**，`image` 选择一张 JPEG/PNG，`intent` 填写“帮我打开微信”，点击 Execute。响应体为：

```json
{"action":"open_app","app_name":"微信","target":null,"confirmation_required":false,"message":"我来帮您打开微信。"}
```

模拟模式通过响应头 `X-AI-Mode: mock` 明确标记，保持响应体只有约定的五个字段。真实适配器模式为 `X-AI-Mode: openai_compatible`。这不表示已执行打开应用。

PowerShell（替换为实际图片路径）：

```powershell
curl.exe -i -F "image=@C:\path\photo.jpg;type=image/jpeg" --form-string "intent=帮我打开微信" http://127.0.0.1:8000/analyze
curl.exe -i -F "image=@C:\path\photo.jpg;type=image/jpeg" --form-string "intent=帮我转账五千元" http://127.0.0.1:8000/analyze
curl.exe -F "file=@C:\path\photo.jpg;type=image/jpeg" http://127.0.0.1:8000/upload
```

不支持或无法确定的意图返回 `unknown`，示例：

```json
{"action":"unknown","app_name":null,"target":null,"confirmation_required":false,"message":"抱歉，我暂时无法确定您想执行的操作。"}
```

## 真实云端模型配置

编辑本地 `.env`，不要在聊天中发送密钥，不要提交真实密钥：

```dotenv
AI_MODE=openai_compatible
AI_PROVIDER=openai_compatible
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4.1-mini
AI_API_KEY=填写自己的密钥
AI_TIMEOUT_SECONDS=25
AI_OUTPUT_FORMAT=json_schema
```

供应商由 Base URL、模型和密钥选择，`AI_PROVIDER` 选择协议适配器。`AI_BASE_URL` 一般填写供应商的 `/v1` 根地址（程序会追加 `/chat/completions`），也兼容完整 `/chat/completions` 地址。选择支持 `image_url` 视觉输入的模型；原始 JPEG/PNG 通过 Base64 Data URL 发送，而不是只发送文件名。图像会发送至配置的云端服务。

使用 httpx 直接请求 OpenAI 风格 Chat Completions，不依赖单一厂商 SDK。默认 `AI_OUTPUT_FORMAT=json_schema` 请求严格 JSON Schema；若模型不支持，可改为 `json_object`；若 JSON 模式也不支持，可改为 `text`。所有模式均严格 JSON 解析及 Pydantic 校验，不接受 Markdown 代码块或随意文本。API 失败不会自动重试或偷偷切换 mock。

超时参数范围 `(0, 120]` 秒；鉴权、网络、配置和 HTTP 错误返回 HTTP 502，响应 `detail` 给出提示；模型响应不是合法 JSON、缺字段、错误类型、不允许的 action 或额外字段时，返回 HTTP 200 的固定 `unknown`。校验通过的 JSON 仅交给调用方，不会执行其中的操作。

当前无真实密钥时仅验证 mock 和本地替身响应；配置有效密钥后，重启再测试，检查 `X-AI-Mode: openai_compatible`、云端调用记录及结果，才能确认真实网络、账户权限、模型兼容性和视觉理解质量。固定 JSON 合法不代表模型理解必然正确。

官方参考：[OpenAI 视觉输入](https://developers.openai.com/api/docs/guides/images-vision)、[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)。

## 给硬件同学

原约定保持不变：ESP32 向 `http://电脑局域网IPv4:8000/upload` 发 HTTP POST，`multipart/form-data`，图片字段 `file`，图片部分 MIME 为 `image/jpeg`。成功返回 `{"success":true,"filename":"随机名.jpg","size":20905}`。

如需一次请求直接获取 Agent JSON，可选择新接口 `/analyze`，图片字段 `image`，同时增加文本字段 `intent`；没有语音输入时由调用方传固定文本“帮我打开微信”。这不改变原 `/upload` 协议，不要把新字段发到旧接口。

当前电脑 Wi-Fi IPv4 为 `192.168.20.42`；首页为 `http://192.168.20.42:8000/`，ESP32 原协议上传地址为 `http://192.168.20.42:8000/upload`。硬件联调使用 `--host 0.0.0.0`，同网设备可先访问 `http://192.168.20.42:8000/health`。地址再次变化时用 `ipconfig` 确认，并同步硬件调用方的后端地址。电脑防火墙可能需要允许可信局域网的 TCP 8000，访客网络隔离可能阻止互访。不要将服务暴露到公网。

板端连接超时时，在接收电脑的**管理员 PowerShell** 中进入项目目录，运行：

```powershell
# 默认只读，显示实际接口/网络类别、监听进程、本机 HTTP 探测与有效防火墙规则
.\scripts\check_receiver_network.ps1
# 显式修复：仅创建/校正 ESP32-JPEG-8000，然后核验有效规则
.\scripts\check_receiver_network.ps1 -ApplyFirewallFix
```

脚本仅放行 `192.168.20.9 → 192.168.20.42:8000/TCP`，从当前地址确定接口和防火墙配置文件，不关闭防火墙、不修改网络类别、不重启服务，也不生成日志文件。其他电脑不包含在这条放行规则中。地址变化后须先更新脚本中的两个地址。命令权限/策略错误会明确报错；显式阻止规则或组织策略可能仍阻止连接。本机访问自身 IP 成功、规则写入成功，都不能代替板端实际上传成功的验证。

ESP32 上传地址指向运行 FastAPI 的设备；当前为电脑。将来迁移后只需在硬件调用方更改后端 IP/端口；手机作为调用方也应使用电脑局域网 IP，不能用手机自身的 `127.0.0.1`。`AI_BASE_URL` 是云端模型地址，与电脑或手机访问后端的地址不同。本阶段不开发 Android。

## 自动化测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

模型 HTTP 响应由本地替身模拟，不调用收费接口，不代表真实云端调用成功。运行时与测试日志不会记录 API Key 或图片内容。
