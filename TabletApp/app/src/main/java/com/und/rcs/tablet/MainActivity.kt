package com.und.rcs.tablet

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.webkit.JavascriptInterface
import android.view.View
import android.view.WindowManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
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

    // ── 연결 끊김 자동 복구 (2026-09-18) ────────────────────────────
    //   현장 보고: 자재실 태블릿이 WiFi 끊김 후 "모든 명령 실패" 상태로 멈추고,
    //   전원을 껐다 켜야만 정상화됐다.
    //   원인: WebViewClient 가 기본 구현이라 로드 실패 시 크롬 오류 페이지로
    //   넘어갔다. 그 순간 페이지의 JS(2초 폴링)가 사라져 스스로 복구할 길이
    //   없어진다. 아래 셋으로 해결한다.
    //     ① onReceivedError 를 가로채 자체 재시도 화면을 띄운다 (JS 유지 불필요)
    //     ② 5초마다 자동 재시도
    //     ③ WiFi 가 돌아오면 NetworkCallback 으로 즉시 재시도
    private var currentUrl: String = ""
    private var inErrorState = false
    private var retryCount = 0
    private val handler = Handler(Looper.getMainLooper())
    private var connectivityCallback: ConnectivityManager.NetworkCallback? = null

    /** 재시도 주기. 너무 짧으면 오류 화면이 계속 깜빡인다. */
    private val retryIntervalMs = 5000L

    private val retryRunnable = object : Runnable {
        override fun run() {
            if (inErrorState && currentUrl.isNotEmpty()) {
                retryCount++
                webView.loadUrl(currentUrl)
                handler.postDelayed(this, retryIntervalMs)
            }
        }
    }

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
        webView.webViewClient = RecoveringWebViewClient()
        val base = serverUrl.trimEnd('/')
        val url = when (mode) {
            "console" -> "$base/api/dispatch/console"
            "poi"     -> "$base/api/dispatch/tablet/poi/$id"
            else      -> "$base/api/dispatch/robot-tablet/$id"
        }
        currentUrl = url
        retryCount = 0
        startNetworkWatch()
        webView.loadUrl(url)
    }

    /**
     * 로드 실패를 가로채 **자체 재시도 화면**을 띄운다.
     *
     * 기본 WebViewClient 는 크롬 오류 페이지로 넘어가는데, 그러면 페이지 JS 가
     * 통째로 사라져 WiFi 가 돌아와도 영원히 복구되지 않는다(현장 실측).
     */
    private inner class RecoveringWebViewClient : WebViewClient() {

        override fun onReceivedError(
            view: WebView?,
            request: WebResourceRequest?,
            error: WebResourceError?,
        ) {
            // 이미지·API 같은 하위 요청 실패로는 화면을 바꾸지 않는다.
            // 페이지 본문(main frame) 이 실패했을 때만 재시도 화면으로 간다.
            if (request != null && !request.isForMainFrame) return
            showOfflineScreen()
        }

        @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
        override fun onReceivedError(
            view: WebView?,
            errorCode: Int,
            description: String?,
            failingUrl: String?,
        ) {
            // API 23 미만 폴백 (minSdk 24 라 실사용은 거의 없지만 안전하게 남긴다)
            if (failingUrl != null && failingUrl != currentUrl) return
            showOfflineScreen()
        }

        override fun onPageFinished(view: WebView?, url: String?) {
            super.onPageFinished(view, url)
            // 실제 서버 페이지가 떴다 = 복구됨. (재시도 화면은 data: URL 이라 제외)
            if (url != null && url.startsWith("http")) {
                inErrorState = false
                retryCount = 0
                handler.removeCallbacks(retryRunnable)
            }
        }
    }

    /** 서버에 못 붙었을 때 띄우는 화면. 서버가 필요 없도록 내용을 앱이 들고 있는다. */
    private fun showOfflineScreen() {
        inErrorState = true
        handler.removeCallbacks(retryRunnable)
        handler.postDelayed(retryRunnable, retryIntervalMs)

        val tries = if (retryCount > 0) "재시도 ${retryCount}회" else "연결을 시도하는 중"
        val html = """
            <!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <style>
              html,body{margin:0;height:100%;background:#2a1113;color:#ffd7d3;
                font-family:sans-serif;display:flex;align-items:center;
                justify-content:center;text-align:center}
              .box{padding:24px;max-width:760px}
              h1{font-size:40px;margin:0 0 18px;color:#ff8a8a}
              p{font-size:22px;line-height:1.7;margin:10px 0;color:#ffe3e0}
              .sub{font-size:18px;color:#d8a9a5;margin-top:22px;line-height:1.7}
              .tip{margin-top:26px;padding:16px 20px;border-radius:12px;
                background:#3a2a12;border:1px solid #6b5320;color:#ffd36d;
                font-size:19px;line-height:1.7}
              .dot{display:inline-block;width:12px;height:12px;border-radius:50%;
                background:#ff6b6b;margin-right:10px}
            </style></head><body><div class="box">
              <h1><span class="dot"></span>서버에 연결할 수 없습니다</h1>
              <p>WiFi 연결을 확인해 주세요.</p>
              <p class="sub">연결되면 <b>자동으로 복구됩니다</b> — 전원을 끄지 않아도 됩니다.<br>$tries</p>
              <div class="tip">
                급하면 <b>관제 콘솔</b>에서도 같은 조작을 할 수 있습니다.<br>
                작업지점의 <b>[확인 — 랙을 치웠습니다]</b> 도 콘솔에서 누를 수 있습니다.
              </div>
            </div></body></html>
        """.trimIndent()
        webView.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null)
    }

    /** WiFi 가 돌아오면 5초를 기다리지 않고 바로 다시 붙는다. */
    private fun startNetworkWatch() {
        if (connectivityCallback != null) return
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                handler.post {
                    if (inErrorState && currentUrl.isNotEmpty()) {
                        retryCount++
                        webView.loadUrl(currentUrl)
                    }
                }
            }
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                cm.registerDefaultNetworkCallback(cb)
                connectivityCallback = cb
            }
        } catch (_: Exception) {
            // 등록 실패해도 5초 폴링이 있으므로 복구 자체는 된다
        }
    }

    override fun onDestroy() {
        handler.removeCallbacks(retryRunnable)
        connectivityCallback?.let { cb ->
            try {
                (getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager)
                    ?.unregisterNetworkCallback(cb)
            } catch (_: Exception) {
            }
        }
        connectivityCallback = null
        super.onDestroy()
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
