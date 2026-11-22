# Сборка лёгкого релиза ViralCut (Windows 10/11 x64) без CUDA DLL и вшитых моделей.
# Запуск: powershell -ExecutionPolicy Bypass -File build_windows_lite.ps1 -Tag fork-v1.4.1-1 -Version 1.4.1-fork.1
param(
    [string]$Tag = "fork-v1.4.3-3",
    [string]$Version = "1.4.3-fork.3"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[ViralCut Lite] Поиск Python 3.12..." -ForegroundColor Cyan
$py = $null
foreach ($candidate in @("py -3.12", "python")) {
    try {
        $ver = & cmd /c "$candidate -c `"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')`"" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -match "^3\.12") {
            $py = $candidate
            break
        }
    } catch { }
}
if (-not $py) { throw "Не найден Python 3.12 x64. Установите с https://www.python.org/downloads/windows/" }

if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    Write-Host "[ViralCut Lite] Создаю чистое окружение .venv-build..." -ForegroundColor Cyan
    & cmd /c "$py -m venv .venv-build"
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать .venv-build" }
}

$venvPy = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
$venvPip = Join-Path $PSScriptRoot ".venv-build\Scripts\pip.exe"
$venvPyInstaller = Join-Path $PSScriptRoot ".venv-build\Scripts\pyinstaller.exe"

& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить pip в .venv-build" }

& $venvPip install -r requirements-desktop.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости из requirements-desktop.txt" }

Write-Host "[ViralCut Lite] Прогоняю автотесты в .venv-build..." -ForegroundColor Cyan
& $venvPy -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Автотесты завершились с ошибкой" }

Write-Host "[ViralCut Lite] Сборка PyInstaller (ViralCut-lite.spec)..." -ForegroundColor Cyan
if (Test-Path "dist\ViralCut") { Remove-Item "dist\ViralCut" -Recurse -Force }
if (Test-Path "build\ViralCut-lite") { Remove-Item "build\ViralCut-lite" -Recurse -Force }
& $venvPyInstaller ViralCut-lite.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

Write-Host "[ViralCut Lite] Проверка отсутствия CUDA DLL и вшитой модели..." -ForegroundColor Cyan
$cudaHits = Get-ChildItem "dist\ViralCut" -Recurse -File -Include "*cublas*", "*cudnn*", "*nvrtc*", "*cudart*"
if ($cudaHits) {
    $names = ($cudaHits | ForEach-Object { $_.FullName }) -join ", "
    throw "В лёгкой сборке обнаружены лишние CUDA DLL: $names"
}
if (Test-Path "dist\ViralCut\_internal\models\whisper-small") {
    throw "В лёгкой сборке обнаружена вшитая модель whisper-small"
}

Write-Host "[ViralCut Lite] Headless smoke-test ViralCut.exe..." -ForegroundColor Cyan
$smokeData = Join-Path $env:TEMP ("vc-lite-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $smokeData | Out-Null
$prevHeadless = $env:VIRALCUT_HEADLESS
$prevData = $env:VIRALCUT_DATA
$prevPort = $env:VIRALCUT_PORT
$prevUpd = $env:VIRALCUT_UPDATE_CHECK
$proc = $null
try {
    $env:VIRALCUT_HEADLESS = "1"
    $env:VIRALCUT_DATA = $smokeData
    $env:VIRALCUT_PORT = "8799"
    $env:VIRALCUT_UPDATE_CHECK = "0"
    $proc = Start-Process -FilePath "dist\ViralCut\ViralCut.exe" -PassThru

    $healthOk = $false
    for ($i = 0; $i -lt 80; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8799/api/health" -TimeoutSec 2
            if ($resp.ok -eq $true -and $resp.whisper_model -eq "small") {
                $healthOk = $true
                break
            }
        } catch { }
    }
    if (-not $healthOk) {
        if (Test-Path "$smokeData\app.log") { Get-Content "$smokeData\app.log" }
        throw "Smoke-test не прошёл: /api/health не вернул ok=true, whisper_model=small"
    }

    $models = Invoke-RestMethod -Uri "http://127.0.0.1:8799/api/whisper/models" -TimeoutSec 3
    if ($models.active -ne "small" -or $models.models.Count -ne 3) {
        throw "Smoke-test: /api/whisper/models вернул некорректный каталог"
    }
    if (-not $models.active_language -or -not $models.languages) {
        throw "Smoke-test: /api/whisper/models не вернул active_language или languages"
    }

    $dbg = Invoke-RestMethod -Uri "http://127.0.0.1:8799/api/debug" -TimeoutSec 3
    if (-not $dbg.whisper) {
        throw "Smoke-test: /api/debug не вернул секцию whisper"
    }

    $jsResp = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8799/static/custom_whisper.js" -TimeoutSec 3
    if ($jsResp.StatusCode -ne 200) {
        throw "Smoke-test: /static/custom_whisper.js вернул код $($jsResp.StatusCode)"
    }

    $setResp = Invoke-RestMethod -Uri "http://127.0.0.1:8799/api/whisper/settings" -TimeoutSec 3
    if (-not $setResp.presets -or -not $setResp.effective) {
        throw "Smoke-test: /api/whisper/settings не вернул presets или effective"
    }

    $setJs = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8799/static/custom_settings.js" -TimeoutSec 3
    if ($setJs.StatusCode -ne 200) {
        throw "Smoke-test: /static/custom_settings.js вернул код $($setJs.StatusCode)"
    }
    Write-Host "[ViralCut Lite] Smoke-test пройден (small по умолчанию, 3 модели, пресеты оптимизации, UI статика ОК)." -ForegroundColor Green
} finally {
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 400
    }
    $env:VIRALCUT_HEADLESS = $prevHeadless
    $env:VIRALCUT_DATA = $prevData
    $env:VIRALCUT_PORT = $prevPort
    $env:VIRALCUT_UPDATE_CHECK = $prevUpd
    if (Test-Path $smokeData) {
        Remove-Item $smokeData -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$outDir = Join-Path $PSScriptRoot "release\$Tag"
New-Item -ItemType Directory -Force $outDir | Out-Null
$zipName = "ViralCut-$Version-windows-x64-lite.zip"
$zipPath = Join-Path $outDir $zipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Write-Host "[ViralCut Lite] Упаковываю $zipPath..." -ForegroundColor Cyan
Compress-Archive -Path "dist\ViralCut" -DestinationPath $zipPath -CompressionLevel Optimal

$hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower()
$sumsPath = Join-Path $outDir "SHA256SUMS.txt"
"$hash  $zipName" | Out-File -Encoding ascii $sumsPath

$sizeMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "Готово: $zipPath ($sizeMb МБ)" -ForegroundColor Green
Write-Host "SHA256: $sumsPath ($hash)" -ForegroundColor Green
