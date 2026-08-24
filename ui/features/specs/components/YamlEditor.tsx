"use client";

import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { yaml as yamlLanguage } from "@codemirror/lang-yaml";
import { linter, lintGutter, type Diagnostic } from "@codemirror/lint";
import { unifiedMergeView } from "@codemirror/merge";
import { useMemo } from "react";
import { parseAllDocuments } from "yaml";

/**
 * The YAML editor. Loaded only in the browser — see `SpecEditor`.
 *
 * **Two layers of feedback, and they answer different questions.**
 *
 * The linter below is ~20 lines over `parseAllDocuments`, and it answers *is this
 * file syntactically YAML* with no request at all, as you type. That is the error
 * you make constantly and want to see instantly: a bad indent, an unclosed
 * bracket.
 *
 * It deliberately stops there. Whether a document is a *valid spec* — a known
 * kind, a schema its fields satisfy, an entrypoint that exists, a department its
 * actors can name — is `POST /specs/validate`, on a button. The authoritative
 * validator is server-side and already written: Pydantic per document plus
 * `validation.py`'s cross-document checks, none of which a client-side JSON
 * Schema could express. A second, weaker validator in the browser would
 * eventually disagree with it, and the one that is wrong would be the one
 * telling somebody their file is fine.
 */

const yamlSyntax = linter((view): Diagnostic[] => {
  const text = view.state.doc.toString();
  const diagnostics: Diagnostic[] = [];
  const length = text.length;
  for (const document of parseAllDocuments(text)) {
    for (const error of document.errors) {
      // `pos` is a [start, end] offset pair. Clamped because a parser can report
      // a position one past the end of the document for an unterminated block,
      // and CodeMirror throws on an out-of-range diagnostic.
      const [from, to] = error.pos ?? [0, 0];
      diagnostics.push({
        from: Math.max(0, Math.min(from, length)),
        to: Math.max(0, Math.min(to ?? from + 1, length)),
        severity: "error",
        message: error.message,
      });
    }
  }
  return diagnostics;
});

export interface YamlEditorProps {
  value: string;
  onChange: (next: string) => void;
  /** The bytes on disk. Passing it with `showDiff` turns the editor into a
   * unified diff of saved-versus-typed, which is what somebody wants to see
   * immediately before pressing save. */
  original?: string;
  showDiff?: boolean;
  readOnly?: boolean;
}

export default function YamlEditor({
  value,
  onChange,
  original,
  showDiff = false,
  readOnly = false,
}: YamlEditorProps) {
  const extensions = useMemo(() => {
    const base = [yamlLanguage(), yamlSyntax, lintGutter(), EditorView.lineWrapping];
    if (showDiff && original !== undefined) {
      // `mergeControls: false` — the accept/reject chunk buttons belong to a merge
      // tool. This is a preview of what a save would change, and a control that
      // silently rewrote the buffer would be a second way to edit the file.
      return [...base, unifiedMergeView({ original, mergeControls: false })];
    }
    return base;
  }, [original, showDiff]);

  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      theme="dark"
      height="100%"
      className="cm-host"
      editable={!readOnly}
      extensions={extensions}
      basicSetup={{
        lineNumbers: true,
        foldGutter: true,
        highlightActiveLine: !showDiff,
        autocompletion: false,
        // The editor's own search panel. Kept, because a spec folder file is long
        // and scrolling to `maxCostCents` by eye is how the wrong actor gets edited.
        searchKeymap: true,
      }}
    />
  );
}
