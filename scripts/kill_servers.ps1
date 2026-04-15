param(
    [int[]]$Ports = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$trackedPorts = [System.Collections.Generic.List[int]]::new()
$primaryPort = 8765
if ($env:FMJ_PORT) {
    $parsedPrimaryPort = 0
    if ([int]::TryParse($env:FMJ_PORT, [ref]$parsedPrimaryPort)) {
        $primaryPort = $parsedPrimaryPort
    }
}
$trackedPorts.Add($primaryPort)
foreach ($port in @(5173, 3000, 8080)) {
    if (-not $trackedPorts.Contains($port)) {
        $trackedPorts.Add($port)
    }
}
foreach ($port in $Ports) {
    if (-not $trackedPorts.Contains($port)) {
        $trackedPorts.Add($port)
    }
}
if ($env:FMJ_EXTRA_PORTS) {
    foreach ($rawPort in ($env:FMJ_EXTRA_PORTS -split '[,\s]+' | Where-Object { $_ })) {
        $parsedPort = 0
        if ([int]::TryParse($rawPort, [ref]$parsedPort) -and -not $trackedPorts.Contains($parsedPort)) {
            $trackedPorts.Add($parsedPort)
        }
    }
}

$pidMap = @{}

foreach ($connection in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $trackedPorts -contains $_.LocalPort })) {
    if ($connection.OwningProcess -gt 0) {
        $pidMap[$connection.OwningProcess] = $true
    }
}

$patterns = @(
    'findmyjob start',
    'fmj start',
    'uvicorn',
    'vite',
    'llama-server'
)
foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    $name = ''
    if ($null -ne $process.Name) {
        $name = [string]$process.Name
    }
    $commandLine = ''
    if ($null -ne $process.CommandLine) {
        $commandLine = [string]$process.CommandLine
    }
    $line = ($name + ' ' + $commandLine).ToLowerInvariant()
    foreach ($pattern in $patterns) {
        if ($line.Contains($pattern)) {
            $pidMap[$process.ProcessId] = $true
            break
        }
    }
}

Write-Output 'Purging stale FindMyJob servers'
Write-Output ("Tracked ports: " + (($trackedPorts | Sort-Object -Unique) -join ', '))

if ($pidMap.Count -eq 0) {
    Write-Output 'No matching listeners or stale server processes found.'
} else {
    foreach ($processId in ($pidMap.Keys | Sort-Object)) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Output "Killed PID $processId"
        } catch {
            Write-Output "Failed to kill PID $processId"
        }
    }
}

Write-Output 'Remaining listeners on tracked ports:'
$remaining = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $trackedPorts -contains $_.LocalPort } | Sort-Object LocalPort, OwningProcess)
if ($remaining.Count -eq 0) {
    Write-Output 'none'
} else {
    $remaining |
        Select-Object LocalAddress, LocalPort, OwningProcess |
        Format-Table -HideTableHeaders
}
