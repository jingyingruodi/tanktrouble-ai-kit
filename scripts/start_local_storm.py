# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：零容忍，可永久封号且无警告）。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
"""本地对局开毒圈（Radioactive Fog）开发测试工具（2026-10-27）。

背景：
  * 本地 "Local game" 创建的是 `BootCampGameMode` + `storm=false`（`_createLocalGame` 里硬编码），
    所以默认永远不出毒圈；但本地引擎里 `ClassicGameMode` 的毒圈更新逻辑和 `StormZone`/
    `RoundModel.setZoneState` 都在。
  * 本工具先正常启动本地对局（`start_local_game.py`），然后向 `roundController` 注入一个
    **StormZone 初始状态**（字段取自当前 maze 的 `getStormExpansionSequence()`/`getTileBounds()`），
    并 `gc.setStorm(true)`。之后本地 StormZone.update 会自己推进生长/缩圈/序列，毒圈照常杀坦克、
    回合结束清空 —— 可离线测试监控室/录制/毒圈渲染，不需要在线房间。

注意：
  * 这是**本地开发测试钩子**（伪造 zone state），不是绕过在线服务端；在线毒圈仍以服务端为准。
  * 本地 storm 用同一份 StormZone 代码推进，但时序/回合结算细节与在线可能略有差异（仅用于渲染/
    录制/接口测试，不用于在线行为）。
  * 游戏 tab 需要保持前台，否则 Phaser 暂停、缩圈不动。

用法：
  python scripts/start_local_storm.py auto --players 2 --theme 0
  python scripts/start_local_storm.py auto --players 2 --hold 60     # 注入后观察 60s
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'cdp_tools'))
from cdp import CDP                         # noqa: E402
from ego_browser import resolve_port        # noqa: E402

INJECT_JS = r"""(() => {
  const gc = GameManager.getGameController();
  if (!gc || !gc.roundController) return JSON.stringify({ok:false, err:'no roundController'});
  const rc = gc.roundController;
  const maze = rc.getMaze ? rc.getMaze() : null;
  if (!maze) return JSON.stringify({ok:false, err:'no maze'});
  const zones = gc.getZones ? gc.getZones() : {};
  if (zones['sz-local-storm']) {
    return JSON.stringify({ok:true, existing:true, type:zones['sz-local-storm'].getType()});
  }
  gc.setStorm(true);
  const fields = {
    _expansionTime: 0.0,
    _expansionSequence: maze.getStormExpansionSequence(),
    _tileBounds: maze.getTileBounds(),
    _expansionSide: -1,
    _expansionTilesRight: 0,
    _expansionTilesBottom: 0,
    _expansionTilesLeft: 0,
    _expansionTilesTop: 0
  };
  const fakeZoneState = {
    getId: () => 'sz-local-storm',
    getType: () => 3,            // Constants.ZONE_TYPES.STORM
    getTiles: () => [],
    getFields: () => fields      // 注意：返回对象（不是 JSON 字符串），见 Zone._setFields
  };
  try {
    rc.setZoneState(fakeZoneState);
  } catch (e) {
    return JSON.stringify({ok:false, err:String(e)});
  }
  const z = (gc.getZones ? gc.getZones() : {})['sz-local-storm'];
  return JSON.stringify({ok:true, type: z ? z.getType() : null,
    seq: fields._expansionSequence, bounds: fields._tileBounds});
})()"""

STATUS_JS = r"""(() => {
  const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({ok:false, err:'no gc'});
  const z = (gc.getZones ? gc.getZones() : {})['sz-local-storm'];
  if (!z) return JSON.stringify({ok:true, phase: gc.getState && gc.getState(), zone:false,
    storm: gc.getStorm ? gc.getStorm() : null});
  const f = z.getFields(); const tb = f._tileBounds;
  return JSON.stringify({ok:true, phase: gc.getState && gc.getState(), zone:true,
    storm: gc.getStorm ? gc.getStorm() : null,
    safeTile:{minX:tb.minX+f._expansionTilesLeft, maxX:tb.maxX-f._expansionTilesRight,
              minY:tb.minY+f._expansionTilesTop, maxY:tb.maxY-f._expansionTilesBottom},
    expT:+(f._expansionTime||0).toFixed(2), side:f._expansionSide,
    counts:{l:f._expansionTilesLeft,r:f._expansionTilesRight,t:f._expansionTilesTop,b:f._expansionTilesBottom},
    seqLen:(f._expansionSequence||[]).length});
})()"""


def start_local(port, players, theme):
    cmd = [sys.executable, os.path.join(HERE, 'start_local_game.py'), str(port),
           '--players', str(players), '--restart', '--theme', str(theme)]
    print('[local-storm] 启动本地对局: %s' % ' '.join(cmd[1:]), flush=True)
    p = subprocess.run(cmd, cwd=ROOT)
    return p.returncode == 0


async def main():
    ap = argparse.ArgumentParser(description='本地对局开启毒圈（开发测试）')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--players', type=int, default=2, choices=(1, 2, 3))
    ap.add_argument('--theme', type=int, default=0)
    ap.add_argument('--hold', type=float, default=0.0, help='注入后观察秒数（0=立即退出）')
    ap.add_argument('--repeat', action='store_true', help='hold 期间每回合自动重新注入（回合结束/新回合后）')
    ap.add_argument('--no-start', action='store_true', help='不启动本地对局，只对已在局的页面注入')
    ap.add_argument('--timeout', type=float, default=20.0)
    args = ap.parse_args()

    port = args.port if str(args.port).isdigit() else 'auto'
    if not args.no_start:
        port = resolve_port(port)
        if not start_local(port, args.players, args.theme):
            print('[local-storm] ❌ 本地对局启动失败', flush=True)
            return 1
    else:
        port = resolve_port(port)

    c = CDP(port=port)
    await c.connect_page('tanktrouble.com', timeout=10)
    try:
        # 等到进入回合（本地对局启动完可能还在 counting down）
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < args.timeout:
            st = json.loads(await c.eval(STATUS_JS))
            if st.get('ok') and st.get('phase') == 'in round':
                break
            await asyncio.sleep(0.5)
        else:
            print('[local-storm] ❌ 未进入回合（phase=%s）' % st.get('phase'), flush=True)
            return 1

        r = json.loads(await c.eval(INJECT_JS))
        print('[local-storm] 注入结果: %s' % json.dumps(r, ensure_ascii=False), flush=True)
        if not r.get('ok'):
            return 1
        await asyncio.sleep(0.5)
        print('[local-storm] 状态: %s' % await c.eval(STATUS_JS), flush=True)

        if args.hold and args.hold > 0:
            t0 = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - t0 < args.hold:
                st = await c.eval(STATUS_JS)
                print('[local-storm] %6.1fs %s' %
                      (asyncio.get_event_loop().time() - t0, st), flush=True)
                if args.repeat:
                    try:
                        stj = json.loads(st)
                    except Exception:
                        stj = {}
                    if stj.get('ok') and stj.get('phase') == 'in round' and not stj.get('zone'):
                        r = json.loads(await c.eval(INJECT_JS))
                        print('[local-storm] 新回合重新注入: %s' % json.dumps(r, ensure_ascii=False), flush=True)
                await asyncio.sleep(1.0)
        print('[local-storm] ✅ 已开毒圈（本地 StormZone 会自行生长/缩圈）', flush=True)
        return 0
    finally:
        await c.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
