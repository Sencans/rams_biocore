package com.rams.amadeus;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.speech.tts.TextToSpeech;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

public class AmadeusActivity extends Activity {
    private static final int REQ_ARCHIVO_VRM = 4201;
    private static final int REQ_PERMISOS = 4202;
    private static final String ARCHIVO_VRM_USUARIO = "modelo_usuario.vrm";

    private WebView webView;
    private TextToSpeech tts;
    private boolean ttsListo = false;
    private ValueCallback<Uri[]> callbackArchivo;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Window w = getWindow();
        w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        w.setStatusBarColor(Color.parseColor("#060A12"));
        w.setNavigationBarColor(Color.parseColor("#060A12"));

        prefs = getSharedPreferences("amadeus_prefs", Context.MODE_PRIVATE);
        procesarIntentConexion(getIntent());
        solicitarPermisosBase();

        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                int res = tts.setLanguage(new Locale("es", "MX"));
                if (res == TextToSpeech.LANG_MISSING_DATA || res == TextToSpeech.LANG_NOT_SUPPORTED) {
                    tts.setLanguage(new Locale("es", "ES"));
                }
                tts.setPitch(1.08f);
                tts.setSpeechRate(1.04f);
                ttsListo = true;
            }
        });

        webView = new WebView(this);
        webView.setBackgroundColor(Color.parseColor("#060A12"));
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        webView.addJavascriptInterface(new PuenteNativo(), "AmadeusAndroid");

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> request.grant(request.getResources()));
            }

            @Override
            public boolean onShowFileChooser(
                    WebView vista,
                    ValueCallback<Uri[]> filePathCallback,
                    FileChooserParams fileChooserParams
            ) {
                if (callbackArchivo != null) {
                    callbackArchivo.onReceiveValue(null);
                }
                callbackArchivo = filePathCallback;
                Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                try {
                    startActivityForResult(Intent.createChooser(intent, "Seleccionar modelo .VRM"), REQ_ARCHIVO_VRM);
                    return true;
                } catch (Exception e) {
                    callbackArchivo = null;
                    return false;
                }
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (uri == null) {
                    return super.shouldInterceptRequest(view, request);
                }
                String ruta = uri.getPath();
                if (ruta == null) {
                    ruta = "/";
                }

                // Interceptar el modelo de ejemplo VRM incluso si se pide a cdn.jsdelivr.net (100% offline)
                if (ruta.endsWith("VRM1_Constraint_Twist_Sample.vrm") || "/vendor/sample.vrm".equals(ruta)) {
                    return servirAsset("vtuber/vendor/sample.vrm", "model/gltf-binary");
                }

                if (!"amadeus.local".equalsIgnoreCase(uri.getHost())) {
                    return super.shouldInterceptRequest(view, request);
                }

                if (ruta.isEmpty() || "/".equals(ruta) || "/index.html".equals(ruta)) {
                    return servirAsset("movil/index.html", "text/html");
                }
                if ("/manifest.json".equals(ruta)) {
                    return servirAsset("movil/manifest.json", "application/json");
                }
                if ("/sw.js".equals(ruta)) {
                    return servirAsset("movil/sw.js", "text/javascript");
                }
                if (ruta.startsWith("/movil/")) {
                    String sub = ruta.substring("/movil/".length());
                    return servirAsset("movil/" + sub, detectarMime(sub));
                }
                if ("/visor3d".equals(ruta)) {
                    return servirVisor3dLocal();
                }
                if (ruta.startsWith("/vendor/")) {
                    return servirAsset("vtuber" + ruta, detectarMime(ruta));
                }
                if ("/app.js".equals(ruta)
                        || "/style.css".equals(ruta)
                        || "/protocol.js".equals(ruta)
                        || "/amadeus_theme.js".equals(ruta)
                        || "/amadeus_theme.css".equals(ruta)) {
                    return servirAsset("vtuber" + ruta, detectarMime(ruta));
                }
                if (ruta.startsWith("/models/")) {
                    File f = new File(getFilesDir(), ARCHIVO_VRM_USUARIO);
                    if (f.exists() && f.isFile()) {
                        try {
                            return new WebResourceResponse(
                                    "model/gltf-binary",
                                    "UTF-8",
                                    200,
                                    "OK",
                                    crearCabecerasCors(),
                                    new FileInputStream(f)
                            );
                        } catch (Exception ignored) {}
                    }
                    return servirAsset("vtuber/vendor/sample.vrm", "model/gltf-binary");
                }
                if ("/api/models".equals(ruta)) {
                    File f = new File(getFilesDir(), ARCHIVO_VRM_USUARIO);
                    if (f.exists() && f.isFile()) {
                        String json = "[{\"name\":\"" + ARCHIVO_VRM_USUARIO + "\",\"url\":\"/models/" + ARCHIVO_VRM_USUARIO + "\"}]";
                        return crearRespuesta("application/json", json.getBytes(StandardCharsets.UTF_8));
                    }
                    return crearRespuesta("application/json", "[]".getBytes(StandardCharsets.UTF_8));
                }
                if ("/api/state".equals(ruta) || "/api/command".equals(ruta) || "/api/ack".equals(ruta)) {
                    return crearRespuesta("application/json", "{\"ok\":true,\"ready\":true,\"modo\":\"apk_nativo\"}".getBytes(StandardCharsets.UTF_8));
                }
                return super.shouldInterceptRequest(view, request);
            }
        });

        setContentView(webView);
        cargarInterfazPrincipal();
    }

    private void cargarInterfazPrincipal() {
        byte[] bytes = leerBytesAsset("movil/index.html");
        if (bytes != null && bytes.length > 0) {
            String html = new String(bytes, StandardCharsets.UTF_8);
            webView.loadDataWithBaseURL(
                    "https://amadeus.local/index.html",
                    html,
                    "text/html",
                    "UTF-8",
                    "https://amadeus.local/index.html"
            );
        } else {
            webView.loadUrl("https://amadeus.local/index.html");
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (procesarIntentConexion(intent) && webView != null) {
            cargarInterfazPrincipal();
        }
    }

    private boolean procesarIntentConexion(Intent intent) {
        if (intent == null) return false;
        boolean cambio = false;
        String srv = intent.getStringExtra("servidor_url");
        String tok = intent.getStringExtra("token_movil");
        String hud = intent.getStringExtra("token_hud");
        if (srv != null && !srv.trim().isEmpty()) {
            prefs.edit().putString("amadeus.servidor.url", srv.trim()).apply();
            cambio = true;
        }
        if (tok != null && !tok.trim().isEmpty()) {
            prefs.edit().putString("amadeus.movil.token", tok.trim()).apply();
            cambio = true;
        }
        if (hud != null && !hud.trim().isEmpty()) {
            prefs.edit().putString("amadeus.hud.token", hud.trim()).apply();
            cambio = true;
        }
        return cambio;
    }

    private void solicitarPermisosBase() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            boolean faltaAudio = checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED;
            boolean faltaCam = checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED;
            if (faltaAudio || faltaCam) {
                requestPermissions(new String[]{
                        Manifest.permission.RECORD_AUDIO,
                        Manifest.permission.CAMERA
                }, REQ_PERMISOS);
            }
        }
    }

    private byte[] leerBytesAsset(String rutaAsset) {
        String[] candidatos = new String[]{
                rutaAsset,
                rutaAsset.replace('/', '\\')
        };
        for (String cand : candidatos) {
            try {
                InputStream is = getAssets().open(cand);
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = is.read(buf)) != -1) {
                    baos.write(buf, 0, n);
                }
                is.close();
                return baos.toByteArray();
            } catch (Exception ignored) {}
        }
        return null;
    }

    private WebResourceResponse crearRespuesta(String mime, byte[] datos) {
        return new WebResourceResponse(
                mime,
                "UTF-8",
                200,
                "OK",
                crearCabecerasCors(),
                new ByteArrayInputStream(datos)
        );
    }

    private Map<String, String> crearCabecerasCors() {
        Map<String, String> headers = new HashMap<>();
        headers.put("Access-Control-Allow-Origin", "*");
        headers.put("Cache-Control", "no-store");
        return headers;
    }

    private WebResourceResponse servirVisor3dLocal() {
        byte[] raw = leerBytesAsset("vtuber/index.html");
        if (raw == null) {
            return crearRespuesta("text/html", "<html><body>Error cargando visor3d</body></html>".getBytes(StandardCharsets.UTF_8));
        }
        String html = new String(raw, StandardCharsets.UTF_8);
        File modeloUsuario = new File(getFilesDir(), ARCHIVO_VRM_USUARIO);
        String helloJson = (modeloUsuario.exists() && modeloUsuario.isFile())
                ? "{\"id\":\"apk\",\"viewers\":1,\"model\":\"" + ARCHIVO_VRM_USUARIO + "\"}"
                : "{\"id\":\"apk\",\"viewers\":1}";

        String cssFull = "<style>"
                + "html,body,body.obs{display:block!important;width:100%!important;height:100%!important;margin:0!important;padding:0!important;overflow:hidden!important}"
                + ".stage,#scene{position:absolute!important;inset:0!important;width:100%!important;height:100%!important}"
                + ".panel,.hud,.audio-hint{display:none!important}"
                + "</style>";
        String shim = cssFull + "<script>"
                + "(() => {"
                + "  window.EventSource = function() {"
                + "    const ev = new EventTarget();"
                + "    ev.close = () => {};"
                + "    setTimeout(() => {"
                + "      ev.dispatchEvent(new MessageEvent('hello', { data: '" + helloJson + "' }));"
                + "    }, 20);"
                + "    return ev;"
                + "  };"
                + "})();"
                + "</script>\n<script type=\"importmap\">";
        html = html.replaceFirst("<script type=\"importmap\">", shim);
        return crearRespuesta("text/html", html.getBytes(StandardCharsets.UTF_8));
    }

    private WebResourceResponse servirAsset(String rutaAsset, String mime) {
        byte[] datos = leerBytesAsset(rutaAsset);
        if (datos == null) {
            return crearRespuesta("text/plain", new byte[0]);
        }
        return crearRespuesta(mime, datos);
    }

    private String detectarMime(String nombre) {
        String n = nombre.toLowerCase(Locale.ROOT);
        if (n.endsWith(".html")) return "text/html";
        if (n.endsWith(".css")) return "text/css";
        if (n.endsWith(".js") || n.endsWith(".mjs")) return "text/javascript";
        if (n.endsWith(".json")) return "application/json";
        if (n.endsWith(".vrm") || n.endsWith(".glb")) return "model/gltf-binary";
        return "application/octet-stream";
    }

    private void guardarCopiaModeloLocal(Uri uri) {
        if (uri == null) return;
        try {
            InputStream is = getContentResolver().openInputStream(uri);
            if (is == null) return;
            File destino = new File(getFilesDir(), ARCHIVO_VRM_USUARIO);
            FileOutputStream fos = new FileOutputStream(destino);
            byte[] buf = new byte[16384];
            int n;
            while ((n = is.read(buf)) != -1) {
                fos.write(buf, 0, n);
            }
            fos.flush();
            fos.close();
            is.close();
        } catch (Exception ignored) {}
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_ARCHIVO_VRM) {
            if (callbackArchivo != null) {
                Uri[] resultados = null;
                if (resultCode == Activity.RESULT_OK && data != null && data.getData() != null) {
                    Uri elegido = data.getData();
                    guardarCopiaModeloLocal(elegido);
                    resultados = new Uri[]{elegido};
                }
                callbackArchivo.onReceiveValue(resultados);
                callbackArchivo = null;
            }
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (tts != null) {
            try {
                tts.stop();
                tts.shutdown();
            } catch (Exception ignored) {}
        }
        super.onDestroy();
    }

    public class PuenteNativo {
        @JavascriptInterface
        public void hablar(String texto) {
            if (ttsListo && tts != null && texto != null && !texto.trim().isEmpty()) {
                tts.speak(texto.trim(), TextToSpeech.QUEUE_FLUSH, null, "amadeus_utt");
            }
        }

        @JavascriptInterface
        public void callar() {
            if (ttsListo && tts != null) {
                tts.stop();
            }
        }

        @JavascriptInterface
        public void ajustarVoz(float pitch, float rate) {
            if (ttsListo && tts != null) {
                float p = Math.max(0.5f, Math.min(2.0f, pitch));
                float r = Math.max(0.5f, Math.min(2.0f, rate));
                tts.setPitch(p);
                tts.setSpeechRate(r);
            }
        }

        @JavascriptInterface
        public void vibrar(int ms) {
            try {
                Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
                if (v != null && v.hasVibrator()) {
                    int dur = Math.max(20, Math.min(ms, 2000));
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        v.vibrate(VibrationEffect.createOneShot(dur, VibrationEffect.DEFAULT_AMPLITUDE));
                    } else {
                        v.vibrate(dur);
                    }
                }
            } catch (Exception ignored) {}
        }

        @JavascriptInterface
        public void guardarConfig(String clave, String valor) {
            if (clave == null) return;
            prefs.edit().putString(clave, valor == null ? "" : valor).apply();
        }

        @JavascriptInterface
        public String leerConfig(String clave) {
            if (clave == null) return "";
            return prefs.getString(clave, "");
        }
    }
}
