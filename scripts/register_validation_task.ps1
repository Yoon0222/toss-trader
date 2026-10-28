$ErrorActionPreference='Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$runnerPath = Join-Path $projectPath '.venv\Scripts\pythonw.exe'
$entryPath = Join-Path $PSScriptRoot 'start_validation_service.py'
if (!(Test-Path -LiteralPath $runnerPath)) { throw '프로젝트 Python 실행 환경이 없습니다.' }
$action = New-ScheduledTaskAction -Execute $runnerPath -Argument ('"'+$entryPath+'"') -WorkingDirectory $projectPath
$daily = New-ScheduledTaskTrigger -Daily -At '08:25'
$logon = New-ScheduledTaskTrigger -AtLogOn
$daily.EndBoundary = '2026-11-03T00:00:00'
$logon.EndBoundary = '2026-11-03T00:00:00'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 10)
Register-ScheduledTask -TaskName 'TossPaperValidationWeek' -Action $action -Trigger @($daily,$logon) -Principal $principal -Settings $settings -Description '2026-10-27~11-02 실제 시세 기반 가상자금 검증. 실계좌 주문 차단. 로그인 상태에서 실행.' -Force | Select-Object TaskName,State
