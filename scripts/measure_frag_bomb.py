# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为。
# =============================================================================
"""Frag Bomb 本地行为采样（2026-11-19 新武器）
================================================
本地局中给当前坦克人工发放 Frag Bomb，开火后采样：
  - 母弹(type=6)与破片(type=7)出现/消失时间
  - 破片峰值数量、总破片数
  - 武器状态/剩余弹量

只用于本地研究局；不要用于在线公开对局。
用法：
  python scripts/measure_frag_bomb.py 9222 --role arrows --json docs/evidence/frag_bomb_2026-11-19.json
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kit"))
sys.path.insert(0, str(HERE.parent / "cdp_tools"))

from ttkit import Kit, WEAPON_NAMES, PROJECTILE_NAMES  # noqa: E402
from cdp import CDP  # noqa: E402


SETUP_JS = r"""
(() => {
  const pid = %(pid)s;
  const gc = GameManager.getGameController();
  if (!gc) return {ok:false, error:'no game controller'};
  const rc = gc.roundController;
  const t = gc.getTanks()[pid];
  if (!t) return {ok:false, error:'no tank'};
  const T = Constants.COLLECTIBLE_TYPES;
  const before = gc.getActiveWeapon(pid);
  rc.spawnCrate(T.CRATE_FRAG_BOMB, {x: t.getX(), y: t.getY(), rotation: 0});
  const cols = Object.values(gc.getCollectibles() || {});
  const col = cols.find(c => c.getType() === T.CRATE_FRAG_BOMB);
  if (!col) return {ok:false, error:'frag crate not found'};
  rc.pickUpCrate({getCollectibleId: () => col.id, getPlayerId: () => pid});
  const w = gc.getActiveWeapon(pid);
  return {ok: !!(w && w.type === 6), pid, before: before ? before.type : null,
          weapon: w ? w.type : null, tank:[t.getX(), t.getY()],
          keys: w ? Object.keys(w) : null};
})()
"""


async def main():
    ap = argparse.ArgumentParser(description="本地 Frag Bomb 行为采样")
    ap.add_argument("port", nargs="?", default="9222")
    ap.add_argument("--role", default="arrows")
    ap.add_argument("--observe-sec", type=float, default=3.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    kit = await Kit(port=str(args.port)).connect()
    cdp = CDP(port=int(args.port))
    await cdp.connect_page("tanktrouble.com", timeout=15)
    try:
        await cdp.send("Page.bringToFront", {})
        pid = str(await kit.my_pid(args.role) or "")
        if not pid:
            raise RuntimeError(f"没有 role={args.role}")
        if await kit.round_state() != "in round":
            raise RuntimeError("不在本地对局中；先运行 start_local_game.py")
        v = await cdp.eval(SETUP_JS % {"pid": json.dumps(pid)})
        setup = json.loads(v) if isinstance(v, str) else v
        print("[frag] setup:", json.dumps(setup, ensure_ascii=False), flush=True)
        if not setup.get("ok"):
            raise RuntimeError("Frag Bomb 发放失败: " + json.dumps(setup, ensure_ascii=False))

        t0 = time.monotonic()
        await kit.fire(pid, 0.3)
        timeline = []
        seen = {}
        first_type = None
        first_shrapnel_t = None
        peak_shrapnel = 0
        total_shrapnel = 0
        while time.monotonic() - t0 < args.observe_sec:
            now = round(time.monotonic() - t0, 3)
            ps = await kit.projectiles()
            counts = {}
            new = []
            for p in ps:
                typ = p.get("type")
                counts[typ] = counts.get(typ, 0) + 1
                if p["id"] not in seen:
                    seen[p["id"]] = True
                    new.append(p["id"])
                    total_shrapnel += 1 if typ == 7 else 0
                    if first_type is None:
                        first_type = typ
                    if typ == 7 and first_shrapnel_t is None:
                        first_shrapnel_t = now
                peak_shrapnel = max(peak_shrapnel, counts.get(7, 0))
            if new:
                timeline.append({"t": now, "counts": counts,
                                 "new": len(new),
                                 "new_types": sorted({p.get("type") for p in ps
                                                      if p["id"] in new})})
            await asyncio.sleep(0.04)
        result = {
            "date": "2026-11-19",
            "weapon": 6,
            "weapon_name": WEAPON_NAMES.get(6),
            "pid": pid,
            "setup": setup,
            "first_projectile_type": first_type,
            "first_projectile_name": PROJECTILE_NAMES.get(first_type),
            "first_shrapnel_ms": round(first_shrapnel_t * 1000, 1) if first_shrapnel_t is not None else None,
            "peak_shrapnel": peak_shrapnel,
            "total_projectile_ids": len(seen),
            "timeline": timeline[:60],
            "weapon_end": await kit.weapon(pid),
            "note": "本地局人工发放；遇墙/接触会提前引爆；不是自然拾取 DPS 数据",
        }
        print("[frag] result:", json.dumps(
            {k: v for k, v in result.items() if k not in ("timeline", "setup")},
            ensure_ascii=False), flush=True)
        if args.json:
            p = Path(args.json)
            if not p.is_absolute():
                p = HERE.parent / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            print("[frag] wrote", p, flush=True)
    finally:
        await cdp.close()
        await kit.close()


if __name__ == "__main__":
    asyncio.run(main())
