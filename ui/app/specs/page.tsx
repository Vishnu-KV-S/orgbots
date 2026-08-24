import { SpecsScreen } from "@/features/specs";

/** Route only. The screen lives in the feature — and it has to, because the
 * editor is a `next/dynamic` import with `ssr: false`, which Next refuses inside
 * a Server Component. This file is one. */
export default function SpecsPage() {
  return <SpecsScreen />;
}
