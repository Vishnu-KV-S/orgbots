import { Card, CardFoot, CardHead, Pill, Stat, StatRow } from "@/components/ui";
import { plural, relative } from "@/lib/format";
import type { OrganizationSummary } from "@/lib/types";

/**
 * One organization as a tile on the index. Presentation only.
 *
 * The tile is read in three passes, and each fact sits in the pass it belongs
 * to: what this org *is* (name, shape) at the top, what it is *doing* (the
 * counts that move) in the middle, and what it has *done* (totals, last
 * activity) in the footer.
 */
export function OrganizationCard({ org }: { org: OrganizationSummary }) {
  return (
    <Card href={`/org/${org.id}`}>
      <CardHead
        title={org.name}
        sub={`${plural(org.departments, "department")} · ${plural(org.goals, "goal")}`}
        aside={<Pill status={org.status} />}
      />

      <StatRow>
        <Stat label="actors" value={org.actors} />
        <Stat label="live runs" value={org.runs_active} emphasis />
        <Stat label="open tasks" value={org.tasks_open} />
        <Stat label="failed" value={org.runs_failed} warn={org.runs_failed > 0} />
      </StatRow>

      <CardFoot left={plural(org.runs_total, "run")} right={relative(org.last_activity_at)} />
    </Card>
  );
}
