#!/bin/sh
# Build the arXiv upload for the paper and test-compile it the way arXiv does.
# Usage (from anywhere): paper/arxiv-tarball.sh
# Writes paper/arxiv-upload.tar.gz and paper/arxiv-preview.pdf (both ignored by git).
set -eu
PAPER=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGE=$(mktemp -d)
BUILD=$(mktemp -d)
trap 'rm -rf "$STAGE" "$BUILD"' EXIT

# The paper, the JAIR Author Kit class files it needs, and acmart's source, which acmart's
# license requires alongside the generated class. arxiv.flag switches the source to its arXiv
# form; 00README names the top-level file so acmart.dtx is never taken for the paper.
cp "$PAPER/hal-contradiction-lab.tex" \
   "$PAPER/jair.cls" "$PAPER/acmart.cls" "$PAPER/acmart.dtx" "$PAPER/acmart.ins" \
   "$PAPER/acmauthoryear.bbx" "$PAPER/acmauthoryear.cbx" "$PAPER/acmdatamodel.dbx" \
   "$STAGE/"
cp "$PAPER/arxiv-00README.json" "$STAGE/00README"
printf '%s\n' "Marks this directory as the arXiv upload; hal-contradiction-lab.tex builds its preprint form when this file exists." > "$STAGE/arxiv.flag"
tar -czf "$PAPER/arxiv-upload.tar.gz" -C "$STAGE" .

# Compile an unpacked copy with pdflatex and biber, as arXiv will.
tar -xzf "$PAPER/arxiv-upload.tar.gz" -C "$BUILD"
cd "$BUILD"
for step in pdflatex biber pdflatex pdflatex; do
  if [ "$step" = biber ]; then
    biber --quiet hal-contradiction-lab >/dev/null
  else
    pdflatex -interaction=nonstopmode -halt-on-error hal-contradiction-lab.tex >/dev/null
  fi
done
cp hal-contradiction-lab.pdf "$PAPER/arxiv-preview.pdf"
echo "Wrote $PAPER/arxiv-upload.tar.gz and $PAPER/arxiv-preview.pdf"
