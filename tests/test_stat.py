import sys

import _paths  # noqa: F401  (sys.path + chdir bootstrap)
from accelerateddevops.models import Slide
from accelerateddevops.render.carousel import _stat_value

CASES = [
    # (footer, body, expected, why)
    ("", "38% of build time was queue wait\nNot compilation.", "38%",
     "number on first body line"),
    ("", "1247 minutes\nAcross 312 builds", "1247", "plain number"),
    ("", "Not compilation.\nNot test execution.", "", "phrase, not a number -> must bail"),
    ("", "Queue wait dominated everything", "", "pure phrase -> bail"),
    ("", "$4.2M in wasted spend", "$4.2M", "currency"),
    ("12x", "faster rollouts", "12x", "number in footer"),
    ("", "99.99% availability across regions", "99.99%", "decimal + percent"),
    ("", "40 min median deploy time", "40min", "number + unit, unit retained"),
    ("", "kubernetes", "", "single word, no digits"),
    ("", "In 2024 we rebuilt the pipeline", "", "year in a sentence -> bail"),
    ("", "312\nbuilds per quarter", "312", "bare number first line"),
    ("", "3 platforms, 1 standard", "3", "bare count is displayable"),
    ("", "2x faster: here is why", "2x", "prefix with colon split"),
    ("", "", "", "empty -> bail"),
]

fails = 0
for footer, body, expected, why in CASES:
    got = _stat_value(Slide(2, "stat", "head", body=body, footer=footer))
    ok = got == expected
    if not ok:
        fails += 1
    print("%s footer=%-6r body=%-38r -> %-8r expected %-8r  # %s"
          % ("OK  " if ok else "FAIL", footer, body[:36], got, expected, why))

print("\n%d/%d stat-detection cases pass" % (len(CASES) - fails, len(CASES)))
sys.exit(1 if fails else 0)
