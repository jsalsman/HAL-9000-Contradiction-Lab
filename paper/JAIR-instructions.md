# Submitting the paper to JAIR

`jair-submission.tex` is the paper in the Journal of Artificial Intelligence Research format, built on the JAIR Author Kit template dated 15 September 2025 (a customization of ACM's `acmart`). These notes follow JAIR's [submission page](https://www.jair.org/index.php/jair/about/submissions), [formatting page](https://www.jair.org/index.php/jair/formatting), [final preparation guide](https://www.jair.org/index.php/jair/authorinstrs), and the [AI and Society special track](https://www.jair.org/index.php/jair/SpecialTrack-AIandSociety).

## Build

The Author Kit's class files are kept beside the source in this directory: `jair.cls`, `acmart.cls`, `acmauthoryear.bbx`, `acmauthoryear.cbx`, and `acmdatamodel.dbx`. The bibliography is embedded in the `.tex` file and written out as `jair-submission.bib` on the first pass.

```sh
cd paper
pdflatex jair-submission.tex
biber jair-submission
pdflatex jair-submission.tex
pdflatex jair-submission.tex
```

It needs a TeX Live installation with the Libertine, newtx, and Inconsolata fonts, `biblatex`, `biber`, `pgfplots`, and `listings` (on Debian or Ubuntu: `texlive-latex-extra`, `texlive-fonts-extra`, `texlive-pictures`, `texlive-bibtex-extra`, `texlive-plain-generic`, and `biber`). The template can also be opened on [Overleaf](https://www.overleaf.com/read/hycbzkdksrzz#8106d4). The build should finish with no errors and no undefined references; the only expected warnings are that `jair-submission.bib` was written and that the affiliation has no city.

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
- The work must be original and not under review at another journal. Posting to arXiv is allowed.
- No submission or publication fees.

## Submit

1. Register at https://jair.org/index.php/jair/user/register, then start a submission at https://jair.org/index.php/jair/submission/wizard.
2. Choose the AI and Society special track. The source already sets `\JAIRTrack{AI and Society}`.
3. Enter the title, author, contact information, and abstract as they appear in the PDF. The abstract is also shown as HTML on the JAIR site.
4. Confirm the submission declarations and answer the three mandatory survey questions at the end of the form.
5. Upload the review PDF built above. Keep it well under 15 MB.
6. Expect an acknowledgment within three business days (if none arrives, write to editors@jair.org) and a decision in about 8 to 12 weeks.

## If accepted

1. Make the editor's requested changes within two months.
2. Switch to the camera-ready class: `\documentclass[]{jair}`.
3. Replace the template's placeholder values (`\JAIRAE`, `\acmVolume`, `\acmArticle`, `\acmMonth`, `\acmYear`, and `\acmDOI`) with the ones the production editor assigns, and add `\received{...}` and `\received[accepted]{...}` dates before `\maketitle`.
4. The reproducibility checklist may be removed from the final version.
5. Keep the CC BY 4.0 footer that the class adds, which confirms agreement with the JAIR Open Access Publication Agreement.
6. Have the paper proofread by someone other than the author, as JAIR recommends.
7. If source code is published as an online appendix, sign JAIR's source code release form.
8. Upload the final PDF and a zip or tar of all source files through the submission's discussion thread.
9. Update the arXiv entry with the JAIR volume, article number, and DOI.
