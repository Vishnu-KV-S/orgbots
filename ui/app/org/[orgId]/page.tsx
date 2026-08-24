import { OrgGraphScreen } from "@/features/org-graph";

export default async function OrgPage({ params }: { params: Promise<{ orgId: string }> }) {
  const { orgId } = await params;
  return <OrgGraphScreen orgId={orgId} />;
}
