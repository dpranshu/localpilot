"""Halftone dot-sphere: the agent's face.

A sphere made of white dots on a fully transparent background. Dots sit on latitude
rows that follow the sphere's curvature (like a halftone print wrapped on a ball);
each dot's size comes from lighting -- large toward the upper-right, shrinking to
nothing toward the lower-left and the rim -- which is what gives the halftone look.

It spins continuously (a point cloud rotated every frame), and the state changes the
motion: idle = slow spin, thinking = faster + a gentle breathing ripple, acting = fast
+ a strong ripple, done = calm, stopped = frozen and dimmed.

Rendering: dots are drawn 2x onto an alpha mask, box-downsampled (anti-aliased), then
composed as WHITE dots over a faint soft shadow (so white stays visible on bright
pages) over a near-invisible alpha=1 disc (so the window is hit-testable for dragging
without being visible). Returns straight-alpha RGBA.
"""
from __future__ import annotations

import math
import time

from PIL import Image, ImageDraw, ImageFilter

# state -> (spin rad/s, ripple amplitude, ripple speed, opacity)
_STATES = {
    "idle":     (0.35, 0.05, 1.2, 1.00),
    "thinking": (0.95, 0.17, 3.2, 1.00),
    "acting":   (1.80, 0.27, 6.0, 1.00),
    "done":     (0.45, 0.09, 1.5, 1.00),
    "stopped":  (0.00, 0.00, 0.0, 0.50),
}


def _norm(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


class DotSphere:
    def __init__(self, diameter=150, margin=None, rows=None):
        self.D = int(diameter)
        self.M = int(margin) if margin is not None else max(6, int(self.D * 0.09))
        self.S = self.D + 2 * self.M                      # image is S x S
        self.R = self.D / 2 * 0.97                        # sphere radius in px
        # ~36 dots across a big ball (like a halftone print); fewer on small balls so dots
        # stay at least a few px wide
        rows = rows or max(12, min(36, int(self.D / 5.2)))
        self.dlat = math.pi / rows
        # latitude rows, staggered; dots per row ~ cos(lat) so spacing is ~uniform
        pts = []
        for i in range(rows + 1):
            lat = -math.pi / 2 + i * self.dlat
            c = math.cos(lat)
            n = max(1, int(round(2 * math.pi * c / self.dlat)))
            off = 0.5 if i % 2 else 0.0
            for j in range(n):
                lon = (j + off) * 2 * math.pi / n
                pts.append((c * math.sin(lon), math.sin(lat), c * math.cos(lon)))
        self.pts = pts
        self.tilt = 0.40
        self.light = _norm((0.72, 0.52, 0.46))            # from the upper right, toward viewer
        self.angle = 0.0
        self.wave = 0.0
        self._v = _STATES["idle"][0]
        self._amp = _STATES["idle"][1]
        self._opa = 1.0
        self._t = time.time()
        # near-invisible disc (alpha=1): makes the window hit-testable over the whole ball
        base = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
        c = self.S / 2
        r = self.R * 0.99
        ImageDraw.Draw(base).ellipse((c - r, c - r, c + r, c + r), fill=(0, 0, 0, 1))
        self._base = base

    @property
    def size(self) -> int:
        return self.S

    def contains(self, x, y) -> bool:
        """Is image-space point (x, y) inside the ball (hit area)?"""
        c = self.S / 2
        return math.hypot(x - c, y - c) <= self.R * 0.99

    # ------------------------------------------------------------------
    def step(self, state: str, dt: float | None = None):
        now = time.time()
        if dt is None:
            dt = min(0.2, now - self._t)
        self._t = now
        spin, amp, wspeed, opa = _STATES.get(state, _STATES["idle"])
        k = min(1.0, dt * 4.0)                            # ease between states
        self._v += (spin - self._v) * k
        self._amp += (amp - self._amp) * k
        self._opa += (opa - self._opa) * k
        self.angle += self._v * dt
        self.wave += wspeed * dt

    def render(self, state: str = "idle", advance: bool = True) -> Image.Image:
        if advance:
            self.step(state)
        S, R = self.S, self.R
        ss = 2
        S2 = S * ss
        cx = cy = S2 / 2
        R2 = R * ss
        rmax = R2 * self.dlat * 0.64                      # lit dots nearly merge, like the print
        ca, sa = math.cos(self.angle), math.sin(self.angle)
        ct, st = math.cos(self.tilt), math.sin(self.tilt)
        Lx, Ly, Lz = self.light
        amp, wave = self._amp, self.wave

        mask = Image.new("L", (S2, S2), 0)
        d = ImageDraw.Draw(mask)
        for x, y, z in self.pts:
            x1 = x * ca + z * sa                          # spin about the vertical axis
            z1 = -x * sa + z * ca
            y2 = y * ct - z1 * st                         # tilt toward the viewer
            z2 = y * st + z1 * ct
            if z2 <= 0.03:                                # back hemisphere
                continue
            lit = x1 * Lx + y2 * Ly + z2 * Lz
            if lit < 0.0:
                lit = 0.0
            size = 0.07 + 0.93 * (lit ** 1.35)            # halftone: size follows light (high contrast)
            size *= z2 ** 0.38                            # dots fade at the rim
            if amp:
                size *= 1.0 + amp * math.sin(wave - y2 * 4.2 + x1 * 1.5)
            r = rmax * size
            if r < 0.55:
                continue
            sx = cx + R2 * x1
            sy = cy - R2 * y2
            d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=255)

        alpha = mask.resize((S, S), Image.BOX)            # anti-aliased dot coverage
        lut = [int(v * self._opa) for v in range(256)]
        dots_a = alpha.point(lut)
        # faint soft shadow so the white dots stay readable on bright backgrounds
        sh_lut = [min(255, int(v * 0.85 * self._opa)) for v in range(256)]
        shadow_a = alpha.filter(ImageFilter.GaussianBlur(1.4)).point(sh_lut)

        out = self._base.copy()
        dark = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        dark.putalpha(shadow_a)
        out.alpha_composite(dark)
        white = Image.new("RGBA", (S, S), (255, 255, 255, 0))
        white.putalpha(dots_a)
        out.alpha_composite(white)
        return out
