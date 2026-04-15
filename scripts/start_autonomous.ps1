Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$candidates = @(
    (Join-Path $repoRoot ".venv312\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv312\bin\python"),
    (Join-Path $repoRoot ".venv\bin\python")
)

$pythonBin = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        $version = & $resolved -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -eq 0 -and $version -eq "3.12") {
            $pythonBin = $resolved
            break
        }
    }
}

if (-not $pythonBin) {
    throw "Python 3.12 was not found in .venv312 or .venv. Create a repo-local Python 3.12 virtual environment first."
}

$srcPath = Join-Path $repoRoot "src"
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $srcPath
} else {
    $env:PYTHONPATH = "$srcPath;$env:PYTHONPATH"
}

& $pythonBin -m findmyjob auto run --loop --workspace $repoRoot @Args
exit $LASTEXITCODE
