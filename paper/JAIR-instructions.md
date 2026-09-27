# Submitting the paper to JAIR

`hal-contradiction-lab.tex` is the paper's single source. Built as it is in this directory, it produces the Journal of Artificial Intelligence Research review version, on the JAIR Author Kit template dated 15 September 2025 (a customization of ACM's `acmart`). These notes follow JAIR's [submission page](https://www.jair.org/index.php/jair/about/submissions), [formatting page](https://www.jair.org/index.php/jair/formatting), [final preparation guide](https://www.jair.org/index.php/jair/authorinstrs), and the [AI and Society special track](https://www.jair.org/index.php/jair/SpecialTrack-AIandSociety).

## Build

The Author Kit's class files are kept beside the source in this directory: `jair.cls`, `acmart.cls`, `acmauthoryear.bbx`, `acmauthoryear.cbx`, and `acmdatamodel.dbx`. `acmart.cls` is the unmodified acmart v2.12 (2024/12/28) generated from `acmart.dtx` with `acmart.ins`, both included here as its LaTeX Project Public License requires for redistributing a generated file. The Author Kit's own copy of `acmart.cls` is that same version with two warnings commented out (that ACM keywords and CCS concepts are mandatory), which the license does not allow under the original file name, so this directory uses the unmodified class. The output is the same. The three biblatex files are identical to acmart v2.12's. The bibliography is embedded in the `.tex` file and written out as `hal-contradiction-lab.bib` on the first pass. The same source builds the arXiv preprint when `arxiv.flag` is present beside it (see `ARXIV-instructions.md`); never leave that file here when building for JAIR.

From the repository root:

```sh
paper/jair-bundle.sh
```

This writes two files, both ignored by git:

- `paper/jair-paper.pdf`, the PDF to upload (the review version, or the camera-ready one once the source is switched).
- `paper/jair-source.tar.gz`, every source file the build reads: `hal-contradiction-lab.tex`, its bibliography as `hal-contradiction-lab.bib`, the class and biblatex files, and `acmart.dtx` and `acmart.ins`. JAIR asks for this archive only after acceptance.

The script builds the PDF from an unpacked copy of that tarball, so the tarball is proven complete. It never includes `arxiv.flag`, so it always builds the JAIR form. It checks for TeX first and names anything missing with the install command below, and a failed run leaves neither file behind. Without TeX, `paper/jair-bundle.sh --no-pdf` writes only the source tarball.

To build by hand instead, in `paper/`: `pdflatex hal-contradiction-lab.tex`, `biber hal-contradiction-lab`, then `pdflatex hal-contradiction-lab.tex` twice.

It needs a TeX Live installation with the Libertine, newtx, and Inconsolata fonts, `biblatex`, `biber`, `pgfplots`, and `listings`. On Debian or Ubuntu (including GitHub Codespaces), the same packages as for the arXiv preview:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
  texlive-fonts-extra texlive-pictures texlive-bibtex-extra texlive-plain-generic biber
```

The template can also be opened on [Overleaf](https://www.overleaf.com/read/hycbzkdksrzz#8106d4). The build should finish with no errors and no undefined references; the only expected warnings are that `hal-contradiction-lab.bib` was written, that the affiliation has no city, and acmart's notes that ACM keywords and CCS concepts are mandatory, which do not apply to JAIR (its template says to omit both).

Do not commit the PDF or build output; `.gitignore` excludes them.

## What JAIR requires, and where the source meets it

- JAIR format for review: `\documentclass[manuscript, screen, review]{jair}`, which adds line numbers. Submissions in any other format are rejected without review.
- No template modifications: no changed margins, fonts, spacing, or `\vspace`, and no `lmodern`.
- A completed reproducibility checklist, compiled into the PDF as the last appendix. Submissions without it are desk rejected. Keep its answers current if the data, code, or environment change.
- A structured abstract (Background, Objectives, Methods, Results, Conclusions). JAIR encourages it; it is not mandatory.
- No ACM CCS concepts or keywords.
- Numbered sections, and no section or subsection that opens directly with a subsection.
- Table captions above tables, figure captions below figures, and a `\Description` for every figure.
- Figures that can be read in monochrome. The stacked bars are patterned as well as colored, and Appendix E gives their counts.
- Funding, interests, data availability, and AI-use statements in the `acks` environment, just before the references.
- AI tools may assist, but the core contributions must be the authors'. No AI system may be listed as an author or cited as a source.
- The work must be original and not under review at another journal. Posting to arXiv is allowed, and the preprint is already there (built from this same source; see `ARXIV-instructions.md`).
- No submission or publication fees.

## Submit

1. Register at https://jair.org/index.php/jair/user/register, then start a submission at https://jair.org/index.php/jair/submission/wizard.
2. Choose the AI and Society special track. The source already sets `\JAIRTrack{AI and Society}`.
3. Enter the title, author, contact information, and abstract as they appear in the PDF. The abstract is also shown as HTML on the JAIR site.
4. Confirm the submission declarations and answer the three mandatory survey questions at the end of the form. In the comments to the editor, give the arXiv identifier of the preprint so the editor knows it is the same work, and note that the code and data are archived at https://doi.org/10.5281/zenodo.22990641.
5. Upload `paper/jair-paper.pdf` from `paper/jair-bundle.sh` (about 540 KB, well under JAIR's 15 MB guidance). It is the only file the submission needs; `jair-source.tar.gz` is for after acceptance.
6. Expect an acknowledgment within three business days (if none arrives, write to editors@jair.org) and a decision in about 8 to 12 weeks.

## If accepted

1. Make the editor's requested changes within two months.
2. Switch to the camera-ready class: in the `\else` branch near the top, change `\documentclass[manuscript, screen, review]{jair}` to `\documentclass[]{jair}`.
3. Replace the template's placeholder values (`\JAIRAE`, `\acmVolume`, `\acmArticle`, `\acmMonth`, `\acmYear`, and `\acmDOI`) with the ones the production editor assigns, and add `\received{...}` and `\received[accepted]{...}` dates before `\maketitle`.
4. The reproducibility checklist may be removed from the final version.
5. Keep the CC BY 4.0 footer that the class adds, which confirms agreement with the JAIR Open Access Publication Agreement.
6. Have the paper proofread by someone other than the author, as JAIR recommends.
7. If source code is published as an online appendix, sign JAIR's source code release form.
8. Rebuild with `paper/jair-bundle.sh` and upload `paper/jair-paper.pdf` and `paper/jair-source.tar.gz` (JAIR accepts a zip or tar of all source files) through the submission's discussion thread.
9. Update the arXiv entry with the JAIR volume, article number, and DOI (arXiv's journal-reference and DOI fields), and replace its PDF with the accepted version if you wish (see `ARXIV-instructions.md`).
