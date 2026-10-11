# Local development against this machine's private setup: app data directory, training Python and
# eval source come from parameters or the usual RAPPORT_* variables; nothing is hard-coded here.
# Starts the backend on -Port (7860 by default) and Vite on 5173 with its /api proxy pointed at it.
#   scripts\dev-local.ps1 -DataDir <data-dir> -TrainingPython <train-venv>\Scripts\python.exe -Port 7861
param(
    [int]$Port = 7860,
    [string]$DataDir = $env:RAPPORT_DATA_DIR,
    [string]$TrainingPython = $env:RAPPORT_TRAINING_PYTHON,
    [string]$EvalSource = $env:RAPPORT_EVAL_SOURCE
)
$ErrorActionPreference = "Stop"
if (-not $DataDir) { throw "Give -DataDir or set RAPPORT_DATA_DIR: without it the app silently uses ~/.rapport." }
$root = Split-Path -Parent $PSScriptRoot
$env:RAPPORT_DATA_DIR = $DataDir
if ($TrainingPython) { $env:RAPPORT_TRAINING_PYTHON = $TrainingPython }
if ($EvalSource) { $env:RAPPORT_EVAL_SOURCE = $EvalSource }
$env:RAPPORT_PORT = "$Port"
$env:RAPPORT_DEV_CORS_ORIGIN = "http://127.0.0.1:5173"
$env:RAPPORT_PAIRING_CODE = "123456"
Start-Process -NoNewWindow -WorkingDirectory "$root\backend" -FilePath "uv" -ArgumentList "run", "rapport", "serve", "--port", "$Port"
Set-Location "$root\frontend"
npm run dev
