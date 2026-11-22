"""Survival-first Mech Royale bot (mechfight/1).

Scoring is placement, so the plan is: get inside the next zone circle early, hide in tall cover,
stay quiet, run from groups, shoot back only when it pays, and only grab loot when nobody is near.
Standard library only. Nothing is ever printed to stdout except by mechfight.run().
"""
import math
import random
import sys
import time
import traceback

import mechfight as mf
from navmap import NavMap, Search

TAU = 2.0 * math.pi
UPGRADE_RANK = [
    "armor", "plating", "nanites", "sensors", "servos", "gyros",
    "laser_damage", "gun_damage", "lightning_efficiency", "laser_charge", "gun_rate", "gun_mag",
    "gun_velocity", "capacitor", "reactor", "jets", "lightning_chain", "lightning_range",
    "mortar_reload", "mortar_splash", "mortar_velocity", "laser_pierce",
]
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}
SOUND_THREATS = ("gun", "mortar", "laser", "lightning", "explosion", "laser_charge")
SOUND_DIST = {"near": 40.0, "mid": 110.0, "far": 220.0}


def angdiff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def fth(d):
    """Danger of an enemy at distance d (1 at point blank, ~0.5 at 70 m, 0.1 at 200 m)."""
    return 1.0 / (1.0 + (d / 70.0) ** 2)


class Bot:
    name = "survivor-v1"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        self.slot = hello.get("slot", 0)
        self.rng = random.Random(hello.get("seed", 0))
        m = hello.get("map") or {}
        self.nav = NavMap(m.get("dir", "/arena"), m.get("files"))
        self.known = {}            # enemy id -> (x, y, t) last seen
        self.sound_threats = []    # [x, y, weight, expire_t]
        self.dmg_log = []          # (t, amount) from non-zone damage
        self.last_dmg_t = -99.0
        self.last_dmg_bearing = None
        self.bad_pickups = set()
        self.goal = None
        self.goal_key = None
        self.goal_t = -99.0
        self.search = None
        self.search_goal = None
        self.path = []
        self.pi = 0
        self.prev_dir = (0.0, 0.0)
        self.hist = []
        self.escape_until = -1.0
        self.escape_dir = None
        self.strafe_sign = 1
        self.strafe_until = 0.0
        self.crate_t0 = {}
        self.errors = 0
        self.last_move = [0.0, 0.0]
        self.phase = self.rng.uniform(0, TAU)

    # ------------------------------------------------------------------ entry point
    def act(self, obs):
        try:
            return self._act(obs)
        except Exception:
            self.errors += 1
            if self.errors <= 3:
                mf.log("act error:\n" + traceback.format_exc())
            return self._fallback(obs)

    def _fallback(self, obs):
        act = {"tick": obs["tick"]}
        try:
            me = obs["self"]
            z = obs["zone"]
            tx, ty = z["next_center"] if z.get("next_radius", 0) > 0 else z["center"]
            yaw = math.atan2(ty - me["pos"][1], tx - me["pos"][0])
            if math.hypot(tx - me["pos"][0], ty - me["pos"][1]) > 15:
                act["move"] = [math.cos(yaw), math.sin(yaw)]
            if obs.get("offer"):
                act["pick"] = 0
        except Exception:
            pass
        return act

    # ------------------------------------------------------------------ main logic
    def _act(self, obs):
        t0 = time.perf_counter()
        me = obs["self"]
        tick = obs["tick"]
        t = obs["t"]
        px, py, pz = me["pos"]
        stats = me.get("stats", {})
        nav = self.nav
        action = {"tick": tick}

        if obs.get("offer"):
            action["pick"] = self._choose_upgrade(obs["offer"])

        self._events(obs, t, px, py)

        # ---- who is around
        enemies = []
        npcs = []
        for m in obs.get("mechs", []):
            if m.get("kind") == "mech":
                if m.get("id") != me.get("id"):
                    enemies.append(m)
            else:
                npcs.append(m)
        threats = []
        for e in enemies:
            ex, ey, ez = e["pos"]
            d = math.hypot(ex - px, ey - py)
            w = 1.0
            ws = e.get("weapons") or []
            if "laser" in ws and d < 280:
                w += 0.5
            if "lightning" in ws and d < 60:
                w += 0.4
            if "mortar" in ws:
                w += 0.15
            if e.get("charging"):
                w += 0.5
            mh = e.get("max_hp") or 1400.0
            if e.get("hp") is not None and e["hp"] < 0.35 * mh:
                w *= 0.6
            w *= 1.0 + 0.03 * (e.get("cubes") or 0)
            ev = e.get("vel") or [0, 0, 0]
            threats.append((ex, ey, ev[0], ev[1], w, e["id"], d))
            self.known[e["id"]] = (ex, ey, t)
        # sounds -> rough threat positions
        for s in obs.get("sounds", []):
            if s.get("kind") in SOUND_THREATS:
                dist = SOUND_DIST.get(s.get("dist"), 150.0)
                wgt = 0.45 if s.get("dist") == "near" else (0.3 if s.get("dist") == "mid" else 0.15)
                b = s.get("bearing", 0.0)
                self.sound_threats.append([px + math.cos(b) * dist, py + math.sin(b) * dist, wgt, t + 2.5])
        self.sound_threats = [s for s in self.sound_threats if s[3] > t][-12:]
        all_threats = list(threats)
        for sx, sy, sw, _ in self.sound_threats:
            all_threats.append((sx, sy, 0.0, 0.0, sw, -1, math.hypot(sx - px, sy - py)))
        for n in npcs:
            nx_, ny_, _ = n["pos"]
            dn = math.hypot(nx_ - px, ny_ - py)
            if dn < 70:
                all_threats.append((nx_, ny_, 0.0, 0.0, 0.12, -2, dn))
        L = sum(th[4] * fth(th[6]) for th in all_threats)
        real_L = sum(th[4] * fth(th[6]) for th in threats)
        recent_dmg = sum(a for (tt, a) in self.dmg_log if t - tt < 2.0)
        hit_recent = (t - self.last_dmg_t) < 3.0
        calm = L < 0.09 and not hit_recent

        # ---- zone
        z = obs["zone"]
        zc, zr = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        if nr < zr - 1.0:
            ec, er = nc, max(nr, 8.0)
        else:
            ec, er = zc, zr
        Rs = max(6.0, er - 15.0)
        dz_me = math.hypot(px - ec[0], py - ec[1])
        in_zone = me.get("in_zone", True)
        zone_dps = z.get("dps", 20.0)

        # ---- choose where to go
        tgt = None            # (x, y, kind, ref)
        opp = None
        if calm:
            opp = self._opportunity(obs, me, t, ec, er, px, py)
        if opp is not None:
            tgt = opp
        else:
            self._ensure_goal(ec, er, Rs, px, py, t, threats)
            if self.goal is not None:
                tgt = (self.goal[0], self.goal[1], "hide", None)

        # ---- path planning toward target (time-sliced A*)
        wp = None
        if tgt is not None:
            tx, ty = tgt[0], tgt[1]
            dist_t = math.hypot(tx - px, ty - py)
            if dist_t > 14.0 and not nav.ray_free(px, py, tx, ty):
                self._plan(px, py, tx, ty, t0)
                wp = self._waypoint(px, py)
            else:
                self.search = None
                self.path = []
                wp = (tx, ty)
        else:
            self.search = None

        # ---- stuck detection
        self.hist.append((px, py))
        if len(self.hist) > 14:
            self.hist.pop(0)

        holding = False
        if tgt is not None and tgt[2] == "crate" and tgt[3] and tgt[3].get("shoot"):
            holding = True
        if tgt is not None and tgt[2] == "drop" and math.hypot(tgt[0] - px, tgt[1] - py) < 3.0:
            holding = True

        if (not holding and t >= self.escape_until and len(self.hist) >= 14 and self.cfgmove(self.last_move)
                and math.hypot(px - self.hist[0][0], py - self.hist[0][1]) < 2.0):
            self.escape_until = t + 1.0
            best, bd = None, -1.0
            for k in range(16):
                a = k * TAU / 16 + self.rng.uniform(-0.1, 0.1)
                r = nav.free_run(px, py, math.cos(a), math.sin(a), 24.0)
                if r > bd:
                    bd, best = r, a
            self.escape_dir = best
            self.search = None
            self.path = []
            self.hist = []

        # ---- incoming fire (dodge vectors)
        dodge = self._incoming(obs, me, enemies, px, py, pz, t)

        # ---- steering
        move = [0.0, 0.0]
        boost = False
        jump = False
        if t < self.escape_until and self.escape_dir is not None:
            move = [math.cos(self.escape_dir), math.sin(self.escape_dir)]
            boost = me.get("energy", 0) > 15
            jump = me.get("grounded", True) and (tick % 6 == 0)
        elif holding:
            move = [0.0, 0.0]
        else:
            move, boost = self._steer(me, px, py, wp, tgt, all_threats, L, recent_dmg, dodge, ec, Rs, in_zone, t, calm)
        if holding:
            self.prev_dir = (0.0, 0.0)
        elif math.hypot(move[0], move[1]) > 0.01:
            self.prev_dir = (move[0], move[1])
        action["move"] = [round(move[0], 3), round(move[1], 3)]
        self.last_move = move
        action["boost"] = bool(boost)
        if jump:
            action["jump"] = True

        # ---- aim / fire
        self._combat(obs, me, enemies, threats, t, px, py, pz, stats, action, ec, tgt, holding, hit_recent, boost)

        # ---- loot actions (take / reload)
        self._loot(obs, me, action, px, py, pz)

        if mf.draw is not None and tick % 5 == 0:
            try:
                mf.draw.label("%s L%.2f hp%d" % ("calm" if calm else "evade", L, me["hp"]))
                if tgt is not None:
                    mf.draw.point([tgt[0], tgt[1], pz], "#40c0ff", 5)
            except Exception:
                pass
        return action

    @staticmethod
    def cfgmove(m):
        return abs(m[0]) + abs(m[1]) > 0.2

    # ------------------------------------------------------------------ events & upgrades
    def _events(self, obs, t, px, py):
        for ev in obs.get("events", []):
            ty = ev.get("type")
            if ty == "damaged":
                if ev.get("weapon") != "zone":
                    self.last_dmg_t = t
                    self.dmg_log.append((t, ev.get("amount", 0.0)))
                    b = ev.get("bearing")
                    if b is not None:
                        self.last_dmg_bearing = (b, t)
                        self.sound_threats.append([px + math.cos(b) * 50, py + math.sin(b) * 50, 0.8, t + 2.0])
            elif ty in ("take_failed", "took"):
                self.bad_pickups.add(ev.get("id"))
        self.dmg_log = [(tt, a) for (tt, a) in self.dmg_log if t - tt < 3.0]

    def _choose_upgrade(self, offer):
        best, bi = 1e9, 0
        for i, c in enumerate(offer.get("choices", [])):
            cid = c.get("id")
            r = UPGRADE_RANK.index(cid) if cid in UPGRADE_RANK else 50
            if r < best:
                best, bi = r, i
        return bi

    # ------------------------------------------------------------------ goal selection
    def _ensure_goal(self, ec, er, Rs, px, py, t, threats):
        key = (round(ec[0] / 4), round(ec[1] / 4), round(er / 4))
        need = self.goal is None or key != self.goal_key
        if not need:
            gx, gy = self.goal
            if math.hypot(gx - ec[0], gy - ec[1]) > Rs + 4.0:
                need = True
            elif t - self.goal_t > 3.0:
                for th in threats:
                    if math.hypot(th[0] - gx, th[1] - gy) < 90.0:
                        need = True
                        break
        if need:
            g = self._pick_goal(ec, er, Rs, px, py, t, threats)
            if g is not None:
                self.goal = g
                self.goal_key = key
                self.goal_t = t
                self.search = None
                self.path = []

    def _pick_goal(self, ec, er, Rs, px, py, t, threats):
        nav = self.nav
        best, bs = None, -1e9
        n = 0
        tries = 0
        rng = self.rng

        known = [
            (kx, ky)
            for (kx, ky, kt) in self.known.values()
            if t - kt < 25.0
        ]

        while n < 180 and tries < 700:
            tries += 1
            r = Rs * math.sqrt(rng.uniform(0.01, 1.0))
            a = rng.uniform(0.0, TAU)
            x = ec[0] + r * math.cos(a)
            y = ec[1] + r * math.sin(a)

            if not nav.nav_free(x, y) or nav.blocked(x, y):
                continue

            n += 1
            enc = nav.enclosure(x, y)
            d = math.hypot(x - px, y - py)

            s = 2.5 * enc - d / 180.0
            s -= 0.7 * abs(r / max(Rs, 1.0) - 0.55)

            for kx, ky in known:
                dd = math.hypot(x - kx, y - ky)
                if dd < 130.0:
                    s -= 1.2 * (1.0 - dd / 130.0)

            for th in threats:
                ex = th[0] + th[2] * 2.0
                ey = th[1] + th[3] * 2.0
                dd = math.hypot(x - ex, y - ey)

                if dd < 180.0:
                    s -= 4.0 * th[4] * math.exp(-dd / 100.0)

            if s > bs:
                bs, best = s, (x, y)

        if best is None:
            return (ec[0], ec[1])

        return best

    # ------------------------------------------------------------------ opportunities (loot when safe)
    def _opportunity(self, obs, me, t, ec, er, px, py):
        if t < 3.0 and False:
            return None
        if t < 150:
            reach = 45.0
        elif t < 260:
            reach = 28.0
        else:
            reach = 12.0
        nav = self.nav
        known = [(kx, ky) for (kx, ky, kt) in self.known.values() if t - kt < 20.0]
        cubes_have = me.get("cubes", 0)
        w1 = me["weapons"][1] if len(me.get("weapons", [])) > 1 else None
        best, bd = None, 1e9

        def ok_spot(x, y):
            if math.hypot(x - ec[0], y - ec[1]) > er - 10.0:
                return False
            for kx, ky in known:
                if math.hypot(x - kx, y - ky) < 60.0:
                    return False
            return True

        for pk in obs.get("pickups", []):
            if pk["id"] in self.bad_pickups or not self._want_weapon(me, pk["kind"]):
                continue
            x, y, z = pk["pos"]
            if z - pz_safe(self.nav, x, y) > 6.0:
                continue
            d = math.hypot(x - px, y - py)
            if d < reach and d < bd and ok_spot(x, y) and nav.nav_free(x, y):
                best, bd = (x, y, "pickup", pk), d
        if cubes_have < 10:
            for cb in obs.get("cubes", []):
                x, y, z = cb["pos"]
                d = math.hypot(x - px, y - py)
                if d < reach and d < bd and ok_spot(x, y) and z - pz_safe(self.nav, x, y) < 6.0:
                    best, bd = (x, y, "cube", cb), d
        if t < 200:
            for cr in obs.get("crates", []):
                x, y, z = cr["pos"]
                d = math.hypot(x - px, y - py)
                if d < min(reach, 32.0) and d + 8 < bd and ok_spot(x, y) and nav.sight_clear(px, py, x, y):
                    t0 = self.crate_t0.setdefault(cr["id"], t)
                    if t - t0 > 9.0:
                        continue
                    info = {"id": cr["id"], "z": z, "shoot": d < 24.0}
                    best, bd = (x, y, "crate", info), d + 8
        for dr in obs.get("drops", []):
            x, y, z = dr["pos"]
            d = math.hypot(x - px, y - py)
            holder = dr.get("holder")
            if dr.get("lands_in", 99) <= 8 and d < 110 and holder in (None, me.get("id")) and ok_spot(x, y):
                if best is None or best[2] not in ("pickup", "cube") or d < 30:
                    best, bd = (x, y, "drop", dr), -1.0
        return best

    def _want_weapon(self, me, kind):
        if kind not in WEAPON_RANK or kind == "gun":
            return False
        ws = me.get("weapons", [])
        held = [w["kind"] for w in ws if w]
        if kind in held:
            return False
        if len(ws) > 1 and ws[1] is None:
            return True
        cur = ws[1]["kind"] if len(ws) > 1 and ws[1] else None
        if cur is None:
            return True
        if ws[1].get("charge", 0) > 0:
            return False
        return WEAPON_RANK[kind] > WEAPON_RANK.get(cur, 0)

    # ------------------------------------------------------------------ path following
    def _plan(self, px, py, tx, ty, t0):
        need = self.search is None or self.search_goal is None or math.hypot(self.search_goal[0] - tx, self.search_goal[1] - ty) > 10.0
        if need:
            self.search = Search(self.nav, px, py, tx, ty)
            self.search_goal = (tx, ty)
            self.path = []
            self.pi = 0
        if self.search is not None and not self.search.done:
            budget = max(0.002, 0.010 - (time.perf_counter() - t0))
            self.search.step(budget)
        if self.search is not None and self.search.done and not self.path:
            self.path = self.search.path or []
            self.pi = 0

    def _waypoint(self, px, py):
        path = self.path
        if not path:
            g = self.search_goal
            return g
        while self.pi < len(path) - 1 and math.hypot(path[self.pi][0] - px, path[self.pi][1] - py) < 7.0:
            self.pi += 1
        # skip ahead when a later waypoint is directly walkable
        for k in range(min(len(path) - 1, self.pi + 6), self.pi, -1):
            if math.hypot(path[k][0] - px, path[k][1] - py) < 90 and self.nav.ray_free(px, py, path[k][0], path[k][1]):
                self.pi = k
                break
        wp = path[self.pi]
        if not self.nav.ray_free(px, py, wp[0], wp[1]) and math.hypot(wp[0] - px, wp[1] - py) > 10:
            self.search = None   # drifted off the path: replan next tick
            self.path = []
        return wp

    # ------------------------------------------------------------------ dodging
    def _incoming(self, obs, me, enemies, px, py, pz, t):
        """Returns [(nx, ny, urgency)] lateral dodge directions for bullets, shells and charging lasers."""
        out = []
        cz = pz + 4.5
        for pr in obs.get("projectiles", []):
            x, y, z = pr["pos"]
            vx, vy, vz = pr["vel"]
            kind = pr.get("kind")
            if kind == "mortar":
                g = self.cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22.0)
                # time until the shell is back at about our height
                a, b, c = -0.5 * g, vz, z - pz
                disc = b * b - 4 * a * c
                if disc < 0:
                    continue
                tt = (-b - math.sqrt(disc)) / (2 * a)
                if tt < 0:
                    tt = (-b + math.sqrt(disc)) / (2 * a)
                if tt < 0 or tt > 2.5:
                    continue
                ix, iy = x + vx * tt, y + vy * tt
                dd = math.hypot(ix - px, iy - py)
                if dd < 14.0:
                    ax, ay = px - ix, py - iy
                    n = math.hypot(ax, ay) or 1.0
                    out.append((ax / n, ay / n, 1.5 * (1.0 - dd / 14.0) + 0.3))
                continue
            sp2 = vx * vx + vy * vy
            if sp2 < 1.0:
                continue
            rx, ry = x - px, y - py
            tt = -(rx * vx + ry * vy) / sp2
            if tt < 0 or tt > 0.8:
                continue
            cx, cy = rx + vx * tt, ry + vy * tt
            cd = math.hypot(cx, cy)
            if cd > 5.5:
                continue
            sp = math.sqrt(sp2)
            nx, ny = -vy / sp, vx / sp
            side = nx * cx + ny * cy
            if abs(side) < 0.3:
                side = self.rng.choice((-1.0, 1.0))
            sgn = -1.0 if side > 0 else 1.0
            out.append((nx * sgn, ny * sgn, 0.9 * (1.0 - tt / 0.8) + 0.2))
        for e in enemies:
            if e.get("charging"):
                ex, ey, _ = e["pos"]
                d = math.hypot(px - ex, py - ey)
                if d < 290 and d > 1:
                    yaw_to_me = math.atan2(py - ey, px - ex)
                    if abs(angdiff(e["aim"][0], yaw_to_me)) < math.asin(min(1.0, 4.0 / d)) + 0.1:
                        nx, ny = -math.sin(yaw_to_me), math.cos(yaw_to_me)
                        rel = (px - ex) * (-ny) + (py - ey) * nx
                        out.append((nx, ny, 2.0) if self.strafe_sign > 0 else (-nx, -ny, 2.0))
        return out

    # ------------------------------------------------------------------ steering
    def _steer(self, me, px, py, wp, tgt, threats, L, recent_dmg, dodge, ec, Rs, in_zone, t, calm):
        real_L = sum(th[4] * fth(th[6]) for th in threats if th[5] >= 0)
        nav = self.nav
        stats = me.get("stats", {})
        walk = stats.get("walk_speed", 11.7)
        H = 1.3
        # unit vector to the waypoint
        gx = gy = 0.0
        have_goal = False
        if wp is not None:
            dx, dy = wp[0] - px, wp[1] - py
            dn = math.hypot(dx, dy)
            if dn > 1.0:
                gx, gy = dx / dn, dy / dn
                have_goal = True
        if not in_zone:
            dx, dy = ec[0] - px, ec[1] - py
            dn = math.hypot(dx, dy) or 1.0
            ix, iy = dx / dn, dy / dn
            if not have_goal or (gx * ix + gy * iy) < 0.3:
                gx, gy, have_goal = ix, iy, True
        evasive = not calm
        goal_w = 3.0 if calm else 1.0
        if not in_zone:
            goal_w = 3.5
        if t - self.strafe_until > 0:
            self.strafe_sign = self.rng.choice((-1, 1))
            self.strafe_until = t + self.rng.uniform(0.5, 1.1)
        # direction of nearest real threat for strafing
        near = None
        for th in threats:
            if th[5] >= 0 and (near is None or th[6] < near[6]):
                near = th
        sx = sy = 0.0
        if near is not None and near[6] < 160:
            ndx, ndy = near[0] - px, near[1] - py
            nn = math.hypot(ndx, ndy) or 1.0
            sx, sy = -ndy / nn * self.strafe_sign, ndx / nn * self.strafe_sign
        cover_enemies = sorted((th for th in threats if th[5] >= 0 and th[6] < 110), key=lambda th: th[6])[:2]
        best, bs = None, -1e9
        angs = [k * TAU / 24 for k in range(24)]
        if have_goal:
            angs.append(math.atan2(gy, gx))
        pdx, pdy = self.prev_dir
        for a in angs:
            ux, uy = math.cos(a), math.sin(a)
            run = nav.free_run(px, py, ux, uy, 30.0)
            if run < 4.0:
                continue
            s = -(30.0 - run) / 30.0 * (1.0 if evasive else 0.6)
            if have_goal:
                s += goal_w * (ux * gx + uy * gy)
            qx, qy = px + ux * walk * H, py + uy * walk * H
            for th in threats:
                ex, ey = th[0] + th[2] * 1.0, th[1] + th[3] * 1.0
                s -= 8.0 * th[4] * fth(math.hypot(qx - ex, qy - ey))
            if evasive:
                for th in cover_enemies:
                    if not nav.sight_clear(qx, qy, th[0], th[1]):
                        s += 1.2 * min(1.0, th[4] * fth(th[6]) * 3.0)
                if near is not None and (hit_or_close(near, recent_dmg)):
                    s += 0.5 * (ux * sx + uy * sy)
            dzq = math.hypot(qx - ec[0], qy - ec[1])
            if dzq > Rs:
                s -= (dzq - Rs) / 8.0
            for nx, ny, urg in dodge:
                s += 2.0 * urg * (ux * nx + uy * ny)
            s += 0.25 * (ux * pdx + uy * pdy)
            s += self.rng.uniform(0.0, 0.08)
            if s > bs:
                bs, best = s, (ux, uy)
        if best is None:
            a = self.rng.uniform(0, TAU)
            best = (math.cos(a), math.sin(a))
        energy = me.get("energy", 0.0)
        boost = False
        if not in_zone and energy > 8:
            boost = True
        elif evasive and energy > 25 and (recent_dmg > 100 or me.get("hp", 1400) < 0.6 * me.get("max_hp", 1400) or real_L > 0.3 or any(d[2] >= 1.5 for d in dodge)):
            boost = True
        return [best[0], best[1]], boost

    # ------------------------------------------------------------------ combat
    def _combat(self, obs, me, enemies, threats, t, px, py, pz, stats, action, ec, tgt, holding, hit_recent, boosting):
        nav = self.nav
        weapons = me.get("weapons", [])
        muzzle = pz + stats.get("muzzle_height", 7.0)
        slots = {}
        for i, w in enumerate(weapons[:2]):
            if w:
                slots[w["kind"]] = (i, w)
        fire = [False, False]
        yaw_now = me["aim"][0]
        alive = obs.get("alive", 16)

        # Pick a visible, vulnerable enemy rather than blindly
        # choosing the nearest enemy.
        target = None
        td = 1e9
        best_target_score = 1e18

        for e in enemies:
            ex, ey, ez = e["pos"]
            d = math.hypot(ex - px, ey - py)

            if d > 260.0:
                continue

            if not nav.sight_clear(px, py, ex, ey):
                continue

            mh = float(e.get("max_hp") or 1400.0)
            hp = e.get("hp")

            hp_ratio = 1.0
            if hp is not None:
                hp_ratio = max(0.0, min(1.0, float(hp) / mh))

            score = d * (0.55 + 0.45 * hp_ratio)

            if score < best_target_score:
                best_target_score = score
                target = e
                td = d
        if target is not None:
            ex, ey, ez = target["pos"]
            aims_at_me = False
            if "aim" in target and td > 1:
                aims_at_me = abs(angdiff(target["aim"][0], math.atan2(py - ey, px - ex))) < 0.25 and td < 200
            engage = hit_recent or td < 200 or alive <= 3 or aims_at_me
            n_close = sum(1 for th in threats if th[6] < 60)
            tv = target.get("vel") or [0, 0, 0]
            cz = ez + 4.5
            laser = slots.get("laser")
            lightning = slots.get("lightning")
            mortar = slots.get("mortar")
            gun = slots.get("gun")
            used_special = False
            # laser: hold to charge, release when aligned
            if laser is not None and engage and 8.0 < td < 260.0 and n_close < 2 and not boosting:
                li, lw = laser
                lst = lw.get("stats", {})
                ct = lst.get("charge", 0.75)
                lead_t = 0.12
                ax, ay, az = ex + tv[0] * lead_t, ey + tv[1] * lead_t, cz + (tv[2] if len(tv) > 2 else 0) * lead_t
                aim = [math.atan2(ay - py, ax - px), math.atan2(az - muzzle, max(1.0, math.hypot(ax - px, ay - py)))]
                err = max(abs(angdiff(aim[0], yaw_now)), abs(aim[1] - me["aim"][1]))
                tol = math.atan2(1.8, td) + 0.01
                charge = lw.get("charge", 0.0)
                if charge > 0:
                    fire[li] = not ((charge >= ct - 0.06 and err < tol * 1.5) or charge >= ct + 1.0)
                elif lw.get("cooldown", 0) <= 0.1 and err < 0.5:
                    fire[li] = True
                used_special = True
            if not used_special and lightning is not None and td < lightning[1].get("stats", {}).get("range", 45.0) * 0.9 and me.get("energy", 0) > 12:
                li, lw = lightning
                aim = self._gun_aim(target, px, py, muzzle, cz, stats, slots, tv, td)
                cone = math.radians(lw.get("stats", {}).get("cone_deg", 30.0))
                if abs(angdiff(aim[0], yaw_now)) < cone * 0.4:
                    fire[li] = True
                used_special = False  # gun may fire too
            if not used_special and mortar is not None and engage and 30.0 < td < 190.0 and n_close < 3 and mortar[1].get("cooldown", 0) <= 0.1 and aim is None:
                mi, mw = mortar
                ms = mw.get("stats", {})
                sol = self._mortar_aim(px, py, pz, stats, ex, ey, ez, tv, ms)
                if sol is not None:
                    aim = sol
                    err = max(abs(angdiff(aim[0], yaw_now)), abs(aim[1] - me["aim"][1]))
                    if err < 0.06:
                        fire[mi] = True
                    used_special = True
            if aim is None or (not used_special and aim is not None and laser is None):
                if gun is not None and td < gun[1].get("stats", {}).get("range", 230.0) * 0.95:
                    aim = self._gun_aim(target, px, py, muzzle, cz, stats, slots, tv, td)
                    tolg = math.atan2(2.3, td) + 0.015
                    err = max(abs(angdiff(aim[0], yaw_now)), abs(aim[1] - me["aim"][1]))
                    if engage and err < tolg and (gun[1].get("ammo", 1) > 0):
                        fire[gun[0]] = True
        if aim is None:
            # no firing solution: face the nearest threat (front armour), or look where we are going
            if nearest is not None and nd < 220:
                ex, ey, ez = nearest["pos"]
                aim = [math.atan2(ey - py, ex - px), 0.0]
            elif self.last_dmg_bearing is not None and t - self.last_dmg_bearing[1] < 1.5:
                aim = [self.last_dmg_bearing[0], 0.0]
            elif self.sound_threats and not holding:
                s = max(self.sound_threats, key=lambda s: s[3])
                aim = [math.atan2(s[1] - py, s[0] - px), 0.0]
            elif tgt is not None and tgt[2] == "crate" and tgt[3]:
                cx, cy = tgt[0], tgt[1]
                dd = max(1.0, math.hypot(cx - px, cy - py))
                aim = [math.atan2(cy - py, cx - px), math.atan2(tgt[3]["z"] + 1.0 - muzzle, dd)]
                gun = slots.get("gun")
                if gun is not None and tgt[3].get("shoot") and dd < 30:
                    err = abs(angdiff(aim[0], yaw_now))
                    if err < 0.1 and gun[1].get("ammo", 1) > 0:
                        fire[gun[0]] = True
            elif holding:
                base = math.atan2(ec[1] - py, ec[0] - px)
                aim = [base + 1.3 * math.sin(0.55 * t + self.phase), 0.0]
            else:
                mv = self.last_move
                if abs(mv[0]) + abs(mv[1]) > 0.2:
                    aim = [math.atan2(mv[1], mv[0]), 0.0]
        if aim is not None:
            lo = stats.get("min_pitch", -0.7)
            hi = stats.get("max_pitch", 1.3)
            aim[1] = max(lo, min(hi, aim[1]))
            action["aim"] = [round(aim[0], 4), round(aim[1], 4)]
        if fire[0] or fire[1]:
            action["fire"] = fire
        elif any(w is not None and w.get("kind") == "laser" and w.get("charge", 0) > 0 for w in weapons[:2]):
            action["fire"] = [False, False]  # release a held charge

    def _gun_aim(self, target, px, py, muzzle, cz, stats, slots, tv, td):
        sp = 252.0
        g = slots.get("gun")
        if g is not None:
            sp = g[1].get("stats", {}).get("speed", 252.0)
        ex, ey, _ = target["pos"]
        dx, dy, dz = ex - px, ey - py, cz - muzzle
        vx, vy = tv[0], tv[1]
        a = vx * vx + vy * vy - sp * sp
        b = 2.0 * (dx * vx + dy * vy)
        c = dx * dx + dy * dy + dz * dz
        t = math.sqrt(c) / sp
        if abs(a) > 1e-6:
            disc = b * b - 4 * a * c
            if disc >= 0:
                r1 = (-b - math.sqrt(disc)) / (2 * a)
                r2 = (-b + math.sqrt(disc)) / (2 * a)
                cands = [r for r in (r1, r2) if r > 0]
                if cands:
                    t = min(cands)
        t = min(t, 1.2)
        ax, ay = ex + vx * t, ey + vy * t
        return [math.atan2(ay - py, ax - px), math.atan2(dz, max(1.0, math.hypot(ax - px, ay - py)))]

    def _mortar_aim(self, px, py, pz, stats, ex, ey, ez, tv, ms):
        v = ms.get("speed", 72.0)
        g = ms.get("gravity", self.cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22.0))
        tube = pz + stats.get("height", 9.0) + 0.5
        tx, ty = ex, ey
        for _ in range(3):
            d = math.hypot(tx - px, ty - py)
            dz = ez + 1.0 - tube
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1.0:
                return None
            th = math.atan2(v * v - math.sqrt(disc), g * d)
            tt = d / (v * math.cos(th))
            tx, ty = ex + tv[0] * tt, ey + tv[1] * tt
        return [math.atan2(ty - py, tx - px), th]

    # ------------------------------------------------------------------ loot / reload
    def _loot(self, obs, me, action, px, py, pz):
        weapons = me.get("weapons", [])
        reach = self.cfg.get("loot", {}).get("pickup_radius", 4.0) - 0.4
        best, bd = None, 1e9
        for pk in obs.get("pickups", []):
            if pk["id"] in self.bad_pickups or not self._want_weapon(me, pk["kind"]):
                continue
            x, y, z = pk["pos"]
            d = math.hypot(x - px, y - py)
            if d < reach and abs(z - pz) < 3.5 and d < bd:
                best, bd = pk, d
        if best is not None:
            action["take"] = best["id"]
            if len(weapons) > 1 and weapons[1] is not None:
                action["drop"] = 1
        gun = None
        for w in weapons[:2]:
            if w and w.get("kind") == "gun":
                gun = w
        if gun is not None and gun.get("ammo", 1) == 0 and gun.get("reload", 0) == 0:
            action["reload"] = True


def hit_or_close(th, recent_dmg):
    return recent_dmg > 0 or th[6] < 120


def pz_safe(nav, x, y):
    try:
        return nav.ground(x, y)
    except Exception:
        return 0.0


if __name__ == "__main__":
    mf.run(Bot)
