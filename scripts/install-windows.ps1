#requires -Version 5.1
[CmdletBinding()]
param(
    [string[]]$Harness = @('auto'),
    [string]$ManifestPath = '',
    [string]$McpConfigPath = '',
    [string]$UserDataDirectory = (Join-Path $env:APPDATA 'Agent Hub')
)

$ErrorActionPreference = 'Stop'
if (-not $ManifestPath) { $ManifestPath = Join-Path $PSScriptRoot '..\releases\windows-x64.json' }

function Assert-AgentHubPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) { throw '安装路径必须是绝对路径。' }
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.TrimEnd('\') -eq [IO.Path]::GetPathRoot($full).TrimEnd('\')) { throw '不能使用磁盘根目录作为安装数据目录。' }
    for ($current = $full; $current; $current = [IO.Path]::GetDirectoryName($current)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '安装路径包含链接或重定向，已保留原文件。' }
        }
    }
    return $full
}

function Read-AgentHubManifest([string]$Path) {
    $info = Get-Item -LiteralPath $Path
    if ($info.Length -gt 16384) { throw '发行信息过大。' }
    $release = [IO.File]::ReadAllText($info.FullName) | ConvertFrom-Json
    if ($release.schema -ne 1 -or $release.platform -ne 'win-x64' -or $release.version -notmatch '^\d+\.\d+\.\d+$' -or
        $release.files_sha256 -notmatch '^[a-f0-9]{64}$' -or $release.installer.sha256 -notmatch '^[a-f0-9]{64}$' -or
        $release.installer.bytes -lt 1000000 -or $release.installer.bytes -gt 536870912) { throw '发行信息格式无效。' }
    $url = [Uri]$release.installer.url
    $expected = '/desktop-updates/win-x64/AgentHub-Setup-' + $release.version + '.exe'
    if ($url.Scheme -ne 'https' -or $url.Host -ne '47.109.23.86' -or $url.Port -ne 18180 -or
        $url.AbsolutePath -ne $expected -or $url.Query -or $url.Fragment -or $url.UserInfo) { throw '安装包地址不是正式发行地址。' }
    return $release
}

function Get-AgentHubHosts([string[]]$Requested) {
    $selected = @($Requested | ForEach-Object { $_.Split(',') } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Select-Object -Unique)
    if ($selected.Count -eq 1 -and $selected[0] -eq 'auto') {
        $detected = @()
        if ($env:CODEX_HOME -or (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.codex')) -or (Get-Command codex -ErrorAction SilentlyContinue)) { $detected += 'codex' }
        if ((Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.claude')) -or (Get-Command claude -ErrorAction SilentlyContinue)) { $detected += 'claude' }
        if ((Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.cursor')) -or (Get-Command cursor -ErrorAction SilentlyContinue)) { $detected += 'cursor' }
        foreach ($hostName in @('qoder', 'workbuddy', 'codebuddy', 'trae')) {
            if ((Test-Path -LiteralPath (Join-Path $env:USERPROFILE ('.' + $hostName))) -or (Get-Command $hostName -ErrorAction SilentlyContinue)) { $detected += $hostName }
        }
        if ($detected.Count -ne 1) { throw '请使用 -Harness 指明当前工具：codex、claude、cursor、qoder、workbuddy、codebuddy、trae；可用逗号选择多个。' }
        $selected = $detected
    }
    if (-not $selected.Count -or @($selected | Where-Object { $_ -notin @('codex', 'claude', 'cursor', 'qoder', 'workbuddy', 'codebuddy', 'trae') }).Count) { throw '安装支持 codex、claude、cursor、qoder、workbuddy、codebuddy、trae。' }
    return $selected
}

function Find-AgentHubInstallation {
    $locations = @()
    $registry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    if (Test-Path $registry) {
        foreach ($key in Get-ChildItem $registry) {
            $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if ($entry.DisplayName -eq 'Agent Hub') {
                $location = $entry.InstallLocation
                if ($location -and (Test-Path -LiteralPath (Join-Path $location 'Agent Hub.exe'))) { $locations += $location }
            }
        }
    }
    foreach ($relative in @('Programs\Agent Hub', 'Programs\agent-hub-desktop')) {
        $default = Join-Path $env:LOCALAPPDATA $relative
        if (Test-Path -LiteralPath (Join-Path $default 'Agent Hub.exe')) { $locations += $default }
    }
    $locations = @($locations | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\') } | Select-Object -Unique)
    if ($locations.Count -gt 1) { throw '发现多个 Agent Hub 安装位置，请先确认使用的安装目录。' }
    if ($locations.Count) { return $locations[0] }
    return $null
}

function Test-AgentHubInstalledRelease([string]$Directory, $Release) {
    if (-not $Directory) { return $false }
    try {
        $directoryPath = Assert-AgentHubPath $Directory
        $manifest = Join-Path $directoryPath 'files.sha256.json'
        if (-not (Test-Path -LiteralPath $manifest) -or (Get-Item -LiteralPath $manifest).Length -gt 2097152) { throw 'FILE_MANIFEST_MISSING' }
        if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash -ne $Release.files_sha256) { throw 'FILE_MANIFEST_HASH_MISMATCH' }
        $files = [IO.File]::ReadAllText($manifest) | ConvertFrom-Json
        if ($files -isnot [array] -or $files.Count -lt 10 -or $files.Count -gt 5000) { throw 'FILE_MANIFEST_COUNT_INVALID' }
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        foreach ($file in $files) {
            if ($file.path -match '(^/|\\|:|(^|/)\.\.?(/|$))' -or $file.sha256 -notmatch '^[a-f0-9]{64}$' -or -not $seen.Add($file.path)) { throw 'FILE_MANIFEST_ENTRY_INVALID' }
            $target = [IO.Path]::GetFullPath((Join-Path $directoryPath $file.path))
            if (-not $target.StartsWith($directoryPath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'FILE_OUTSIDE_INSTALLATION' }
            $null = Assert-AgentHubPath $target
            if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $file.sha256) { throw 'INSTALLED_FILE_CHANGED' }
        }
        foreach ($required in @('Agent Hub.exe', 'resources/app/package.json', 'resources/app/core/setup-install.cjs', 'resources/app/core/bridge.cjs', 'resources/app/skills/agent-hub/SKILL.md')) {
            if (-not $seen.Contains($required)) { throw ('REQUIRED_FILE_MISSING: ' + $required) }
        }
        # Installer metadata and the uninstaller live outside resources/app.
        # Every executable application resource must belong to the verified release.
        foreach ($item in Get-ChildItem -LiteralPath (Join-Path $directoryPath 'resources\app') -Recurse -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'RESOURCE_LINK_NOT_ALLOWED' }
            if (-not $item.PSIsContainer) {
                $relative = $item.FullName.Substring($directoryPath.Length + 1).Replace('\', '/')
                if (-not $seen.Contains($relative)) { throw ('UNLISTED_APP_RESOURCE: ' + $relative) }
            }
        }
        $package = [IO.File]::ReadAllText((Join-Path $directoryPath 'resources\app\package.json')) | ConvertFrom-Json
        return $package.name -eq 'agent-hub-desktop' -and $package.version -eq $Release.version
    } catch { Write-Verbose ('安装文件检查未通过：' + $_.Exception.Message); return $false }
}

function Receive-AgentHubInstaller($Release, [string]$CacheDirectory, [scriptblock]$ResponseFactory = $null) {
    $cache = Assert-AgentHubPath $CacheDirectory
    $null = New-Item -ItemType Directory -Path $cache -Force
    $file = Join-Path $cache ('AgentHub-Setup-' + $Release.version + '.exe')
    $null = Assert-AgentHubPath $file
    if ((Test-Path -LiteralPath $file) -and (Get-Item -LiteralPath $file).Length -eq $Release.installer.bytes -and
        (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $Release.installer.sha256) { return $file }
    Add-Type -AssemblyName System.Net.Http
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $partial = $file + '.partial-' + [Guid]::NewGuid().ToString()
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $cancel = New-Object Threading.CancellationTokenSource
    $cancel.CancelAfter(300000)
    $response = $null; $stream = $null; $output = $null
    try {
        $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Get, [Uri]$Release.installer.url)
        if ($ResponseFactory) { $response = & $ResponseFactory $Release }
        else { $response = $client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult() }
        if ([int]$response.StatusCode -ne 200) { throw '安装包下载失败，正式服务器没有返回 HTTP 200。' }
        if ($response.Content.Headers.ContentLength -and $response.Content.Headers.ContentLength -ne $Release.installer.bytes) { throw '安装包长度与发行信息不一致。' }
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $output = [IO.File]::Open($partial, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = New-Object byte[] 131072
        [long]$total = 0
        while (($count = $stream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token).GetAwaiter().GetResult()) -gt 0) {
            $total += $count
            if ($total -gt $Release.installer.bytes) { throw '安装包超过已批准的大小。' }
            $output.Write($buffer, 0, $count)
        }
        $output.Dispose(); $output = $null
        if ($total -ne $Release.installer.bytes -or (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Release.installer.sha256) { throw '安装包完整性校验失败，未执行安装。' }
        Move-Item -LiteralPath $partial -Destination $file -Force
        return $file
    } finally {
        if ($output) { $output.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($response) { $response.Dispose() }
        $client.Dispose(); $handler.Dispose(); $cancel.Dispose()
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    }
}

function Invoke-AgentHubSetupCore {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem -or
        ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64')) { throw '当前安装入口需要 Windows x64。' }
    $selected = @(Get-AgentHubHosts $Harness)
    $release = Read-AgentHubManifest ([IO.Path]::GetFullPath($ManifestPath))
    $dataDirectory = Assert-AgentHubPath ([IO.Path]::GetFullPath($UserDataDirectory))
    $null = New-Item -ItemType Directory -Path $dataDirectory -Force
    # Verify durable same-directory replacement before starting the desktop vault.
    $probe = Join-Path $dataDirectory ('storage-probe-' + [Guid]::NewGuid().ToString())
    try {
        [IO.File]::WriteAllText($probe, 'probe')
        $node = Get-Command node -ErrorAction SilentlyContinue
        if ($node) {
            $probePrior = $env:AGENT_HUB_STORAGE_PROBE
            try {
                $env:AGENT_HUB_STORAGE_PROBE = $probe
                'try{require("fs").renameSync(process.env.AGENT_HUB_STORAGE_PROBE,process.env.AGENT_HUB_STORAGE_PROBE+".moved")}catch{process.exitCode=1}' | & $node.Source
                if ($LASTEXITCODE -ne 0) { throw '当前数据目录不支持应用所需的文件替换。请使用 -UserDataDirectory 指定其他本机缓存目录，原数据保留。' }
            } finally {
                if ($null -eq $probePrior) { Remove-Item Env:AGENT_HUB_STORAGE_PROBE -ErrorAction SilentlyContinue }
                else { $env:AGENT_HUB_STORAGE_PROBE = $probePrior }
            }
        }
    } finally {
        if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force }
        if (Test-Path -LiteralPath ($probe + '.moved')) { Remove-Item -LiteralPath ($probe + '.moved') -Force }
    }
    $directory = Find-AgentHubInstallation
    if ($directory) {
        $packagePath = Join-Path $directory 'resources\app\package.json'
        if (Test-Path -LiteralPath $packagePath) {
            $package = [IO.File]::ReadAllText($packagePath) | ConvertFrom-Json
            if ($package.version -match '^\d+\.\d+\.\d+$' -and [version]$package.version -gt [version]$release.version) {
                throw '已安装的客户端比该安装入口更新，请从框架仓库获取最新入口，不降级程序。'
            }
        }
    }
    if (-not (Test-AgentHubInstalledRelease $directory $release)) {
        Write-Host ('下载并校验 Agent Hub ' + $release.version + ' 安装包…')
        $installer = Receive-AgentHubInstaller $release (Join-Path $env:LOCALAPPDATA 'AgentHub\installers')
        $process = Start-Process -FilePath $installer -ArgumentList @('/S') -WindowStyle Hidden -PassThru -Wait
        if ($process.ExitCode -ne 0) { throw 'EXE 安装没有完成，请检查现有客户端是否可正常退出后重试。' }
        $directory = Find-AgentHubInstallation
        if (-not (Test-AgentHubInstalledRelease $directory $release)) { throw '安装后的程序文件校验未通过，未执行配置助手。' }
    } else { Write-Host ('已核验本机 Agent Hub ' + $release.version + '，复用现有安装。') }
    $null = New-Item -ItemType Directory -Path $dataDirectory -Force
    $requestFile = Join-Path $dataDirectory 'setup-request.json'
    $null = Assert-AgentHubPath $requestFile
    $request = @{ version = 1; request_id = [Guid]::NewGuid().ToString() } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($requestFile, $request, (New-Object Text.UTF8Encoding($false)))
    $executable = Join-Path $directory 'Agent Hub.exe'
    $nodeMode = $env:ELECTRON_RUN_AS_NODE
    try {
        Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
        # This is the interactive desktop window where the user signs in.
        $null = Start-Process -FilePath $executable -ArgumentList @('--setup-mcp', ('--user-data-dir="' + $dataDirectory + '"')) -PassThru
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ((Test-Path -LiteralPath $requestFile) -or -not (Test-Path -LiteralPath (Join-Path $dataDirectory 'setup-mcp.json'))) {
            if ($timer.Elapsed.TotalSeconds -gt 90) { throw '桌面端尚未就绪。请打开 Agent Hub 检查状态后重新运行安装入口。' }
            Start-Sleep -Milliseconds 250
        }
        $env:ELECTRON_RUN_AS_NODE = '1'
        $arguments = @((Join-Path $directory 'resources\app\core\setup-install.cjs'), '--data-dir', $dataDirectory,
            '--executable', $executable, '--hosts', ($selected -join ','), '--home', $env:USERPROFILE)
        if ($env:CODEX_HOME) { $arguments += @('--codex-home', $env:CODEX_HOME) }
        if ($McpConfigPath) { $arguments += @('--config-file', (Assert-AgentHubPath $McpConfigPath)) }
        $result = & $executable @arguments 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw ('Skill 或 MCP 配置未完成；原配置保留，可修复冲突后重试。' + [Environment]::NewLine + $result) }
        $installed = $result | ConvertFrom-Json
        # Persist a custom data directory for ordinary desktop/start-menu launches.
        $shell = New-Object -ComObject WScript.Shell
        foreach ($shortcut in @((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Agent Hub.lnk'),
            (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\Agent Hub.lnk'))) {
            if (Test-Path -LiteralPath $shortcut) {
                $link = $shell.CreateShortcut($shortcut)
                if ($link.TargetPath -eq $executable) { $link.Arguments = '--user-data-dir="' + $dataDirectory + '"'; $link.Save() }
            }
        }
        Write-Host '桌面端、Skill 和 MCP 安装完成。请在 PC 端完成本人登录，并重新加载开发工具。'
        $installed | ConvertTo-Json -Depth 8
    } finally {
        if ($null -eq $nodeMode) { Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue }
        else { $env:ELECTRON_RUN_AS_NODE = $nodeMode }
    }
}

function Invoke-AgentHubSetup {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = [Threading.Mutex]::new($false, ('Local\AgentHub.Setup.' + $sid))
    $held = $false
    try {
        try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw '当前 Windows 用户已有一个安装任务，请等待其完成后重试。' }
        Invoke-AgentHubSetupCore
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-AgentHubSetup }
