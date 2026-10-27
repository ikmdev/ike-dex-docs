# ike-dex-docs — Claude instructions

IKE documentation project: sources are decomposed into a topic library (`topics/`) and
reassembled by assemblies (`assemblies/`). **[README.md](README.md) is the source of truth** for
layout, fragment format, registry, assemblies, ingestion workflow and build commands; read it first.

## Initial setup — always do this first

Run `mvn validate`. It unpacks the IKE build standards into `.claude/standards/` (build artifacts:
never edit or commit them). Read `IKE-INGEST`, `IKE-TOPIC-DECOMPOSITION`, `IKE-ASCIIDOC-FRAGMENT`,
`IKE-TOPIC-REGISTRY`, `IKE-ASSEMBLY`, `IKE-INDEX` and `IKE-DOC` before ingesting. `IKE-DEX-INGEST`
is not in the unpacked set; read it from `IKE-Network/ike-tooling`.

## Rules that are easy to get wrong

- Never write or commit registry YAML; run `mvn -B idoc:topic-registry -pl topics` and fix findings
  until it reports `0 findings`. Topic ids are immutable once committed.
- Every ingest that creates an assembly must add it to `assemblies/pom.xml` `<subprojects>`.
  Assembly directory, `artifactId`, `.adoc` name and `pdf.source.document` are identical, with no spaces.
- Run `semantic-linebreak` (`mvn -N slb:reformat "-Dfile=…"`) on converted files before decomposing and
  again after hand edits; commit reflows separately.
- Cover pages and contents lists belong in the assembly, not in topics. Topic includes use
  `leveloffset` equal to the topic's depth in the source.
- Do not hand-write `.asciidoctorconfig` (the build generates it) but do commit it.
- Authored sources need no provenance attributes. Third-party sources need the `IKE-INGEST`
  confirmation step first, and `ext/` topics must not be included in assemblies.
- Don't `sed`/`awk`/regex-edit POMs. Don't commit or push unless asked.
- Python work uses `utilities/.venv` (`./utilities/setup.sh`); scratch output goes in `work/`.
