# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为。
# For education & academic research only. No online public matches.
# =============================================================================
"""Gatling Gun 本地量测（2026-11-09 加强后）
============================================
在**本地局**给当前坦克发放 Gatling，按住开火，记录：
  - 首次弹丸延迟（含输入/服务器帧延迟的近似）
  - 1.x 秒内生成的弹丸数量、间隔
  - 武器状态 `_weaponCharge/_timeSinceFire/_newBurst/_bulletCount/_bulletsFired`
  - 弹丸存活数

本脚本只用于本地研究局；不要用于在线公开对局。
用法：
  python scripts/measure_gatling.py 9222 --role arrows --fire-sec 1.2
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kit"))
sys.path.insert(0, str(HERE.parent / "cdp_tools"))

from ttkit import Kit  # noqa: E402
from cdp import CDP  # noqa: E402


SETUP_JS = r"""
(() => {
  const pid = %(pid)s;
  const gc = GameManager.getGameController();
  if (!gc) return {ok:false, error:'no game controller'};
  const rc = gc.roundController;
  const t = gc.getTanks()[pid];
  if (!t) return {ok:false, error:'no tank ' + pid};
  const T = Constants.COLLECTIBLE_TYPES;
  const before = gc.getActiveWeapon(pid);
  rc.spawnCrate(T.CRATE_GATLING_GUN, {x: t.getX(), y: t.getY(), rotation: 0});
  const cols = Object.values(gc.getCollectibles() || {});
  const col = cols.find(c => c.getType() === T.CRATE_GATLING_GUN);
  if (!col) return {ok:false, error:'gatling crate not found'};
  rc.pickUpCrate({getCollectibleId: () => col.id, getPlayerId: () => pid});
  const w = gc.getActiveWeapon(pid);
  return {
    ok: !!(w && w.type === 5),
    pid,
    tank: [t.getX(), t.getY()],
    before_type: before ? before.type : null,
    weapon_type: w ? w.type : null,
    bullet_count: w ? w._bulletCount : null,
    keys: w ? Object.keys(w) : null
  };
})()
"""

START_REC_JS = r"""
(() => {
  const pid = %(pid)s;
  window.__grec = {
    t0: performance.now(), start: null, stop: false,
    seen: {}, events: [], samples: [],
    pid
  };
  // 关键：把开火前已经在飞的弹丸记入 seen，避免把上一轮的残留算成本轮新弹
  try {
    const gc0 = GameManager.getGameController();
    const ps0 = (gc0 && gc0.getProjectiles()) || {};
    for (const id of Object.keys(ps0)) window.__grec.seen[id] = 1;
    window.__grec.baseline_projectiles = Object.keys(ps0).length;
  } catch (e) { window.__grec.baseline_projectiles = null; }
  window.__grec.timer = setInterval(() => {
    if (!window.__grec || window.__grec.stop) return;
    try {
      const gc = GameManager.getGameController();
      if (!gc) return;
      const now = performance.now();
      const ws = gc.getActiveWeapon(pid);
      const ps = gc.getProjectiles() || {};
      for (const id of Object.keys(ps)) {
        if (window.__grec.seen[id]) continue;
        window.__grec.seen[id] = 1;
        let p = null;
        try { p = gc.getProjectile(id); } catch (e) {}
        window.__grec.events.push({
          t: now - window.__grec.t0,
          rel: window.__grec.start === null ? null : now - window.__grec.start,
          id,
          x: p && p.getX ? p.getX() : null,
          y: p && p.getY ? p.getY() : null
        });
      }
      window.__grec.samples.push({
        t: now - window.__grec.t0,
        charge: ws && ws._weaponCharge !== undefined ? ws._weaponCharge : null,
        time_since_fire: ws && ws._timeSinceFire !== undefined ? ws._timeSinceFire : null,
        new_burst: ws && ws._newBurst !== undefined ? ws._newBurst : null,
        bullets: ws && ws._bulletCount !== undefined ? ws._bulletCount : null,
        fired: ws && ws._bulletsFired !== undefined ? ws._bulletsFired : null,
        projectiles: Object.keys(ps).length
      });
    } catch (e) {}
  }, 5);
  return {ok: true};
})()
"""


async def main():
    ap = argparse.ArgumentParser(description="量测本地局 Gatling Gun")
    ap.add_argument("port", nargs="?", default="9222")
    ap.add_argument("--role", default="arrows")
    ap.add_argument("--fire-sec", type=float, default=1.2)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    kit = await Kit(port=str(args.port)).connect()
    cdp = CDP(port=int(args.port))
    await cdp.connect_page("tanktrouble.com", timeout=15)
    try:
        await cdp.send("Page.bringToFront", {})
        await asyncio.sleep(0.5)
        hidden = await cdp.eval("document.hidden")
        if hidden:
            raise RuntimeError("游戏页在后台，Phaser 会冻结；请先把游戏标签页切到前台")

        pid = await kit.my_pid(args.role)
        if not pid:
            raise RuntimeError(f"没有 role={args.role} 的可控角色")
        pid = str(pid)
        state = await kit.round_state()
        if state != "in round":
            for _ in range(20):
                await asyncio.sleep(0.5)
                if await kit.round_state() == "in round":
                    break
            else:
                raise RuntimeError(f"round_state={await kit.round_state()}，不在对局中")

        def as_obj(v):
            return json.loads(v) if isinstance(v, (str, bytes, bytearray)) else v
        setup = as_obj(await cdp.eval(SETUP_JS % {"pid": json.dumps(pid)}))
        print("[gatling] setup:", json.dumps(setup, ensure_ascii=False), flush=True)
        # 测试用：清空当前弹丸并补满 Gatling 弹药/重置 spin-up 状态，保证重复实验可比
        await cdp.eval(
            "(() => { const gc=GameManager.getGameController();"
            " const pid=%s; const w=gc.getActiveWeapon(pid);"
            " if(w){ w._bulletCount=20; w._weaponCharge=0;"
            " w._timeSinceFire=0; w._newBurst=true; w._bulletsFired=0; }"
            " return 'reset'; })()" % json.dumps(pid))
        await asyncio.sleep(0.2)
        if not setup.get("ok"):
            raise RuntimeError("Gatling 发放失败: " + json.dumps(setup, ensure_ascii=False))
        if setup.get("before_type") == 5:
            raise RuntimeError(
                "当前已是 Gatling（重复发放会进武器队列，测量到的是旧枪）；"
                "请重启本地局后再测：python scripts/start_local_game.py auto --players 2 --theme 0 --restart")
        round_id0 = await kit.round_id()

        rec = as_obj(await cdp.eval(START_REC_JS % {"pid": json.dumps(pid)}))
        if not rec.get("ok"):
            raise RuntimeError("recorder 启动失败")
        await asyncio.sleep(0.15)
        await cdp.eval("window.__grec.start = performance.now()")
        await kit.fire(pid, args.fire_sec)
        await asyncio.sleep(0.35)
        raw = await cdp.eval(
            "window.__grec.stop = true; clearInterval(window.__grec.timer);"
            "JSON.stringify({events: window.__grec.events, samples: window.__grec.samples,"
            " baseline_projectiles: window.__grec.baseline_projectiles})")
        data = as_obj(raw)

        round_id1 = await kit.round_id()
        window_ms = float(args.fire_sec) * 1000.0 + 60.0
        events_all = data.get("events") or []
        events = [e for e in events_all
                  if e.get("rel") is not None and 0 <= float(e["rel"]) <= window_ms]
        samples = data.get("samples") or []
        rels = [e["rel"] for e in events]
        intervals = [b - a for a, b in zip(rels, rels[1:]) if b >= a]
        bullets = [s["bullets"] for s in samples if s.get("bullets") is not None]
        charges = [s["charge"] for s in samples if s.get("charge") is not None]
        result = {
            "ok": True,
            "pid": pid,
            "round_id": round_id0,
            "round_changed": round_id0 != round_id1,
            "setup": setup,
            "window_ms": window_ms,
            "fire_sec": args.fire_sec,
            "baseline_projectiles": data.get("baseline_projectiles"),
            "weapon": await kit.weapon(pid),
            "ammo": await kit.ammo(pid),
            "projectiles_generated": len(events),
            "first_projectile_rel_ms": round(rels[0], 2) if rels else None,
            "projectile_intervals_ms": [round(x, 2) for x in intervals],
            "projectiles_per_sec": round(len(events) / args.fire_sec, 3),
            "bullet_count_min": min(bullets) if bullets else None,
            "bullet_count_max": max(bullets) if bullets else None,
            "charge_max": max(charges) if charges else None,
            "events": events[:40],
            "samples_tail": samples[-8:],
        }
        print("[gatling] result:", json.dumps(
            {k: v for k, v in result.items() if k not in ("events", "samples_tail")},
            ensure_ascii=False), flush=True)
        if args.json:
            p = Path(args.json)
            if not p.is_absolute():
                p = HERE.parent / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            print("[gatling] wrote", p, flush=True)
    finally:
        await cdp.close()
        await kit.close()


if __name__ == "__main__":
    asyncio.run(main())
