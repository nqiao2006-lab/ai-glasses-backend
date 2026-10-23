package com.example.vision

import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/** Integration example. Inject the server URL and a provisioned token at runtime. */
class VisionApiClient(baseUrl: String, private val token: String) {
    private val base = (baseUrl.trimEnd('/') + "/").toHttpUrl()
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .callTimeout(40, TimeUnit.SECONDS)
        .build()

    private suspend fun request(method: String, path: String, bytes: ByteArray? = null,
                                contentType: String = "application/json; charset=utf-8"): JSONObject =
        withContext(Dispatchers.IO) {
            val url = base.resolve(path) ?: throw IllegalArgumentException("Invalid URL")
            val body = bytes?.toRequestBody(contentType.toMediaType())
            val builder = Request.Builder().url(url).method(method, body)
            if (token.isNotBlank()) builder.header("Authorization", "Bearer $token")
            client.newCall(builder.build()).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    val detail = runCatching {
                        JSONObject(text).optJSONObject("error")?.optString("message")
                    }.getOrNull()
                    throw IOException("HTTP ${response.code}: ${detail ?: "Vision request failed"}")
                }
                JSONObject(text)
            }
        }

    suspend fun ready(): Boolean = request("GET", "health").optBoolean("ready", false)

    suspend fun createTask(question: String, expectedFrames: Int = 1,
                           taskId: String = "app_" + UUID.randomUUID().toString().replace("-", "")): String {
        require(question.isNotBlank() && question.length <= 1000)
        require(expectedFrames in 1..4)
        val json = JSONObject().put("task_id", taskId).put("question", question)
            .put("expected_frames", expectedFrames)
        request("POST", "api/v1/vision/tasks", json.toString().toByteArray(Charsets.UTF_8))
        return taskId
    }

    private fun checkId(value: String) {
        require(Regex("[A-Za-z0-9][A-Za-z0-9_-]{0,63}").matches(value))
    }

    suspend fun uploadJpeg(taskId: String, frameId: String, jpeg: ByteArray): JSONObject {
        checkId(taskId)
        checkId(frameId)
        require(jpeg.isNotEmpty() && jpeg.size <= 8 * 1024 * 1024)
        return request("PUT", "api/v1/vision/tasks/$taskId/frames/$frameId", jpeg, "image/jpeg")
    }

    suspend fun getTask(taskId: String): JSONObject {
        checkId(taskId)
        return request("GET", "api/v1/vision/tasks/$taskId")
    }

    suspend fun waitForResult(taskId: String, timeoutMillis: Long = 240_000): JSONObject {
        require(timeoutMillis > 0)
        val started = System.nanoTime()
        while ((System.nanoTime() - started) / 1_000_000 < timeoutMillis) {
            val snapshot = getTask(taskId)
            if (snapshot.optString("status") in setOf("succeeded", "failed", "timed_out")) {
                return snapshot
            }
            delay(1000)
        }
        // The server task continues; query this same taskId after reconnecting.
        throw IOException("Polling timed out; query the same task_id: $taskId")
    }
}
