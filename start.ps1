param(
    [switch]$NoOpen,
    [switch]$SkipFrontendBuild,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

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

function Resolve-BootstrapPython([string]$RepoRoot) {
    $bootstrapScript = Join-Path $RepoRoot "scripts\bootstrap_env.py"
    $candidates = @(
        @{ Command = "py"; PrefixArgs = @("-3.12") },
        @{ Command = "python"; PrefixArgs = @() },
        @{ Command = "python3.12"; PrefixArgs = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) {
            continue
        }
        & $candidate.Command @($candidate.PrefixArgs + @("-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)")) 2>$null
        if ($LASTEXITCODE -ne 0) {
            continue
        }
        return @($candidate.Command) + $candidate.PrefixArgs + @($bootstrapScript, "--project-root", $RepoRoot)
    }

    throw "Python 3.12 was not found. Install Python 3.12 or create .venv312 manually before launching FindMyJob."
}

function Ensure-RepoEnvironment([string]$RepoRoot) {
    $bootstrapScript = Join-Path $RepoRoot "scripts\bootstrap_env.py"
    try {
        $repoPython = Resolve-Python $RepoRoot
        & $repoPython $bootstrapScript --project-root $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Repo-local bootstrap failed."
        }
        return (Resolve-Python $RepoRoot)
    } catch {
        if ($_ -match "Unsupported Python") {
            throw
        }
    }

    $bootstrapCommand = Resolve-BootstrapPython $RepoRoot
    & $bootstrapCommand[0] @($bootstrapCommand[1..($bootstrapCommand.Length - 1)])
    if ($LASTEXITCODE -ne 0) {
        throw "Automatic repo-local bootstrap failed. Create .venv312 manually or run scripts\bootstrap_env.py with Python 3.12."
    }
    return (Resolve-Python $RepoRoot)
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
$script:PythonBin = Ensure-RepoEnvironment $repoRoot
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

& $PythonBin $preflightScript --workspace $repoRoot 2>&1 | Out-Null
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

$buildArguments = @(
    "-m", "findmyjob", "build",
    "--workspace", $repoRoot
)
if ($SkipFrontendBuild) {
    $buildArguments += "--skip-frontend-build"
}

& $PythonBin @buildArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
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
