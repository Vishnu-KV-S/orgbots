"use client";

import { Toolbar } from "@/components/layout";
import { Button } from "@/components/ui";

/**
 * The canvas controls, as data. A new toggle is an entry in the array the
 * screen passes in — nothing here has to change, and none of them is wired up
 * by hand.
 */

export interface ToggleItem<K extends string> {
  key: K;
  label: React.ReactNode;
  title?: string;
}

export interface ActionItem {
  label: string;
  onClick: () => void;
  title?: string;
}

export function CanvasToolbar<K extends string>({
  toggles,
  values,
  onToggle,
  actions = [],
}: {
  toggles: ToggleItem<K>[];
  values: Record<K, boolean>;
  onToggle: (key: K) => void;
  actions?: ActionItem[];
}) {
  return (
    <Toolbar>
      {toggles.map((item) => (
        <Button
          key={item.key}
          pressed={values[item.key]}
          title={item.title}
          onClick={() => onToggle(item.key)}
        >
          {item.label}
        </Button>
      ))}
      {actions.map((action) => (
        <Button key={action.label} title={action.title} onClick={action.onClick}>
          {action.label}
        </Button>
      ))}
    </Toolbar>
  );
}
