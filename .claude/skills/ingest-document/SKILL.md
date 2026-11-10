---
name: ingest-document
description: Ingest a document into this project as topics plus an assembly, following the FA3-D1 pattern and the IKE ingestion standards. Use when asked to ingest, import or decompose a source document (Word, PDF, or a 510(k) decision summary) into the topic library.
argument-hint: <path to the source document>
---

Ingest the source named in the arguments (`$ARGUMENTS`; ask for its path if none was given) into the ike-dex-docs project as a topic library plus an assembly, the way the FA3-D1 report was ingested (commit 6e61326). That means two things:
- **Topics:** the source is split into reusable fragments, one file per topic in `topics/src/docs/asciidoc/topics/{domain}/` (FA3-D1: 14 files in `dex/`). Each has a header (id, type, status, keywords, summary), an anchor, index terms, and its own tables, figures and captions.
- **Assembly:** `assemblies/{name}/` (FA3-D1: `assemblies/FA3-D1_Current-State_DeX_Production_Workflow_Overview/`) holds the cover and contents list and re-includes the topics in source order, so the rebuilt document matches the original.

Use those files as the worked example.

**Read first**
1. `CLAUDE.md`, then `README.md` (Layout, Ingestion workflow, Build and validate, Known issues). README is the source of truth.
2. Run `mvn validate` to unpack the standards into `.claude/standards/` (build artifacts: never edit or commit). Read IKE-INGEST, IKE-TOPIC-DECOMPOSITION, IKE-ASCIIDOC-FRAGMENT, IKE-TOPIC-REGISTRY, IKE-ASSEMBLY, IKE-INDEX and IKE-DOC. If the source is a 510(k) decision summary, read IKE-DEX-INGEST from IKE-Network/ike-tooling too (it is not unpacked).

**Classify and confirm before converting.** Classify the source (authored, internal, regulatory, standards, literature). A third-party source needs the IKE-INGEST confirmation step: stop and ask me how to handle it. `ext/` topics are never included in assemblies. Authored sources need no provenance attributes.

**Convert and verify**
- `utilities/.venv/bin/python utilities/document_to_asciidoc.py <src> -o work/raw/<name>.adoc` (setup: `./utilities/setup.sh`; scratch output goes in `work/`).
- For a PDF, run `utilities/check_fidelity.py <src.pdf> <adoc>`; every gate should pass. The checker trusts the PDF's tags, so also compare page images against the source. See `utilities/README.md` for known gaps (cell shading, cover art, diagrams).
- Report a converter defect rather than hand-patching one document; fixes belong in the converter as general rules.

**Ingest**
- Run `mvn -N slb:reformat "-Dfile=…"` on the converted file before decomposing and again after hand edits; commit reflows separately.
- Decompose per the standards. Each include's `leveloffset` equals the topic's depth in the source (+1 chapter, +2 section, +3 subsection). Add every topic to `index.adoc` and the compendium.
- Never write registry YAML: run `mvn -B idoc:topic-registry -pl topics` until it reports `0 findings`. Topic ids are immutable once committed.
- Cover pages and contents lists belong in the assembly, not topics. Create `assemblies/{name}/` (directory, `artifactId`, `.adoc` name and `pdf.source.document` identical, no spaces) and add it to `assemblies/pom.xml` `<subprojects>`. Don't hand-write `.asciidoctorconfig` (the build generates it) but do commit it. Don't `sed`/`awk` POMs.

**Done means** `mvn clean verify` (add `-Dike.skip.prince=false` for the PDF) gives BUILD SUCCESS with no unresolved include or invalid-reference warning; registry `0 findings`; every topic in the index and compendium; the assembly in the reactor output; and no source content lost, checked page by page against the source.

**Constraints.** Don't commit or push unless I ask; if asked, end commit messages with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Don't deploy or release. Finish with a short summary: topics created, assembly name, check results, open questions.
