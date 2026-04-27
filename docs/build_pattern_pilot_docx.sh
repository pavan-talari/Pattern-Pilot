#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$ROOT_DIR/docs"
OUT_FILE="$DOCS_DIR/Pattern_Pilot_LinkedIn_Knowledge_Base.docx"
IMAGE_FILE="$DOCS_DIR/pattern_pilot_workflow_infographic.png"
BUILD_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

if [[ ! -f "$IMAGE_FILE" ]]; then
  echo "Missing workflow image: $IMAGE_FILE" >&2
  exit 1
fi

mkdir -p "$BUILD_DIR/_rels" "$BUILD_DIR/word/_rels" "$BUILD_DIR/word/media" "$BUILD_DIR/docProps"
cp "$IMAGE_FILE" "$BUILD_DIR/word/media/workflow.png"

cat > "$BUILD_DIR/[Content_Types].xml" <<'XML'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
XML

cat > "$BUILD_DIR/_rels/.rels" <<'XML'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
XML

cat > "$BUILD_DIR/word/_rels/document.xml.rels" <<'XML'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/workflow.png"/>
</Relationships>
XML

cat > "$BUILD_DIR/docProps/core.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Pattern Pilot Context-Aware QC Workflow</dc:title>
  <dc:subject>LinkedIn knowledge base and implementation summary</dc:subject>
  <dc:creator>Pattern Pilot and Codex</dc:creator>
  <cp:keywords>Pattern Pilot, Context-Aware QC, AI code review, governance, workflow</cp:keywords>
  <dc:description>A presentation document describing Pattern Pilot's context-aware quality control workflow, results, metrics, and LinkedIn narrative.</dc:description>
  <dcterms:created xsi:type="dcterms:W3CDTF">2026-04-13T00:00:00Z</dcterms:created>
</cp:coreProperties>
XML

cat > "$BUILD_DIR/docProps/app.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Pattern Pilot</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <Company>AmiTara</Company>
  <LinksUpToDate>false</LinksUpToDate>
  <SharedDoc>false</SharedDoc>
  <HyperlinksChanged>false</HyperlinksChanged>
  <AppVersion>1.0</AppVersion>
</Properties>
XML

cat > "$BUILD_DIR/word/document.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
  <w:body>
    <w:p>
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="52"/></w:rPr><w:t>Pattern Pilot Context-Aware QC Workflow</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:rPr><w:color w:val="5A6B7D"/><w:sz w:val="24"/></w:rPr><w:t>A practical AI quality-control pattern for autonomous code writing, independent review, and governance-led delivery</w:t></w:r>
    </w:p>
    <w:p/>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>1. Executive Summary</w:t></w:r></w:p>
    <w:p><w:r><w:t>Pattern Pilot is a standalone quality-control plane that allows a coding agent and a reviewer agent to work together with clear boundaries. The writer changes code in the target project. Pattern Pilot reads the changed files, resolves project, decision, and task context, runs deterministic checks, asks an independent reviewer model for structured findings, and returns only the required actions back to the writer.</w:t></w:r></w:p>
    <w:p><w:r><w:t>The improvement is not simply "AI reviews AI code." The important shift is context-aware governance: the reviewer understands the target project rules, the decision being implemented, the task objective, acceptance criteria, known exceptions, waived findings, prior rounds, and late-round severity policy. This helps move from repeated generic review loops toward focused completion.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>2. Problem Statement</w:t></w:r></w:p>
    <w:p><w:r><w:t>The original workflow produced too many review iterations for a single task. Some tasks went through five, six, or even more than ten submissions before completion. A few rounds ended as false positives or low-value recommendations after both agents had already spent significant time negotiating the same issue.</w:t></w:r></w:p>
    <w:p><w:r><w:t>The core gap was not agent effort. Both agents were working hard. The gap was shared context and stable identity. Without a stable task identity, Pattern Pilot could treat resubmits as separate runs. Without decision and task context, the reviewer could over-generalize findings instead of judging whether the code satisfied the actual task. Without an iteration policy, medium and low risks could continue blocking even after repeated review cycles.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>3. Workflow Infographic</w:t></w:r></w:p>
    <w:p><w:r><w:t>The following diagram shows the end-to-end review loop: Codex writes code, Pattern Pilot resolves context from the target project, deterministic checks run first, the reviewer produces tiered findings, the writer fixes only blocking issues, and the task exits with PASS, PASS_WITH_ADVISORIES, or human escalation when needed.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r>
        <w:drawing>
          <wp:inline distT="0" distB="0" distL="0" distR="0">
            <wp:extent cx="5943600" cy="1428720"/>
            <wp:effectExtent l="0" t="0" r="0" b="0"/>
            <wp:docPr id="1" name="Pattern Pilot Context-Aware QC Workflow Infographic"/>
            <wp:cNvGraphicFramePr>
              <a:graphicFrameLocks noChangeAspect="1"/>
            </wp:cNvGraphicFramePr>
            <a:graphic>
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
                <pic:pic>
                  <pic:nvPicPr>
                    <pic:cNvPr id="0" name="workflow.png"/>
                    <pic:cNvPicPr/>
                  </pic:nvPicPr>
                  <pic:blipFill>
                    <a:blip r:embed="rId1"/>
                    <a:stretch><a:fillRect/></a:stretch>
                  </pic:blipFill>
                  <pic:spPr>
                    <a:xfrm>
                      <a:off x="0" y="0"/>
                      <a:ext cx="5943600" cy="1428720"/>
                    </a:xfrm>
                    <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
                  </pic:spPr>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>4. Standard Operating Model</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 1: Writer agent implements the task in the target project.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 2: Writer submits project_name, task_ref, task_id, decision_id, and files_changed to Pattern Pilot.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 3: Pattern Pilot resolves context from the filesystem, including decision markdown, task markdown, governance rules, changed files, and nearby dependencies.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 4: Deterministic checks run before LLM review. Lint, typecheck, tests, and forbidden-pattern checks must be clean or the run returns immediately.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 5: Reviewer evaluates the diff through the hierarchy of project -> decision -> task, not through speculative hardening.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 6: Blocking findings go back to the writer. Advisories are stored and surfaced without blocking task completion.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Step 7: On round three or later, repeated medium and low findings are downgraded unless they represent a real high-severity governance or contract risk.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>5. What Changed</w:t></w:r></w:p>
    <w:p><w:r><w:t>Stable task identity: task_id and decision_id now group resubmissions under the same lifecycle instead of creating fragmented runs.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Filesystem context resolver: Pattern Pilot can rehydrate decision and task context directly from the target project markdown files, so callers do not need to pass the whole context every time.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Reviewer instruction upgrade: the reviewer now optimizes for task completion and verification-first feedback instead of broad, generic code review.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Iteration policy: later rounds focus on true blockers. Repeated medium and low findings become recommendations when they do not materially block the task.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Three-state submission contract: omitted fields allow filesystem fallback, explicit empty values are preserved, and caller-provided values take precedence.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>6. Results and Observations</w:t></w:r></w:p>
    <w:p><w:r><w:t>Before the improvements, review history showed many abandoned, blocked, or fragmented runs. The workflow was doing useful review work, but repeated context loss and broad findings created unnecessary cycles.</w:t></w:r></w:p>
    <w:p><w:r><w:t>After TASK-668 and the context-aware improvements, recent completed work showed a healthier pattern: tasks passed after focused review rounds, and blocking findings were typically real implementation issues. A representative sample after TASK-668 showed 16 runs, 12 passed runs, 4 operationally failed or interrupted runs, average 2.00 review rounds, and average 2.25 submissions. Passed runs averaged around 2.42 rounds, which is a sign of quality gate engagement rather than simple first-pass rubber-stamping.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Important reframing: a BLOCKING verdict is not a failed task. It is the QC system identifying a required fix. The real success metric is whether the task eventually passes with fewer redundant loops and with higher confidence in correctness, governance alignment, and integration behavior.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>7. Example: TASK-668</w:t></w:r></w:p>
    <w:p><w:r><w:t>TASK-668 is a useful proof point. The first run was interrupted by an operational hang after useful feedback. The second run completed in three rounds. Round 1 identified missing cross-execution query behavior and swallowed database errors. Round 2 identified source_scene_key being incorrectly stamped on window-scoped hero segments. Round 3 identified promoted non-location segments incorrectly satisfying the hero gate. The final result passed cleanly.</w:t></w:r></w:p>
    <w:p><w:r><w:t>This is exactly the intended value of Pattern Pilot: not to avoid review findings, but to surface the right findings early enough for the writer to correct them before the task is considered complete.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>8. Best Practices Learned</w:t></w:r></w:p>
    <w:p><w:r><w:t>Use stable identity for every task: task_id must remain stable across resubmissions, while attempt_number should be metadata only.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Keep decision and task context in the target project filesystem. Pattern Pilot should read it, not become the owner of project workflow truth.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Separate blocking findings from recommendations. Not every observation should continue the loop.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Run deterministic checks before LLM review so expensive review rounds are not spent on basic failures.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Make late-round blocking stricter. After multiple rounds, only high-severity governance, correctness, or contract issues should block completion.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Protect runtime continuity. If Pattern Pilot runs on a laptop, use caffeinate around the API process to avoid sleep-related hung sessions.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>9. LinkedIn Positioning</w:t></w:r></w:p>
    <w:p><w:r><w:t>This can be positioned as a practical implementation pattern rather than a claim that AI code review is new. The novelty is in the operating model: a separate QC control plane, stable task identity, filesystem-based context rehydration, governance-aware reviewer instructions, tiered findings, and an iteration policy that prevents low-value looping.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Suggested headline: I built a context-aware AI QC loop where one agent writes code and another independently reviews it against project governance.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Suggested framing: The goal was not to make AI write code faster. The goal was to make autonomous coding safer, auditable, and less repetitive by giving the reviewer the same project, decision, and task context as the writer.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>10. LinkedIn Post Draft</w:t></w:r></w:p>
    <w:p><w:r><w:t>I have been experimenting with a pattern I call Pattern Pilot: a context-aware QC control plane for AI-assisted software delivery.</w:t></w:r></w:p>
    <w:p><w:r><w:t>In this workflow, one agent writes the code, while a separate reviewer agent validates the change against the project's governance rules, decision record, task objective, acceptance criteria, changed files, and prior findings.</w:t></w:r></w:p>
    <w:p><w:r><w:t>The important lesson was that review quality depends heavily on context identity. When resubmits lose task identity, the system behaves like every request is new. When the reviewer does not understand the decision or task, it can produce broad recommendations instead of completion-focused findings.</w:t></w:r></w:p>
    <w:p><w:r><w:t>After adding stable task IDs, decision IDs, filesystem context resolution, reviewer instructions, and a late-round severity policy, the workflow became much more useful. Blocking findings now represent real fixes more often, while advisories no longer slow down completion unnecessarily.</w:t></w:r></w:p>
    <w:p><w:r><w:t>My biggest takeaway: in AI coding workflows, a "failed" review is not failure. It is quality control doing its job. The better metric is whether the task passes after focused iterations with fewer repeated or low-value review cycles.</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>11. Carousel Slide Outline</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 1: Pattern Pilot - Context-Aware QC for AI Coding</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 2: The problem - repeated review loops, fragmented run identity, and generic findings</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 3: The shift - writer agent plus independent reviewer agent plus governance control plane</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 4: The missing layer - project, decision, and task context</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 5: The workflow - submit, resolve context, deterministic checks, LLM review, fix, resubmit, pass</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 6: The results - fewer redundant loops and more meaningful blocking findings</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 7: The lesson - BLOCKING is not failure; it is quality control doing its job</w:t></w:r></w:p>
    <w:p><w:r><w:t>Slide 8: Best practices - stable identity, filesystem context, tiered severity, deterministic checks first</w:t></w:r></w:p>

    <w:p><w:r><w:rPr><w:b/><w:color w:val="17324D"/><w:sz w:val="32"/></w:rPr><w:t>12. Next Enhancements</w:t></w:r></w:p>
    <w:p><w:r><w:t>Add a run health monitor that detects long-running or hung reviews and marks them as interrupted instead of silently waiting.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Add operational metrics that separate QC-blocked, operationally interrupted, failed deterministic checks, and final task outcome.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Add a lightweight dashboard for task-level success rate, rounds-to-pass, repeated finding classes, and advisory trends.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Add a resume-safe review protocol so writer sessions can reconnect to an existing Pattern Pilot run after laptop sleep, API restart, or network interruption.</w:t></w:r></w:p>

    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="900" w:right="900" w:bottom="900" w:left="900" w:header="720" w:footer="720" w:gutter="0"/>
      <w:cols w:space="720"/>
      <w:docGrid w:linePitch="360"/>
    </w:sectPr>
  </w:body>
</w:document>
XML

(cd "$BUILD_DIR" && zip -qr "$OUT_FILE" .)
echo "$OUT_FILE"
