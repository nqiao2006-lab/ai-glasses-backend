# 本地后端集成改动记录

2026-10-24：从用户提供的 `medicine-vision-apk-integration.zip` 导入全部 48 个文件（其中原 manifest 列出 47 个文件），导入前逐项核对 bytes/SHA-256，导入后再次运行原版 `verify_bundle.py`。原 manifest、模型和原始代码均未修改。

从 `Render后端部署指令与模板.zip` 复制部署说明、Dockerfile、.dockerignore、verify_render.py。其中 Dockerfile 增加构建时原包完整性检查，改用新增部署入口；.dockerignore 排除秘密、本地任务与测试。verify_render.py 保持附件原样；部署说明已将启动命令改为 `python deploy_vision.py`、平台健康检查改为 `/ready`，APK 仍读取 `/health`。

GitHub 网页上传适配：新增 `restore_models.py` 和项目级 `scripts/prepare_ocr_upload.py`。上传包使用不超过 20 MiB 的模型分片；Docker 在安装依赖前按分片 SHA-256 及原始 bundle_manifest 校验并还原模型，原模型内容未改。原始本地模型完整保留。分片还原已通过 47 个原包文件校验，49 项 OCR 接口回归通过；尚未因此完成真实模型推理或 Render 发布。

新增：deployment.py、deploy_vision.py、.env.example、cleanup_frames.py、tests/、requirements-contract-tests.txt、TEST_REPORT.md、本记录。主项目新增 OCR_INTEGRATION.md，并在 README/DEPLOY_RENDER.md 中区分 A/B 两套服务；.gitignore 排除 OCR 本地任务数据；pytest.ini 保持默认测试原 B，OCR 测试显式指定。没有改动原 B API、根 render.yaml 或现有 .env。

运行时协议不替换原任务实现；deployment.py 调用原 create_app，补强令牌必填、有限正超时、显式 CORS、/ready、Swagger Bearer 描述，以及异常退出时停止模型线程。清理工具只在人工 --apply 时删除旧终态照片，保留任务结果证据，不安装定时任务。

所有本次新增文件均不在原始 bundle_manifest.json 的完整性声明内；这些文件应随实际仓库 commit/镜像记录版本。原 smoke_test.py 仍启动原 serve_vision.py；部署适配层另有 tests 和 HTTPS verify_render.py 验证。未执行 Linux Docker 构建、Render 发布、真实 PaddleOCR 推理、真实药盒或硬件验收，不能将附件原交付报告作为本轮实测。
