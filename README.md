# ike-dex-docs

An IKE documentation project. A converted document is decomposed into a **topic library**,
indexed, and reassembled by one or more **assemblies** that render to HTML and PDF.

It supports:

- converting Word or PDF sources to AsciiDoc (`utilities/`);
- a topic library with generated registry validation (`topics/`);
- any number of assemblies composed from those topics, each with its own cover and contents
  (`assemblies/`), built to multi-page HTML, self-contained HTML and Prince PDF.

It follows the standards in
[`IKE-Network/ike-tooling`](https://github.com/IKE-Network/ike-tooling)
(`ike-build-standards/src/main/standards/`), with
[`hl7-ig-corpus-example`](https://github.com/IKE-Network/hl7-ig-corpus-example)
as the nearest worked example and `doc-example` as the source of the build setup.

## Current contents

| Item | What it is |
| --- | --- |
| `topics/.../topics/dex/` | 14 `dex-*` topics decomposed from the Deloitte FA3-D1 report "Current-State DeX Production Workflow Overview" (an *authored* source). |
| `assemblies/FA3-D1_Current-State_DeX_Production_Workflow_Overview` | Reconstruction of that report: cover, disclaimers, contents, body. |
| `assemblies/compendium` | All-topics validation assembly with an index. |

## Setup

- Maven 4 (`./mvnw` pins 4.0.0-rc-7) and a JDK; Prince 17 is optional (PDF only);
  [Pandoc](https://pandoc.org/installing.html) for conversion.
- Run `mvn validate` first. It unpacks the standards into `.claude/standards/` (git-ignored).
  Read `IKE-INGEST`, `IKE-TOPIC-DECOMPOSITION`, `IKE-ASCIIDOC-FRAGMENT`, `IKE-TOPIC-REGISTRY`,
  `IKE-ASSEMBLY`, `IKE-INDEX` and `IKE-DOC` before ingesting. The unpacked set (build 261) is
  older than `ike-tooling` main and lacks `IKE-DEX-INGEST`; read that one from the repository.
- Python utilities: `./utilities/setup.sh` (see [`utilities/README.md`](utilities/README.md)).

## Layout

```
ike-dex-docs/
├── pom.xml                      # reactor: root="true"; lists topics and assemblies only
├── .mvn/, mvnw, mvnw.cmd        # copied from doc-example
├── topics/                      # topic library; artifactId "topics" (fixed by standard)
│   ├── pom.xml
│   └── src/docs/asciidoc/
│       ├── index.adoc           # all-topics preview; must include every topic
│       ├── topics/{domain}/{slug}.adoc
│       └── images/{domain}/     # topic images
├── assemblies/                  # LOCAL CONVENTION: every assembly lives here
│   ├── pom.xml                  # intermediate aggregator: the <subprojects> list of assemblies
│   ├── compendium/              # all-topics validation assembly (required)
│   └── {assembly-name}/         # pom.xml, src/docs/asciidoc/{assembly-name}.adoc, images/ (cover art)
├── utilities/                   # Python helpers and their virtual environment
└── work/                        # git-ignored scratch space for conversion output
```

- **Reactor.** The root POM lists `topics` and `assemblies`; `assemblies/pom.xml` lists the
  assembly modules, so the root POM never changes when an assembly is added.
  **Every ingest that creates an assembly must add its directory to the `<subprojects>` in
  `assemblies/pom.xml`.** An assembly missing from that list is silently not built.
  The aggregator also holds assembly-wide build settings (currently `ike.skip.html-single=false`);
  put any assembly-specific plugin configuration there.
- **Nesting is local.** The standards put assemblies beside `topics/`; grouping them is a local
  choice. Assembly POMs name `dev.ikm.dex.docs:assemblies` as their parent (default `../pom.xml`).
- **Assembly names.** The directory, `artifactId`, `.adoc` file name (without extension) and
  `<pdf.source.document>` are identical, per `IKE-NAMING`. Use underscores, not spaces (a Maven
  `artifactId` cannot contain them).
- **Assembly POMs.** `pom` packaging, the `<pdf.source.document>` property, and a dependency on
  `${project.groupId}:topics:${project.version}` with `<classifier>adoc</classifier><type>zip</type>`.
  Leave `<build>` empty; `ike-parent` supplies the plugins.
- **Parent.** `network.ike.platform:ike-parent:204`. `doc-example`'s 191 (and the example's 98)
  pin `ike-doc-maven-plugin` 116, which lacks `idoc:topic-registry`; 204 pins 122, which has it.
- **Coordinates.** Group id `dev.ikm.dex.docs`, version `1-SNAPSHOT`. The site URL in the root
  POM is a placeholder that `ike-parent`'s enforcer requires; nothing is deployed.

## Fragments

Header comments, attributes, a literal anchor, a level-1 title:

```asciidoc
// dex-background
// Topic: Background
// Type: concept
// Status: draft
:topic-id: dex-background
:topic-type: concept
:topic-status: draft
:topic-keywords: real-world data, traceability gap, GUDID
:topic-summary: One to three sentences, 150-400 characters, term-rich.

[[dex-background]]
= Background
```

- Type is `concept`, `task`, `reference` or `dialog`. Status is `draft`, `proposed`,
  `review`, `published` or `deprecated`.
- Id is `{prefix}-{slug}`, lowercase kebab-case, under 40 characters, and **immutable** once
  committed. The directory is the domain (`topics/dex/`, prefix `dex`).
- 500-5000 characters per topic (target 1500-3000). Document exceptions in `:topic-notes:`.
- 3-10 index terms per topic as `indexterm:[primary, secondary]`, before the paragraph that
  discusses them, never directly before a list.
- Keep `:topic-related:` bidirectional. Never write `[[{topic-id}]]`, `:doctype:`, `:toc:`.
- Titled tables and images: `.Caption` above the block; images as
  `image::{domain}/{file}.png[descriptive alt text,scaledwidth=90%]`, with the file in
  `topics/src/docs/asciidoc/images/{domain}/`.
- **Source type.** Authored sources carry no provenance attributes. Third-party sources add
  `:topic-provenance: external`, `:topic-citation:`, `:topic-license:`, use an `ext-` id under
  `topics/ext/{type}/`, cap status at `review`, and need the `IKE-INGEST` confirmation step.

## Registry

The registry is **generated from topic headers**: never write or commit YAML.

```bash
mvn -B idoc:topic-registry -pl topics        # full scan -> topics/target/topic-registry.yaml
mvn -B idoc:topic-registry -pl topics -Dike.topic-registry.add=src/docs/asciidoc/topics/{domain}/{topic}.adoc
```

The goal must report `0 findings`. Run a full scan after any move or delete. It does not check
summaries, `related` bidirectionality, size bounds, index density, or that published topics
appear in an assembly; check those by hand.

## Assemblies

```asciidoc
:doctype: book
:topics: {generated}/topics-asciidoc
:imagesdir: {topics}/images

include::{topics}/topics/{domain}/{slug}.adoc[leveloffset=+1]
```

- Assemblies own `:doctype:`, `:toc:`, `:icons:`, `:sectnums:`. The compendium ends with `[index]`.
- **`leveloffset` equals the topic's depth in the source**: `+1` chapter, `+2` section, `+3`
  subsection. A reconstruction assembly includes topics at their original depth with no
  scaffolding headings; the compendium puts every topic under a domain `==` heading at `+2`.
- **Images.** `:imagesdir: images` for the assembly's own cover art (in the assembly's
  `src/docs/asciidoc/images/`), then `:imagesdir: {topics}/images` before the topic includes.
  Topic `index.adoc` uses `:imagesdir: images`.
- **Cover and contents live only in the assembly**, not in topics. See "PDF output" for the
  print-only style block that makes the cover and contents render correctly in Prince.
- Do not hand-write `.asciidoctorconfig`. The build extracts one into each module with
  `src/docs/asciidoc/`. **Commit these files** so every developer's IDE resolves includes;
  IDE previews still need one `mvn generate-sources`. After creating an assembly, build once and
  commit its generated file.
- `ext/` topics must not be included in any assembly; reference them with `xref:`.

## Ingestion workflow

1. **Classify** the source (authored, internal, regulatory, standards, literature) and confirm
   handling with the user. Check for a 510(k) decision summary (`IKE-DEX-INGEST` applies instead).
2. **Convert**: `utilities/.venv/bin/python utilities/document_to_asciidoc.py src.docx -o work/raw/src.adoc`.
   Review the result against the source (cover art, text boxes and shapes are flattened into
   pictures or lost; see `utilities/README.md`).
3. **Normalize** line breaks on the converted file (below).
4. **Decompose** into topics: author headers, summaries, keywords and index terms; move tables and
   images into the topics, with captions.
5. **Place** under `topics/src/docs/asciidoc/topics/{domain}/`; add every topic to `index.adoc`.
6. **Register**: run `idoc:topic-registry`; fix findings.
7. **Assemble**: create `assemblies/{name}/`, **add `{name}` to `assemblies/pom.xml`**, add the new
   topics to the compendium (and its `[index]`).
8. **Validate**, re-normalize any hand-edited file, and build the outputs (below).

### Semantic line breaks

```bash
mvn -N slb:reformat "-Dfile=topics/src/docs/asciidoc/topics/{domain}/"                 # in place
mvn -N slb:reformat "-Dfile=path/to/file.adoc" -DdryRun=true                           # preview
```

Plugin `network.ike.docs:semantic-linebreak` (resolves as `slb:122` under `ike-parent` 204).
Run from the reactor root with `-N`. Options: `outputFile` (single file only), `sentencesOnly`,
`clauseBreak` (default true), `maxLineLength` (64), `noWrap`. It reformats paragraphs only, not
tables or listings, and does not read stdin. It re-joins a sentence break after a closing
parenthesis (`(Smith, 2024). Next`), so hand-fixing that is not durable. Commit a reflow on its
own and list it in `.git-blame-ignore-revs`.

## Build and validate

```bash
mvn validate                                      # unpack standards into .claude/standards/
mvn clean verify                                  # HTML for every module (no Prince needed)
mvn clean verify -Dike.skip.prince=false          # add the Prince PDF (needs prince on PATH)
mvn clean verify -pl assemblies -am               # every assembly plus the topic library
mvn clean verify -pl assemblies/compendium -am    # one assembly plus the topic library
```

- `ike-parent` 204 skips every renderer by default. The reactor POM turns HTML on and the Prince
  PDF off; `assemblies/pom.xml` turns on `html-single`.
- HTML lands in `{module}/target/generated-docs/html/` (multi-page; topic images are linked by
  absolute path into `target/`) and, for assemblies, `html-single/` (self-contained, portable).
- `mvn validate` alone fails on a fresh clone, because assemblies need the topic library's `adoc`
  ZIP; the first build must reach `package` (`clean verify`).
- **A passing result:** `BUILD SUCCESS`; no unresolved include and no "possible invalid reference"
  warning; registry `0 findings`; every new topic in `index.adoc` and the compendium; every new
  assembly listed in `assemblies/pom.xml` and present in the reactor output; no source content lost.

### PDF output

`-Dike.skip.prince=false` renders with Prince (17 verified; an unlicensed Prince stamps a small
badge on page 1). A command-line `-D` also applies to `topics`, which then renders a PDF of its
preview. Output is in `target/generated-docs/`:

- `pdf-prince/` holds the renderer's output. `pdf/` is a copy of the default renderer's PDF that
  `ike-parent` also zips as the `pdf-default` artifact; it appears only with
  `-Dike.skip.pdf-default=false`, so a local build needs just `pdf-prince/`.
- `{name}.pdf` is the plain layout. `{name}-loose-leaf.pdf` is a second Prince run with
  chapter-local page numbers, a binding margin and a page per chapter, for double-sided printing
  (blank pages by design). There is no separate switch for it.

**Layout fixes live in the assembly, not the shared theme.** `ike-parent`'s `ike-print.css` gives the
generated title its own page, starts every level-2 heading on a new page, resets the page counter at
the body (Prince starts a new page at any reset), and forces the contents list onto a recto page;
the build also forces `toc=auto`. Left alone, that separates the cover from the title, puts the
contents first and leaves blank pages. The FA3-D1 assembly overrides this with:

- a print-only `<style>` block (`@media print`) that moves the page-counter reset to the first
  chapter, puts the cover on the title page, and lets chapters continue on the current page (the
  disclaimers keep their own page);
- `:toc-placement: macro` plus `toc::[]`, applied only when `ike-pdf-renderer` is not `html5`, so
  the contents list follows the disclaimers in PDF while HTML keeps its sidebar.

Copy that block for any assembly that needs a cover. Not working here (tested): `-Dike.pdf.prawn`
is ignored by `ike-parent` 204, `-Dike.skip.prawn=false` fails on a JRuby YAML error in
asciidoctorj-pdf, and FOP fails on the topics preview.

## Deploying to Nexus

Repositories come from `ike-parent` (`ike-base-parent`), not from this project's POMs:

| Version | Repository id | URL |
| --- | --- | --- |
| `-SNAPSHOT` | `ike-snapshots` | `https://nexus.tinkar.org/repository/ike-snapshots/` |
| release | `ike-staging` | `https://nexus.tinkar.org/repository/ike-staging/` (promoted to `ike-releases` by TeamCity) |

**Credentials.** Maven reads `<server>` entries from `~/.m2/settings.xml` by repository id, so add
entries with ids `ike-snapshots` and `ike-staging`. A settings file with only `tinkar-nexus` and
`central` servers will fail to authenticate on deploy. (Not tested against the live server.)

**Snapshot deploy** (day-to-day; the project stays at `1-SNAPSHOT`):

```bash
mvn clean deploy                           # HTML only
mvn clean deploy -Dike.skip.prince=false   # also publish the PDFs (needs prince)
```

Each module publishes its POM, a `-build.pom` and CycloneDX SBOMs under `dev.ikm.dex.docs`.
Classifier zips: `adoc` (source, `topics` and each assembly), `html` (every module), `html-single`
(assemblies) and, with Prince enabled, `prince` (the PDFs). The `-D` flag also reaches `topics`, so
its `prince` zip holds a PDF per topic fragment; ignore it. Check what would be published without
touching Nexus (the metadata check needs an `http(s)` repository, so skip it):

```bash
mvn clean deploy -Dike.metadata.heal.skip=true \
  "-DaltDeploymentRepository=local::file:$PWD/target/deploy-test" \
  "-DaltSnapshotDeploymentRepository=local::file:$PWD/target/deploy-test"
```

**Release.** Never delete `-SNAPSHOT` by hand and run `mvn deploy` (`IKE-MAVEN` prohibits it).
Use the `ike` goals, which branch, set the version, build with `mvnw clean verify`, tag, deploy
from the tag to `ike-staging`, bump to the next snapshot and publish:

```bash
mvn ike:release-draft     # preview only; changes nothing (1 -> tag v1 -> 2-SNAPSHOT)
mvn ike:release-publish   # does it for real
```

`release-publish` also pushes the tag and `main` to `origin`, force-pushes a generated site to
`gh-pages`, and creates a GitHub Release, so it needs push access and an authenticated `gh`. Read the
draft first, and resolve the site target (below) before the first release. Releases build with this
POM's defaults, so they contain no PDFs unless `ike.skip.prince` is set to `false` in the root POM.
`IKE-COMMITS` expects an issue trailer (`Fixes:` / `Refs:`) on commits in the release range; the
release warns about commits without one.

## Known issues and open decisions

- **Third-party sources.** The standard forbids `ext/` topics in assemblies, and the example makes
  an undocumented exception for per-source assemblies. Decide per source at the confirmation step;
  the FA3-D1 report is authored, so it does not arise yet.
- **Index vocabulary additions to propose to `IKE-INDEX`** (in `ike-tooling`): new categories
  `device data` (GUDID, 510(k) premarket submission, package insert, decision summary, GMDN,
  Primary DI, IVD device, DeX record, multi-analyte panel), `knowledge exchange` (IKE, DeX, Komet,
  SOLOR, Concept, Pattern, Semantic, DeX starter data, DeX v1/v2 model), `data quality`
  (real-world data, traceability, variability), `workflow` (data collection, SME validation,
  record authoring) and `automation` (AI-assisted conversion, DeX creation pipeline); new entries
  under existing categories: `organizations` (FDA, CLIA), `interoperability` (health information
  exchange).
- **Release site target.** The release preview says it would publish the site at
  `https://ike.network/ike-dex-docs/`, the placeholder in the root POM, and force-push `gh-pages`
  on `origin`. Decide where this project's site should live, or whether to publish one, before
  the first `ike:release-publish`.
- **Two topics are under the 500-character minimum** (`dex-disclaimers`, `dex-purpose`), kept
  to preserve the source's chapter structure; see their `:topic-notes:`.
- **Fragment format may change** (`ike-issues#1088`: anchor as id, no `:topic-id:` or comment
  header, widened provenance). Keep fragments uniform so migration is mechanical.
- No `lint-adoc` goal yet (`ike-issues#555`); index placement and density are manual checks.
- Avoid the example repo's divergences: hand-kept registry YAML, headers without summaries, no index
  terms, verbatim table reproduction, long-line prose, stale parent version.
