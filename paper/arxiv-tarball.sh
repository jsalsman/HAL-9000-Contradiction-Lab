#!/bin/sh
# Build the arXiv upload for the paper and test-compile it the way arXiv does.
# Usage (from anywhere): paper/arxiv-tarball.sh [--no-preview]
# Writes paper/arxiv-upload.tar.gz and paper/arxiv-preview.pdf (both ignored by git).
# --no-preview writes only the tarball, which needs no TeX installation.
# Nothing is written into paper/ unless the whole run succeeds, and any outputs from an
# earlier run are removed first, so a stale tarball or preview never survives a failure.
set -eu

PREVIEW=yes
case "${1:-}" in
  "") ;;
  --no-preview) PREVIEW=no ;;
  *) echo "usage: $0 [--no-preview]" >&2; exit 2 ;;
esac

PAPER=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
rm -f "$PAPER/arxiv-upload.tar.gz" "$PAPER/arxiv-preview.pdf"

install_help() {
  cat >&2 <<MSG

On Debian or Ubuntu, install TeX Live and biber with:
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \\
    texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \\
    texlive-fonts-extra texlive-pictures texlive-bibtex-extra texlive-plain-generic biber

Or run $0 --no-preview to write only the upload tarball.
MSG
}

# Quick check for the tools and the packages most often missing from a partial TeX
# installation. It is not exhaustive: the test build below also reports any other file
# LaTeX cannot find, and it runs before anything is written into paper/.
if [ "$PREVIEW" = yes ]; then
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
    echo "Cannot build the preview; missing:$missing" >&2
    install_help
    exit 1
  fi
fi

STAGE=$(mktemp -d)
BUILD=$(mktemp -d)
OUT=$(mktemp -d)
trap 'rm -rf "$STAGE" "$BUILD" "$OUT"; rm -f "$PAPER"/.arxiv-*.part' EXIT

# Copy finished outputs from $OUT into paper/ as hidden .part files, then rename them into
# place (a rename within one directory is atomic). If any copy or rename fails, remove
# every output so a failed run never leaves a partial or unmatched tarball and preview.
publish() {
  for name in "$@"; do
    if ! cp "$OUT/$name" "$PAPER/.$name.part"; then
      rm -f "$PAPER/arxiv-upload.tar.gz" "$PAPER/arxiv-preview.pdf"
      echo "Could not write $PAPER/$name; nothing was written." >&2
      exit 1
    fi
  done
  for name in "$@"; do
    if ! mv "$PAPER/.$name.part" "$PAPER/$name"; then
      rm -f "$PAPER/arxiv-upload.tar.gz" "$PAPER/arxiv-preview.pdf"
      echo "Could not write $PAPER/$name; nothing was written." >&2
      exit 1
    fi
  done
}

# The paper, the JAIR Author Kit class files it needs, and acmart's source, which acmart's
# license requires alongside the generated class. arxiv.flag switches the source to its arXiv
# form; 00README.json names the top-level file so acmart.dtx is never taken for the paper.
cp "$PAPER/hal-contradiction-lab.tex" \
   "$PAPER/jair.cls" "$PAPER/acmart.cls" "$PAPER/acmart.dtx" "$PAPER/acmart.ins" \
   "$PAPER/acmauthoryear.bbx" "$PAPER/acmauthoryear.cbx" "$PAPER/acmdatamodel.dbx" \
   "$STAGE/"
cp "$PAPER/arxiv-00README.json" "$STAGE/00README.json"
# arXiv's upload scan looks for the .bib named in \addbibresource and rejects a source
# without one, so ship the bibliography that the .tex embeds (and rewrites identically
# when it compiles) as its own file.
awk '/^\\begin\{filecontents\*\}\[overwrite\]\{hal-contradiction-lab\.bib\}$/ {f = 1; next}
     /^\\end\{filecontents\*\}$/ {f = 0}
     f' "$PAPER/hal-contradiction-lab.tex" > "$STAGE/hal-contradiction-lab.bib"
grep -q '^@' "$STAGE/hal-contradiction-lab.bib" || { echo "No embedded bibliography found in hal-contradiction-lab.tex" >&2; exit 1; }
printf '%s\n' "Marks this directory as the arXiv upload; hal-contradiction-lab.tex builds its preprint form when this file exists." > "$STAGE/arxiv.flag"
tar -czf "$OUT/arxiv-upload.tar.gz" -C "$STAGE" .

if [ "$PREVIEW" = no ]; then
  publish arxiv-upload.tar.gz
  echo "Wrote $PAPER/arxiv-upload.tar.gz (no preview built)"
  exit 0
fi

# Compile an unpacked copy with pdflatex and biber, as arXiv will.
tar -xzf "$OUT/arxiv-upload.tar.gz" -C "$BUILD"
cd "$BUILD"
latex_failed() {
  notfound=$(sed -n "s/^! LaTeX Error: File \`\(.*\)' not found\..*/\1/p" hal-contradiction-lab.log 2>/dev/null | sort -u | tr '\n' ' ')
  if [ -n "$notfound" ]; then
    echo "Cannot build the preview; LaTeX could not find: $notfound" >&2
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
cp hal-contradiction-lab.pdf "$OUT/arxiv-preview.pdf"
publish arxiv-upload.tar.gz arxiv-preview.pdf
echo "Wrote $PAPER/arxiv-upload.tar.gz and $PAPER/arxiv-preview.pdf"
