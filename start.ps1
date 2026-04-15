param(
    [switch]$NoOpen,
    [switch]$SkipFrontendBuild,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Python([string]$RepoRoot) {
    $candidates = @(
        (Join-Path $RepoRoot ".venv312\Scripts\python.exe"),
        (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
        (Join-Path $RepoRoot ".venv312\bin\python"),
        (Join-Path $RepoRoot ".venv\bin\python")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            $version = & $resolved -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($LASTEXITCODE -eq 0 -and $version -eq "3.12") {
                return $resolved
            }
            throw "Unsupported Python at $resolved. FindMyJob requires a repo-local Python 3.12 virtual environment."
        }
    }
    throw "Python 3.12 was not found in .venv312 or .venv. Create a repo-local Python 3.12 virtual environment first."
}

function Test-TruthyEnv([string]$Value) {
    if ($null -eq $Value) {
        return $false
    }
    return @("1", "true", "yes") -contains $Value.Trim().ToLowerInvariant()
}

function Test-PortAvailable([string]$BindHost, [int]$BindPort) {
    @'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
family = socket.AF_INET6 if ":" in host and host != "0.0.0.0" else socket.AF_INET
bind_target = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
with socket.socket(family, socket.SOCK_STREAM) as probe:
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(bind_target)
    except OSError:
        raise SystemExit(1)
'@ | & $script:PythonBin - $BindHost $BindPort
    return $LASTEXITCODE -eq 0
}

$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$script:PythonBin = Resolve-Python $repoRoot
$preflightScript = Join-Path $repoRoot "scripts\lmstudio_preflight.py"

$bindHost = if ($env:FMJ_HOST) { $env:FMJ_HOST } else { "127.0.0.1" }
$bindPort = if ($env:FMJ_PORT) { [int]$env:FMJ_PORT } else { 8765 }
$page = if ($env:FMJ_PAGE) { [string]$env:FMJ_PAGE } else { "" }
$openBrowser = -not $NoOpen
if (Test-TruthyEnv $env:FMJ_OPEN_BROWSER) {
    $openBrowser = $true
} elseif ($null -ne $env:FMJ_OPEN_BROWSER -and -not (Test-TruthyEnv $env:FMJ_OPEN_BROWSER)) {
    $openBrowser = $false
}

$requireLmStudioPreflight = Test-TruthyEnv $env:FMJ_REQUIRE_LMSTUDIO_PREFLIGHT

& $PythonBin $preflightScript --workspace $repoRoot
if ($LASTEXITCODE -ne 0) {
    if ($requireLmStudioPreflight) {
        throw "LM Studio preflight failed. Start LM Studio, load the configured models, then retry."
    }
    Write-Warning "LM Studio preflight failed. Starting the web console anyway so readiness, settings, and review pages stay available."
}

if (-not (Test-PortAvailable -BindHost $bindHost -BindPort $bindPort)) {
    throw "Port $bindPort is already in use on $bindHost. Stop the previous backend before starting a new run. Run .\scripts\kill_servers.ps1 first."
}

if ($SkipFrontendBuild) {
    $env:SKIP_FRONTEND_BUILD = "1"
}

$existingPythonPath = [string]$env:PYTHONPATH
$srcPath = Join-Path $repoRoot "src"
if ([string]::IsNullOrWhiteSpace($existingPythonPath)) {
    $env:PYTHONPATH = $srcPath
} else {
    $env:PYTHONPATH = "$srcPath;$existingPythonPath"
}

$arguments = @(
    "-m", "findmyjob", "start",
    "--workspace", $repoRoot,
    "--host", $bindHost,
    "--port", [string]$bindPort
)

if (-not $openBrowser) {
    $arguments += "--no-open"
}
if (-not [string]::IsNullOrWhiteSpace($page)) {
    $arguments += @("--page", $page)
}
if ($ForwardArgs) {
    $arguments += @($ForwardArgs | Where-Object { $_ -ne $null -and [string]$_ -ne "" })
}

& $PythonBin @arguments
exit $LASTEXITCODE
