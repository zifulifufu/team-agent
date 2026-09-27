/**
 * The mark: three nodes on a rounded tile — a team of agents around one table.
 *
 * **One drawing.** This was drawn twice — the sidebar had three dots in an outline, the middle panel
 * had a filled square with a `Sparkles` glyph in it — so the app introduced itself two different ways
 * on the same screen, and the Dock icon (Electron's) was a third. It was then still two, because the
 * outline and the filled tile *are* different pictures. Now there is one: the filled tile, always,
 * at whatever size it is asked for.
 *
 * The filled tile is the one it has to be, because it is also the app icon: `scripts/make-icon.py`
 * draws this exactly the way an icon has to be drawn, and an outline cannot survive 16 px in a Dock.
 * Same shape in the sidebar, on the home page, in About, and in the Dock — the colours are in
 * `styles.css` (`.bm-tile` / `.bm-node`) so it follows the theme's accent like everything else.
 *
 * The geometry — a 20-unit view box, tile inset 1.4 with radius 5, nodes at (10, 6.9), (6.3, 13),
 * (13.7, 13) with radius 2.1 — is the one `scripts/make-icon.py` reproduces pixel for pixel. Change
 * one, change both.
 */
export default function BrandMark({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 20 20"
      aria-hidden="true" focusable="false">
      <rect className="bm-tile" x="1.4" y="1.4" width="17.2" height="17.2" rx="5" />
      <circle className="bm-node" cx="10" cy="6.9" r="2.1" />
      <circle className="bm-node" cx="6.3" cy="13" r="2.1" />
      <circle className="bm-node" cx="13.7" cy="13" r="2.1" />
    </svg>
  );
}
