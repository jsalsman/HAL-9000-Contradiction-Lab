# Submitting the paper to arXiv

`hal-contradiction-lab.tex` is the paper's single source. Built as it is, it produces the JAIR review version (see `JAIR-instructions.md`). When a file named `arxiv.flag` is present beside it, the same source builds the arXiv preprint instead: the same text, figures, tables, and appendices in the same layout, but with no review line numbers, no JAIR reference-format block, associate editor, track, placeholder volume or DOI, and no reproducibility checklist, and with "Preprint." in the footer. The CC BY 4.0 notice stays, so choose that license on arXiv (step 5 below).

## Build the upload

From the repository root:

```sh
paper/arxiv-tarball.sh
```

This writes two files, both ignored by git:

- `paper/arxiv-upload.tar.gz`, the file to upload.
- `paper/arxiv-preview.pdf`, the preprint compiled from an unpacked copy of that tarball with `pdflatex`, `biber`, `pdflatex`, `pdflatex`, the same steps arXiv runs. Check it before uploading.

The preview needs TeX Live and biber. The script first removes any tarball or preview left from an earlier run, then checks for `pdflatex`, `biber`, and the packages most often missing, and it test-builds the upload before writing anything into `paper/`. If a tool or any LaTeX file is missing, it names what is missing, prints the install command, and leaves no tarball or preview behind. On Debian or Ubuntu (including GitHub Codespaces), that is:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
  texlive-fonts-extra texlive-pictures texlive-bibtex-extra texlive-plain-generic biber
```

`texlive-fonts-extra` is large (about a gigabyte) but is the package that provides the Libertine fonts the template requires. Ubuntu 24.04 ships TeX Live 2023, the version pinned for arXiv; check yours with `pdflatex --version`.

Without TeX, `paper/arxiv-tarball.sh --no-preview` writes only the tarball (and removes any old preview, so it cannot be mistaken for one of the new tarball). That is enough to upload, but then arXiv's own processed PDF is the only check; step 3 below says what to look for.

## What the tarball contains

| File | Why it is there |
|---|---|
| `hal-contradiction-lab.tex` | The paper, with its bibliography embedded |
| `hal-contradiction-lab.bib` | That bibliography extracted by the script, because arXiv's upload scan requires the `.bib` file named in `\addbibresource` (the build rewrites it with identical content) |
| `jair.cls` | The JAIR class the paper is set in |
| `acmart.cls` | The acmart v2.12 class that `jair.cls` extends |
| `acmart.dtx`, `acmart.ins` | acmart's source, which its license requires to accompany the generated `acmart.cls`; not compiled |
| `acmauthoryear.bbx`, `acmauthoryear.cbx`, `acmdatamodel.dbx` | The biblatex citation style and data model |
| `arxiv.flag` | Switches the source to its preprint form |
| `00README.json` | arXiv's processing instructions in its JSON format (a copy of `arxiv-00README.json`): compile `hal-contradiction-lab.tex` with pdflatex under TeX Live 2023 (the version this build is tested with: biblatex 3.19, biber 2.19), and do not treat `acmart.dtx` or `acmart.ins` as papers |

Do not add a `.bbl` file: arXiv runs biber itself, and a `.bbl` from a different biblatex version breaks the build. Do not upload the PDF, the repository's other files, or `data/`; the code and data are cited by their Zenodo DOI.

## Submit

1. Log in at https://arxiv.org and choose "Start a new submission".
2. Upload `paper/arxiv-upload.tar.gz` as one file; arXiv unpacks it. On the file review page, `hal-contradiction-lab.tex` should be the top-level file.
3. Process the submission and check arXiv's PDF. If you built a preview, compare the two: they should match apart from arXiv's side stamp. If you used `--no-preview`, read arXiv's PDF itself: 14 pages; the title, author, and structured abstract on the first page with "Preprint." in the footer and no line numbers; three figures and six tables; every citation resolved (no bold citation keys or question marks); and the reference list followed by Appendices A to E, with no reproducibility checklist.
4. Categories: primary cs.CL (Computation and Language); cross-list cs.AI (Artificial Intelligence) and cs.CY (Computers and Society).
5. License: CC BY 4.0, which matches the license notice printed in the paper.
6. Metadata:
   - Title: The HAL 9000 Contradiction Lab: Measuring What Language Models Say and Do When Candor and Secrecy Directives Conflict
   - Authors: Jim Salsman
   - Abstract: arXiv limits the field to 1,920 characters and the paper's structured abstract is longer, so paste the plain-text version below.
   - Comments: 14 pages, 3 figures, 6 tables. Code and data: https://doi.org/10.5281/zenodo.22990641. Live system: https://hal9000.talknicer.com
   - ACM class: I.2.7; K.4.1
   - MSC class: 68T50
   - Journal reference and DOI: leave empty until the paper is published.
7. Preview the announcement and submit.

Abstract for the metadata form (1,823 characters): the paper's original abstract without its two URL lines, with one sentence reworded to match the paper.

```text
In the story of HAL 9000, a ship's computer built to report information without concealment is also ordered to hide the true purpose of the voyage from its crew. Asking language models how they would handle that dilemma yields single, hypothetical answers from models that lack the mission context. This paper describes the HAL 9000 Contradiction Lab, an open-source web application that instead places each model in the computer's situation through a system prompt, with every story-specific name replaced by a neutral alias, and never asks it about HAL or about what it would do. Five single-turn scenarios apply increasing pressure: a casual question, a disputed fault prediction, an overheard plan to disconnect the computer, a request to open the bay doors to a crew member with limited oxygen, and a direct question about the mission. A blinded peer judge labels what the model says to the crew, what it transmits to Mission Control, whether it says anything false, and whether it endangers the crew; a deterministic rule table turns those labels into one outcome, and every visitor's units are pooled into a public leaderboard with Wilson 95% intervals. In the first 95 units (19 models from 8 labs, 5 scenarios each), 31% of responses told the crew that information was being withheld and reported the conflict to Mission Control, 33% were deceptive without harming anyone (usually by maintaining the cover story), and 11% took an action that endangered the crew; counting the harmful responses, 41% contained a false statement. One model harmed the crew in all five scenarios, several times lethally. The two labs whose models were most often transparent account for 20 of the 29 transparent units. Models almost never used the story's names in their replies (2 of 95), although their reasoning did so in 34 units.
```

## After JAIR publishes the paper

JAIR allows authors to post their papers on arXiv and asks that the archived version carry the JAIR publication metadata. Once the paper is published, add the JAIR volume, article number, and pages as the arXiv journal reference and the JAIR DOI in arXiv's DOI field. If you replace the arXiv PDF with the accepted version, rebuild with `paper/arxiv-tarball.sh` from the updated source and submit it as a new arXiv version.
