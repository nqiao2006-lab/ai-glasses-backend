# Render 免费 Web Service 部署

## 已适配的运行方式

本项目的入口是 `app.main:app`，不是根目录 `main:app`。从仓库根目录运行：

```text
python -m app
```

启动器读取环境变量 `PORT`，未设置时使用 8000；监听 `0.0.0.0`，仅一个 worker。无需 shell 展开 `${PORT:-8000}`，Windows 和 Render 使用同一命令。不要添加 `--reload` 或多 worker，否则内存预览会在不同进程之间不一致。

## 上传 GitHub 前

当前交付目录没有 `.git`，尚不知道对应 GitHub 仓库或分支。将这些改动同步到已有仓库的正确分支，避免覆盖远程已有文件；不要创建一个与现有仓库无关的新历史后强推。

提交前检查 `git status --short`、`git diff --cached`，确认没有 `.env`、真实密钥、照片、音频、虚拟环境或 artifacts。`.gitignore` 无法保护已经被 Git 跟踪的文件；若密钥曾提交，需撤销密钥并清理仓库历史，不能只删除工作区文件。

`requirements.txt` 已包含 FastAPI、Uvicorn、multipart、dotenv、httpx、Pydantic、无界面的 OpenCV、NumPy 及现有 pytest；本次未升级依赖。现有版本范围没有完全锁定，所以云端首次安装版本可能与本机不同。Python 固定为 3.11.11（现有视觉代码使用 `asyncio.timeout`，至少需要 Python 3.11）。

## Render 设置

登录 Render 后可使用仓库根目录 `render.yaml` 创建 Blueprint，或手动 New → Web Service 连接已有 GitHub 仓库：

| 设置 | 值 |
| --- | --- |
| Runtime | Python |
| Branch | 实际包含本次修改的分支 |
| Root Directory | 留空；如果 GitHub 是多项目仓库，填写包含 requirements.txt 和 app 的相对目录 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python -m app` |
| Instance Type | Free |
| Health Check Path | `/health` |
| PYTHON_VERSION | `3.11.11` |

Render 自动提供 `PORT`；不要把本地 `.env` 上传或手动设置固定公网 8000 端口。平台为服务提供 HTTPS 域名，客户端地址不带 `:8000`。

默认 `AI_MODE=mock`、`VISION_MODE=mock`，先验证服务启动。真实分析需要在 Render Environment 单独配置：旧 `/analyze` 使用 `AI_MODE=openai_compatible`、`AI_API_KEY`、`AI_BASE_URL`、`AI_MODEL`、`AI_OUTPUT_FORMAT` 和 `AI_TIMEOUT_SECONDS`；独立视觉使用 `VISION_MODE=ark`、`VISION_PROVIDER=ark`、`VISION_BASE_URL`、`VISION_MODEL`、`ARK_API_KEY`、`VISION_OUTPUT_FORMAT=json_object` 和 `VISION_TIMEOUT_SECONDS=25`。模型 ID 须使用自己账号支持视觉的实际值。两条链路分别配置，不能认为配置视觉后旧接口也自动启用真实模型。

密钥只在 Render 后端环境设置，不能发给前端、硬件或写入 GitHub；本地已有 `.env` 保留。Blueprint 中的 mock 值仅用于首次启动，启用真实模型后如再次同步 Blueprint，要核对这些值是否被恢复。

## CORS 与公网原型的边界

首页由后端托管，使用相对路径，不需要设置 CORS。若有独立前端，将 `CORS_ALLOW_ORIGINS` 设为真实来源，例如 `https://frontend.example.com`，多来源逗号分隔，包含协议和实际端口，不含路径或末尾 `/`。不使用 `*`。默认不允许跨域浏览器读取；允许 GET/POST/PUT，支持 Content-Type、If-None-Match，并暴露 AI 模式和预览所需响应头；不启用跨域 Cookie。

CORS 不是身份认证，不限制硬件或其他非浏览器客户端。当前接口没有鉴权、限流或设备隔离；公网访问者可以调用模型、读取最新预览、修改共享增强设置。这是沿用现有协议的演示服务，只使用非敏感测试图片；如果需要保护访问，应另行约定前端/硬件都能使用的认证协议。本次未擅自加必填 token 改变协议。

## 前端与硬件地址

把下面的 `<实际服务名>` 替换为 Render 控制台显示的域名，不要直接复制占位符：

| 调用 | 云端地址与请求 |
| --- | --- |
| 健康检查 | `GET https://<实际服务名>.onrender.com/health` |
| 首页 / Swagger | `https://<实际服务名>.onrender.com/`、`/docs` |
| 联合意图分析 | `POST /analyze`，multipart 字段 `image` + 文本 `intent` |
| 仅视觉分析 | `POST /vision/analyze`，multipart 字段 `image` |
| 原有临时上传 | `POST /upload`，multipart 字段 `file`，不会调用模型 |
| 实时预览上传 | `POST /camera/frame`，multipart 字段 `file`，不保存、不调用模型 |

独立前端将 API base URL 配置为实际 HTTPS 域名；本目录没有独立 Android/ESP32 工程，无法代改固件。硬件需支持 HTTPS/TLS、正确证书校验与公网 DNS；不能只把旧 HTTP 字符串换域名后就假定兼容，也不要通过关闭证书验证解决连接问题。multipart boundary 由客户端生成；不要手动填写缺少 boundary 的 Content-Type。

`app/index.html` 使用同源路径，无需改地址。README、REALTIME_ENHANCEMENT.md 中的 192.168.20.42、127.0.0.1 和 Windows 示例属于本地开发。`scripts/check_receiver_network.ps1`、`scripts/collect_integration_materials.py` 是本地防火墙/材料采集工具，不用于 Render；其余增强测试脚本已有 `--url` 可指定公网域名。artifacts 中的地址属于历史联调记录，已忽略，不随部署提交。

云端请求示例（PowerShell）：

```powershell
$backendUrl = 'https://<实际服务名>.onrender.com'
curl.exe --max-time 120 "$backendUrl/health"
curl.exe --max-time 120 -i -F "image=@C:\path\photo.jpg;type=image/jpeg" --form-string "intent=帮我打开微信" "$backendUrl/analyze"
curl.exe --max-time 120 -i -F "image=@C:\path\photo.jpg;type=image/jpeg" "$backendUrl/vision/analyze"
```

## 已有能力与限制

`/analyze` 成功返回固定五字段 `action/app_name/target/confirmation_required/message`。mock 仅按意图文本匹配，不识别图片、不真的操作手机。真实模式发送原图 Base64 和 intent 到模型；默认模型为可配置的 `gpt-4.1-mini`。`/vision/analyze` 返回 `status/mode/analysis/error`，默认 mock 的 analysis 为 null。两条链路均没有音频采集、音频上传、ASR 或 TTS；语音转写须由调用方另行实现。

已限制 JPEG/PNG、非空、10 MB，并检查文件签名与 MIME 是否相符；签名校验不等于完整解码验证。multipart 在进入路由前解析，当前 10 MB 检查不是整个 HTTP 请求体的传输层上限，也没有应用级上传总时限。无需为部署改字段或返回协议。AI 默认超时 25 秒，可配置到最多 120 秒；旧 `/analyze` 使用 httpx 超时（各网络阶段的等待），视觉线额外使用整体调用时限。旧接口模型调用失败通常返回 502；视觉线区分 422/502/503/504。客户端应另外设置包括免费实例唤醒在内的请求超时，避免无条件自动重试产生重复计费。

`/upload` 默认仅临时保存约 30–35 秒；Render 重启、重新部署或实例替换还可能更早丢失这些文件。其他预览/增强状态在内存，重启即清空，所有设备共用最近一帧。本项目无数据库、无永久存储依赖，本次不引入付费存储。

免费服务可能空闲休眠、首请求冷启动，套餐配额及限制以 Render 当前控制台为准。首页持续轮询和硬件连续上传也会消耗流量；免费实例的图像增强 FPS/内存无法由本机性能推定。演示前手动打开健康检查和首页预热，用真实硬件验证 TLS、延迟和图片尺寸。

## 上线验收

必须看到部署成功日志及公网 `/health` 返回 `{"status":"ok"}`，再验证 `/docs`、首页、mock `/analyze` 和 `/vision/analyze`。独立前端确认合法 origin 可读、非白名单 origin 无允许头；硬件确认能收到上传响应。切换真实模型后用非敏感图片检查 X-AI-Mode 和真实响应，并检查超时/失败提示；不要把 mock 成功当成真实 AI 已联通。

当前本地检查不代表 Render Linux 构建、真实 AI 调用或真实硬件公网联调已完成。

本次本地验证：`pytest -q --tb=short -p no:cacheprovider --basetemp artifacts/render-tests-20261023-a` 为 72 passed；保留一条现有 Starlette/httpx TestClient 弃用警告，未为此升级依赖。另外实际启动 `python -m app`，验证自定义 PORT 下 `/health` 正常；测试结束仅关闭该测试子进程，没有停止原有本地服务。首次测试遇到 Windows 默认临时目录访问权限问题，改用上述项目内专用目录后通过。
