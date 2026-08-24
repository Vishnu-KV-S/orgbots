"use client";

import { AppShell, Crumb, Page, Spacer, TopBar } from "@/components/layout";
import { ApiUnreachable, CardGrid, Empty, Loading } from "@/components/ui";
import { listOrganizations } from "@/lib/api";
import { useResource } from "@/lib/hooks/useResource";
import type { OrganizationSummary } from "@/lib/types";
import { OrganizationCard } from "./components/OrganizationCard";

const REFRESH_MS = 4000;

/** Every organization the runtime knows about. */
export function OrganizationsScreen() {
  const { data: orgs, error, loading } = useResource<OrganizationSummary[]>(listOrganizations, {
    intervalMs: REFRESH_MS,
  });

  return (
    <AppShell
      bar={
        <TopBar>
          <Crumb>companies</Crumb>
          <Spacer />
          <Crumb>refreshing every {REFRESH_MS / 1000}s</Crumb>
        </TopBar>
      }
    >
      <Page>
        <h1>Companies</h1>
        <p className="lede">
          Every organization the runtime knows about. <strong>Running</strong> means at least
          one run is queued or executing right now; <strong>halted</strong> means a kill switch
          is engaged, which outranks any run still draining. Open one to see its structure.
        </p>

        {error && <ApiUnreachable detail={error} />}
        {loading && <Loading />}
        {orgs?.length === 0 && <Empty>No organizations yet. Apply a spec to create one.</Empty>}

        <CardGrid>
          {orgs?.map((org) => (
            <OrganizationCard key={org.id} org={org} />
          ))}
        </CardGrid>
      </Page>
    </AppShell>
  );
}
