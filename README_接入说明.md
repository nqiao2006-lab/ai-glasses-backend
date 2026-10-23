# APK 视觉识别接入包

这份包供 Android 开发同学接入现有 APK：**模型在 Windows 电脑/服务器运行，APK 上传 JPEG 并查询结果**。包含完整检测/识别权重、Python 服务、SQLite 建表脚本、Kotlin 调用示例和两张合成测试图。不是可安装的 APK，也不是 Android 离线推理 SDK。

## 1. 启动服务

已验证环境：Windows x64、Python 3.11.5、CPU；PaddlePaddle 3.3.1、PaddleOCR 3.7.0、PaddleX 3.7.2。不要求独立显卡。

1. 安装 64 位 Python 3.11，将整个目录解压到**全英文路径**，例如 `C:\vision\medicine-vision-apk-integration`。当前 Paddle 原生加载器在中文模型路径下曾失败。
2. 双击 `install_service.cmd`，创建本机 `.venv` 并安装依赖。**首次安装依赖需要联网**；模型权重已在包内，不需要再次下载。未附 Python 安装包或离线依赖 wheel。
3. 在解压目录打开 PowerShell，运行：

```powershell
.\.venv\Scripts\python.exe verify_bundle.py
$env:VISION_API_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
.\start_api.cmd --host 0.0.0.0 --port 8000
```

由部署人员将该终端内的 token 安全交给调试客户端；服务和客户端保持一致，不在源代码中固定真实凭证。生产接入应由团队网关负责客户端鉴权和 HTTPS。

手机与服务处于同一网络，访问 `http://电脑的局域网IP:8000/health`，等待 `ready=true`。真机地址不能写 `127.0.0.1`；标准 Android 模拟器访问宿主机通常使用 `10.0.2.2`。主机防火墙需允许所用端口。

数据库和上传图片首次运行自动保存在 `local_vision_data/`，这是联调用 SQLite。Ctrl+C 停止；保留该目录后重启可读取原任务。

## 2. APK 调用顺序

每个受保护接口均带 `Authorization: Bearer <token>`。示例代码在 [android/VisionApiClient.kt](android/VisionApiClient.kt)，复制到已有 Android 项目并修改 `package`；依赖和调用方式见 [android/README.md](android/README.md)。没有 Android 工程/签名配置，本包未构建 APK，Kotlin 示例未在真机编译验证。

| 步骤 | 请求 | 内容 |
|---|---|---|
| 创建 | POST `/api/v1/vision/tasks` | `{"task_id":"app_001","question":"生产日期是什么？","expected_frames":1}` |
| 传图 | PUT `/api/v1/vision/tasks/app_001/frames/f0` | `Content-Type: image/jpeg`，body 为 JPEG 原始字节 |
| 查询 | GET `/api/v1/vision/tasks/app_001` | 每 1 秒查询，到终态结束 |

单任务支持同一物品 1–4 张不同照片，创建时设置 `expected_frames`，使用 `f0`、`f1` 等不同帧号；全部传齐才识别。上传不使用 multipart、base64 或 JSON；单图最多 8 MiB / 1600 万像素。

终态为 `succeeded`、`failed`、`timed_out`。成功后读取 `answer`、`evidence_text` 和 `result.answer_type`：`field_candidate` 需核对原图；`conflicting_candidates` 有多个候选；`not_found` 需要补拍；`unsupported` 是当前能力不支持。**HTTP 202 仅代表收图，仍要检查 JSON 中的状态。** 重拍/更换问题请创建新任务；同一任务与帧编号只能重传完全相同的 JPEG 字节。

当前支持读文字，以及有明确标签的药名、用法用量、生产日期、有效期至、期限、批号和厂家。没有开放式物体识别或生成式视觉问答。

## 3. 自检与已知效果

安装依赖后运行以下命令，会启动独立临时端口的本机服务，真实识别包内两张合成日期图，校验日期、鉴权、幂等和模型只初始化一次；结果保存在 `smoke-results/`，不使用业务数据库：

```powershell
.\.venv\Scripts\python.exe smoke_test.py
```

CPU 既有测试单图约 5–15 秒，多图和排队耗时更长。客户端应异步调用，给任务约 240 秒等待预算。包内权重与当前测试版本相同，**打包不会改善识别准确率**：既有 3 张票据全文 CER 为 20.32%，其中布局排序扩大了误差，使用模型原始顺序重算为 10.28%；该问题尚未修复。真实药品生产日期准确率尚未充分验证。合成图仅用于接通链路，不代表实际药盒效果。

完整接口与错误码：[API_REFERENCE.md](API_REFERENCE.md)；模型来源及许可：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；逐文件校验：[bundle_manifest.json](bundle_manifest.json)。
