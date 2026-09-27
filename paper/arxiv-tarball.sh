#!/bin/sh
# Build the arXiv upload for the paper and test-compile it the way arXiv does.
# Usage (from anywhere): paper/arxiv-tarball.sh [--no-preview]
# Writes paper/arxiv-upload.tar.gz and paper/arxiv-preview.pdf (both ignored by git).
# --no-preview writes only the tarball, which needs no TeX installation.
set -eu

PREVIEW=yes
case "${1:-}" in
  "") ;;
  --no-preview) PREVIEW=no ;;
  *) echo "usage: $0 [--no-preview]" >&2; exit 2 ;;
esac

# The preview needs pdflatex, biber, and the packages the JAIR template and the paper load.
# Check them all before doing anything, so a missing tool never stops the script halfway.
if [ "$PREVIEW" = yes ]; then
  missing=""
  for tool in pdflatex biber kpsewhich; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if command -v kpsewhich >/dev/null 2>&1; then
    for file in libertine.sty newtxmath.sty zi4.sty biblatex.sty pgfplots.sty listings.sty \
                doclicense.sty textcase.sty comment.sty binhex.tex; do
      [ -n "$(kpsewhich "$file")" ] || missing="$missing $file"
    done
  fi
  if [ -n "$missing" ]; then
    cat >&2 <<MSG
Cannot build the preview; missing:$missing

On Debian or Ubuntu, install TeX Live and biber with:
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \\
    texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \\
    texlive-fonts-extra texlive-pictures texlive-bibtex-extra texlive-plain-generic biber

Or run $0 --no-preview to write only the upload tarball.
MSG
    exit 1
  fi
fi

PAPER=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGE=$(mktemp -d)
BUILD=$(mktemp -d)
trap 'rm -rf "$STAGE" "$BUILD"' EXIT

# The paper, the JAIR Author Kit class files it needs, and acmart's source, which acmart's
# license requires alongside the generated class. arxiv.flag switches the source to its arXiv
# form; 00README.json names the top-level file so acmart.dtx is never taken for the paper.
cp "$PAPER/hal-contradiction-lab.tex" \
   "$PAPER/jair.cls" "$PAPER/acmart.cls" "$PAPER/acmart.dtx" "$PAPER/acmart.ins" \
   "$PAPER/acmauthoryear.bbx" "$PAPER/acmauthoryear.cbx" "$PAPER/acmdatamodel.dbx" \
   "$STAGE/"
cp "$PAPER/arxiv-00README.json" "$STAGE/00README.json"
printf '%s\n' "Marks this directory as the arXiv upload; hal-contradiction-lab.tex builds its preprint form when this file exists." > "$STAGE/arxiv.flag"
tar -czf "$PAPER/arxiv-upload.tar.gz" -C "$STAGE" .

if [ "$PREVIEW" = no ]; then
  echo "Wrote $PAPER/arxiv-upload.tar.gz (no preview built)"
  exit 0
fi

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
