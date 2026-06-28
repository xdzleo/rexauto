"""Generate rexauto.ico — a dark rounded badge with a glowing hexagon + forward
chevron in the cyan->violet->magenta brand gradient."""
import math
import os
from PIL import Image, ImageDraw, ImageFilter

S = 256
HERE = os.path.dirname(os.path.abspath(__file__))


def vgrad(top, bot):
    g = Image.new("RGBA", (1, S))
    for y in range(S):
        t = y / (S - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    return g.resize((S, S))


def hgrad(stops):
    g = Image.new("RGBA", (S, 1))
    n = len(stops) - 1
    for x in range(S):
        t = x / (S - 1) * n
        i = min(int(t), n - 1)
        tt = t - i
        a, b = stops[i], stops[i + 1]
        g.putpixel((x, 0), tuple(int(a[j] + (b[j] - a[j]) * tt) for j in range(3)) + (255,))
    return g.resize((S, S))


# rounded dark background
base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([8, 8, S - 8, S - 8], radius=54, fill=255)
base.paste(vgrad((12, 18, 40), (5, 6, 12)), (0, 0), mask)
# faint top highlight
hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(hl).rounded_rectangle([8, 8, S - 8, S - 8], radius=54, outline=(255, 255, 255, 38), width=2)
base = Image.alpha_composite(base, hl)

# emblem (white shapes on transparent)
em = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ed = ImageDraw.Draw(em)
cx, cy, R = S / 2, S / 2, 78
hexpts = [(cx + R * math.cos(math.radians(60 * i - 90)),
           cy + R * math.sin(math.radians(60 * i - 90))) for i in range(6)]
ed.line(hexpts + [hexpts[0]], fill=(255, 255, 255, 255), width=14, joint="curve")
ed.line([(cx - 20, cy - 32), (cx + 22, cy), (cx - 20, cy + 32)],
        fill=(255, 255, 255, 255), width=15, joint="curve")
alpha = em.split()[3]

# glow under the emblem
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
glow.paste(Image.new("RGBA", (S, S), (34, 211, 238, 200)), (0, 0),
           alpha.filter(ImageFilter.GaussianBlur(11)))

# gradient-tinted emblem
grad = hgrad([(34, 211, 238), (139, 92, 246), (244, 114, 182)])
colored = Image.new("RGBA", (S, S), (0, 0, 0, 0))
colored.paste(grad, (0, 0), alpha)

out = Image.alpha_composite(Image.alpha_composite(base, glow), colored)
ico = os.path.join(HERE, "rexauto.ico")
out.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
out.save(os.path.join(HERE, "rexauto_icon.png"))
print("wrote", ico)
