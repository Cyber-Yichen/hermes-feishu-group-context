[CmdletBinding()]
param(
    [string]$HermesHome = 'D:\env\hermes',
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pluginSource = Join-Path $projectRoot 'plugin'
$pluginTarget = Join-Path $HermesHome 'plugins\feishu-context-archive'
$repoRoot = Join-Path $HermesHome 'hermes-agent'
$adapterPath = Join-Path $repoRoot 'plugins\platforms\feishu\adapter.py'
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
$hermes = Join-Path $repoRoot 'venv\Scripts\hermes.exe'
$backupRoot = Join-Path $HermesHome 'backups\feishu-context-archive'

foreach ($required in @($pluginSource, $adapterPath, $python, $hermes)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

$env:HERMES_HOME = $HermesHome

& $python (Join-Path $projectRoot 'scripts\patch_adapter.py') $adapterPath --check
if ($LASTEXITCODE -ne 0) {
    throw 'Feishu adapter compatibility check failed.'
}

New-Item -ItemType Directory -Path (Split-Path -Parent $pluginTarget) -Force | Out-Null
if (Test-Path -LiteralPath $pluginTarget) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $pluginBackup = Join-Path $backupRoot "plugin-$stamp"
    New-Item -ItemType Directory -Path $pluginBackup -Force | Out-Null
    Copy-Item -LiteralPath $pluginTarget -Destination $pluginBackup -Recurse -Force
    Remove-Item -LiteralPath $pluginTarget -Recurse -Force
}
Copy-Item -LiteralPath $pluginSource -Destination $pluginTarget -Recurse -Force

& $python (Join-Path $projectRoot 'scripts\patch_adapter.py') $adapterPath --apply --backup-dir $backupRoot
if ($LASTEXITCODE -ne 0) {
    throw 'Feishu adapter patch failed.'
}

& $hermes plugins enable feishu-context-archive
if ($LASTEXITCODE -ne 0) {
    throw 'Could not enable feishu-context-archive.'
}

if (-not $NoRestart) {
    & $hermes gateway restart
    if ($LASTEXITCODE -ne 0) {
        throw 'Hermes gateway restart failed.'
    }
}

Write-Output "plugin=$pluginTarget"
Write-Output "archive=$(Join-Path $HermesHome 'archives\feishu_group_messages.sqlite3')"
