@echo off
chcp 65001 >nul
echo ============================================
echo   Xiaolong - Model Download
echo ============================================
echo.

cd /d "%~dp0"
if not exist models mkdir models
cd models

echo [1/2] Downloading SenseVoice ASR model...
if exist sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17\model.int8.onnx (
    echo   Already exists, skipping
) else (
    echo   Downloading... (~200MB)
    curl -L -O https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
    if errorlevel 1 (
        echo   Download failed! Try setting proxy:
        echo   set HTTPS_PROXY=http://127.0.0.1:7890
        goto :step2
    )
    tar -xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
    del sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
    echo   Done
)

:step2
echo.
echo [2/2] Downloading Silero VAD model...
if exist silero_vad.onnx (
    echo   Already exists, skipping
) else (
    echo   Downloading... (~2MB)
    curl -L -o silero_vad.onnx https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
    if errorlevel 1 (
        echo   Download failed!
    ) else (
        echo   Done
    )
)

echo.
echo [Check] Model files...
set OK=1
if not exist sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17\model.int8.onnx (
    echo   [MISSING] SenseVoice ASR model
    set OK=0
)
if not exist silero_vad.onnx (
    echo   [MISSING] Silero VAD model
    set OK=0
)
if "%OK%"=="1" (
    echo   All models ready
)

echo.
pause
