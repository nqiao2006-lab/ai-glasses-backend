# 视觉服务 API v1

默认地址 `http://127.0.0.1:8000`。交互文档 `/docs`，机器接口 `/openapi.json`，导出副本 [openapi.json](vision_service/openapi.json)。服务直接提供 HTTP；HTTPS 由部署方反向代理终止。当前为单进程应用与单 OCR worker，不支持 Uvicorn 多 workers。

有 `VISION_API_TOKEN` 时，任务接口均需 `Authorization: Bearer <token>`；`/health` 不需认证。绑定非 loopback 地址的启动脚本要求设置 token。不要把正式服务凭证提交到代码仓库。

## 接口与请求

| 方法与路径 | 输入 | 成功码 |
| --- | --- | --- |
| GET `/health` | 无 | 200；不可用时 503 |
| POST `/api/v1/vision/tasks` | JSON：task_id 可选，question 可空，expected_frames 默认 1 | 201 新建 / 200 幂等 |
| PATCH `/api/v1/vision/tasks/{task_id}` | JSON：question | 200 |
| PUT `/api/v1/vision/tasks/{task_id}/frames/{frame_id}` | 原始 JPEG 二进制 | 202 新帧 / 200 幂等 |
| GET `/api/v1/vision/tasks/{task_id}` | 无 | 200 |

创建请求示例：

```json
{"task_id":"android-001","question":"这个药盒的生产日期是什么？","expected_frames":1}
```

编号允许 1–64 个英文字母、数字、下划线和短横线，首字符须为英文字母或数字。问题最多 1000 字符；仅支持约定的字段路由，不提供任意语义理解。expected_frames 是 1–4 的整数，创建后不能修改。

PUT 必须使用 `Content-Type: image/jpeg`，body 是 JPEG 文件字节，不能用 multipart、base64 或 JSON。可提供 `X-Task-Id` / `X-Frame-Id`，其值必须分别与路径一致。最大 8 MiB、1600 万像素。解码后统一 EXIF 方向，返回的 bbox 坐标为方向校正后图像像素。

“图片先到”仍须先 POST 空问题任务，再 PUT 图片，最后 PATCH 问题。问题和约定数量的图片全部齐备才入队。药盒不同面可以作为同一任务的多个 frame；程序不会证明它们属于同一实物。

## 状态与幂等

| status | 含义 |
| --- | --- |
| awaiting_question | 缺少问题，可先上传帧 |
| awaiting_frame | 已有问题，图片数量未齐 |
| queued | 问题与图片已齐，等待串行 worker |
| running | 正在 OCR |
| succeeded | OCR 处理完成，需继续检查 answer_type |
| failed | 图片、模型或处理失败 |
| timed_out | 等待或推理超时 |

相同 task_id、问题和 expected_frames 重建为幂等；不同参数为 409。相同 task_id/frame_id/图片 SHA256 重传为幂等，即使任务已结束也不会新增识别。相同 frame_id 不同字节或同任务另一 frame_id 重复相同内容均为 409。任务 queued/running/终态后不能换问题或追加新图；相同问题的 PATCH 可以重试。

严重质量失败的首次 PUT 可返回 **202 且 status=failed**，表示上传已记录但被拒绝识别；必须读取 JSON 状态。重拍、换问题或终态重试请创建新 task_id。

## 查询结果

以下片段来自合成日期样本的真实模型输出，非生产样例准确率承诺：

```json
{
  "task_id": "date_a",
  "frame_id": "f0",
  "status": "succeeded",
  "answer": "生产日期识别候选：2026/08/12。请对照原图确认。",
  "evidence_text": "生产日期：2026/08/12",
  "message": "",
  "result": {
    "intent": "生产日期",
    "answer_type": "field_candidate",
    "review_required": true
  }
}
```

完整查询还包含 question、expected_frames、created_at/updated_at/started_at（Unix 秒）、error_code、frames 和 result。未完成或失败任务 result 为 null。result 结构：

| 字段 | 含义 |
| --- | --- |
| answer / evidence_text | 面向朗读的回答与 OCR 证据原文 |
| answer_type | ocr_text、field_candidate、conflicting_candidates、not_found、unsupported |
| intent | read_text 或明确字段名；unsupported 问题保留不支持类型 |
| fields | 各字段候选列表；每项 value 和 evidence（frame_id、原文、bbox、置信度等） |
| frames | 各 frame_id 的 SHA256、quality、ocr_text、lines |
| runtime | 实际推理秒数、RSS、worker PID、engine、device、coordinate_space |
| capabilities | ocr=true，explicit_field_extraction=true，object_recognition=false，generative_vqa=false |

没有生产日期标签时不会从批号、收据日期或有效期时长猜生产日期。跨帧不同值返回 conflicting_candidates，不自动选一个。药名与用法需要可见的明确字段标题；全文仍可返回其他未归属文字。

## 错误与超时

请求错误结构：`{"error":{"code":"...","message":"..."}}`。任务处理错误保存在顶层 status、error_code、message，可继续 GET 查询。

| HTTP / code | 处理建议 |
| --- | --- |
| 401 unauthorized | 检查 Bearer 凭证 |
| 404 task_not_found | 先创建任务，检查编号 |
| 409 id_mismatch / task_conflict / frame_conflict / duplicate_content / task_sealed | 修正编号或创建新任务 |
| 413 upload_too_large / image_too_large / request_too_large | 减小请求 |
| 415 unsupported_media_type / unsupported_image | 发送真实 JPEG 字节及正确 Content-Type |
| 422 invalid_request / invalid_id / invalid_image | 修正格式；损坏图任务可已进入 failed |
| 429 queue_full | 退避后重试创建 |
| 503 database_unavailable / storage_unavailable | 等服务恢复后按原 task_id 查询 |
| 任务 image_quality | 重拍，创建新任务 |
| 任务 model_unavailable / inference_failed | 检查服务日志；模型加载失败需重启服务 |
| 任务 waiting_timeout / inference_timeout | 查询终态，减少帧数或稍后新建 |

默认最多 32 个非终态任务；等待问题、图片或排队共 600 秒；单任务推理 120 秒；模型初始化预算 180 秒。CPU 实测单图约 5–15 秒，排队会增加等待。客户端建议每 0.5–1 秒查询，断网重试先用原编号查状态，不直接创建重复任务。

健康检查的 ready=true 才表示模型和数据库均就绪。加载中可返回 200/status=loading/ready=false；数据库或模型故障为 503/status=degraded。设置：VISION_DATA_DIR、VISION_INFERENCE_TIMEOUT、VISION_STARTUP_TIMEOUT、VISION_WAITING_TIMEOUT、VISION_API_TOKEN。
