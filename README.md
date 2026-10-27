# ike-dex-docs

An IKE documentation project: a converted AsciiDoc document is decomposed into a
**topic library** and reassembled by one or more **assemblies**.
It follows the standards in
[`IKE-Network/ike-tooling`](https://github.com/IKE-Network/ike-tooling)
(`ike-build-standards/src/main/standards/`), with
[`hl7-ig-corpus-example`](https://github.com/IKE-Network/hl7-ig-corpus-example)
as the nearest worked example.
Run `mvn validate` first: it unpacks the standards into `.claude/standards/`
(git-ignored). Read `IKE-INGEST`, `IKE-TOPIC-DECOMPOSITION`, `IKE-ASCIIDOC-FRAGMENT`,
`IKE-TOPIC-REGISTRY`, `IKE-ASSEMBLY`, `IKE-INDEX` and `IKE-DOC` before ingesting.
The unpacked set (build 261) is older than `ike-tooling` main and lacks `IKE-DEX-INGEST`;
read that one from the repository.

## Layout

```
ike-dex-docs/
├── pom.xml                      # reactor: root="true"; lists topics and assemblies only
├── .mvn/, mvnw, mvnw.cmd        # copied from doc-example
├── topics/                      # topic library; artifactId "topics" (fixed by standard)
│   ├── pom.xml
│   └── src/docs/asciidoc/
│       ├── index.adoc           # all-topics preview; must include every topic
│       └── topics/{domain}/{slug}.adoc
├── assemblies/                  # LOCAL CONVENTION: every assembly lives here
│   ├── pom.xml                  # intermediate aggregator: the <subprojects> list of assemblies
│   ├── compendium/              # all-topics validation assembly (required)
│   │   ├── pom.xml
│   │   └── src/docs/asciidoc/compendium.adoc
│   └── {assembly-name}/         # reconstruction or targeted-guide assemblies
├── utilities/                   # Python helpers and their virtual environment
└── .gitignore
```

- **Reactor.** The root POM lists `topics` and `assemblies`. `assemblies/pom.xml` lists the
  assembly modules, so the root POM never changes when an assembly is added.
  **Every ingest that creates an assembly must add its directory to the `<subprojects>` in
  `assemblies/pom.xml`.** An assembly missing from that list is silently not built.
  Select modules with `-pl assemblies -am` (all assemblies) or `-pl assemblies/compendium -am`.
  Assembly-specific plugin configuration belongs in `assemblies/pom.xml` under
  `<pluginManagement>`; none is needed yet.
- **Nesting is local.** The standards put assemblies beside `topics/`; grouping them is a local
  choice. Assembly POMs name `dev.ikm.dex.docs:assemblies` as their parent (found at the default
  `../pom.xml`).
- **Assembly POMs.** `pom` packaging, a `<pdf.source.document>` property matching the
  `.adoc` name, and a dependency on `${project.groupId}:topics:${project.version}`
  with `<classifier>adoc</classifier><type>zip</type>`.
  Leave `<build>` empty; `ike-parent` supplies the plugins.
- **Parent.** `network.ike.platform:ike-parent:204`, with `.mvn/` and `mvnw` copied from
  `doc-example`. `doc-example`'s 191 (and the example's 98) pin `ike-doc-maven-plugin` 116,
  which has no `idoc:topic-registry` goal; 204 pins 122, which does. Use 204 or later.
- **Coordinates.** Group id `dev.ikm.dex.docs`, version `1-SNAPSHOT`. The site URL in the root
  POM is a placeholder that `ike-parent`'s enforcer requires; nothing is deployed.

## Fragments

Header comments, then attributes, a literal anchor, and a level-1 title:

```asciidoc
// arch-coord-versioning
// Topic: Coordinate-Based Versioning
// Type: concept
// Status: draft
:topic-id: arch-coord-versioning
:topic-type: concept
:topic-status: draft
:topic-keywords: versioning, coordinates, STAMP
:topic-summary: One to three sentences, 150-400 characters, term-rich.

[[arch-coord-versioning]]
= Coordinate-Based Versioning
```

- Type is `concept`, `task`, `reference` or `dialog`. Status is `draft`, `proposed`,
  `review`, `published` or `deprecated`.
- Id is `{2-5 char prefix}-{2-5 word slug}`, lowercase kebab-case, under 40 characters,
  and **immutable** once committed. The file path mirrors it.
- 500-5000 characters per topic (target 1500-3000). Exceptions go in `:topic-notes:`.
- 3-10 index terms per topic from the `IKE-INDEX` controlled vocabulary.
- Never `[[{topic-id}]]`, `:doctype:`, `:toc:` or other document-level attributes.
- Third-party sources add `:topic-provenance: external`, `:topic-citation:`,
  `:topic-license:`, use an `ext-` id under `topics/ext/{type}/`, and cap status at `review`.

## Registry

The registry is **generated from topic headers**: never write or commit YAML.

```bash
mvn -B idoc:topic-registry -pl topics        # full scan -> topics/target/topic-registry.yaml
mvn -B idoc:topic-registry -pl topics -Dike.topic-registry.add=src/docs/asciidoc/topics/{domain}/{topic}.adoc
```

The goal must report `0 findings`. Run a full scan after any move or delete.
It does not check summaries, `related` bidirectionality, size bounds,
index density, or that published topics appear in an assembly; check those by hand.

## Assemblies

```asciidoc
:doctype: book
:topics: {generated}/topics-asciidoc

== Chapter

include::{topics}/topics/{domain}/{slug}.adoc[leveloffset=+2]
```

- Topic includes under a `==` chapter use `leveloffset=+2`. Blank line before and after each include.
- Assemblies own `:doctype:`, `:toc:`, `:sectnums:`, `:icons:`. The compendium ends with `[index]`.
- Do not hand-write `.asciidoctorconfig`. The build extracts one into each module that has
  `src/docs/asciidoc/` (it points `:generated:` and `:topics:` at `target/generated-sources/asciidoc`).
  **Commit these files** so every developer's IDE resolves includes; IDE previews still need one
  `mvn generate-sources` to populate `target/`. After creating an assembly, build once and
  commit its generated `.asciidoctorconfig`.
- `ext/` topics must not be included in any assembly; reference them with `xref:`.
  A reconstruction assembly of third-party content therefore needs an explicit decision
  (see "Open decisions").

## Ingestion workflow

1. **Classify** the source (internal, regulatory, standards, literature, or authored)
   and confirm handling, quoting and license with the user. Check for a 510(k) decision
   summary (`IKE-DEX-INGEST` applies instead).
2. **Convert** to AsciiDoc: `utilities/.venv/bin/python utilities/document_to_asciidoc.py`.
3. **Normalize** line breaks (below).
4. **Decompose** into topics; author headers and index terms.
5. **Place** under `topics/src/docs/asciidoc/topics/{domain}/`; add to `index.adoc`.
6. **Register**: run `idoc:topic-registry`; fix findings.
7. **Assemble**: create the assembly module under `assemblies/{name}/`, **add `{name}` to the
   `<subprojects>` in `assemblies/pom.xml`**, and add the new topics to the compendium.
8. **Validate** (below) and re-normalize any file edited by hand.

### Semantic line breaks

```bash
mvn -N slb:reformat "-Dfile=topics/src/docs/asciidoc/topics/{domain}/"                 # in place
mvn -N slb:reformat "-Dfile=path/to/file.adoc" -DdryRun=true                           # preview
```

Plugin `network.ike.docs:semantic-linebreak` (source in `ike-docs`; resolves as `slb:122` under
`ike-parent` 204; dry run verified in this repo).
Run from the reactor root with `-N`. Options: `outputFile` (single file only),
`sentencesOnly`, `clauseBreak` (default true), `maxLineLength` (64), `noWrap`.
It reformats paragraphs only and does not read stdin.
Commit the reflow on its own and list it in `.git-blame-ignore-revs`.

## Build and validate

```bash
mvn validate                              # unpack standards into .claude/standards/
mvn clean verify                          # HTML for every module (no Prince needed)
mvn clean verify -Dike.pdf.prawn          # add a Prawn PDF
mvn clean verify -pl assemblies -am       # every assembly plus the topic library
mvn clean verify -pl assemblies/compendium -am   # one assembly plus the topic library
```

HTML lands in `{module}/target/generated-docs/html/`. `ike-parent` 204 skips all renderers by
default; the reactor POM turns HTML on and the Prince PDF off. For Prince, pass
`-Dike.skip.prince=false -Dike.skip.pdf-default=false` (needs `prince` on `PATH`).
`mvn validate` alone fails on the first run in a fresh clone, because the compendium
needs the topic library's `adoc` ZIP; the first build must reach `package` (`clean verify`).
A passing result is `BUILD SUCCESS`; no unresolved include or "possible invalid reference"
warning; registry `0 findings`; every new topic in `index.adoc` and the compendium; every new assembly listed in
`assemblies/pom.xml` and present in the reactor output; no source content lost.

## Open decisions

- **Third-party source reconstruction.** The standard forbids `ext/` topics in assemblies;
  the example makes an undocumented exception for per-source assemblies.
  Decide per source at the confirmation step. Authored or internal content uses a normal domain.
- **Fragment format may change** (`ike-issues#1088`: anchor as id, no `:topic-id:` or comment
  header, widened provenance). Keep fragments generated or uniform so migration is mechanical.
- No `lint-adoc` goal yet (`ike-issues#555`); index placement and density are manual checks.
- Example repo divergences to avoid: hand-kept registry YAML, headers without summaries,
  no index terms, verbatim table reproduction, long-line prose, stale parent version.

## Utilities

See [`utilities/README.md`](utilities/README.md) for the Python environment and scripts.
