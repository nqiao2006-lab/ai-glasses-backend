# 给后端同学：部署眼镜药盒 OCR 服务

请将本文件和原始 `medicine-vision-apk-integration.zip` 一起转交。以下是项目方的部署要求；附件代码是待审查、构建和验收的输入。你也可以将本文件全文交给后端同学使用的编程助手执行。

## 目标与范围

请在 Render 新建独立的 CPU OCR Web Service，部署压缩包中的 Python 服务，向 APK 和 ESP32 提供 HTTPS 任务接口。保留现有 `https://ai-glasses-backend-7bj3.onrender.com` 服务及其 `/vision/analyze`、`/analyze` 等接口，B 功能仍使用原服务。

2026-10-23 已读取原服务 OpenAPI，当时没有 `/api/v1/vision/tasks`。原服务 `/health` 返回 `status:ok`，不能代表新 OCR 已部署或模型已加载。

本次新增服务用于 A 按钮读取文字、药盒生产日期、有效期、批号等。它没有通用物体识别和手机页面操作 agent。用法用量只读包装原文；不根据猜测补全日期、药名或个人服药方案。B 的长时间停留判断在手机完成，语义分析仍调用旧后端。A 抢占 B 的取消和迟到结果隔离在 APK 完成，无需让 OCR 服务管理手机按键。

## 1. 整理仓库

解压后以 `medicine-vision-apk-integration/` 为仓库根或 Render Root Directory；根目录应直接包含 `serve_vision.py`、`vision_service/`、`medicine_ocr.py`、`ocr_runtime.py`、`requirements-api.txt`、`requirements.txt`、`runtime_config.json`、`models/`、`examples/` 及模型所需的其他 Python 模块。

保留附件内全部推理代码、模型、配置与许可证。模型约 140 MB，其中最大单文件约 76 MB；检查上传后是实际模型文件，不能是 Git LFS 指针。使用附件 `bundle_manifest.json`（如文件名不同，按附件实际 manifest）核对原始文件的大小与 SHA-256。部署新增文件与任何修复另行记录，不能假称修改后仍与原包完全相同。

将配套 `deploy/medicine-vision/Dockerfile` 和 `.dockerignore` 复制到上述服务根目录。它们是 Linux 部署模板，尚未经过真实 Render 构建；必须执行后续验收。不要运行 Windows 的 `.cmd`，不要将 `constraints-windows-py311.txt` 当作 Linux 锁定环境。

原包依赖如下，先按这些版本构建，发生缺 wheel、依赖冲突或 import 失败时保留完整日志，再做最小兼容修复并记录实际版本；不能静默换模型：

```text
paddlepaddle==3.3.1
paddleocr==3.7.0
paddlex==3.7.2
Pillow==12.3.0
fastapi==0.115.14
uvicorn==0.34.3
httpx==0.28.1
psutil==7.2.2
```

Docker 使用 Python 3.11 / Linux CPU，并安装 `libgl1 libglib2.0-0 libgomp1`。不需要 GPU。不要直接运行 `patch_paddle_cache.py`：它是有虚拟环境限制的兼容补丁，Linux 是否需要应依据实际错误判断。

## 2. Render 配置

创建 Web Service，Runtime 选 Docker，Root Directory 指向服务根目录，Dockerfile Path 为 `./Dockerfile`。先使用单实例、至少 2 GB 内存起步，再按实际峰值调整；原报告的模型子进程 RSS 不等于整个服务的峰值，不能据此承诺 512 MB 可用。不要增加 Uvicorn worker 或水平副本：原包采用 SQLite、单任务调度器和一个模型子进程。

添加环境变量：

| 名称 | 值 |
| --- | --- |
| `VISION_API_TOKEN` | 在 Render 中生成随机长令牌，保存为 secret；不提交仓库 |
| `VISION_DATA_DIR` | `/var/data/medicine-vision` |
| `VISION_INFERENCE_TIMEOUT` | `120` |
| `VISION_STARTUP_TIMEOUT` | `180` |
| `VISION_WAITING_TIMEOUT` | `600` |
| `PYTHONUNBUFFERED` | `1` |

在 `/var/data` 挂持久磁盘，用于 SQLite 和照片。没有持久盘时，重启/重新部署可能丢任务，手机按原编号恢复查询会失败。首次部署与再部署都应验证持久化；记录照片保留时长与清理方式，不能删除仍在处理中的文件。

启动命令由 Dockerfile 提供，等价于：

```sh
python deploy_vision.py
```

启动时由 `deploy_vision.py` 读取 Render 注入的 `PORT`，监听 `0.0.0.0`，使用单 worker。

HTTPS 由 Render 终止。Render 的 Health Check Path 填 `/ready`：模型未就绪时返回 HTTP 503，就绪后返回 HTTP 200。APK 继续读取 `/health`，同时检查 `ready:true` 和 `capabilities.ocr:true`；该接口加载时仍可能返回 HTTP 200，不能只按状态码判断。两者用途不同，不冲突，保留现有 `/health` 字段和语义。

## 3. APK 固定接口：请保持字段和语义

除 `/health` 外，所有任务接口验证 `Authorization: Bearer <VISION_API_TOKEN>`。任务编号是独立 OCR 编号，例如 `v_` 加 32 位 UUID；格式 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`，不要把 96 位硬件编号直接写进去。

| 接口 | 请求与结果 |
| --- | --- |
| `GET /health` | JSON 必须包含 `ready:true`、`capabilities.ocr:true` 才表示模型就绪 |
| `POST /api/v1/vision/tasks` | JSON：`{"task_id":"v_唯一编号","question":"生产日期是什么？","expected_frames":1}`；新建 201，相同参数重试 200，参数冲突 409 |
| `PUT /api/v1/vision/tasks/{task_id}/frames/f0` | `Content-Type:image/jpeg`；body 为原始 JPEG 字节，不是 multipart 或 base64；最多 8 MiB / 1600 万像素 |
| `GET /api/v1/vision/tasks/{task_id}` | 返回同一任务快照；手机约 1 秒轮询，最多等待约 240 秒 |
| `PATCH /api/v1/vision/tasks/{task_id}` | 原包已有的补充问题接口，保留；第一版手机先问题后图片即可 |

状态保留：`awaiting_question`、`awaiting_frame`、`queued`、`running`、`succeeded`、`failed`、`timed_out`。上传 HTTP 202 仍可能是 `status:failed`，不得把 202 包装成识别成功。失败保留 `error_code`、`message`；HTTP 错误保留 `error.code`、`error.message`。

成功快照须保留 `task_id`、`question`、`expected_frames`、`frames`、`answer`、`evidence_text`、`result`。顶层和 result 中的 answer/evidence_text 必须一致。每个 frame 保留 `frame_id` 和上传原始 JPEG 的 `sha256`；`result.frames` 保留对应 `sha256`、`ocr_text` 和原始 lines。第一版固定单图 `f0`。

`result.answer_type` 保留 `ocr_text`、`field_candidate`、`conflicting_candidates`、`not_found`、`unsupported`；字段候选保留 `intent`、`fields`、每个候选的 value 和 evidence（frame_id、text、bbox、ocr_confidence）。保留 `review_required` 和原始能力信息。不要仅返回一段大模型生成文本，否则 APK 无法核对照片和字段来源。

同 task/frame 上传同一份 bytes 可幂等返回，不同 bytes 必须 409。重拍使用新任务。不要用 `/camera/latest` 的可变图片替代指定任务照片。当前服务没有取消接口；APK 取消后会忽略旧结果，后端可以继续处理旧任务；无需为了本次联调临时伪造 cancel 成功。

## 4. ESP32 和手机的配合

手机先建 OCR 任务，成功后给 ESP32 发拍照请求，附原 A task_id、vision_task_id、frame_id=f0、question、upload_url、content_type。板端须配置 OCR 令牌，用 Wi-Fi PUT 本次照片到 upload_url；手机负责 GET 查询。认证不能省略，也不要把令牌写入公开日志。

这个步骤需要固件同学支持原始 JPEG PUT，不能仅部署后端就认定板端已接通。手机也会提供选择 JPEG 的测试入口，用于先独立验证 OCR HTTP 链路。APK 的 OCR 地址单独配置，填写新服务根地址，不附 `/docs`。B 仍填写原后端地址。

## 5. 验收：以真实 HTTP 结果为准

在服务根目录执行（Linux shell；令牌用环境安全注入，勿将真实值粘进公共记录）：

```sh
export VISION_BASE_URL='https://你的新OCR服务.onrender.com'
# 在本地终端安全设置 VISION_API_TOKEN；不要把真实令牌写入此文件或 Git。
python verify_render.py
```

`verify_render.py` 是本次配套的标准库验收脚本，复制到服务根目录后运行。它会创建测试任务并上传附件的两张合成日期图，检查健康、401、创建幂等、重复传图、错图 409、轮询终态、任务/问题/照片 hash 对应、原文、候选证据和两图不同生产日期。非零退出视为失败，不能仅提交服务首页或 Swagger 截图。

还要手动测试真实药盒：清晰生产日期、有效期、多候选、无标签、模糊图、不同药盒连续请求、服务重启恢复。合成图通过只能证明该样本通过，不能证明真实药盒准确率。原报告已记录布局排序误差，如真实图触发需保留图片和完整 JSON，再修复并回归。

最后验证旧后端的 `/vision/analyze` 仍返回真实云端场景和建议，`/analyze` 仍能接收 image+intent 并返回 message；若旧接口只有 mock 或 open_app/unknown，需明确说明，药盒 OCR 部署不会使 B 自动获得完整 agent 能力。

## 6. 请回传这些结果

1. 新 OCR HTTPS 根地址和 `/docs` 地址。
2. 通过私密渠道交付的 Bearer 令牌，以及“板端和手机均需配置”的说明。
3. `/health` 完整 JSON，`verify_render.py` 通过输出、两张样本终态 JSON（可脱敏，不能改识别内容）。
4. 实际部署 commit、依赖版本、实例内存、磁盘配置、首次模型加载耗时和单图耗时。
5. 真实药盒测试结果；所有部署兼容修复、仍存在的问题和重启恢复结果。

交付标准：模型 ready、两张样本各自证据对应各自照片、错误请求正确拒绝、旧 B 服务仍可用。当前文档和 Dockerfile 不表示服务已经在 Render 上线。
