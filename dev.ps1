# ============================================
#  TankTrouble AI — 开发/测试快捷命令
#  用法: .\dev.ps1 <命令> [参数]
#  例:   .\dev.ps1 list
#        .\dev.ps1 ping 9222
#        .\dev.ps1 test 9222 0
#        .\dev.ps1 suicide 9222 1
#        .\dev.ps1 farm 3
# ============================================

param(
    [Parameter(Position=0)]
    [string]$Cmd = 'help',
    [Parameter(Position=1)]
    [string]$Arg1 = '',
    [Parameter(Position=2)]
    [string]$Arg2 = ''
)

$Root = $PSScriptRoot

function Show-Help {
    Write-Host @"
TankTrouble AI — 命令中心
==========================
  list                列出所有 CDP 实例和窗口 ID
  ping [port]         检查实例连通性 + 游戏状态 (默认 9222)
  info [port]         查看实例窗口详情
  farm <count>        启动 N 个 Edge 实例
  test [port] [idx]   运行 SET/GET 接口测试 (默认 port=9222 idx=0)
  suicide [port] [idx]运行自杀脚本验证
  docs                打开文档
"@
}

switch ($Cmd.ToLower()) {
    'list'   { python (Join-Path $Root 'tt.py') list }
    'ping'   { python (Join-Path $Root 'tt.py') ping $Arg1 }
    'info'   { python (Join-Path $Root 'tt.py') info $Arg1 }
    'farm'   { python (Join-Path $Root 'tt.py') farm $Arg1 }
    'test'   { python (Join-Path $Root 'tt.py') test $Arg1 $Arg2 }
    'suicide' { python (Join-Path $Root 'tt.py') suicide $Arg1 $Arg2 }
    'docs'   { Start-Process "D:\AI Agent\tanktrouble_ai\docs\README.md" }
    default  { Show-Help }
}
