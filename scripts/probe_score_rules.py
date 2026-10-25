#!/usr/bin/env python
# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：
#   零容忍，可永久封号且无警告）。在线相关脚本另见文件内附加说明。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
"""
TankTrouble 计分规则实测
========================
源码级规则（2026-10-16 从打包代码读出，见 docs/12）：
  * 每名玩家初始两条分数：SCORE_TYPES.KILL=0、SCORE_TYPES.VICTORY=0
  * TANK_KILLED 事件：
      killerPlayerId != victimPlayerId → 击杀者 KILL +1
      killerPlayerId == victimPlayerId → **谁都不加分（也无扣分）**   ← 自杀
  * ROUND_ENDED 事件：胜者 VICTORY +1
本工具在**本地两人局**里逐条实测，用游戏自己的消息类构造真实事件
（`TankKilledMessage` + `gc.killTank`，走的就是游戏内部的计分路径）。

用法：
    python scripts/probe_score_rules.py auto
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tankctl import TankCtl                          # noqa: E402

SCORES_JS = """(() => {
  const gc = GameManager.getGameController();
  const out = {scores: [], emblems: [], mode: null};
  try { out.mode = String(gc.model.getState()); } catch (e) {}
  try {
    const ss = gc.getScores ? gc.getScores() : {};
    for (const id in ss) { const s = ss[id];
      out.scores.push({id: id, pid: s.getPlayerId ? s.getPlayerId() : null,
                       type: s.getType ? s.getType() : null,
                       value: s.getValue ? s.getValue() : null}); }
  } catch (e) { out.sErr = String(e); }
  try { const es = gc.getEmblems ? gc.getEmblems() : {};
    for (const id in es) { const e = es[id];
      out.emblems.push({pid: e.getPlayerId ? e.getPlayerId() : null, type: e.getType ? e.getType() : null}); } } catch (e) {}
  try { out.victoryGold = gc.getVictoryGoldAmount ? gc.getVictoryGoldAmount() : null; } catch (e) {}
  return JSON.stringify(out);
})()"""

# 用游戏自己的消息类构造一次"坦克被击毁"事件（killer/victim 可指定）
KILL_JS = """(() => {
  const gc = GameManager.getGameController();
  const M = (window.__TT_MSG && window.__TT_MSG[3]);
  if (!M) return JSON.stringify({ok: false, err: 'no TankKilledMessage'});
  let m;
  try { m = new M(); } catch (e) { m = Object.create(M.prototype); }
  try {
    if (m.setVictimPlayerId) m.setVictimPlayerId(VICTIM); else m.victimPlayerId = VICTIM;
    if (m.setKillerPlayerId) m.setKillerPlayerId(KILLER); else m.killerPlayerId = KILLER;
  } catch (e) { return JSON.stringify({ok: false, err: 'set failed: ' + e}); }
  const before = JSON.stringify((gc.getScores ? gc.getScores() : {}));
  try { gc.killTank(m); } catch (e) { return JSON.stringify({ok: false, err: 'killTank: ' + e}); }
  return JSON.stringify({ok: true, victim: VICTIM, killer: KILLER});
})()"""


def brief(states):
    """把分数列表压成 {pid: {KILL: n, VICTORY: n}}"""
    out = {}
    for s in states.get('scores', []):
        out.setdefault(s['pid'], {})[s['type']] = s['value']
    return out


async def read(ctl):
    return json.loads(await ctl.cdp.eval(SCORES_JS))


async def wait_in_round(ctl, timeout=90.0):
    """等回合真正开始（本地模式下开局前 killTank 会被拒绝：
    "Attempt to kill tank while round was not started"）。"""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        st = json.loads(await ctl.cdp.eval(
            "(() => { try { return JSON.stringify(String(GameManager.getGameController().model.getState())); }"
            " catch (e) { return JSON.stringify('none'); } })()"))
        if st == 'in round':
            return True
        await asyncio.sleep(1)
    return False


async def main():
    ap = argparse.ArgumentParser(description='计分规则实测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    live = json.loads(await ctl.cdp.eval(
        "(() => { const gc = GameManager.getGameController();"
        " if (!gc) return '[]'; const tanks = Object.keys(gc.getTanks() || {});"
        " const users = Users.getAllPlayerIds();"
        " return JSON.stringify(tanks.filter(p => users.indexOf(p) >= 0)); })()"))
    if len(live) < 2:
        print('[score] 需要 2 辆我方存活坦克（当前 %s）→ 先开一局两人本地局' % live, flush=True)
        return 1
    a, b = live[0], live[1]
    print('[score] 我方坦克: A=%s B=%s' % (a, b), flush=True)

    res = {'initial': None, 'kill': None, 'suicide': None}

    st = await read(ctl)
    res['initial'] = st
    print('\n[score] ── 初始状态 ──', flush=True)
    print('[score] 分数: %s' % json.dumps(brief(st), ensure_ascii=False), flush=True)
    print('[score] 徽章: %s  胜利金币: %s  回合: %s'
          % (st.get('emblems'), st.get('victoryGold'), st.get('mode')), flush=True)

    # ── 用例 1：A 击杀 B（killer != victim）→ 期望 A.KILL +1
    print('\n[score] 等回合开始…', flush=True)
    if not await wait_in_round(ctl):
        print('[score] ❌ 回合一直没开始', flush=True)
        return 1
    pre = await read(ctl)
    print('[score] ── 用例1：A(%s) 击杀 B(%s)；开火前分数=%s ──'
          % (a, b, json.dumps(brief(pre), ensure_ascii=False)), flush=True)
    r = json.loads(await ctl.cdp.eval(KILL_JS.replace('VICTIM', json.dumps(b)).replace('KILLER', json.dumps(a))))
    print('[score] 触发击杀事件: %s' % json.dumps(r, ensure_ascii=False), flush=True)
    await asyncio.sleep(1.5)
    st = await read(ctl)
    res['kill'] = {'before': pre, 'after': st}
    print('[score] 分数: %s' % json.dumps(brief(st), ensure_ascii=False), flush=True)
    print('[score] 变化: %s' % json.dumps(
        {p: {'before': brief(pre).get(p), 'after': brief(st).get(p)}
         for p in set(list(brief(pre)) + list(brief(st)))}, ensure_ascii=False), flush=True)

    # ── 用例 2：自杀（killer == victim）→ 期望【无人加分】（等下一回合开始）
    print('\n[score] 等下一回合开始…', flush=True)
    if not await wait_in_round(ctl):
        print('[score] ⚠️ 未等到新回合（可能回合未结束），仍在当前回合内做自杀用例', flush=True)
    pre2 = await read(ctl)
    print('[score] ── 用例2：A(%s) 自杀（killer==victim）；前分数=%s ──'
          % (a, json.dumps(brief(pre2), ensure_ascii=False)), flush=True)
    r = json.loads(await ctl.cdp.eval(KILL_JS.replace('VICTIM', json.dumps(a)).replace('KILLER', json.dumps(a))))
    print('[score] 触发自杀事件: %s' % json.dumps(r, ensure_ascii=False), flush=True)
    await asyncio.sleep(1.5)
    st = await read(ctl)
    res['suicide'] = {'before': pre2, 'after': st}
    print('[score] 分数: %s' % json.dumps(brief(st), ensure_ascii=False), flush=True)
    print('[score] 变化: %s' % json.dumps(
        {p: {'before': brief(pre2).get(p), 'after': brief(st).get(p)}
         for p in set(list(brief(pre2)) + list(brief(st)))}, ensure_ascii=False), flush=True)
    print('\n[score] 判据：KILL 分量只有 killer!=victim 时才 +1；自杀不变（无惩罚）', flush=True)

    # ── 用例 3：清场（A 杀掉其余所有坦克）→ 回合结束 → 期望幸存者 VICTORY +1
    #    ★ 前置：必须等 A 自己在场上（用例2 自杀后 A 是死的，不算"最后存活者"）
    print('\n[score] ── 用例3：A(%s) 活着并清场，期望 VICTORY +1 ──' % a, flush=True)
    alive = False
    for _ in range(90):
        stx = json.loads(await ctl.cdp.eval(
            "(() => { const gc = GameManager.getGameController();"
            " if (!gc) return JSON.stringify({state: 'none', tanks: []});"
            " return JSON.stringify({state: String(gc.model.getState()),"
            " tanks: Object.keys(gc.getTanks() || {})}); })()"))
        if stx.get('state') == 'in round' and a in (stx.get('tanks') or []):
            alive = True
            break
        await asyncio.sleep(1)
    if not alive:
        print('[score] ⚠️ 等不到 A 在场的新回合，跳过用例 3', flush=True)
    else:
        pre3 = await read(ctl)
        tanks = stx['tanks']
        victims = [p for p in tanks if p != a]
        print('[score] 场上坦克=%s → 逐个由 A 击杀: %s' % (tanks, victims), flush=True)
        for v in victims:
            await ctl.cdp.eval(KILL_JS.replace('VICTIM', json.dumps(v)).replace('KILLER', json.dumps(a)))
            await asyncio.sleep(1.2)
        # 等回合结束（ROUND_ENDED → VICTORY +1）
        st3 = await read(ctl)
        for _ in range(20):
            await asyncio.sleep(1)
            st3 = await read(ctl)
            if brief(st3).get(a, {}).get(1, 0) > brief(pre3).get(a, {}).get(1, 0):
                break
        res['victory'] = {'before': pre3, 'after': st3, 'victims': victims}
        print('[score] 前=%s' % json.dumps(brief(pre3), ensure_ascii=False), flush=True)
        print('[score] 后=%s' % json.dumps(brief(st3), ensure_ascii=False), flush=True)
        print('[score] 变化: %s' % json.dumps(
            {p: {'before': brief(pre3).get(p), 'after': brief(st3).get(p)}
             for p in set(list(brief(pre3)) + list(brief(st3)))}, ensure_ascii=False), flush=True)

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print('[score] 明细 → %s' % args.json, flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
