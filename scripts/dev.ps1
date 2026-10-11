# Dev helper (Windows PowerShell): start backend on 7860 and Vite on 5173 with CORS enabled.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:RAPPORT_DEV_CORS_ORIGIN = "http://127.0.0.1:5173"
$env:RAPPORT_PAIRING_CODE = "123456"
Start-Process -NoNewWindow -WorkingDirectory "$root\backend" -FilePath "uv" -ArgumentList "run", "rapport", "serve"
Set-Location "$root\frontend"
npm run dev
