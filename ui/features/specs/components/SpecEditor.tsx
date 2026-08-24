"use client";

import dynamic from "next/dynamic";
import { Loading } from "@/components/ui";
import type { YamlEditorProps } from "./YamlEditor";

/**
 * The client boundary around CodeMirror.
 *
 * `next/dynamic` with `ssr: false` is **rejected inside a Server Component** in
 * Next 15+, and route files are Server Components by default — so this wrapper
 * exists to be the `"use client"` module the dynamic import lives in.
 * `app/specs/page.tsx` stays a thin server route, per the app-layer rule.
 *
 * The editor genuinely cannot be server-rendered: CodeMirror measures the DOM to
 * lay out its gutters and reads `document` at construction.
 */
const YamlEditor = dynamic(() => import("./YamlEditor"), {
  ssr: false,
  loading: () => <Loading what="the editor" />,
});

export function SpecEditor(props: YamlEditorProps) {
  return <YamlEditor {...props} />;
}
