#requires -Version 5.1
<#
安全清理脚本（仅针对已检查并确认的低风险项目）

默认只预览：
  powershell.exe -ExecutionPolicy Bypass -File .\safe-cleanup.ps1

实际执行：
  powershell.exe -ExecutionPolicy Bypass -File .\safe-cleanup.ps1 -Execute

脚本不会清空回收站，不会进入重解析点，不会处理 Windows、Program Files、
ProgramData、Docker/WSL 虚拟磁盘、IDE 配置、Conda 环境或用户大型文件。
#>

[CmdletBinding()]
param(
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$ExpectedProfile = 'C:\Users\Lenovo'
$TempCutoff = (Get-Date).AddDays(-7)
$LogCutoff = (Get-Date).AddDays(-30)
$ReportPath = Join-Path $PSScriptRoot ("safe-cleanup-report-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

$script:DeletedFiles = [System.Collections.Generic.List[object]]::new()
$script:DeletedDirectories = [System.Collections.Generic.List[string]]::new()
$script:SkippedItems = [System.Collections.Generic.List[object]]::new()
$script:TargetResults = [System.Collections.Generic.List[object]]::new()

function Format-Size {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N2} KB' -f ($Bytes / 1KB)) }
    return ("$Bytes B")
}

function Get-FixedDriveSnapshot {
    Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
        [pscustomobject]@{
            Drive   = $_.DeviceID
            TotalGB = [math]::Round($_.Size / 1GB, 2)
            FreeGB  = [math]::Round($_.FreeSpace / 1GB, 2)
            FreePct = [math]::Round(100 * $_.FreeSpace / $_.Size, 1)
        }
    }
}

function Test-IsReparsePoint {
    param([System.IO.FileSystemInfo]$Item)
    return (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Test-PathInsideRoot {
    param(
        [string]$Path,
        [string]$Root
    )
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    return $fullPath.StartsWith($fullRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-FileUnlocked {
    param([string]$LiteralPath)
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $LiteralPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::None
        )
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Invoke-SafeRootCleanup {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][scriptblock]$FileFilter
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        Write-Host "[不存在，跳过] $Root"
        return
    }

    $rootItem = Get-Item -LiteralPath $Root -Force
    if (Test-IsReparsePoint $rootItem) {
        $script:SkippedItems.Add([pscustomobject]@{ Path = $Root; Reason = '根目录是重解析点' })
        Write-Warning "跳过重解析点：$Root"
        return
    }

    $candidateBytes = [long]0
    $candidateCount = 0
    $deletedBytes = [long]0
    $deletedCount = 0
    $directories = [System.Collections.Generic.List[string]]::new()
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($rootItem.FullName)

    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        try {
            $children = @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)
        }
        catch {
            $script:SkippedItems.Add([pscustomobject]@{ Path = $current; Reason = "无法读取：$($_.Exception.Message)" })
            continue
        }

        foreach ($item in $children) {
            if (Test-IsReparsePoint $item) {
                $script:SkippedItems.Add([pscustomobject]@{ Path = $item.FullName; Reason = '重解析点' })
                continue
            }

            if ($item.PSIsContainer) {
                if (Test-PathInsideRoot -Path $item.FullName -Root $Root) {
                    $directories.Add($item.FullName)
                    $stack.Push($item.FullName)
                }
                else {
                    $script:SkippedItems.Add([pscustomobject]@{ Path = $item.FullName; Reason = '超出允许根目录' })
                }
                continue
            }

            if (-not (& $FileFilter $item)) { continue }
            if (-not (Test-PathInsideRoot -Path $item.FullName -Root $Root)) {
                $script:SkippedItems.Add([pscustomobject]@{ Path = $item.FullName; Reason = '超出允许根目录' })
                continue
            }

            $candidateBytes += $item.Length
            $candidateCount++

            if (-not $Execute) { continue }
            if (-not (Test-FileUnlocked -LiteralPath $item.FullName)) {
                $script:SkippedItems.Add([pscustomobject]@{ Path = $item.FullName; Reason = '文件正在使用或无法取得独占读取权限' })
                continue
            }

            try {
                $length = $item.Length
                Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
                $deletedBytes += $length
                $deletedCount++
                $script:DeletedFiles.Add([pscustomobject]@{ Path = $item.FullName; Bytes = $length; Target = $Label })
            }
            catch {
                $script:SkippedItems.Add([pscustomobject]@{ Path = $item.FullName; Reason = "删除失败：$($_.Exception.Message)" })
            }
        }
    }

    if ($Execute) {
        foreach ($directory in ($directories | Sort-Object Length -Descending)) {
            try {
                $directoryItem = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
                if (Test-IsReparsePoint $directoryItem) { continue }
                if (@(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop).Count -eq 0) {
                    Remove-Item -LiteralPath $directory -Force -ErrorAction Stop
                    $script:DeletedDirectories.Add($directory)
                }
            }
            catch {
                # 非空、正在使用或权限不足的目录保留，不影响其他项目。
            }
        }
    }

    $script:TargetResults.Add([pscustomobject]@{
        Target         = $Label
        Root           = $Root
        CandidateFiles = $candidateCount
        CandidateSize  = $candidateBytes
        DeletedFiles   = $deletedCount
        DeletedSize    = $deletedBytes
    })

    if ($Execute) {
        Write-Host ("[已处理] {0}：删除 {1} 个文件，释放 {2}" -f $Label, $deletedCount, (Format-Size $deletedBytes))
    }
    else {
        Write-Host ("[预览] {0}：{1} 个候选文件，预计 {2}" -f $Label, $candidateCount, (Format-Size $candidateBytes))
    }
}

function Invoke-VerifiedDuplicateCleanup {
    param(
        [Parameter(Mandatory)][string]$Original,
        [Parameter(Mandatory)][string]$Duplicate
    )

    if (-not (Test-Path -LiteralPath $Original -PathType Leaf) -or
        -not (Test-Path -LiteralPath $Duplicate -PathType Leaf)) {
        Write-Host '[重复文件] 文件已不存在，跳过。'
        return
    }

    $originalItem = Get-Item -LiteralPath $Original -Force
    $duplicateItem = Get-Item -LiteralPath $Duplicate -Force
    if ((Test-IsReparsePoint $originalItem) -or (Test-IsReparsePoint $duplicateItem)) {
        $script:SkippedItems.Add([pscustomobject]@{ Path = $Duplicate; Reason = '重复文件候选是重解析点' })
        return
    }

    $originalHash = (Get-FileHash -LiteralPath $Original -Algorithm SHA256).Hash
    $duplicateHash = (Get-FileHash -LiteralPath $Duplicate -Algorithm SHA256).Hash
    if ($originalHash -ne $duplicateHash) {
        $script:SkippedItems.Add([pscustomobject]@{ Path = $Duplicate; Reason = 'SHA-256 已不一致，保留' })
        return
    }

    $length = $duplicateItem.Length
    if (-not $Execute) {
        $script:TargetResults.Add([pscustomobject]@{
            Target = '已验证重复视频'; Root = $Duplicate; CandidateFiles = 1
            CandidateSize = $length; DeletedFiles = 0; DeletedSize = 0
        })
        Write-Host ("[预览] 已验证重复视频：预计 {0}" -f (Format-Size $length))
        return
    }

    if (-not (Test-FileUnlocked -LiteralPath $Duplicate)) {
        $script:SkippedItems.Add([pscustomobject]@{ Path = $Duplicate; Reason = '文件正在使用' })
        return
    }

    try {
        Remove-Item -LiteralPath $Duplicate -Force -ErrorAction Stop
        $script:DeletedFiles.Add([pscustomobject]@{ Path = $Duplicate; Bytes = $length; Target = '已验证重复视频' })
        $script:TargetResults.Add([pscustomobject]@{
            Target = '已验证重复视频'; Root = $Duplicate; CandidateFiles = 1
            CandidateSize = $length; DeletedFiles = 1; DeletedSize = $length
        })
        Write-Host ("[已处理] 删除哈希完全相同的副本：{0}" -f $Duplicate)
    }
    catch {
        $script:SkippedItems.Add([pscustomobject]@{ Path = $Duplicate; Reason = "删除失败：$($_.Exception.Message)" })
    }
}

if (-not [string]::Equals($env:USERPROFILE, $ExpectedProfile, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "当前用户目录是 '$env:USERPROFILE'，与预期 '$ExpectedProfile' 不一致。为安全起见已停止。"
}

try { Start-Transcript -LiteralPath $ReportPath -Force | Out-Null } catch {}

try {
    Write-Host '============================================================'
    Write-Host '安全清理：低风险缓存、临时文件、旧日志及公共着色器缓存'
    Write-Host ("模式：{0}" -f $(if ($Execute) { '实际执行' } else { '仅预览，不删除' }))
    Write-Host '============================================================'

    $before = @(Get-FixedDriveSnapshot)
    Write-Host "`n清理前磁盘空间："
    $before | Format-Table -AutoSize

    if ($Execute) {
        Write-Warning '请先关闭游戏、显卡工具、Python/Node 包管理器及其他正在运行的软件。'
        $confirmation = Read-Host '输入 CLEAN 确认执行；输入其他内容取消'
        if ($confirmation -cne 'CLEAN') {
            Write-Host '用户取消，未删除任何文件。'
            return
        }
    }

    $allFiles = { param($File) $true }
    $oldTempFiles = { param($File) $File.LastWriteTime -lt $TempCutoff }
    $oldLogFiles = {
        param($File)
        ($File.LastWriteTime -lt $LogCutoff) -and
        (($File.Extension -ieq '.log') -or ($File.Name -match '(?i)\.log\.'))
    }

    Invoke-SafeRootCleanup -Label '用户临时文件（超过 7 天）' `
        -Root "$ExpectedProfile\AppData\Local\Temp" -FileFilter $oldTempFiles

    Invoke-SafeRootCleanup -Label 'NVIDIA DirectX 着色器缓存' `
        -Root "$ExpectedProfile\AppData\Local\NVIDIA\DXCache" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label 'NVIDIA OpenGL 着色器缓存' `
        -Root "$ExpectedProfile\AppData\Local\NVIDIA\GLCache" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label 'Windows Direct3D 着色器缓存' `
        -Root "$ExpectedProfile\AppData\Local\D3DSCache" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label 'pip 下载缓存' `
        -Root "$ExpectedProfile\AppData\Local\pip\Cache" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label 'npm 本地缓存' `
        -Root "$ExpectedProfile\AppData\Local\npm-cache" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label 'npm 用户缓存' `
        -Root "$ExpectedProfile\.npm" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label 'pnpm 内容缓存' `
        -Root "$ExpectedProfile\AppData\Local\pnpm\store" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label '崩溃转储' `
        -Root "$ExpectedProfile\AppData\Local\CrashDumps" -FileFilter $allFiles

    Invoke-SafeRootCleanup -Label '超过 30 天的 Nacos/Sentinel 日志' `
        -Root "$ExpectedProfile\logs" -FileFilter $oldLogFiles

    Invoke-VerifiedDuplicateCleanup `
        -Original "$ExpectedProfile\Downloads\e88e4e15-6933-4a63-b4a2-a779b51b963b.mp4" `
        -Duplicate "$ExpectedProfile\Downloads\e88e4e15-6933-4a63-b4a2-a779b51b963b (1).mp4"

    $after = @(Get-FixedDriveSnapshot)
    Write-Host "`n清理后磁盘空间："
    $after | Format-Table -AutoSize

    Write-Host "`n各磁盘可用空间变化："
    $driveChanges = @()
    foreach ($beforeDrive in $before) {
        $afterDrive = $after | Where-Object Drive -eq $beforeDrive.Drive | Select-Object -First 1
        if ($null -ne $afterDrive) {
            $driveChanges += [pscustomobject]@{
                Drive        = $beforeDrive.Drive
                BeforeFreeGB = $beforeDrive.FreeGB
                AfterFreeGB  = $afterDrive.FreeGB
                ChangeGB     = [math]::Round($afterDrive.FreeGB - $beforeDrive.FreeGB, 2)
            }
        }
    }
    $driveChanges | Format-Table -AutoSize

    Write-Host "`n清理项目汇总："
    $script:TargetResults | Select-Object Target, Root, CandidateFiles,
        @{N='CandidateSize';E={Format-Size $_.CandidateSize}}, DeletedFiles,
        @{N='DeletedSize';E={Format-Size $_.DeletedSize}} | Format-Table -AutoSize

    Write-Host "`n实际删除的目录："
    if ($script:DeletedDirectories.Count -eq 0) {
        Write-Host '无（缓存根目录始终保留）。'
    }
    else {
        $script:DeletedDirectories | ForEach-Object { Write-Host "- $_" }
    }

    Write-Host "`n实际删除的文件："
    if ($script:DeletedFiles.Count -eq 0) {
        Write-Host '无。'
    }
    else {
        $script:DeletedFiles | Group-Object Target | ForEach-Object {
            $bytes = ($_.Group | Measure-Object Bytes -Sum).Sum
            Write-Host ("- {0}：{1} 个文件，{2}" -f $_.Name, $_.Count, (Format-Size $bytes))
        }
    }

    Write-Host "`n因占用、权限或安全检查而保留的项目："
    if ($script:SkippedItems.Count -eq 0) {
        Write-Host '无。'
    }
    else {
        $script:SkippedItems | Format-Table Path, Reason -AutoSize
    }

    Write-Host "`n刻意保留、未做任何操作的高风险项目："
    @(
        'C:\Users\Lenovo\AppData\Local\Docker\wsl\disk\docker_data.vhdx',
        'C:\Users\Lenovo\AppData\Local\Docker\wsl\main\ext4.vhdx',
        'C:\Aomei\AomeiBoot.wim',
        'C:\Users\Lenovo\.conda\envs',
        'C:\Users\Lenovo\.vscode',
        'C:\Users\Lenovo\.lingma',
        'C:\Users\Lenovo\.qoder-cn',
        'C:\Users\Lenovo\.codebuddycn',
        'C:\Users\Lenovo\.m2',
        'C:\Users\Lenovo\.gradle',
        'C:\Users\Lenovo\.jdks',
        'C:\Users\Lenovo\.codex'
    ) | ForEach-Object { Write-Host "- $_" }

    Write-Host "`n仍需手动决定、未做任何操作的项目："
    @(
        'C:\Users\Lenovo\.conda\pkgs（2.46 GB）',
        'C:\Users\Lenovo\.cache\huggingface（0.38 GB）',
        'C:\Users\Lenovo\Documents\猎天使魔女2 [版本1.2.0]（12.48 GB）',
        'C:\Users\Lenovo\Downloads 下的大模型文件（约 7.04 GB）',
        'C:\Users\Lenovo\Videos\Outer Wilds [01003DC0144B6000][v0].nsp（3.91 GB）',
        '下载目录中的安装包、压缩包和其他媒体文件'
    ) | ForEach-Object { Write-Host "- $_" }

    Write-Host "`n报告文件：$ReportPath"
}
catch {
    Write-Error "脚本发生错误，已停止后续处理：$($_.Exception.Message)"
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
