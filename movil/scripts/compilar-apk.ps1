# Compilador e instalador de la App Nativa Android "Amadeus" (com.rams.amadeus)
# Usa las herramientas oficiales del Android SDK + JBR locales sin descargas externas.
$ErrorActionPreference = "Stop"

$Raiz = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Sdk = "$env:LOCALAPPDATA\Android\Sdk"
$BuildTools = "$Sdk\build-tools\36.1.0"
$AndroidJar = "$Sdk\platforms\android-36.1\android.jar"
$Adb = "$Sdk\platform-tools\adb.exe"
$JbrBin = "C:\Program Files\Android\Android Studio\jbr\bin"
$PythonExe = Join-Path $Raiz ".venv\Scripts\python.exe"

$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:PATH = "$JbrBin;$BuildTools;$Sdk\platform-tools;$env:PATH"

$TmpBuild = Join-Path $env:TEMP "amadeus_apk_build"
if (Test-Path $TmpBuild) {
    Remove-Item -Recurse -Force $TmpBuild
}
New-Item -ItemType Directory -Force -Path "$TmpBuild\gen" | Out-Null
New-Item -ItemType Directory -Force -Path "$TmpBuild\obj" | Out-Null
New-Item -ItemType Directory -Force -Path "$TmpBuild\dex" | Out-Null
New-Item -ItemType Directory -Force -Path "$TmpBuild\assets\movil" | Out-Null
New-Item -ItemType Directory -Force -Path "$TmpBuild\assets\vtuber" | Out-Null

Write-Host "[1/6] Copiando interfaz Amadeus Movil y motor 3D VTuber-AI (con vendor offline) a assets..."
$VtuberRepo = Join-Path (Split-Path $Raiz -Parent) "vtuber-ai"
if (-not (Test-Path "$VtuberRepo\public\vendor")) {
    $VtuberRepo = "$Raiz\vtuber-ai"
}
Copy-Item "$Raiz\movil\public\*" "$TmpBuild\assets\movil\" -Recurse -Force
Copy-Item "$VtuberRepo\public\*" "$TmpBuild\assets\vtuber\" -Recurse -Force
Copy-Item "$VtuberRepo\lib\protocol.js" "$TmpBuild\assets\vtuber\protocol.js" -Force

$SampleCache = Join-Path $env:TEMP "amadeus_sample_cache.vrm"
if (-not (Test-Path $SampleCache)) {
    try {
        Write-Host "      Descargando modelo VRM base para uso 100% offline..."
        Invoke-WebRequest -Uri "https://cdn.jsdelivr.net/gh/pixiv/three-vrm@v3.4.4/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm" -OutFile $SampleCache -UseBasicParsing
    } catch {
        Write-Warning "No se pudo descargar sample.vrm: $_"
    }
}
if (Test-Path $SampleCache) {
    New-Item -ItemType Directory -Force -Path "$TmpBuild\assets\vtuber\vendor" | Out-Null
    Copy-Item $SampleCache "$TmpBuild\assets\vtuber\vendor\sample.vrm" -Force
}

Write-Host "[2/6] Empaquetando manifiesto Android con aapt2..."
& "$BuildTools\aapt2.exe" link `
    -o "$TmpBuild\unaligned.apk" `
    --manifest "$Raiz\movil\android\AndroidManifest.xml" `
    -I "$AndroidJar" `
    --java "$TmpBuild\gen"

Write-Host "[3/6] Compilando AmadeusActivity.java (bytecode Java 11)..."
$JavaFiles = @(Get-ChildItem -Path "$Raiz\movil\android\src", "$TmpBuild\gen" -Filter "*.java" -Recurse | Select-Object -ExpandProperty FullName)
& "$JbrBin\javac.exe" `
    --release 11 `
    -encoding UTF-8 `
    -cp "$AndroidJar" `
    -d "$TmpBuild\obj" `
    $JavaFiles

Write-Host "[4/6] Convirtiendo clases a Dalvik DEX y empaquetando assets con barras POSIX '/'..."
& "$JbrBin\jar.exe" cf "$TmpBuild\classes.jar" -C "$TmpBuild\obj" .
& "$BuildTools\d8.bat" "$TmpBuild\classes.jar" --lib "$AndroidJar" --min-api 26 --output "$TmpBuild\dex"

& $PythonExe -c @"
import zipfile
from pathlib import Path

tmp = Path(r'$TmpBuild')
apk = tmp / 'unaligned.apk'
assets_dir = tmp / 'assets'
dex_file = tmp / 'dex' / 'classes.dex'

with zipfile.ZipFile(apk, 'a', compression=zipfile.ZIP_DEFLATED) as zf:
    zf.write(dex_file, 'classes.dex')
    for f in sorted(assets_dir.rglob('*')):
        if f.is_file():
            rel = f.relative_to(assets_dir).as_posix()
            zf.write(f, f'assets/{rel}')
"@

Write-Host "[5/6] Alineando y firmando APK..."
& "$BuildTools\zipalign.exe" -f -p 4 "$TmpBuild\unaligned.apk" "$TmpBuild\aligned.apk"

$Keystore = Join-Path $env:TEMP "amadeus_debug.keystore"
if (-not (Test-Path $Keystore)) {
    & "$JbrBin\keytool.exe" -genkeypair `
        -keystore "$Keystore" `
        -storepass android `
        -keypass android `
        -alias androiddebugkey `
        -keyalg RSA `
        -keysize 2048 `
        -validity 10000 `
        -dname "CN=Amadeus Mobile,O=RAMS BioCore,C=CO"
}

$ApkFinal = Join-Path $TmpBuild "AmadeusMovil.apk"
& "$BuildTools\apksigner.bat" sign `
    --ks "$Keystore" `
    --ks-pass pass:android `
    --key-pass pass:android `
    --out "$ApkFinal" `
    "$TmpBuild\aligned.apk"

$DocsDir = [Environment]::GetFolderPath("MyDocuments")
if ($DocsDir -and (Test-Path $DocsDir)) {
    Copy-Item -Path $ApkFinal -Destination (Join-Path $DocsDir "AmadeusMovil.apk") -Force
}

Write-Host "[6/6] Enviando e instalando AmadeusMovil.apk en tu POCO X6..."
if (Test-Path $Adb) {
    & $Adb push "$ApkFinal" "/sdcard/Download/AmadeusMovil.apk" | Out-Null
    & $Adb reverse tcp:8767 tcp:8767 | Out-Null
    $inst = & $Adb install -r "$ApkFinal" 2>&1
    if ($LASTEXITCODE -eq 0) {
        & $Adb shell am start -n com.rams.amadeus/.AmadeusActivity
        Write-Host ""
        Write-Host "================================================================="
        Write-Host "  APK NATIVO ACTUALIZADO Y ABIERTO EN TU POCO X6 (App: Amadeus)"
        Write-Host "================================================================="
    } else {
        Write-Host ""
        Write-Host "================================================================="
        Write-Host "  APK ACTUALIZADO EN: $DocsDir\AmadeusMovil.apk"
        Write-Host "================================================================="
    }
} else {
    Write-Host "APK generado en: $ApkFinal"
}
