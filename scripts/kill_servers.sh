#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PRIMARY_PORT="${FMJ_PORT:-8765}"
TRACKED_PORTS=("$PRIMARY_PORT" 5173 3000 8080)
if [[ -n "${FMJ_EXTRA_PORTS:-}" ]]; then
  # shellcheck disable=SC2206
  EXTRA_PORTS=(${FMJ_EXTRA_PORTS})
  TRACKED_PORTS+=("${EXTRA_PORTS[@]}")
fi
if (($# > 0)); then
  TRACKED_PORTS+=("$@")
fi

declare -A PORT_MAP=()
declare -A PID_MAP=()

record_port() {
  local port="$1"
  [[ -n "$port" ]] || return 0
  [[ "$port" =~ ^[0-9]+$ ]] || return 0
  PORT_MAP["$port"]=1
}

record_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  if [[ "$pid" -gt 0 ]]; then
    PID_MAP["$pid"]=1
  fi
}

for port in "${TRACKED_PORTS[@]}"; do
  record_port "$port"
done

collect_pids_by_port() {
  if command -v powershell.exe >/dev/null 2>&1; then
    local port_csv
    local output
    port_csv="$(
      {
        first=1
        for port in "${!PORT_MAP[@]}"; do
          if [[ $first -eq 1 ]]; then
            printf '%s' "$port"
            first=0
          else
            printf ',%s' "$port"
          fi
        done
      }
    )"
    if [[ -n "$port_csv" ]]; then
      while IFS= read -r pid; do
        record_pid "$pid"
      done < <(
        FMJ_KILL_PORTS="$port_csv" powershell.exe -NoProfile -Command @'
$ports = @()
foreach ($rawPort in ($env:FMJ_KILL_PORTS -split "," | Where-Object { $_ })) {
  $parsedPort = 0
  if ([int]::TryParse($rawPort, [ref]$parsedPort)) {
    $ports += $parsedPort
  }
}
$connections = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $ports -contains $_.LocalPort } |
  Select-Object -ExpandProperty OwningProcess -Unique
foreach ($processId in $connections) {
  if ($processId) { Write-Output $processId }
}
'@ 2>/dev/null | tr -d '\r'
      )
    fi
  fi

  if command -v lsof >/dev/null 2>&1; then
    for port in "${!PORT_MAP[@]}"; do
      while IFS= read -r pid; do
        record_pid "$pid"
      done < <(lsof -ti :"$port" 2>/dev/null || true)
    done
  elif command -v netstat >/dev/null 2>&1; then
    for port in "${!PORT_MAP[@]}"; do
      while IFS= read -r pid; do
        record_pid "$pid"
      done < <(
        netstat -ano 2>/dev/null |
          awk -v port=":$port" '$0 ~ port && $0 ~ /LISTEN/ { print $NF }'
      )
    done
  fi
}

collect_pids_by_pattern() {
  if command -v powershell.exe >/dev/null 2>&1; then
    while IFS= read -r pid; do
      record_pid "$pid"
    done < <(
      powershell.exe -NoProfile -Command @'
$patterns = @(
  "findmyjob start",
  "fmj start",
  "uvicorn",
  "vite",
  "llama-server"
)
$processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
foreach ($process in $processes) {
  $name = ""
  if ($null -ne $process.Name) {
    $name = [string]$process.Name
  }
  $commandLine = ""
  if ($null -ne $process.CommandLine) {
    $commandLine = [string]$process.CommandLine
  }
  $line = ($name + " " + $commandLine).ToLowerInvariant()
  foreach ($pattern in $patterns) {
    if ($line.Contains($pattern)) {
      Write-Output $process.ProcessId
      break
    }
  }
}
'@ 2>/dev/null | tr -d '\r'
    )
  fi

  if command -v ps >/dev/null 2>&1; then
    while IFS= read -r pid; do
      record_pid "$pid"
    done < <(
      ps -ef 2>/dev/null |
        awk '
          BEGIN { IGNORECASE = 1 }
          /findmyjob start|fmj start|uvicorn|vite|llama-server/ && $0 !~ /awk/ { print $2 }
        '
    )
  fi
}

kill_pid() {
  local pid="$1"
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //F //T //PID "$pid" >/dev/null 2>&1 && return 0
  fi
  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" >/dev/null 2>&1 && return 0
  fi
  if command -v kill >/dev/null 2>&1; then
    kill -9 "$pid" >/dev/null 2>&1 && return 0
  fi
  return 1
}

print_remaining() {
  if command -v powershell.exe >/dev/null 2>&1; then
    local port_csv
    port_csv="$(
      {
        first=1
        for port in "${!PORT_MAP[@]}"; do
          if [[ $first -eq 1 ]]; then
            printf '%s' "$port"
            first=0
          else
            printf ',%s' "$port"
          fi
        done
      }
    )"
    output="$(
      FMJ_KILL_PORTS="$port_csv" powershell.exe -NoProfile -Command @'
$ports = @()
foreach ($rawPort in ($env:FMJ_KILL_PORTS -split "," | Where-Object { $_ })) {
  $parsedPort = 0
  if ([int]::TryParse($rawPort, [ref]$parsedPort)) {
    $ports += $parsedPort
  }
}
$connections = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $ports -contains $_.LocalPort } |
  Sort-Object LocalPort, OwningProcess
if (-not $connections) {
  Write-Output "none"
} else {
  $connections |
    Select-Object LocalAddress, LocalPort, OwningProcess |
    Format-Table -HideTableHeaders
}
'@ 2>/dev/null | tr -d '\r' || true
    )"
    if [[ -n "$output" ]]; then
      printf '%s\n' "$output"
    else
      echo "none"
    fi
    return 0
  fi

  if command -v lsof >/dev/null 2>&1; then
    local ports=()
    for port in "${!PORT_MAP[@]}"; do
      ports+=("-i" ":$port")
    done
    lsof "${ports[@]}" 2>/dev/null || echo "none"
    return 0
  fi

  if command -v netstat >/dev/null 2>&1; then
    local found=0
    for port in "${!PORT_MAP[@]}"; do
      local output
      output="$(netstat -ano 2>/dev/null | awk -v port=":$port" '$0 ~ port && $0 ~ /LISTEN/ { print }')"
      if [[ -n "$output" ]]; then
        printf '%s\n' "$output"
        found=1
      fi
    done
    if [[ "$found" -eq 0 ]]; then
      echo "none"
    fi
    return 0
  fi

  echo "unable to verify"
}

echo "Purging stale FindMyJob servers"
echo "Tracked ports: $(printf '%s ' "${!PORT_MAP[@]}" | sed 's/ $//')"

collect_pids_by_port
collect_pids_by_pattern

if [[ ${#PID_MAP[@]} -eq 0 ]]; then
  echo "No matching listeners or stale server processes found."
else
  echo "Killing PIDs: $(printf '%s ' "${!PID_MAP[@]}" | sed 's/ $//')"
  killed=0
  failed=0
  for pid in "${!PID_MAP[@]}"; do
    if kill_pid "$pid"; then
      echo "Killed PID $pid"
      killed=$((killed + 1))
    else
      echo "Failed to kill PID $pid"
      failed=$((failed + 1))
    fi
  done
  echo "Killed $killed process(es)."
  if [[ "$failed" -gt 0 ]]; then
    echo "$failed process(es) could not be terminated."
  fi
fi

echo "Remaining listeners on tracked ports:"
print_remaining
