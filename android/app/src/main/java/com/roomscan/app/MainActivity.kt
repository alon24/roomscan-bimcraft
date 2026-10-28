package com.roomscan.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText

/**
 * A thin shell around the server's web app: it opens Photo Measure from your own RoomScan server.
 * All the logic stays on the server; this only gives the page a window, a camera and a photo picker.
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private var chooser: ValueCallback<Array<Uri>>? = null
    private var cameraUri: Uri? = null
    private val prefs by lazy { getSharedPreferences("app", MODE_PRIVATE) }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        setContentView(web, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
        }
        web.addJavascriptInterface(Bridge(), "AndroidApp")
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) askServer("Can't reach ${request.url.host} (${error.description}).")
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(w: WebView, cb: ValueCallback<Array<Uri>>, p: FileChooserParams): Boolean {
                chooser?.onReceiveValue(null)
                chooser = cb
                val values = ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, "roomscan_${System.currentTimeMillis()}.jpg")
                    put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                }
                cameraUri = contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
                val camera = Intent(MediaStore.ACTION_IMAGE_CAPTURE).putExtra(MediaStore.EXTRA_OUTPUT, cameraUri)
                val pick = Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE).setType("image/*")
                val intent = if (p.isCaptureEnabled) Intent.createChooser(camera, "Photo")
                else Intent.createChooser(pick, "Photo").putExtra(Intent.EXTRA_INITIAL_INTENTS, arrayOf(camera))
                return try {
                    startActivityForResult(intent, REQ_PHOTO)
                    true
                } catch (e: Exception) {
                    chooser = null
                    false
                }
            }
        }
        val server = prefs.getString("server", null)
        if (server == null) askServer("Type the address of your RoomScan server, for example http://192.168.1.20:8010") else load(server)
    }

    private fun load(server: String) = web.loadUrl("$server/photo.html")

    private fun askServer(message: String) {
        val input = EditText(this).apply { setText(prefs.getString("server", "http://")); setSelection(text.length) }
        AlertDialog.Builder(this)
            .setTitle("RoomScan server")
            .setMessage(message)
            .setView(input)
            .setCancelable(false)
            .setPositiveButton("Open") { _, _ ->
                var s = input.text.toString().trim().trimEnd('/')
                if (s.isNotEmpty() && !s.startsWith("http")) s = "http://$s"
                // An empty answer would leave a blank page with no way back to this dialog: ask again.
                if (s.isNotEmpty()) { prefs.edit().putString("server", s).apply(); load(s) } else askServer(message)
            }
            .setNegativeButton("Try again") { _, _ -> prefs.getString("server", null)?.let { load(it) } ?: askServer(message) }
            .show()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode != REQ_PHOTO) return super.onActivityResult(requestCode, resultCode, data)
        val picked = if (resultCode == RESULT_OK) data?.data else null
        val result: Array<Uri>? = when {
            picked != null -> arrayOf(picked)                                   // chosen from the gallery
            resultCode == RESULT_OK && cameraUri != null -> arrayOf(cameraUri!!)   // taken with the camera
            else -> null
        }
        if (result == null || picked != null) cameraUri?.let { contentResolver.delete(it, null, null) }   // drop the unused empty picture
        chooser?.onReceiveValue(result)
        chooser = null
        cameraUri = null
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    /** The web page can offer "change server" when it runs inside this app. */
    inner class Bridge {
        @JavascriptInterface
        fun changeServer() = runOnUiThread { askServer("Server address") }
    }

    companion object { private const val REQ_PHOTO = 1 }
}
