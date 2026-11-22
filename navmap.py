"""Static map model for the mechfight bot: grids built from world.json + terrain.i16.

Everything here is standard library only. Grids (2 m cells):
  B  walk-blocked cells (buildings, low bridges, steep slopes, map edge, inflated by ~1 cell)
  T  tall solids that stop sight and shots (building roofs above muzzle height)
A resumable A* runs on a 4 m grid derived from B so it can be spread over several ticks.
"""
import array
import heapq
import json
import math
import os
import sys
import time

C = 2.0  # fine grid cell size (m)
INF = 1e18


class NavMap:
    def __init__(self, map_dir, files=None):
        self.ok = False
        # Defaults match the known arena so a failed load still gives sane bounds.
        self.ox, self.oy = -513.0, -334.0
        self.tnx, self.tny = 1070, 724
        self.terrain = None
        self.minX, self.maxX, self.minY, self.maxY = -512.38, 555.08, -333.09, 388.64
        try:
            self._load(map_dir, files or ["v2/world.json", "v2/terrain.i16"])
            self.ok = True
        except Exception as e:  # never crash the bot over the map
            sys.stderr.write("navmap load failed: %r\n" % (e,))
            sys.stderr.flush()
            self._empty()
        self._enc = {}

    # ------------------------------------------------------------------ loading
    def _empty(self):
        self.nx = int((self.maxX - self.minX) / C) + 2
        self.ny = int((self.maxY - self.minY) / C) + 2
        self.ox, self.oy = self.minX, self.minY
        self.B = bytearray(self.nx * self.ny)
        self.T = bytearray(self.nx * self.ny)
        self._build_nav()

    def _load(self, map_dir, files):
        wpath = tpath = None
        for f in files:
            if f.endswith("world.json"):
                wpath = os.path.join(map_dir, f)
            elif f.endswith(".i16"):
                tpath = os.path.join(map_dir, f)
        with open(wpath) as fh:
            world = json.load(fh)
        b = world["bounds"]
        self.minX, self.maxX, self.minY, self.maxY = b["minX"], b["maxX"], b["minY"], b["maxY"]
        tr = world["terrain"]
        self.ox, self.oy = tr["minX"], tr["minY"]
        self.tnx, self.tny = tr["nx"], tr["ny"]
        arr = array.array("h")
        with open(tpath, "rb") as fh:
            arr.frombytes(fh.read())
        if sys.byteorder != "little":
            arr.byteswap()
        if len(arr) != self.tnx * self.tny:
            raise ValueError("terrain size mismatch")
        self.terrain = arr
        self.nx = (self.tnx + 1) // 2 + 1
        self.ny = (self.tny + 1) // 2 + 1
        solid = bytearray(self.nx * self.ny)
        tall = bytearray(self.nx * self.ny)
        for p in world["prisms"]:
            ring = p["ring"]
            if len(ring) < 6:
                continue
            xs = ring[0::2]
            ys = ring[1::2]
            g = sum(self.ground(x, y) for x, y in zip(xs, ys)) / len(xs)
            base_rel = p["base"] - g
            top_rel = p["top"] - g
            kind = p.get("kind")
            if kind == "bridge" and base_rel >= 5.0:
                continue  # walk under it
            if top_rel <= 1.2:
                continue  # walkable step
            self._fill(solid, ring, p.get("holes") or [])
            if kind != "bridge" and top_rel > 7.5:
                self._fill(tall, ring, p.get("holes") or [])
        self.T = tall
        # Inflate solids by one cell (mech radius 2.5 m) and add steep terrain + map edge.
        B = bytearray(solid)
        nx, ny = self.nx, self.ny
        idxs = [k for k in range(nx * ny) if solid[k]]
        for k in idxs:
            j, i = divmod(k, nx)
            for dj in (-1, 0, 1):
                jj = j + dj
                if jj < 0 or jj >= ny:
                    continue
                for di in (-1, 0, 1):
                    ii = i + di
                    if 0 <= ii < nx:
                        B[jj * nx + ii] = 1
        tan_max = math.tan(math.radians(50.0)) * 0.95
        arr = self.terrain
        tnx = self.tnx
        for j in range(ny):
            ty = min(self.tny - 3, j * 2)
            row = ty * tnx
            for i in range(nx):
                tx = min(tnx - 3, i * 2)
                h = arr[row + tx]
                if abs(arr[row + tx + 2] - h) > 200 * tan_max or abs(arr[row + 2 * tnx + tx] - h) > 200 * tan_max:
                    B[j * nx + i] = 1
        for j in range(ny):
            y = self.oy + (j + 0.5) * C
            edge_y = y < self.minY + 3.0 or y > self.maxY - 3.0
            base = j * nx
            for i in range(nx):
                x = self.ox + (i + 0.5) * C
                if edge_y or x < self.minX + 3.0 or x > self.maxX - 3.0:
                    B[base + i] = 1
        self.B = B
        self._build_nav()

    def _fill(self, grid, ring, holes):
        nx, ny = self.nx, self.ny
        edges = []
        for pts in [ring] + list(holes):
            n = len(pts) // 2
            for k in range(n):
                x0, y0 = pts[2 * k], pts[2 * k + 1]
                x1, y1 = pts[2 * ((k + 1) % n)], pts[2 * ((k + 1) % n) + 1]
                if y0 != y1:
                    edges.append((x0, y0, x1, y1))
        if not edges:
            return
        ymin = min(min(e[1], e[3]) for e in edges)
        ymax = max(max(e[1], e[3]) for e in edges)
        j0 = max(0, int((ymin - self.oy) / C) - 1)
        j1 = min(ny - 1, int((ymax - self.oy) / C) + 1)
        one = b"\x01"
        for j in range(j0, j1 + 1):
            yc = self.oy + (j + 0.5) * C
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc) != (y1 <= yc):
                    xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
            xs.sort()
            base = j * nx
            for k in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[k] - self.ox) / C - 0.5)))
                i1 = min(nx - 1, int(math.floor((xs[k + 1] - self.ox) / C - 0.5)))
                if i1 >= i0:
                    grid[base + i0:base + i1 + 1] = one * (i1 - i0 + 1)

    def _build_nav(self):
        """4 m navigation grid: a cell is free when at least 2 of its 4 fine cells are free."""
        self.n4x = self.nx // 2
        self.n4y = self.ny // 2
        n4x, n4y, nx, B = self.n4x, self.n4y, self.nx, self.B
        N = bytearray(n4x * n4y)
        for j in range(n4y):
            r0 = (2 * j) * nx
            r1 = r0 + nx
            for i in range(n4x):
                a = 2 * i
                free = 4 - (B[r0 + a] + B[r0 + a + 1] + B[r1 + a] + B[r1 + a + 1])
                if free >= 2:
                    N[j * n4x + i] = 1
        self.N = N

    # ------------------------------------------------------------------ queries
    def ground(self, x, y):
        if self.terrain is None:
            return 0.0
        fx = x - self.ox
        fy = y - self.oy
        i = int(fx)
        j = int(fy)
        if i < 0:
            i = 0
        elif i > self.tnx - 2:
            i = self.tnx - 2
        if j < 0:
            j = 0
        elif j > self.tny - 2:
            j = self.tny - 2
        tx = min(1.0, max(0.0, fx - i))
        ty = min(1.0, max(0.0, fy - j))
        a = self.terrain
        k = j * self.tnx + i
        h00 = a[k]
        h10 = a[k + 1]
        h01 = a[k + self.tnx]
        h11 = a[k + self.tnx + 1]
        h = h00 * (1 - tx) * (1 - ty) + h10 * tx * (1 - ty) + h01 * (1 - tx) * ty + h11 * tx * ty
        return h * 0.01

    def blocked(self, x, y):
        i = int((x - self.ox) / C)
        j = int((y - self.oy) / C)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return True
        return self.B[j * self.nx + i] != 0

    def tall(self, x, y):
        i = int((x - self.ox) / C)
        j = int((y - self.oy) / C)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return False
        return self.T[j * self.nx + i] != 0

    def sight_clear(self, x0, y0, x1, y1, step=3.0):
        """True when no tall building lies between the two points (2-D)."""
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        if d < 1.0:
            return True
        n = int(d / step)
        sx, sy = dx / (n + 1), dy / (n + 1)
        T, nx, ny, ox, oy = self.T, self.nx, self.ny, self.ox, self.oy
        x, y = x0, y0
        for _ in range(n):
            x += sx
            y += sy
            i = int((x - ox) / C)
            j = int((y - oy) / C)
            if 0 <= i < nx and 0 <= j < ny and T[j * nx + i]:
                return False
        return True

    def ray_free(self, x0, y0, x1, y1, step=2.0):
        """True when the straight walk from point 0 to point 1 touches no blocked fine cell."""
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        n = max(1, int(d / step))
        sx, sy = dx / n, dy / n
        B, nx, ny, ox, oy = self.B, self.nx, self.ny, self.ox, self.oy
        x, y = x0, y0
        for _ in range(n):
            x += sx
            y += sy
            i = int((x - ox) / C)
            j = int((y - oy) / C)
            if i < 0 or j < 0 or i >= nx or j >= ny or B[j * nx + i]:
                return False
        return True

    def free_run(self, x, y, ux, uy, maxd, step=2.0):
        """Distance you can walk from (x, y) along unit (ux, uy) before hitting a blocked cell (capped at maxd)."""
        B, nx, ny, ox, oy = self.B, self.nx, self.ny, self.ox, self.oy
        d = step
        while d <= maxd:
            i = int((x + ux * d - ox) / C)
            j = int((y + uy * d - oy) / C)
            if i < 0 or j < 0 or i >= nx or j >= ny or B[j * nx + i]:
                return d - step
            d += step
        return maxd

    _DIRS = [(math.cos(k * math.pi / 5), math.sin(k * math.pi / 5)) for k in range(10)]

    def enclosure(self, x, y):
        """0..1: how much tall cover surrounds a point (higher = more hidden). Cached per 8 m cell."""
        key = (int(x // 8), int(y // 8))
        v = self._enc.get(key)
        if v is not None:
            return v
        cx, cy = key[0] * 8 + 4.0, key[1] * 8 + 4.0
        T, nx, ny, ox, oy = self.T, self.nx, self.ny, self.ox, self.oy
        tot = 0.0
        for ux, uy in self._DIRS:
            d = 4.0
            while d <= 48.0:
                i = int((cx + ux * d - ox) / C)
                j = int((cy + uy * d - oy) / C)
                if 0 <= i < nx and 0 <= j < ny and T[j * nx + i]:
                    tot += 1.0 - d / 52.0
                    break
                d += 4.0
        v = tot / 10.0
        self._enc[key] = v
        return v

    def nav_free(self, x, y):
        i = int((x - self.ox) / (2 * C))
        j = int((y - self.oy) / (2 * C))
        if i < 0 or j < 0 or i >= self.n4x or j >= self.n4y:
            return False
        return self.N[j * self.n4x + i] != 0


class Search:
    """Resumable weighted A* on the 4 m grid. Call step(budget_s) until .done."""

    NB = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]

    def __init__(self, nav, sx, sy, gx, gy, weight=1.3):
        self.nav = nav
        self.goal_xy = (gx, gy)
        self.w = weight
        self.done = False
        self.path = None
        self.start = self._nearest_free(sx, sy)
        self.goal = self._nearest_free(gx, gy)
        if self.start is None or self.goal is None:
            self.done = True
            self.path = []
            return
        n = nav.n4x * nav.n4y
        self.g = {}
        self.parent = {}
        self.closed = bytearray(n)
        self.open = []
        s = self.start
        self.g[s] = 0.0
        heapq.heappush(self.open, (self._h(s), s))
        self.best = s
        self.best_h = self._h(s)

    def _cell(self, x, y):
        nav = self.nav
        i = int((x - nav.ox) / (2 * C))
        j = int((y - nav.oy) / (2 * C))
        i = min(nav.n4x - 1, max(0, i))
        j = min(nav.n4y - 1, max(0, j))
        return j * nav.n4x + i

    def _nearest_free(self, x, y):
        nav = self.nav
        k = self._cell(x, y)
        if nav.N[k]:
            return k
        n4x, n4y = nav.n4x, nav.n4y
        j0, i0 = divmod(k, n4x)
        for r in range(1, 12):
            best = None
            bd = 1e9
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < n4x and 0 <= j < n4y and nav.N[j * n4x + i]:
                        d = di * di + dj * dj
                        if d < bd:
                            bd, best = d, j * n4x + i
            if best is not None:
                return best
        return None

    def _h(self, k):
        n4x = self.nav.n4x
        j, i = divmod(k, n4x)
        gj, gi = divmod(self.goal, n4x)
        dx, dy = abs(i - gi), abs(j - gj)
        return self.w * (max(dx, dy) + 0.4142 * min(dx, dy))

    def step(self, budget_s):
        if self.done:
            return True
        deadline = time.perf_counter() + budget_s
        nav = self.nav
        N, n4x, n4y = nav.N, nav.n4x, nav.n4y
        open_, g, parent, closed = self.open, self.g, self.parent, self.closed
        goal = self.goal
        count = 0
        while open_:
            count += 1
            if (count & 31) == 0 and time.perf_counter() > deadline:
                return False
            f, k = heapq.heappop(open_)
            if closed[k]:
                continue
            closed[k] = 1
            if k == goal:
                self._finish(k)
                return True
            j, i = divmod(k, n4x)
            gk = g[k]
            for di, dj, cost in self.NB:
                ni, nj = i + di, j + dj
                if ni < 0 or nj < 0 or ni >= n4x or nj >= n4y:
                    continue
                nk = nj * n4x + ni
                if not N[nk] or closed[nk]:
                    continue
                if di and dj and not (N[j * n4x + ni] and N[nj * n4x + i]):
                    continue  # no corner cutting
                ng = gk + cost
                if ng < g.get(nk, INF):
                    g[nk] = ng
                    parent[nk] = k
                    h = self._h(nk)
                    if h < self.best_h:
                        self.best_h, self.best = h, nk
                    heapq.heappush(open_, (ng + h, nk))
        self._finish(self.best)  # unreachable: go as near as possible
        return True

    def _finish(self, k):
        nav = self.nav
        cells = [k]
        while cells[-1] in self.parent:
            cells.append(self.parent[cells[-1]])
        cells.reverse()
        pts = []
        n4x = nav.n4x
        for c in cells:
            j, i = divmod(c, n4x)
            pts.append((nav.ox + (i + 0.5) * 2 * C, nav.oy + (j + 0.5) * 2 * C))
        # string-pull: keep only the waypoints needed for straight, unblocked walking
        out = [pts[0]]
        a = 0
        m = len(pts)
        while a < m - 1:
            b = min(m - 1, a + 40)
            while b > a + 1 and not nav.ray_free(pts[a][0], pts[a][1], pts[b][0], pts[b][1]):
                b -= 1
            out.append(pts[b])
            a = b
        self.path = out
        self.done = True
