# Changelog

## Unreleased — since LogicBoard 3.1.3 and LogicShare 1.1.2

The whole ecosystem is now free and open source under the GPL-3.0, and all
three apps update from this repository's Releases (`LogicBoard-v*` and
`LogicShare-v*` tags).

### LogicBoard

**Writing and erasing**
- New **Lasso eraser**: draw a loop around what should go and everything
  inside it is erased in one step — one undo brings it all back.
- The **Drawing Tool** and **Eraser** panels are redesigned: the tools are
  stacked on the left, everything about the chosen tool on the right, and
  the two columns end level.
- The eraser preview shows the eraser at its **true size**, all the way to
  200 px; it used to stop growing halfway along the slider.
- The pen slider gives its **first third to 1–10 px**, the sizes used most,
  so they are easy to set exactly; the rest of the slider covers 11–40 px.
- The highlighter goes up to **60 px**, and its preview shows it whole.
- The brush nib sits at **45°**, and the brush can be the Dual Pen's back tip.
- A new palette of 23 distinct shades plus a custom colour, shared by the
  pen and shape tools; the default ink is a lighter blue.
- Shapes have their own colour column.
- Settings has a **palm size** control for palm rejection.

**Pages**
- Deleting a page no longer scrambles the pages panel. A tapped delete
  button used to fling the list several cards down and leave pages missing
  or showing under the wrong number.
- The pages below a deleted one slide up into its place.
- **Drag to reorder** is animated end to end — the page lifts, a dashed slot
  shows where it will land, and it settles into place — and runs well within
  a 60 fps frame (shadows are drawn from a cache, and page previews wait
  until nothing is moving).
- The page counter is a single number tile between Prev and Next. It sizes
  itself to the board's page count and never clips a large number.

**Media**
- "Remove From Board" is yellow, set apart from the red "Delete Media".
- Media that has not been rendered gets a **Render** button.
- When PowerPoint is not installed, LogicBoard says so instead of reporting
  a failed import.
- The Media Manager remembers the chosen Layout until it is changed or
  LogicBoard restarts.

**Everywhere**
- Scrollbars are solid, wider and round-ended, so they can be seen and
  grabbed on a board.
- A closed panel (Settings, the tool panels, Pages, Files, Media Manager)
  is now freed instead of staying alive hidden until LogicBoard quits —
  opening and closing them over a lesson no longer piles up memory.
- The toolbar is 15 % smaller.
- A new logo and pen icon.
- A smaller install: unused Qt libraries and the FFmpeg multimedia stack are
  no longer shipped; the click sound is played by Windows itself.
- Update checks get through school proxies (PAC scripts, certificate
  revocation servers that are blocked) and explain what went wrong when they
  cannot.

### LogicShare (Windows)
- Warns when Windows Firewall is keeping it from seeing the boards, with a
  button that asks Windows for access again.
- A smaller install, as for LogicBoard.

### LogicShare (Android)
- Finds its updates in this repository's `LogicShare-v*` releases.

## Earlier releases

Releases before the move to open source were not recorded in a changelog.
[`LogicBoard/CHANGES.md`](LogicBoard/CHANGES.md) keeps the engineering notes
from the LogicBoard v3 redesign.
