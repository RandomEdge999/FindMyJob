param(
    [string]$InstallRoot,
    [string]$RepoUrl = "https://github.com/RandomEdge999/FindMyJob.git",
    [string]$Branch = "main",
    [switch]$NoLaunch,
    [switch]$NoOpen,
    [switch]$SkipFrontendBuild,
    [switch]$NoPathUpdate,
    [switch]$ForceArchive,
    [switch]$Yes,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-DefaultInstallRoot() {
    $base = [Environment]::GetFolderPath("LocalApplicationData")
    if ([string]::IsNullOrWhiteSpace($base)) {
        throw "LOCALAPPDATA is unavailable."
    }
    return Join-Path $base "Programs\FindMyJob"
}

function Get-ManagedRepoRoot([string]$Root) {
    return Join-Path $Root "repo"
}

function Get-ManagedBinRoot([string]$Root) {
    return Join-Path $Root "bin"
}

function Get-ManagedMetadataPath([string]$Root) {
    return Join-Path $Root "install-metadata.json"
}

function Test-WindowsHost() {
    if (($env:OS | Out-String).Trim() -ne "Windows_NT") {
        throw "FindMyJob managed install currently supports Windows only."
    }
}

function Test-CommandAvailable([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Resolve-Python312() {
    $candidates = @(
        @{ Command = "py"; PrefixArgs = @("-3.12") },
        @{ Command = "python"; PrefixArgs = @() },
        @{ Command = "python3.12"; PrefixArgs = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Test-CommandAvailable $candidate.Command)) {
            continue
        }
        & $candidate.Command @($candidate.PrefixArgs + @("-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)")) 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @($candidate.Command) + $candidate.PrefixArgs
        }
    }

    throw "Python 3.12 is required for FindMyJob. Install Python 3.12 first, then rerun this installer."
}

function New-BackupPath([string]$InstallRoot, [string]$LeafName) {
    $backupRoot = Join-Path $InstallRoot "backups"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    return Join-Path $backupRoot ("{0}-{1}" -f $LeafName, $timestamp)
}

function Move-DirectoryAside([string]$PathToMove, [string]$InstallRoot, [string]$LeafName) {
    if (-not (Test-Path -LiteralPath $PathToMove)) {
        return
    }
    $backupPath = New-BackupPath -InstallRoot $InstallRoot -LeafName $LeafName
    Move-Item -LiteralPath $PathToMove -Destination $backupPath -Force
    Write-Host "Moved existing managed checkout to $backupPath"
}

function Get-CanonicalRepoUrl([string]$Value) {
    $trimmed = ($Value | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        return ""
    }

    if ($trimmed -match '^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$') {
        return ("https://github.com/{0}/{1}" -f $Matches[1], $Matches[2]).ToLowerInvariant()
    }
    if ($trimmed -match '^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$') {
        return ("https://github.com/{0}/{1}" -f $Matches[1], $Matches[2]).ToLowerInvariant()
    }

    try {
        $uri = [System.Uri]$trimmed
        if ($uri.IsAbsoluteUri) {
            if ($uri.Scheme -eq "file") {
                return [System.IO.Path]::GetFullPath($uri.LocalPath).TrimEnd('\\').ToLowerInvariant()
            }
            $normalized = $uri.AbsoluteUri.TrimEnd('/')
            if ($normalized.EndsWith('.git', [System.StringComparison]::OrdinalIgnoreCase)) {
                $normalized = $normalized.Substring(0, $normalized.Length - 4)
            }
            return $normalized.ToLowerInvariant()
        }
    } catch {
    }

    try {
        return [System.IO.Path]::GetFullPath($trimmed).TrimEnd('\\').ToLowerInvariant()
    } catch {
    }

    $fallback = $trimmed.TrimEnd('/')
    if ($fallback.EndsWith('.git', [System.StringComparison]::OrdinalIgnoreCase)) {
        $fallback = $fallback.Substring(0, $fallback.Length - 4)
    }
    return $fallback.ToLowerInvariant()
}

function Sync-RepoWithGit([string]$RepoRoot, [string]$OriginUrl, [string]$BranchName, [string]$InstallRoot) {
    $gitDir = Join-Path $RepoRoot ".git"
    if (-not (Test-Path -LiteralPath $RepoRoot)) {
        & git clone --depth 1 --branch $BranchName $OriginUrl $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Git clone failed."
        }
        return
    }

    if (-not (Test-Path -LiteralPath $gitDir)) {
        Move-DirectoryAside -PathToMove $RepoRoot -InstallRoot $InstallRoot -LeafName "repo"
        & git clone --depth 1 --branch $BranchName $OriginUrl $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Git clone failed after replacing the existing managed directory."
        }
        return
    }

    $remoteUrlLines = @(& git -C $RepoRoot remote get-url origin 2>$null)
    $remoteExitCode = $LASTEXITCODE
    $remoteUrl = [string]::Join("`n", $remoteUrlLines).Trim()
    if ($remoteExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($remoteUrl)) {
        Move-DirectoryAside -PathToMove $RepoRoot -InstallRoot $InstallRoot -LeafName "repo"
        & git clone --depth 1 --branch $BranchName $OriginUrl $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Git clone failed after replacing the managed checkout with missing origin metadata."
        }
        return
    }

    $workingTreeDirtyLines = @(& git -C $RepoRoot status --porcelain --untracked-files=no 2>$null)
    $statusExitCode = $LASTEXITCODE
    $workingTreeDirty = [string]::Join("`n", $workingTreeDirtyLines)
    $urlMismatch = (Get-CanonicalRepoUrl $remoteUrl) -ne (Get-CanonicalRepoUrl $OriginUrl)
    $shouldReplaceRepo = ($statusExitCode -ne 0) -or (-not [string]::IsNullOrWhiteSpace($workingTreeDirty)) -or $urlMismatch
    if ($shouldReplaceRepo) {
        Move-DirectoryAside -PathToMove $RepoRoot -InstallRoot $InstallRoot -LeafName "repo"
        & git clone --depth 1 --branch $BranchName $OriginUrl $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Git clone failed after replacing the managed checkout."
        }
        return
    }

    Push-Location $RepoRoot
    try {
        & git fetch --depth 1 origin $BranchName
        if ($LASTEXITCODE -ne 0) {
            throw "Git fetch failed."
        }

        $currentBranchLines = @(& git rev-parse --abbrev-ref HEAD 2>$null)
        $currentBranch = [string]::Join("`n", $currentBranchLines).Trim()
        if ([string]::IsNullOrWhiteSpace($currentBranch)) {
            & git checkout -B $BranchName FETCH_HEAD
        } elseif ($currentBranch -ne $BranchName) {
            & git checkout $BranchName
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Git checkout failed."
        }

        & git merge --ff-only FETCH_HEAD
        if ($LASTEXITCODE -ne 0) {
            throw "Git fast-forward update failed."
        }
    } finally {
        Pop-Location
    }
}

function Get-ArchiveUrl([string]$OriginUrl, [string]$BranchName) {
    $trimmed = $OriginUrl.Trim()
    if ($trimmed -match '^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$') {
        return "https://codeload.github.com/$($Matches[1])/$($Matches[2])/zip/refs/heads/$BranchName"
    }
    throw "Archive fallback only supports GitHub HTTPS repository URLs. Install Git or use a GitHub HTTPS repo URL."
}

function Sync-RepoFromArchive([string]$RepoRoot, [string]$OriginUrl, [string]$BranchName, [string]$InstallRoot) {
    $archiveUrl = Get-ArchiveUrl -OriginUrl $OriginUrl -BranchName $BranchName
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("findmyjob-install-" + [guid]::NewGuid().ToString("N"))
    $archivePath = Join-Path $tempRoot "repo.zip"
    $extractRoot = Join-Path $tempRoot "expanded"
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $archivePath
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
        $expandedRepo = Get-ChildItem -LiteralPath $extractRoot -Directory | Select-Object -First 1
        if ($null -eq $expandedRepo) {
            throw "Archive download did not contain a repository directory."
        }

        Move-DirectoryAside -PathToMove $RepoRoot -InstallRoot $InstallRoot -LeafName "repo"
        New-Item -ItemType Directory -Path (Split-Path -Parent $RepoRoot) -Force | Out-Null
        Move-Item -LiteralPath $expandedRepo.FullName -Destination $RepoRoot -Force
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-InstallMetadata([string]$InstallRoot, [string]$RepoRoot, [string]$RepoUrl, [string]$BranchName, [string]$Mode) {
    $payload = [ordered]@{
        install_root = $InstallRoot
        repo_root = $RepoRoot
        repo_url = $RepoUrl
        branch = $BranchName
        mode = $Mode
        updated_at = (Get-Date).ToString("o")
    }
    $metadataPath = Get-ManagedMetadataPath -Root $InstallRoot
    $payload | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
}

function Set-UserPathEntry([string]$BinRoot) {
    $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $segments = @()
    if (-not [string]::IsNullOrWhiteSpace($currentUserPath)) {
        $segments = $currentUserPath.Split(';', [System.StringSplitOptions]::RemoveEmptyEntries)
    }
    $alreadyPresent = $segments | Where-Object { $_.TrimEnd('\\') -ieq $BinRoot.TrimEnd('\\') }
    if ($alreadyPresent) {
        if ($env:Path -notmatch [regex]::Escape($BinRoot)) {
            $env:Path = "$BinRoot;$env:Path"
        }
        return $false
    }

    $newSegments = @($BinRoot) + $segments
    $newPath = ($newSegments -join ';').Trim(';')
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$BinRoot;$env:Path"
    return $true
}

function Write-LauncherFiles([string]$InstallRoot, [string]$RepoRoot, [string]$RepoUrl, [string]$BranchName) {
    $binRoot = Get-ManagedBinRoot -Root $InstallRoot
    New-Item -ItemType Directory -Path $binRoot -Force | Out-Null

    $startScript = Join-Path $RepoRoot "start.ps1"
    $installScript = Join-Path $RepoRoot "install.ps1"
    $escapedStartScript = $startScript.Replace('"', '""')
    $escapedInstallScript = $installScript.Replace('"', '""')
    $escapedInstallRoot = $InstallRoot.Replace('"', '""')
    $escapedRepoUrl = $RepoUrl.Replace('"', '""')
    $escapedBranch = $BranchName.Replace('"', '""')

    $launchCmd = @"
@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "$escapedStartScript" %*
exit /b %ERRORLEVEL%
"@
    Set-Content -LiteralPath (Join-Path $binRoot "findmyjob.cmd") -Value $launchCmd -Encoding ASCII

    $updateCmd = @"
@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "$escapedInstallScript" -InstallRoot "$escapedInstallRoot" -RepoUrl "$escapedRepoUrl" -Branch "$escapedBranch" -NoLaunch %*
exit /b %ERRORLEVEL%
"@
    Set-Content -LiteralPath (Join-Path $binRoot "findmyjob-update.cmd") -Value $updateCmd -Encoding ASCII

    $launchPs1 = @"
param(
    [Parameter(ValueFromRemainingArguments = `$true)]
    [string[]]`$ForwardedArgs
)

& "$escapedStartScript" @ForwardedArgs
exit `$LASTEXITCODE
"@
    Set-Content -LiteralPath (Join-Path $binRoot "findmyjob.ps1") -Value $launchPs1 -Encoding UTF8

    $updatePs1 = @"
param(
    [Parameter(ValueFromRemainingArguments = `$true)]
    [string[]]`$ForwardedArgs
)

& "$escapedInstallScript" -InstallRoot "$escapedInstallRoot" -RepoUrl "$escapedRepoUrl" -Branch "$escapedBranch" -NoLaunch @ForwardedArgs
exit `$LASTEXITCODE
"@
    Set-Content -LiteralPath (Join-Path $binRoot "findmyjob-update.ps1") -Value $updatePs1 -Encoding UTF8
}

function Invoke-FindMyJobLaunch([string]$RepoRoot, [switch]$SuppressBrowserOpen, [switch]$SuppressFrontendBuild, [string[]]$Arguments) {
    $startScript = Join-Path $RepoRoot "start.ps1"
    $commandArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $startScript)
    if ($SuppressBrowserOpen) {
        $commandArgs += "-NoOpen"
    }
    if ($SuppressFrontendBuild) {
        $commandArgs += "-SkipFrontendBuild"
    }
    if ($Arguments) {
        $commandArgs += @($Arguments | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }
    & powershell @commandArgs
    if ($LASTEXITCODE -ne 0) {
        throw "FindMyJob launch failed."
    }
}

Test-WindowsHost
Resolve-Python312 | Out-Null

if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $defaultRoot = Get-DefaultInstallRoot
    $isInteractive = (-not $Yes) -and ([Environment]::UserInteractive) -and ([System.Console]::IsInputRedirected -eq $false)
    if ($isInteractive) {
        $reply = Read-Host ("Install FindMyJob into [{0}]" -f $defaultRoot)
        if ([string]::IsNullOrWhiteSpace($reply)) {
            $InstallRoot = $defaultRoot
        } else {
            $InstallRoot = $reply
        }
    } else {
        $InstallRoot = $defaultRoot
    }
}

$resolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$repoRoot = Get-ManagedRepoRoot -Root $resolvedInstallRoot
New-Item -ItemType Directory -Path $resolvedInstallRoot -Force | Out-Null

$syncMode = "git"
if ($ForceArchive -or -not (Test-CommandAvailable "git")) {
    $syncMode = "archive"
}

Write-Host "Installing FindMyJob into $resolvedInstallRoot"
if ($syncMode -eq "git") {
    Sync-RepoWithGit -RepoRoot $repoRoot -OriginUrl $RepoUrl -BranchName $Branch -InstallRoot $resolvedInstallRoot
} else {
    Sync-RepoFromArchive -RepoRoot $repoRoot -OriginUrl $RepoUrl -BranchName $Branch -InstallRoot $resolvedInstallRoot
}

Write-InstallMetadata -InstallRoot $resolvedInstallRoot -RepoRoot $repoRoot -RepoUrl $RepoUrl -BranchName $Branch -Mode $syncMode
Write-LauncherFiles -InstallRoot $resolvedInstallRoot -RepoRoot $repoRoot -RepoUrl $RepoUrl -BranchName $Branch

$binRoot = Get-ManagedBinRoot -Root $resolvedInstallRoot
$pathChanged = $false
if (-not $NoPathUpdate) {
    $pathChanged = Set-UserPathEntry -BinRoot $binRoot
}

Write-Host "Managed repo: $repoRoot"
Write-Host "Launch command: findmyjob"
Write-Host "Update command: findmyjob-update"

if ($pathChanged) {
    Write-Host "Added $binRoot to the user PATH. Open a new terminal to use `findmyjob` by name."
} elseif ($NoPathUpdate) {
    Write-Host "PATH was not modified. Launch manually with $binRoot\findmyjob.cmd"
}

if (-not $NoLaunch) {
    Invoke-FindMyJobLaunch -RepoRoot $repoRoot -SuppressBrowserOpen:$NoOpen -SuppressFrontendBuild:$SkipFrontendBuild -Arguments $ForwardArgs
}