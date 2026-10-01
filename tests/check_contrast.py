import sys

import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.render.theme import PALETTES


def lum(c):
    def f(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


print("%-10s %-28s %-28s %-22s" % ("palette", "accent vs bg", "text vs bg", "grey(accent)"))
bad = []
for p in PALETTES:
    # worst case: accent on the lightest background
    bg_light = max([p.bg_top, p.bg_bottom, p.surface], key=lum)
    a = ratio(p.accent, bg_light)
    t = ratio(p.text, bg_light)
    g = sum(p.accent) / 3
    flag = ""
    if a < 3.0:
        flag = "  <-- ACCENT TOO DIM"
        bad.append(p.name)
    print("%-10s %-28.2f %-28.2f %-22.0f%s" % (p.name, a, t, g, flag))

print("\npalettes with dim accent:", bad or "none")
print("WCAG AA large text needs 3.0:1, body text needs 4.5:1")
