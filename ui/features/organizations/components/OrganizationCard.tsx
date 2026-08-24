import { Card, CardFoot, CardHead, Pill, Stat, StatRow } from "@/components/ui";
import { relative } from "@/lib/format";
import type { OrganizationSummary } from "@/lib/types";

/** One organization as a tile on the index. Presentation only. */
export function OrganizationCard({ org }: { org: OrganizationSummary }) {
  return (
    <Card href={`/org/${org.id}`}>
      <CardHead title={org.name} aside={<Pill status={org.status} />} />

      <StatRow>
        <Stat label="actors" value={org.actors} />
        <Stat label="depts" value={org.departments} />
        <Stat label="live runs" value={org.runs_active} />
        <Stat label="failed" value={org.runs_failed} warn={org.runs_failed > 0} />
      </StatRow>

      <CardFoot
        left={`${org.runs_total} runs · ${org.tasks_open} open tasks · ${org.goals} goals`}
        right={relative(org.last_activity_at)}
      />
    </Card>
  );
}
