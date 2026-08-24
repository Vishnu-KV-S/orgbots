"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell, Crumb, Spacer, TopBar } from "@/components/layout";
import { ApiUnreachable, Button, Empty, ErrorNotice, Loading } from "@/components/ui";
import { listSpecs, readSpecFile, writeSpecFile } from "@/lib/api";
import { useAction } from "@/lib/hooks/useAction";
import { useResource } from "@/lib/hooks/useResource";
import type { SpecFile, SpecFolder, SpecListing } from "@/lib/types";
import { PlanPane } from "./components/PlanPane";
import { SpecEditor } from "./components/SpecEditor";
import { SpecTree } from "./components/SpecTree";

/**
 * The editor screen: a tree, a file, and the plan/apply flow beside it.
 *
 * **This screen writes to `config/`.** That is only defensible because three
 * things are true underneath it: every path is confined to `RUNTIME_SPEC_ROOTS`,
 * every save is guarded by the digest the file was opened at, and the repository
 * is under git — so the undo for a bad edit is `git checkout`, not a backup
 * scheme nobody tested.
 *
 * The screen does not poll. A canvas that refreshes under you is fine; an editor
 * that does it is an editor that eats what you were typing. The listing is
 * re-read on demand instead — after a save, and after an apply.
 */

export function SpecsScreen() {
  const listing = useResource<SpecListing>(listSpecs);
  const [folderPath, setFolderPath] = useState<string | null>(null);
  const [filePath, setFilePath] = useState<string | null>(null);
  const [buffer, setBuffer] = useState("");
  const [saved, setSaved] = useState<SpecFile | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [orgIds, setOrgIds] = useState<Record<string, string>>({});

  const folders = useMemo(
    () => (listing.data?.roots ?? []).flatMap((root) => root.folders),
    [listing.data],
  );
  const folder: SpecFolder | null =
    folders.find((entry) => entry.path === folderPath) ?? null;

  // Open the first folder once, so the screen is not an empty frame on arrival.
  useEffect(() => {
    if (folderPath === null && folders.length > 0) setFolderPath(folders[0].path);
  }, [folderPath, folders]);

  const openFile = useCallback(async (path: string) => {
    setFilePath(path);
    setShowDiff(false);
    const file = await readSpecFile(path);
    setSaved(file);
    setBuffer(file.content);
  }, []);

  const save = useAction(
    useCallback(async () => {
      if (!saved || !filePath) throw new Error("nothing open");
      const result = await writeSpecFile(filePath, buffer, saved.sha256);
      // The digest the *next* save is guarded by is the one this save produced.
      setSaved({ ...saved, content: buffer, sha256: result.sha256 });
      setShowDiff(false);
      listing.refresh();
      return result;
    }, [buffer, filePath, listing, saved]),
  );

  const reload = useCallback(() => {
    if (filePath) void openFile(filePath);
  }, [filePath, openFile]);

  const dirty = saved !== null && buffer !== saved.content;

  // A generated id per folder, remembered for as long as the screen is open. A new
  // company needs an organization id and there is nobody to ask for one; showing a
  // fresh UUID that can be replaced is honest about that.
  const organizationId =
    folder === null
      ? ""
      : (folder.organization_id ?? orgIds[folder.path] ?? "");

  useEffect(() => {
    if (!folder || folder.organization_id || orgIds[folder.path]) return;
    setOrgIds((current) => ({ ...current, [folder.path]: crypto.randomUUID() }));
  }, [folder, orgIds]);

  // The browser's own warning. A dirty buffer is unsaved work in a text box, and
  // the tab close is the way it is most often lost.
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const editable = listing.data?.editable ?? false;

  return (
    <AppShell
      bar={
        <TopBar>
          <Crumb separator>specs</Crumb>
          {folder && <Crumb separator>{folder.name}</Crumb>}
          {dirty && <span className="chip warn">unsaved</span>}
          <Spacer />
          {!editable && listing.data && (
            <Crumb>read-only (RUNTIME_SPEC_EDITABLE=false)</Crumb>
          )}
          <div className="toggles">
            <Button
              onClick={() => setShowDiff((on) => !on)}
              pressed={showDiff}
              title="Show what a save would change, against the bytes on disk"
            >
              diff
            </Button>
            <Button onClick={reload} title="Discard the buffer and re-read the file">
              reload
            </Button>
            <Button
              onClick={() => void save.run()}
              pressed={dirty}
              title={editable ? "Write the file" : "This runtime serves the specs read-only"}
            >
              {save.pending ? "saving…" : "save"}
            </Button>
          </div>
        </TopBar>
      }
    >
      {listing.error && <ApiUnreachable detail={listing.error} />}
      {listing.loading && <Loading what="the spec folders" />}

      <div className="spec-layout">
        {listing.data && (
          <SpecTree
            listing={listing.data}
            selectedFolder={folderPath}
            selectedFile={filePath}
            onSelectFolder={(path) => setFolderPath(path)}
            onSelectFile={(path) => void openFile(path)}
          />
        )}

        <section className="spec-editor">
          {save.error && <ErrorNotice>{save.error}</ErrorNotice>}
          {filePath === null ? (
            <Empty>
              Pick a file. A <strong>folder</strong> is what compiles into a company; a{" "}
              <strong>file</strong> is what you edit. Saving parses the document and
              nothing more — a corpus is allowed to be briefly incoherent, which is what
              makes renaming anything possible.
            </Empty>
          ) : saved === null ? (
            <Loading what={filePath} />
          ) : (
            <>
              <div className="spec-editor-head">
                <span className="mono">{filePath}</span>
                <span className="dim mono">{saved.sha256.slice(0, 12)}</span>
              </div>
              <div className="spec-editor-body">
                <SpecEditor
                  value={buffer}
                  onChange={setBuffer}
                  original={saved.content}
                  showDiff={showDiff}
                  readOnly={!editable}
                />
              </div>
            </>
          )}
        </section>

        <aside className="spec-side">
          {folder ? (
            <PlanPane
              folder={folder}
              organizationId={organizationId}
              onOrganizationId={(next) =>
                setOrgIds((current) => ({ ...current, [folder.path]: next }))
              }
              onApplied={listing.refresh}
            />
          ) : (
            <Empty>No spec folders under the configured roots.</Empty>
          )}
        </aside>
      </div>
    </AppShell>
  );
}
