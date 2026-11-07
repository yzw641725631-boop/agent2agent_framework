#requires -Version 5.1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\install-windows.ps1')
Add-Type -AssemblyName System.Net.Http

function Assert-True($Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Assert-Fails([scriptblock]$Action, [string]$Message) {
    $failed = $false
    try { & $Action | Out-Null } catch { $failed = $true }
    Assert-True $failed $Message
}
function Hash-Bytes([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

$base = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$testDirectory = Join-Path $base ('agent-hub-bootstrap-' + [Guid]::NewGuid().ToString())
$null = New-Item -ItemType Directory -Path $testDirectory
try {
    $body = [Text.Encoding]::UTF8.GetBytes('isolated installer fixture')
    $release = [PSCustomObject]@{ schema=1; platform='win-x64'; version='0.6.0'; files_sha256=('a' * 64);
        installer=[PSCustomObject]@{ url='https://47.109.23.86:18180/desktop-updates/win-x64/AgentHub-Setup-0.6.0.exe';
            bytes=$body.Length; sha256=(Hash-Bytes $body) } }
    $factory = { param($unused)
        $response = [Net.Http.HttpResponseMessage]::new([Net.HttpStatusCode]::OK)
        $response.Content = [Net.Http.ByteArrayContent]::new($body)
        return $response
    }
    $cache = Join-Path $testDirectory 'download'
    $file = Receive-AgentHubInstaller $release $cache $factory
    Assert-True ((Get-FileHash -LiteralPath $file).Hash -eq $release.installer.sha256) 'Verified download was not stored.'
    $same = Receive-AgentHubInstaller $release $cache { throw 'A valid cached installer must not download again.' }
    Assert-True ($same -eq $file) 'Cached download was not reused.'

    $bad = Join-Path $testDirectory 'bad-checksum'
    $release.installer.sha256 = 'b' * 64
    Assert-Fails { Receive-AgentHubInstaller $release $bad $factory } 'Corrupted download was accepted.'
    Assert-True (@(Get-ChildItem -LiteralPath $bad -Force).Count -eq 0) 'Corrupted temporary download was retained.'
    $release.installer.sha256 = Hash-Bytes $body
    $release.installer.bytes = $body.Length - 1
    Assert-Fails { Receive-AgentHubInstaller $release (Join-Path $testDirectory 'oversized') $factory } 'Unexpected content size was accepted.'
    $release.installer.bytes = $body.Length
    Assert-Fails { Receive-AgentHubInstaller $release (Join-Path $testDirectory 'redirect') {
        [Net.Http.HttpResponseMessage]::new([Net.HttpStatusCode]::Redirect)
    } } 'Redirect response was accepted.'

    $manifestPath = Join-Path $testDirectory 'release.json'
    $release.installer.bytes = 1000000
    $release | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    $null = Read-AgentHubManifest $manifestPath
    $release.installer.url = 'https://example.com/unsafe.exe'
    $release | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Assert-Fails { Read-AgentHubManifest $manifestPath } 'An unrelated download server was accepted.'

    $application = Join-Path $testDirectory 'application'
    $sources = @{
        'Agent Hub.exe'='fixture';
        'resources/app/package.json'='{"name":"agent-hub-desktop","version":"0.6.0"}';
        'resources/app/core/setup-install.cjs'='fixture';
        'resources/app/core/bridge.cjs'='fixture';
        'resources/app/skills/agent-hub/SKILL.md'='fixture';
        'resources/app/core/one.cjs'='fixture'; 'resources/app/core/two.cjs'='fixture';
        'resources/app/core/three.cjs'='fixture'; 'resources/app/core/four.cjs'='fixture';
        'resources/app/core/five.cjs'='fixture'
    }
    $records = @()
    foreach ($name in $sources.Keys) {
        $target = Join-Path $application $name
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force
        [IO.File]::WriteAllText($target, $sources[$name], [Text.UTF8Encoding]::new($false))
        $records += @{ path=$name; sha256=(Get-FileHash -LiteralPath $target).Hash.ToLowerInvariant() }
    }
    $treeManifest = Join-Path $application 'files.sha256.json'
    [IO.File]::WriteAllText($treeManifest, ($records | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $release.files_sha256 = (Get-FileHash -LiteralPath $treeManifest).Hash.ToLowerInvariant()
    Assert-True (Test-AgentHubInstalledRelease $application $release) 'A complete verified application tree was rejected.'
    [IO.File]::WriteAllText((Join-Path $application 'resources/app/core/bridge.cjs'), 'changed')
    Assert-True (-not (Test-AgentHubInstalledRelease $application $release)) 'A changed application file was accepted.'
    [IO.File]::WriteAllText((Join-Path $application 'resources/app/core/bridge.cjs'), 'fixture', [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $application 'resources/app/core/unlisted.cjs'), 'unexpected')
    Assert-True (-not (Test-AgentHubInstalledRelease $application $release)) 'An unlisted executable resource was accepted.'
    Write-Output 'PASS: checksum, cache, size, redirect, download host, complete tree, tampering and unlisted-resource checks.'
} finally {
    $actual = [IO.Path]::GetFullPath($testDirectory).TrimEnd('\')
    if ([IO.Path]::GetDirectoryName($actual) -ne $base -or [IO.Path]::GetFileName($actual) -notlike 'agent-hub-bootstrap-*') { throw 'Refusing to remove an unexpected test directory.' }
    Remove-Item -LiteralPath $actual -Recurse -Force
}
