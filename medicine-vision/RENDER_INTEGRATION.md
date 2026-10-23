# 前端 / ESP32 / OCR 后端集成

## 本次落地范围

- A 按钮：新增 `medicine-vision/` 独立 CPU OCR 服务，按附件协议建任务、上传指定照片、查询结果。保留全部原始代码、模型、许可证和 manifest，47 个原始文件的大小和 SHA-256 校验通过。
- B 功能：保留根目录 `app/` 和现有接口；没有修改 `.env`、云模型密钥、默认模型或根目录 `render.yaml`。A 抢占 B、按钮取消和迟到结果隔离由 APK 实现；本服务不提供 cancel，不执行手机操作。
- 新增 `deployment.py` 部署适配层：强制令牌、显式浏览器跨域、`/ready`、Swagger Bearer 认证说明和启动参数校验。原始 `vision_service/`、推理代码和模型不变，`verify_bundle.py` 仍可直接验证原包。
- 本地代码接入不等于 Render 已上线。本目录没有 Android/ESP32 固件工程，也没有替你配置手机或板端。

## 两个地址，不可混用

| 前端配置 | 指向 | 认证与图片格式 |
| --- | --- | --- |
| B_BACKEND_BASE_URL（建议客户端变量名） | 原有 B HTTPS 根地址 | 沿用现有 `/vision/analyze` 或 `/analyze` multipart 协议 |
| OCR_BASE_URL（建议客户端变量名） | **新建** OCR HTTPS 根地址 | 任务接口 Bearer；照片为原始 JPEG PUT |
| OCR_TOKEN（建议客户端变量名） | 新 OCR 的 `VISION_API_TOKEN` | 手机、板端均需通过私密渠道配置，不是云模型 API Key |

以上客户端变量名仅供前端采用，后端没有自动创建这些配置。地址不带 `/docs`、`/api` 或固定 `:8000`。不要使用 `/camera/latest` 替代任务照片。

特别注意：原 B 后端的 `VISION_BASE_URL` 是**云模型供应商地址**；附件 `verify_render.py` 的同名变量则是**OCR 服务地址**。OCR 独立部署，不读取父目录 `.env`，不要覆盖 B 的配置。

## Render 单独新建服务

先将整个项目同步到你自己的仓库；当前目录没有 Git 元数据，本次未提交、推送或新建远程服务。原根目录 `render.yaml` 仍只管理 B。通过 Render 新建另一个 Web Service，按以下值配置：

| 项目 | 设置 |
| --- | --- |
| Root Directory | `medicine-vision`（相对于实际仓库根；若只上传此子目录则留空） |
| Runtime | Docker |
| Dockerfile Path | `./Dockerfile`（相对于上述服务根） |
| Start Command | 使用 Dockerfile 默认命令 `python deploy_vision.py`，不要覆盖为 `python -m app` |
| Health Check Path | `/ready`；模型加载时返回 503，就绪后才返回 200 |
| 实例 | 按附件要求至少 2 GB 内存起步，再测峰值；不是原 B 的免费 512 MB 配置 |
| 持久磁盘 | 挂载 `/var/data`，单实例、单 worker，不水平扩容 |

Render 使用 `/ready` 做平台就绪检查；APK 继续读取 `/health`，检查 `ready` 和 `capabilities.ocr`。这两个接口用途不同，不冲突，无需更改 APK 的健康检查地址。

环境变量（在 Render 内设置，不提交实际值）：

```dotenv
VISION_API_TOKEN=<随机长令牌>
VISION_DATA_DIR=/var/data/medicine-vision
VISION_INFERENCE_TIMEOUT=120
VISION_STARTUP_TIMEOUT=180
VISION_WAITING_TIMEOUT=600
VISION_CORS_ALLOW_ORIGINS=
PYTHONUNBUFFERED=1
```

`PORT` 由平台注入。原生 APK 和 ESP32 不需要 CORS；浏览器前端才将 `VISION_CORS_ALLOW_ORIGINS` 填为真实 origin，例如 `https://frontend.example.com,http://localhost:5173`，不带路径/末尾斜杠，不允许 `*`。CORS 不是身份认证。静态网页中分发共享令牌不能真正保密；当前仅适合受控联调，正式多用户应另行设计用户认证或代理。

Docker 采用附件要求的 Python 3.11/Linux CPU，安装原包固定版本和系统库，执行 `pip check`、模型 import/文件检查及原包 SHA-256 校验。未静默更换模型或改用 Windows 约束；真实 Linux 构建仍须在 Docker/Render 执行。本机目前是 Python 3.13，无 Docker/Paddle，不能以本地接口测试替代模型验收。

## 给前端 AI 的固定动作协议

第一版单图 `f0`；新任务建议 `v_` + 32 位 UUID，不使用硬件 96 位编号。

1. `GET {OCR_BASE_URL}/health`，不带令牌。必须同时满足 `ready === true`、`capabilities.ocr === true` 才可认为可识别；仅 HTTP 200 不够。
2. `POST {OCR_BASE_URL}/api/v1/vision/tasks`，请求头 `Authorization: Bearer <OCR_TOKEN>`、`Content-Type: application/json`：

   ```json
   {"task_id":"v_本次UUID","question":"生产日期是什么？","expected_frames":1}
   ```

   新建 201，同参数重试 200，编号对应参数冲突 409。先确认创建成功再通知板端拍照。
3. 手机向 ESP32 发拍照指令时，携带原 A task_id、独立 `vision_task_id`、`frame_id=f0`、question、upload_url、content_type。**附件没有规定手机与 ESP32 的 BLE/Wi-Fi 指令帧，此处不是已经实现的固件协议，需与固件同学确认。** HTTP 上传契约如下：

   ```text
   PUT {OCR_BASE_URL}/api/v1/vision/tasks/{vision_task_id}/frames/f0
   Authorization: Bearer <OCR_TOKEN>
   Content-Type: image/jpeg
   body: 本次拍摄 JPEG 的原始 bytes
   ```

   不用 multipart、base64 或 JSON；最多 8 MiB、1600 万像素。手机选图测试入口也使用同一 PUT。令牌不能放在 URL/query 或公开日志中。
4. 手机约每 1 秒 `GET /api/v1/vision/tasks/{vision_task_id}`，带同样的 Bearer，最多等约 240 秒。状态为 `awaiting_question/awaiting_frame/queued/running/succeeded/failed/timed_out`。上传 202 仍可能 `status=failed`，只说明上传被记录，不能显示识别成功。
5. `succeeded` 后读取 `answer`、`evidence_text`、`result.answer_type`、`review_required` 和字段证据。`succeeded` 也可能是 `not_found/unsupported/conflicting_candidates`，不能强行选值。比对 task_id、question、frame_id、SHA-256，顶层和 result 中的 answer/evidence_text 保持一致。不能从有效期、批号猜生产日期，不能生成个人服药方案。
6. `failed/timed_out` 显示 `error_code/message`；HTTP 错误读取 `error.code/error.message`。同 task/frame 相同 bytes 重传幂等，不同 bytes 409；重拍创建新任务。APK 取消后忽略旧任务结果，后端可继续旧任务。

补充问题仍支持 `PATCH /api/v1/vision/tasks/{task_id}`，JSON `{"question":"请读一下文字"}`，带 Bearer；queued/running/终态不可换问题。完整原始协议见 `medicine-vision/API_REFERENCE.md`，运行服务的 `/openapi.json` 还包含部署适配层的 `/ready` 和 Bearer 声明；原包静态 OpenAPI 只用于追溯。

## 本地与部署验收

仅检查原包完整性（标准库即可，修改后的部署适配层未冒充原包）：

```powershell
.\.venv\Scripts\python.exe medicine-vision/verify_bundle.py
```

本地真实 OCR 应另建 Python 3.11 环境，在 `medicine-vision/` 下安装 `requirements-api.txt`，用 shell 设置 `VISION_API_TOKEN` 和本地 `VISION_DATA_DIR`，再运行 `python deploy_vision.py --host 127.0.0.1 --port 8001`。不与 B 的 8000 混用；`.env.example` 只是配置示例，不会自动读取。不要在原 B 环境直接安装整套 Paddle 来代替隔离部署。

部署后在 `medicine-vision/` 下，通过私密环境变量设置 `VISION_BASE_URL=<新OCR的HTTPS根地址>`、`VISION_API_TOKEN`，运行附件原版 `python verify_render.py`。脚本会创建任务、检查 401、创建幂等、重复传图、不同图 409、两张真实模型输出、日期证据和照片哈希；非零退出视为失败。之后测试真实药盒、模糊图、无标签、多候选、连续任务和挂盘重启恢复。再单独验证旧 B 的真实云端响应；原 B 的 mock 配置不会因为接入 OCR 自动变为真实模型。

本次自动化使用可控 OCR 替身检查 HTTP/SQLite/证据结构，不把样本标注当作真实推理结果，也不声称已通过真实药盒或公网验收。最终交付仍需真实 `/health`、两张样本终态 JSON、脚本通过输出、镜像/依赖版本、内存峰值、模型加载和单图耗时。

协议自动化在独立测试环境安装 `medicine-vision/requirements-contract-tests.txt`，从仓库根运行 `python -m pytest medicine-vision/tests -q`，无需安装 Paddle。根目录默认 `pytest` 仍只测试原 B 的 `tests/`；显式 `python -m pytest tests medicine-vision/tests -q` 执行两套回归。具体实测与未验收项见 `medicine-vision/TEST_REPORT.md`。

## 照片保留与清理

OCR SQLite 和照片只写入 `VISION_DATA_DIR`，不受原 B 的 uploads 30 秒清理影响。默认**不自动删除 OCR 照片或任务**，直到运维显式清理；应监控磁盘容量。新增标准库工具 `medicine-vision/cleanup_frames.py`，默认只预览，`--apply` 才永久删除终态超过 24 小时的服务生成 JPEG。它不删除等待、queued、running 的照片、不跟随文件符号链接、不删除数据库、结果证据和 SHA-256；被删图片本身不再可恢复。阈值可配置，没有自动安装定时任务。

```text
python cleanup_frames.py --data-dir /var/data/medicine-vision --older-than-hours 24
python cleanup_frames.py --data-dir /var/data/medicine-vision --older-than-hours 24 --apply
```

持久盘负责重启后继续按原编号查询。清理照片不等于删除识别文本；如要完整隐私删除或正式保留策略，需要额外明确数据库和备份规则。
