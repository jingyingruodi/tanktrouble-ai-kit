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
