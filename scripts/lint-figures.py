#!/usr/bin/env python3
"""Measure a folder of pictures the way the film does, and say which ones do not belong.

This is the check for 「图不专业」, and it exists because the first version of that diagnosis was
done by hand, in a chat, and could not be repeated. What it prints is what `app/visual.py` decides
by: the same `profile()`, the same `admit()`, the same numbers. So a group can run it before a film
and know what the assembler is about to do, and run it after and check the promise was kept.

Run it on the material a film is being made from:

    python3 scripts/lint-figures.py ~/.team-agent/workspaces/<id>
    python3 scripts/lint-figures.py <folder> --size 1080x1920
    python3 scripts/lint-figures.py <folder> --json > figures.json

Exit code is 1 when something is worth acting on, so it can gate a pipeline: pictures whose shape is
far from the frame, pictures too small to fill it, and pictures that disagree with the majority
background. Finding nothing wrong is exit 0 — and the headline numbers, before and after, are what
the work is judged on: one film's material measured **nine different backgrounds** and a 14x spread
in colour count.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import visual  # noqa: E402

SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def collect(folder: Path, *, recursive: bool = True) -> list[Path]:
    """The pictures under a folder, skipping the assembler's own scratch.

    `.assemble/` is skipped deliberately: those files are the *output* of the pass this script
    checks, so measuring them as material would make the check report its own work as a violation.
    """
    walk = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(p for p in walk
                  if p.is_file() and p.suffix.lower() in SUFFIXES and ".assemble" not in p.parts)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Measure pictures against the frame they go into.")
    ap.add_argument("folder", type=Path, help="the folder of material to measure")
    ap.add_argument("--size", default="1080x1920", help="the film's frame, e.g. 1080x1920")
    ap.add_argument("--no-recursive", action="store_true", help="only the folder itself")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--limit", type=int, default=40, help="how many violations to list")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        print(f"not a folder: {args.folder}", file=sys.stderr)
        return 2
    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        print(f"--size wants WIDTHxHEIGHT, not {args.size!r}", file=sys.stderr)
        return 2

    pictures = collect(args.folder, recursive=not args.no_recursive)
    if not pictures:
        print(f"no pictures under {args.folder}")
        return 0
    size = (w, h)
    prof = visual.profile(pictures)
    rep = visual.report(pictures, size)

    if args.json:
        print(json.dumps({"frame": rep["frame"], "style": prof, "report": rep},
                         ensure_ascii=False, indent=1))
        return 0

    print(f"{len(pictures)} pictures against a {rep['frame']} frame\n")
    print("The style they have in common")
    print(f"  background        {prof['canvas']}   ({prof['agreement']:.0%} of them agree)")
    print(f"  palette           {', '.join(prof['ramp'])}")
    print(f"  line weight       {prof['line_weight']}")
    print(f"  drawable again    {prof['flat_share']:.0%}")

    print("\nWhat the assembler will do with them")
    print(f"  fill the frame    {rep['count'] - rep['off_shape']}")
    print(f"  draw again        {rep['to_redraw']}")
    print(f"  frame whole       {rep['to_plate']}")

    print("\nThe numbers the complaint was about")
    print(f"  distinct backgrounds   {len(rep['backgrounds'])}   {rep['backgrounds'][:9]}")
    print(f"  colour-count spread    {rep['colour_spread']}")
    print(f"  worst shape mismatch   {rep['worst_delta']:.3f} of the frame's aspect")

    flagged = [r for r in rep["rows"] if "error" in r
               or r["verdict"] != "cover"
               or r["short_edge"] < visual.SOFT_SHORT_EDGE
               or r["corner_delta"] > 40]
    if flagged:
        print(f"\n{len(flagged)} worth a look")
        for r in flagged[:args.limit]:
            if "error" in r:
                print(f"  unreadable      {Path(r['path']).name}: {r['error']}")
                continue
            bits = [f"{r['verdict']}", f"{r['width']}x{r['height']}", f"{r['ratio_delta']:.2f}"]
            if r["short_edge"] < visual.MIN_SHORT_EDGE:
                bits.append("TOO SMALL")
            elif r["short_edge"] < visual.SOFT_SHORT_EDGE:
                bits.append("small")
            if r["corner_delta"] > 40:
                bits.append(f"uneven background {r['corner_delta']}")
            print(f"  {Path(r['path']).name[:56]:<58} {' · '.join(bits)}")
    else:
        print("\nNothing flagged: every picture is the frame's shape, big enough, and on one ground.")

    bad = rep["unreadable"] or rep["small"] or rep["worst_delta"] > 0.25 or len(rep["backgrounds"]) > 3
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
