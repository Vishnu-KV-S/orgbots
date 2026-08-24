import type { EdgeKind } from "@/lib/types";
import { EDGE_STYLES } from "../lib/edges";

/**
 * The legend is generated from the edge table, so a kind that gets a `legend`
 * label there appears here automatically and a colour can never be changed in
 * one place and not the other.
 */
export function CanvasLegend({ hide = [] }: { hide?: readonly EdgeKind[] }) {
  const items = (Object.entries(EDGE_STYLES) as Array<[EdgeKind, (typeof EDGE_STYLES)[EdgeKind]]>)
    .filter(([kind, style]) => style.legend && !hide.includes(kind))
    .map(([kind, style]) => ({ kind, label: style.legend!, style }));

  return (
    <div className="legend">
      {items.map((item) => (
        <div key={item.kind} className="item">
          <span
            className="swatch"
            style={{
              borderTopColor: item.style.stroke,
              borderTopStyle: item.style.dash ? "dashed" : "solid",
            }}
          />
          {item.label}
        </div>
      ))}
    </div>
  );
}
