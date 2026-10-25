REM =============================================================================
REM TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
REM 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
REM 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
REM 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
REM 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
REM （官方 Community Standards 第 8 条 "No hacking on the website"：
REM   零容忍，可永久封号且无警告）。在线相关脚本另见文件内附加说明。
REM For education & academic research only. No online public matches. No affiliation.
REM =============================================================================
@echo off
REM ============================================
REM  TankTrouble AI — CDP 农场一键启动
REM  启动 N 个 Edge CDP 实例（独立 profile、独立端口）
REM  用法:  start-farm.bat [实例数] [起始端口]
REM  例:    start-farm.bat 3      -> 3 个实例 (9222,9223,9224)
REM ============================================
setlocal
set COUNT=%1
if "%COUNT%"=="" set COUNT=2

echo [farm] Starting %COUNT% Edge CDP instances...
cd /d "%~dp0"
python tt.py farm %COUNT%

echo.
echo [farm] 所有实例已启动。查看实例列表:
echo   python tt.py list
echo 查看某实例状态:
echo   python tt.py ping 9222
echo.
pause
