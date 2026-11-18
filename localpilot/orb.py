"""Premium liquid-orb renderer for the agent's face.

Reproduces the glossy sphere from the design preview with PIL (tkinter Canvas has
no gradients/alpha): a dark base, 3 slowly-rotating color "pools" blurred into a
liquid, a top-left reflection highlight, spherical shade + rim, and a gloss dot.
Rendered to an image and shown on the click-through overlay.

9 states, each with its own 4-color palette and motion speed (from the design):
  0 Thinking  1 Puzzle  2 Researching  3 Working  4 Verifying
  5 Sleeping  6 Stopping 7 Stopped     8 Complete
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFilter, ImageChops, ImageEnhance

PALETTES = [
    ["#092350", "#3765dc", "#24daca", "#efbafa"],   # 0 Thinking
    ["#30205b", "#ac52cc", "#ff966f", "#ffe0a3"],   # 1 Puzzle solving
    ["#063849", "#07979a", "#75e9ff", "#d3fff3"],   # 2 Researching
    ["#191d65", "#4f66f1", "#27d9ef", "#edc8ff"],   # 3 Working
    ["#202652", "#816dd7", "#60e2c7", "#dbffea"],   # 4 Verifying
    ["#14214f", "#5665cf", "#46ceea", "#f3bcfa"],   # 5 Sleeping
    ["#41293d", "#aa6577", "#efa184", "#ffdad2"],   # 6 Stopping
    ["#25303e", "#536574", "#8297a3", "#b9c8cb"],   # 7 Stopped
    ["#103f42", "#208c89", "#76e2ae", "#e3ffe4"],   # 8 Complete
]
SPEED = [.72, 1.1, .95, 1.45, .58, .38, .3, 0.0, .34]

# agent state -> visual mode
STATE_MODE = {"idle": 5, "thinking": 0, "acting": 3, "done": 8, "stopped": 7}


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Orb:
    def __init__(self, diameter=120, key=(1, 1, 1), ss=2):
        self.D = int(diameter)
        self.S = int(diameter * ss)
        self.key = key
        S = self.S
        # circle masks
        self.cmask_ss = Image.new("L", (S, S), 0)
        ImageDraw.Draw(self.cmask_ss).ellipse((2, 2, S - 3, S - 3), fill=255)
        self.cmask = self.cmask_ss.resize((self.D, self.D), Image.LANCZOS)

        # glow canvas: sphere + a soft halo margin
        self.M = max(16, int(self.D * 0.40))
        self.GD = self.D + 2 * self.M
        gc = self.GD / 2
        gmask = Image.new("L", (self.GD, self.GD), 0)
        gr = self.D / 2 + self.M * 0.20
        ImageDraw.Draw(gmask).ellipse((gc - gr, gc - gr, gc + gr, gc + gr), fill=255)
        gmask = gmask.filter(ImageFilter.GaussianBlur(self.M * 0.36))
        # radial taper: force the glow to reach 0 well inside the image bounds so the
        # image edge can never show as a faint square over bright backgrounds
        taper = Image.new("L", (self.GD, self.GD), 0)
        tr = gc - 2
        ImageDraw.Draw(taper).ellipse((gc - tr * 0.93, gc - tr * 0.93, gc + tr * 0.93, gc + tr * 0.93), fill=255)
        taper = taper.filter(ImageFilter.GaussianBlur(self.M * 0.18))
        self.glow_mask = ImageChops.multiply(gmask, taper)

        c = S / 2
        R = S * 0.47
        # reflection highlight (top-left), soft and bright
        refl = Image.new("L", (S, S), 0)
        rr = R * 0.95
        ox, oy = c - R * 0.34, c - R * 0.52
        ImageDraw.Draw(refl).ellipse((ox - rr, oy - rr, ox + rr, oy + rr), fill=150)
        self.refl = refl.filter(ImageFilter.GaussianBlur(S * 0.07))
        # bottom shade (sphere body) -- lighter so colors stay vivid
        shade = Image.new("L", (S, S), 0)
        sr = R * 1.05
        sx, sy = c, c + R * 0.42
        ImageDraw.Draw(shade).ellipse((sx - sr, sy - sr * 0.8, sx + sr, sy + sr * 1.2), fill=98)
        self.shade = shade.filter(ImageFilter.GaussianBlur(S * 0.09))
        # rim (soft dark ring near the edge -> spherical edge), lighter
        rim = Image.new("L", (S, S), 0)
        dr = ImageDraw.Draw(rim)
        dr.ellipse((3, 3, S - 4, S - 4), outline=150, width=int(S * 0.045))
        self.rim = rim.filter(ImageFilter.GaussianBlur(S * 0.03))
        # gloss specular dot
        self.spec_xy = (int(c - R * 0.33), int(c - R * 0.40))
        self.spec_r = int(R * 0.13)

    def render(self, mode: int, t: float):
        """Return an RGB image (size D) of the orb; outside the circle == key color."""
        S = self.S
        pal = [_rgb(x) for x in PALETTES[mode % len(PALETTES)]]
        c = S / 2
        R = S * 0.47

        layer = Image.new("RGB", (S, S), pal[0])
        d = ImageDraw.Draw(layer)
        for i in range(3):
            ang = t * (0.66 + i * 0.12) + i * 2.1
            px = c + S * 0.34 * math.cos(ang)
            py = c + S * 0.32 * math.sin(ang * 1.13)
            pr = R * (1.38 - i * 0.11)        # larger pools -> more color
            d.ellipse((px - pr, py - pr, px + pr, py + pr), fill=pal[i + 1])
        layer = layer.filter(ImageFilter.GaussianBlur(S * 0.07))    # liquid blend (less wash)

        # shade (darken bottom for sphere volume)
        layer.paste((9, 10, 22), (0, 0), self.shade)
        # reflection highlight
        layer.paste((246, 244, 255), (0, 0), self.refl)
        # rim darkening
        layer.paste((7, 7, 20), (0, 0), self.rim)
        # gloss dot
        gd = ImageDraw.Draw(layer)
        sx, sy = self.spec_xy
        gd.ellipse((sx - self.spec_r, sy - self.spec_r, sx + self.spec_r, sy + self.spec_r),
                   fill=(255, 255, 255))
        layer = layer.filter(ImageFilter.GaussianBlur(S * 0.006))

        # make it pop: saturation + a touch of contrast/brightness
        layer = ImageEnhance.Color(layer).enhance(1.34)
        layer = ImageEnhance.Contrast(layer).enhance(1.07)
        layer = ImageEnhance.Brightness(layer).enhance(1.05)

        sphere = layer.resize((self.D, self.D), Image.LANCZOS).convert("RGBA")
        sphere.putalpha(self.cmask)   # true transparent, anti-aliased edge

        # baked soft glow halo (smooth, composites cleanly over anything)
        out = Image.new("RGBA", (self.GD, self.GD), (0, 0, 0, 0))
        glow_alpha = int(80 + 30 * (0.5 + 0.5 * math.sin(t * 0.5)))   # gentle pulse
        glow = Image.new("RGBA", (self.GD, self.GD), pal[2] + (0,))
        glow.putalpha(self.glow_mask.point(lambda v: v * glow_alpha // 255))
        out = Image.alpha_composite(out, glow)
        out.alpha_composite(sphere, (self.M, self.M))
        return out
