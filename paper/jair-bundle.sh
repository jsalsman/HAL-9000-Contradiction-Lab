#!/bin/sh
# Build the files for a JAIR submission from the paper's single source.
# Usage (from anywhere): paper/jair-bundle.sh [--no-pdf]
# Writes paper/jair-paper.pdf (the JAIR build: the review version, or the camera-ready one
# once the source is switched) and paper/jair-source.tar.gz (every source file, for JAIR's
# archive after acceptance). Both are ignored by git.
# --no-pdf writes only the source tarball, which needs no TeX installation.
# Nothing is written into paper/ unless the whole run succeeds, and any outputs from an
# earlier run are removed first, so a stale PDF or tarball never survives a failure.
set -eu

BUILD_PDF=yes
case "${1:-}" in
  "") ;;
  --no-pdf) BUILD_PDF=no ;;
  *) echo "usage: $0 [--no-pdf]" >&2; exit 2 ;;
esac

PAPER=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
rm -f "$PAPER/jair-paper.pdf" "$PAPER/jair-source.tar.gz"

install_help() {
  cat >&2 <<MSG

On Debian or Ubuntu, install TeX Live and biber with:
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \\
    texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \\
    texlive-fonts-extra texlive-pictures texlive-bibtex-extra texlive-plain-generic biber

Or run $0 --no-pdf to write only the source tarball.
MSG
}

# Quick check for the tools and the packages most often missing from a partial TeX
# installation; the build below also reports any other file LaTeX cannot find.
if [ "$BUILD_PDF" = yes ]; then
  missing=""
  for tool in pdflatex biber kpsewhich; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if command -v kpsewhich >/dev/null 2>&1; then
    for file in amsart.cls tikz.sty pgfplots.sty libertine.sty newtxmath.sty zi4.sty \
                biblatex.sty listings.sty doclicense.sty textcase.sty comment.sty binhex.tex; do
      [ -n "$(kpsewhich "$file")" ] || missing="$missing $file"
    done
  fi
  if [ -n "$missing" ]; then
    echo "Cannot build the PDF; missing:$missing" >&2
    install_help
    exit 1
  fi
fi

STAGE=$(mktemp -d)
BUILD=$(mktemp -d)
OUT=$(mktemp -d)
trap 'rm -rf "$STAGE" "$BUILD" "$OUT"; rm -f "$PAPER"/.jair-*.part' EXIT

# Copy finished outputs from $OUT into paper/ as hidden .part files, then rename them into
# place. If any copy or rename fails, remove every output.
publish() {
  for name in "$@"; do
    if ! cp "$OUT/$name" "$PAPER/.$name.part"; then
      rm -f "$PAPER/jair-paper.pdf" "$PAPER/jair-source.tar.gz"
      echo "Could not write $PAPER/$name; nothing was written." >&2
      exit 1
    fi
  done
  for name in "$@"; do
    if ! mv "$PAPER/.$name.part" "$PAPER/$name"; then
      rm -f "$PAPER/jair-paper.pdf" "$PAPER/jair-source.tar.gz"
      echo "Could not write $PAPER/$name; nothing was written." >&2
      exit 1
    fi
  done
}

# Every file the JAIR build reads: the paper, its bibliography as a file (the .tex embeds
# it and rewrites it identically), the Author Kit class files, and acmart's source, which
# acmart's license requires alongside the generated class. No arxiv.flag, so the source
# builds its JAIR form.
cp "$PAPER/hal-contradiction-lab.tex" \
   "$PAPER/jair.cls" "$PAPER/acmart.cls" "$PAPER/acmart.dtx" "$PAPER/acmart.ins" \
   "$PAPER/acmauthoryear.bbx" "$PAPER/acmauthoryear.cbx" "$PAPER/acmdatamodel.dbx" \
   "$STAGE/"
awk '/^\\begin\{filecontents\*\}\[overwrite\]\{hal-contradiction-lab\.bib\}$/ {f = 1; next}
     /^\\end\{filecontents\*\}$/ {f = 0}
     f' "$PAPER/hal-contradiction-lab.tex" > "$STAGE/hal-contradiction-lab.bib"
grep -q '^@' "$STAGE/hal-contradiction-lab.bib" || { echo "No embedded bibliography found in hal-contradiction-lab.tex" >&2; exit 1; }
tar -czf "$OUT/jair-source.tar.gz" -C "$STAGE" .

if [ "$BUILD_PDF" = no ]; then
  publish jair-source.tar.gz
  echo "Wrote $PAPER/jair-source.tar.gz (no PDF built)"
  exit 0
fi

# Build the PDF from an unpacked copy of the source tarball, so the tarball is proven to
# be complete.
tar -xzf "$OUT/jair-source.tar.gz" -C "$BUILD"
cd "$BUILD"
latex_failed() {
  notfound=$(sed -n "s/^! LaTeX Error: File \`\(.*\)' not found\..*/\1/p" hal-contradiction-lab.log 2>/dev/null | sort -u | tr '\n' ' ')
  if [ -n "$notfound" ]; then
    echo "Cannot build the PDF; LaTeX could not find: $notfound" >&2
    install_help
  else
    echo "pdflatex failed; the end of its log:" >&2
    tail -n 25 hal-contradiction-lab.log >&2 2>/dev/null || true
  fi
  echo "Nothing was written to $PAPER." >&2
  exit 1
}
for step in pdflatex biber pdflatex pdflatex; do
  if [ "$step" = biber ]; then
    biber --quiet hal-contradiction-lab >/dev/null || { echo "biber failed; nothing was written to $PAPER." >&2; exit 1; }
  else
    pdflatex -interaction=nonstopmode -halt-on-error hal-contradiction-lab.tex >/dev/null || latex_failed
  fi
done
cp hal-contradiction-lab.pdf "$OUT/jair-paper.pdf"
publish jair-paper.pdf jair-source.tar.gz
echo "Wrote $PAPER/jair-paper.pdf and $PAPER/jair-source.tar.gz"
