package com.und.rcs.tablet

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.view.View
import android.view.WindowManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView

    // 빌드 flavor 로 결정되는 동작 모드
    //   "console" — 관리자 콘솔        → /api/dispatch/console            (서버주소만)
    //   "robot"   — 로봇 부착 태블릿    → /api/dispatch/robot-tablet/{id}  (서버주소 + 로봇ID)
    //   "poi"     — 위치별 태블릿       → /api/dispatch/tablet/poi/{id}    (서버주소 + POI ID)
    //
    // 2026-08-24 에 "poi" 를 추가했다. 현장은 R1·R2(호출) / J1·J2(확인) 네 곳에
    // 각각 태블릿을 두는데, 기존 두 모드로는 그 화면을 열 수 없었다.
    private val mode: String get() = BuildConfig.APP_MODE
    private val isConsole: Boolean get() = mode == "console"
    private val isPoi: Boolean get() = mode == "poi"

    /** 이 모드가 서버주소 말고 ID 를 하나 더 받아야 하나 */
    private val needsId: Boolean get() = !isConsole

    /** 그 ID 를 SharedPreferences 에 어떤 키로 저장할지 */
    private val idKey: String get() = if (isPoi) "poi_id" else "robot_id"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 화면 항상 켜두기 (WakeLock 대신 FLAG 사용)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        webView = WebView(this)
        setContentView(webView)

        // 전체화면
        hideSystemUI()

        webView.addJavascriptInterface(AndroidBridge(), "Android")

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_NO_CACHE
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            mediaPlaybackRequiresUserGesture = false
        }

        val prefs = getSharedPreferences("config", Context.MODE_PRIVATE)
        val serverUrl = prefs.getString("server_url", "") ?: ""
        val savedId = prefs.getString(idKey, "") ?: ""

        if (serverUrl.isEmpty() || (needsId && savedId.isEmpty())) {
            showConfigDialog(prefs) { url, id -> loadPage(url, id) }
        } else {
            loadPage(serverUrl, savedId)
        }
    }

    private fun loadPage(serverUrl: String, id: String) {
        webView.webViewClient = WebViewClient()
        val base = serverUrl.trimEnd('/')
        val url = when (mode) {
            "console" -> "$base/api/dispatch/console"
            "poi"     -> "$base/api/dispatch/tablet/poi/$id"
            else      -> "$base/api/dispatch/robot-tablet/$id"
        }
        webView.loadUrl(url)
    }

    private fun showConfigDialog(
        prefs: SharedPreferences,
        onConfirm: (String, String) -> Unit
    ) {
        val dp = resources.displayMetrics.density
        val pad = (24 * dp).toInt()

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
        }

        fun label(text: String) = TextView(this).apply {
            this.text = text
            textSize = 16f
            setPadding(0, (8 * dp).toInt(), 0, (4 * dp).toInt())
        }

        val etUrl = EditText(this).apply {
            setText(prefs.getString("server_url", "http://192.168.0.21:8002"))
            hint = "http://서버IP:8002"
            textSize = 18f
        }
        layout.addView(label("서버 주소"))
        layout.addView(etUrl)

        // 콘솔 말고는 ID 를 하나 더 받는다 (로봇 ID / POI ID)
        val etId: EditText? = if (needsId) {
            EditText(this).apply {
                setText(prefs.getString(idKey, ""))
                hint = if (isPoi) "관제 화면의 위치 ID (예: R1, J1 의 POI ID)"
                       else "예: 1, 2, 3 … (로봇 ID)"
                textSize = 22f
                inputType = android.text.InputType.TYPE_CLASS_NUMBER
            }.also {
                layout.addView(label(if (isPoi) "위치(POI) ID" else "로봇 ID"))
                layout.addView(it)
                if (isPoi) {
                    layout.addView(TextView(this).apply {
                        text = "이 태블릿을 놓을 자리의 POI ID 를 넣으세요.\n" +
                               "R(랙 보관) 이면 [로봇 호출], J(작업지점) 이면 [확인] 화면이 뜹니다."
                        textSize = 13f
                        setPadding(0, (6 * dp).toInt(), 0, 0)
                    })
                }
            }
        } else null

        val title = when (mode) {
            "console" -> "관리자 콘솔 설정"
            "poi"     -> "위치 태블릿 설정"
            else      -> "로봇 태블릿 설정"
        }

        AlertDialog.Builder(this)
            .setTitle(title)
            .setView(layout)
            .setCancelable(false)
            .setPositiveButton("시작") { _, _ ->
                val url = etUrl.text.toString().trim().trimEnd('/')
                val id = etId?.text?.toString()?.trim() ?: ""
                val ok = url.isNotEmpty() && (!needsId || id.isNotEmpty())
                if (ok) {
                    val edit = prefs.edit().putString("server_url", url)
                    if (needsId) edit.putString(idKey, id)
                    edit.apply()
                    onConfirm(url, id)
                } else {
                    showConfigDialog(prefs, onConfirm)
                }
            }
            .setNeutralButton("설정 초기화") { _, _ ->
                prefs.edit().clear().apply()
                showConfigDialog(prefs, onConfirm)
            }
            .show()
    }

    private fun hideSystemUI() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_FULLSCREEN or
            View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        )
    }

    inner class AndroidBridge {
        @JavascriptInterface
        fun openSettings() {
            runOnUiThread {
                val prefs = getSharedPreferences("config", Context.MODE_PRIVATE)
                showConfigDialog(prefs) { url, id -> loadPage(url, id) }
            }
        }

        @JavascriptInterface
        fun goHome() {
            runOnUiThread {
                val intent = Intent(Intent.ACTION_MAIN).apply {
                    addCategory(Intent.CATEGORY_HOME)
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                }
                startActivity(intent)
            }
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUI()
    }
}
