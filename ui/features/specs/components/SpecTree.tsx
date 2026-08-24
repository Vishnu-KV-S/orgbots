"use client";

import { cx } from "@/lib/cx";
import type { SpecListing } from "@/lib/types";

/**
 * Roots → folders → files.
 *
 * The hierarchy is not cosmetic: a **folder** is what compiles into a company,
 * and a **file** is what you edit. Showing them at the same level would invite
 * planning one file of a corpus, which is not a thing that means anything.
 *
 * Loose YAML at a root — `config/agents.example.yaml` — is shown greyed and
 * unselectable rather than hidden. It is not a company, and pretending it does
 * not exist is how somebody edits it and wonders why nothing happened.
 */

export interface SpecTreeProps {
  listing: SpecListing;
  selectedFolder: string | null;
  selectedFile: string | null;
  onSelectFolder: (path: string) => void;
  onSelectFile: (path: string) => void;
}

export function SpecTree({
  listing,
  selectedFolder,
  selectedFile,
  onSelectFolder,
  onSelectFile,
}: SpecTreeProps) {
  return (
    <nav className="spec-tree" aria-label="spec folders">
      {listing.roots.map((root) => (
        <div key={root.path} className="spec-root">
          <div className="spec-root-head">
            <span className="mono">{root.path}</span>
            {!root.exists && <span className="spec-note">missing</span>}
          </div>

          {root.folders.map((folder) => {
            const open = selectedFolder === folder.path;
            const kinds = Object.entries(folder.kinds);
            return (
              <div key={folder.path} className="spec-folder">
                <button
                  type="button"
                  className={cx("spec-folder-head", open && "on")}
                  onClick={() => onSelectFolder(folder.path)}
                >
                  <span className="spec-folder-name">{folder.name}</span>
                  <span className="spec-folder-meta">
                    {folder.organization ?? "no Organization document"}
                    {folder.organization_id ? " · applied" : " · new"}
                  </span>
                </button>

                {folder.error && <p className="spec-error">{folder.error}</p>}

                {open && (
                  <>
                    <ul className="spec-files">
                      {folder.files.map((file) => (
                        <li key={file.path}>
                          <button
                            type="button"
                            className={cx("spec-file", selectedFile === file.path && "on")}
                            onClick={() => onSelectFile(file.path)}
                          >
                            {file.name}
                          </button>
                        </li>
                      ))}
                      {folder.other.map((path) => (
                        <li key={path}>
                          <span className="spec-file muted" title="not a spec document">
                            {path.split("/").pop()}
                          </span>
                        </li>
                      ))}
                    </ul>
                    {kinds.length > 0 && (
                      <p className="spec-kinds">
                        {kinds
                          .sort(([a], [b]) => a.localeCompare(b))
                          .map(([kind, count]) => `${count}× ${kind}`)
                          .join(" · ")}
                      </p>
                    )}
                  </>
                )}
              </div>
            );
          })}

          {root.loose.length > 0 && (
            <p className="spec-loose">
              loose, not compiled: {root.loose.map((p) => p.split("/").pop()).join(", ")}
            </p>
          )}
        </div>
      ))}
    </nav>
  );
}
