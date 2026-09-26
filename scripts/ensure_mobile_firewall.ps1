param(
  [Parameter(Mandatory=$true)][string]$Program,
  [Parameter(Mandatory=$true)][int]$Port,
  [Parameter(Mandatory=$true)][string]$StatusPath
)
$ErrorActionPreference = 'Stop'
$Name = "EIRVEN Mobile LAN ($Port)"
function Mark([string]$Value) {
  $dir = Split-Path -Parent $StatusPath
  if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  [IO.File]::WriteAllText($StatusPath, $Value, [Text.UTF8Encoding]::new($false))
}
$existing = @(Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue)
foreach ($rule in $existing) {
  $pf = $rule | Get-NetFirewallPortFilter
  $af = $rule | Get-NetFirewallAddressFilter
  $app = $rule | Get-NetFirewallApplicationFilter
  # A Private-only rule is not sufficient when Windows classifies the current
  # Wi-Fi as Public (a common default on home networks).  Keep the scope narrow
  # to LocalSubnet.  A Private-only rule is valid when the active adapter is
  # actually classified Private; accepting it avoids a false UAC_REQUIRED result
  # on the common home-network setup.
  $profile = [string]$rule.Profile
  $profileReady = $profile -match '(?i)Any|Public|Domain'
  if (-not $profileReady -and $profile -match '(?i)Private') {
    $profileReady = @(
      Get-NetConnectionProfile -ErrorAction SilentlyContinue |
        Where-Object { $_.NetworkCategory -eq 'Private' -and $_.IPv4Connectivity -ne 'Disconnected' }
    ).Count -gt 0
  }
  if ($rule.Enabled -eq 'True' -and $rule.Action -eq 'Allow' -and
      $profileReady -and
      [string]$pf.Protocol -eq 'TCP' -and [string]$pf.LocalPort -eq [string]$Port -and
      [string]$af.RemoteAddress -match 'LocalSubnet' -and
      ((-not $app.Program) -or [string]$app.Program -eq 'Any' -or
       ([IO.Path]::GetFullPath($app.Program) -eq [IO.Path]::GetFullPath($Program)))) {
    Mark 'READY'; Write-Output 'EIRVEN-FIREWALL-READY'; exit 0
  }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Error 'UAC_REQUIRED'; exit 5
}
$existing | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $Name -Group 'EIRVEN' -Direction Inbound -Action Allow `
  -Enabled True -Profile Any -Protocol TCP -LocalPort $Port -Program $Program `
  -RemoteAddress LocalSubnet | Out-Null
Mark 'READY'
Write-Output 'EIRVEN-FIREWALL-READY'
exit 0
