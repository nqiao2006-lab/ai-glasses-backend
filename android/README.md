# 接入已有 Android 工程

`VisionApiClient.kt` 是调用示例，未构建为 APK、未进行 Android 编译或真机测试。复制到工程后修改包名。该客户端使用已有 API，不加载 Python 模型。

在 App 的 `build.gradle.kts` 中添加（若已有同类依赖，使用项目兼容版本）：

```kotlin
dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
```

`AndroidManifest.xml` 的 `<manifest>` 内声明：

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

局域网 HTTP 仅限调试。Android 9+ 默认可能阻止明文流量：可在 **debug 源集** `src/debug/AndroidManifest.xml` 的 `<application>` 配置 `android:usesCleartextTraffic="true"`；已有 network security config 时按项目规则调整。正式服务使用 HTTPS。

在已有协程作用域中调用，例如 Activity 的 `lifecycleScope.launch`（需要项目已有 lifecycle-runtime-ktx）：

```kotlin
val api = VisionApiClient(serviceBaseUrl, provisionedToken)
lifecycleScope.launch {
    try {
        check(api.ready()) { "识别服务尚未就绪" }
        // 在发请求前生成并持久化 ID，以便断网后使用相同 ID 查询/重试。
        val id = "app_" + java.util.UUID.randomUUID().toString().replace("-", "")
        // TODO: 在本地持久保存 id，以及要上传的原始 JPEG 字节。
        api.createTask("生产日期是什么？", taskId = id)
        // jpegBytes 必须是真实 JPEG；拍摄和读取图片由现有 App 提供。
        val uploaded = api.uploadJpeg(id, "f0", jpegBytes)
        val result = if (uploaded.optString("status") in setOf("failed", "timed_out")) {
            uploaded
        } else {
            api.waitForResult(id)
        }
        val status = result.optString("status")
        if (status == "succeeded") {
            val type = result.getJSONObject("result").optString("answer_type")
            val answer = result.optString("answer")
            // TODO: 显示 answer、evidence_text；根据 type 决定核对、补拍或朗读。
        } else {
            // TODO: 显示 message/error_code；重拍应创建新任务。
        }
    } catch (cancelled: kotlinx.coroutines.CancellationException) {
        throw cancelled
    } catch (error: Exception) {
        // TODO: 展示网络/服务错误；保留原 task_id，先查状态再决定重试。
    }
}
```

ESP32 上传时，Android 只调用 `createTask`/`waitForResult`，将同一 task_id 交给眼镜；眼镜执行原始 JPEG PUT。多图将 expectedFrames 设为照片数，逐一上传不同 frame_id，再轮询。网络请求在 IO 线程运行；示例使用阻塞 OkHttp 调用，协程取消不能立即中断正在进行的单次请求，最长受 callTimeout 限制。
