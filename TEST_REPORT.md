# 本轮后端集成测试记录

日期：2026-10-24。地点：本地 Windows/Python 3.13.9。不是附件历史报告，不是 Render 或真实 OCR 验收。

## 已实测

| 检查 | 结果 |
| --- | --- |
| 原始附件导入前逐项大小/SHA-256 | 47 个 manifest 文件全部匹配 |
| 导入后原版 `verify_bundle.py` | passed=true，verified_files=47；包括全部模型文件 |
| 原后端 + 新 OCR 合并回归 | 121 passed（原后端 72 项、OCR 49 项） |
| 附件固定 API 版本下的 OCR 回归 | 49 passed |
| 原项目虚拟环境 `pip check` | No broken requirements found |

合并回归命令（项目根）：

```text
.venv\Scripts\python.exe -m pytest tests medicine-vision/tests -q --tb=short -p no:cacheprovider --basetemp artifacts/backend-ocr-regression-20261024-b
```

合并回归环境：FastAPI 0.159.1、Uvicorn 0.54.0、httpx 0.28.1、Pydantic 2.14.1、Starlette 1.9.0、NumPy 2.5.4、opencv-python-headless 4.14.0.94、Pillow 12.3.0、psutil 7.2.2、pytest 9.1.1。

随后将 FastAPI 0.115.14、Uvicorn 0.34.3、httpx 0.28.1 及其依赖装入 `artifacts/ocr-api-pins-20261024`，由独立进程优先加载，实测 Starlette 0.46.2。没有降级原 B 虚拟环境；在这一 API 组合下运行同一 `medicine-vision/tests`，49 passed。仍使用 Windows/Python 3.13.9、上述图像库，不等同 Linux/Python 3.11 全部 Paddle 依赖构建验证。

测试覆盖：所有任务路由鉴权、禁止空令牌启动、CORS PUT/PATCH/Authorization 预检、非法 origin、超时配置、PORT/单 worker、health 与 ready 区分、Swagger Bearer、创建幂等和冲突、原始 JPEG PUT、重复/错图冲突、问题后补、结果和任务/照片哈希对应、证据与 bbox、不同照片结果隔离、多帧冲突、not_found/unsupported/全文、错误载荷与大小限制、202 但 failed 的质量拒绝、模型不可用、推理超时、重启任务查询恢复、旧终态照片清理及非终态/外部文件保护。

OCRDouble 只在 tests 中按合成样本生成已知转录，以隔离验证任务和证据管线；生产部署没有导入它，未将其结果当成模型识别结果。清理测试只删除测试临时目录中生成的占位文件，没有清理用户 uploads 或真实 OCR 照片。

## 环境情况和限制

- 首次依赖安装遇到网络超时；经批准联网后安装附件指定 Pillow 12.3.0、psutil 7.2.2。API 固定版本隔离目录测试遇到沙盒文件读取限制，获准后执行成功，没有改动原服务依赖版本。
- 合并回归出现已有 TestClient/httpx 弃用提醒；固定版本测试出现 anyio 别名弃用提醒；均无测试失败。
- 本机没有可用 Docker，也未安装 Paddle/PaddleOCR；未进行大体积推理环境安装或更换模型。
- 未运行真实模型的 smoke_test.py 或 HTTPS verify_render.py，未调用收费云模型、旧公网后端、Render 或 ESP32。

## 部署后仍须验收

1. Linux Docker build 的完整依赖解析、pip check、model import 与原包完整性检查。
2. `/ready` 返回 200、`/health` 同时 ready=true 和 capabilities.ocr=true。
3. 原版 verify_render.py 通过，保留两张不同日期照片的真实终态 JSON 和证据。
4. 真实药盒、模糊/无标签/多候选、连续任务的识别质量。
5. Render 持久盘重启后按原 task_id 查询、运行中任务恢复、照片与结果保留策略。
6. APK/ESP32 原始 JPEG HTTPS PUT、私密令牌配置，以及旧 B 真实云模型能力。

本轮没有提供实际 Render URL、真实 token、部署 commit 或模型性能数据；这些必须在实际发布和硬件测试后填写。
