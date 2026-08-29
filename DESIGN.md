---
name: Agentic Debugger
description: A forensic terminal console for evidence-driven software repair.
colors:
  canvas-deep: "#07131C"
  surface: "#0C1A24"
  panel: "#102430"
  panel-raised: "#15303E"
  live-cyan: "#49D8FF"
  model-violet: "#A98BFF"
  evidence-amber: "#FFB454"
  evidence-surface: "#3A2B12"
  foreground: "#E7F2F7"
  muted: "#91A8B5"
  faint: "#6D8794"
  line: "#294655"
  line-strong: "#3D687C"
  verified-green: "#45E0A8"
  warning-amber: "#FFCA72"
  error-coral: "#FF7185"
  debugger-magenta: "#E88CFF"
  tool-teal: "#5CC8C8"
  selection-blue: "#19485B"
  border-blurred: "#1A313E"
  code-function: "#C5A5FF"
  code-string: "#A8D8F0"
typography:
  display:
    fontFamily: "terminal-provided monospace"
    fontWeight: 700
    lineHeight: 1
  headline:
    fontFamily: "terminal-provided monospace"
    fontWeight: 700
    lineHeight: 1
  title:
    fontFamily: "terminal-provided monospace"
    fontWeight: 700
    lineHeight: 1
  body:
    fontFamily: "terminal-provided monospace"
    fontWeight: 400
    lineHeight: 1
  label:
    fontFamily: "terminal-provided monospace"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "normal"
rounded:
  none: "0"
  terminal-round: "round"
spacing:
  zero: "0 cells"
  unit: "1 cell"
  compact: "2 cells"
  standard: "3 cells"
  roomy: "4 cells"
components:
  button-primary:
    backgroundColor: "{colors.live-cyan}"
    textColor: "{colors.canvas-deep}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 2 cells"
    height: "1 cell"
  button-primary-hover:
    backgroundColor: "{colors.live-cyan}"
    textColor: "{colors.canvas-deep}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 2 cells"
    height: "1 cell"
  button-secondary:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 2 cells"
    height: "1 cell"
  button-secondary-hover:
    backgroundColor: "{colors.live-cyan}"
    textColor: "{colors.canvas-deep}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 2 cells"
    height: "1 cell"
  field:
    backgroundColor: "{colors.canvas-deep}"
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.terminal-round}"
    padding: "0 1 cell"
    height: "1 cell"
  panel-evidence:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.terminal-round}"
    padding: "1 2 cells"
  table-row-selected:
    backgroundColor: "{colors.live-cyan}"
    textColor: "{colors.canvas-deep}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    height: "1 cell"
  tab-active:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.live-cyan}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    height: "1 cell"
---

# Design System: Agentic Debugger

## Overview

**Creative North Star: "The Forensic Instrument Console"**

The interface treats the terminal as a calibrated debugging instrument: dense but legible, low-light, and evidence-led. Layered blue-black surfaces establish the workspace while cyan marks live focus or action. Amber marks evidence boundaries and verifier authority; green appears only when an independent verification result is resolved.

Controls are decisive but restrained, with keyboard movement, explicit state, and compact information geometry taking priority over ornament. The visual system is terminal-native: hierarchy comes from weight, case, color, spacing, and cell geometry rather than multiple font families or simulated chrome.

**Key Characteristics:**

- Precise, low-light, and evidence-led.
- Restrained, instrument-like, and keyboard-first.
- Semantic signal colors on layered blue-black neutrals.
- Wider terminals reveal context instead of stretching the primary reading surface.

## Colors

The palette behaves like an instrument legend: every hue has one durable job, and blue-black neutrals carry most of the screen.

### Primary

- **Live Cyan:** The active focus, selected row, primary action, live signal, evidence reference, and active-tab color.

### Secondary

- **Evidence Amber:** Marks proof chains, evidence boundaries, and authoritative-verdict language.
- **Verified Green:** Reserved for independently proven or verifier-resolved success.

### Tertiary

- **Model Violet:** Identifies model provenance and official-verification context.
- **Debugger Magenta:** Identifies debugger and PDB-specific signal.
- **Tool Teal:** Identifies typed tool activity.
- **Code Function Violet and Code String Blue:** Provide bounded source-code syntax distinction without competing with operational status.

### Neutral

- **Deep Canvas:** The application background and lowest visual plane.
- **Surface, Panel, and Raised Panel:** Tonal layers for footers, context rails, dialogs, action controls, and bounded content.
- **Foreground, Muted, and Faint:** A three-step text hierarchy for facts, supporting explanation, and low-priority hints.
- **Line and Strong Line:** Structural borders, dividers, scrollbars, and focus boundaries.
- **Selection Blue:** Focused setting rows and option-list selection when a full cyan fill would be too forceful.

### Semantic states

- **Warning Amber:** Pending, interrupted, or cautionary state.
- **Error Coral:** Failed, invalid, timed-out, or rejected state.

### Named Rules

**The Signal Discipline Rule.** Cyan means live interaction, amber means evidence or authority, green means independently verified success, violet means model provenance, and magenta means debugger signal; never assign these colors decoratively.

**The Authority Rule.** A controller or patch state is a claim; only the independent verifier may receive authoritative success treatment.

## Typography

**Display Font:** Terminal-provided monospace

**Body Font:** Terminal-provided monospace

**Label/Mono Font:** Terminal-provided monospace

**Character:** One terminal face keeps facts, code, commands, and evidence aligned. Hierarchy is created through bold weight, uppercase labels, semantic color, whitespace, and one-cell line rhythm.

### Hierarchy

- **Display** (bold, one-cell line rhythm): Product identity and screen-opening statements.
- **Headline** (bold, one-cell line rhythm): Screen theses and high-value status declarations.
- **Title** (bold, usually uppercase): Section labels, pane names, modal titles, and instrument readouts.
- **Body** (regular, one-cell line rhythm): Evidence, paths, commands, explanations, and runtime facts.
- **Label** (bold, often uppercase): State markers, field names, tabs, and compact controls.

### Named Rules

**The Cell Geometry Rule.** Do not introduce a second font family or arbitrary type scale; establish hierarchy with weight, case, color, spacing, and aligned terminal cells.

## Layout

The accepted minimum terminal is 80 columns by 24 rows. At widths below 100 columns, the interface enters compact behavior: pre-flight and live-run context rails are hidden, footer vocabulary shortens, long task or path labels compact, and workstream previews remove expanded diff detail. At 100 columns and wider, fixed-width context rails (36 columns for session pre-flight and 46 columns for a live run) appear beside the flexible primary workspace.

Primary content fills the remaining cells and uses one-cell rows for settings, controls, tabs, status, and footers. Common internal padding is one or two cells; screen edges and major headers use three cells; centered empty states use four. Evidence panes retain bounded line length through their flexible main column while added width earns contextual facts.

**The Context-not-Stretch Rule.** Wider terminals reveal provenance and operational context; they do not inflate controls or loosen the evidence rhythm.

## Elevation & Depth

Depth is flat and tonal. The deep canvas, surface, panel, and raised-panel steps create hierarchy, while rounded border glyphs and stronger focus lines establish containment. There are no shadows, glows, translucency effects, or decorative gradients.

### Named Rules

**The Flat Instrument Rule.** Use tonal layering and borders for depth; never use shadow-driven elevation or neon glow.

## Shapes

The form language is rectangular and cell-aligned. Buttons and selected rows are flat blocks with no radius. Dialogs, editors, tables, evidence containers, and content switchers use Textual's rounded border glyphs rather than a pixel-radius system. Dividers are single-line rules, and clipped terminal edges remain intentional at compact sizes.

## Components

The components below are Textual terminal primitives. The extension sidecar carries HTML/CSS analogues only so design tooling can preview their visual character; the application has no HTML runtime.

### Buttons

- **Shape:** Flat one-row blocks with no border or radius; primary actions use two cells of horizontal padding and a bounded minimum width.
- **Primary:** Live-cyan fill, deep-canvas text, and bold label treatment.
- **Hover / Focus:** Preserve the cyan fill and add underline for the strongest action; secondary actions invert to cyan and deep canvas.
- **Disabled:** Panel fill, faint text, and regular weight.

### Cards / Containers

- **Corner Style:** Textual rounded border glyphs, not CSS-like corner radii.
- **Background:** Surface or panel over the deep canvas.
- **Shadow Strategy:** None; tonal contrast and line borders carry depth.
- **Border:** Standard line at rest; strong line, cyan, or evidence amber when focus or authority requires it.
- **Internal Padding:** Commonly one row by two columns; empty-state containers use two rows by four columns.

### Inputs / Fields

- **Style:** Deep-canvas field, foreground text, one-cell horizontal padding, and a rounded line border where the field spans more than a single row.
- **Focus:** Border changes to live cyan; focused one-row setting and choice rows use selection blue with bold text.
- **Error / Disabled:** Error coral for invalid input; panel and faint text for disabled actions.

### Navigation

Tabs live on the surface layer. Inactive labels are muted, the active tab is bold live cyan, and keyboard focus raises the label to foreground. Persistent footers use faint text on a surface strip and expose the exact keyboard vocabulary available in the current mode.

### Evidence Review

The causal case brief is the signature review component. It pairs colored state markers with aligned stage labels, plain-language evidence, bounded references, and an explicit authoritative verdict. Proven stages are green, recorded stages cyan, pending stages warning amber, and failed stages error coral; the authority label is evidence amber.

### History Table

Headers are bold and muted on the surface layer. The current row is a full cyan bar with bold deep-canvas text, matching the decisive focus language used elsewhere.

### Independent Proof Chain

The pre-flight proof-chain panel uses evidence amber for its label, foreground for the FAILURE → PDB EVIDENCE → PATCH → VERIFIER VERDICT sequence, and muted copy to state that run completion is not correctness.

## Do's and Don'ts

### Do:

- **Do** preserve the semantic color assignments across screens, panes, and event renderers.
- **Do** make verifier authority visually explicit wherever a result or completion claim appears.
- **Do** keep the primary workflow usable at 80x24 and switch to compact behavior below 100 columns.
- **Do** add context on wider terminals while preserving the compact evidence rhythm.
- **Do** use terminal-native focus, selection, borders, and keyboard vocabulary.

### Don't:

- **Don't** use neon glow, generic sci-fi chrome, decorative gradients, or shadow-driven elevation.
- **Don't** treat controller completion, patch application, or model confidence as verified success.
- **Don't** assign status colors randomly or use green before independent verification succeeds.
- **Don't** introduce proportional or multiple font families into the terminal surface.
- **Don't** stretch controls or prose merely because more columns are available.
