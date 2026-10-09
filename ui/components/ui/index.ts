/**
 * The primitive layer: presentational components with no knowledge of the
 * runtime's domain. Features import from `@/components/ui`; nothing here
 * imports from a feature.
 */

export { BadgeRow, Chip, Pill, StatusTag, type PillStatus, type Tone } from "./Badge";
export { Button, IconButton, InlineButton } from "./Button";
export { Card, CardFoot, CardGrid, CardHead, Stat, StatRow } from "./Card";
export { ApiUnreachable, Empty, ErrorNotice, Json, Loading } from "./Feedback";
export { Field, Fields, KeyValue } from "./KeyValue";
export { List, ListRow, type ListRowProps } from "./ListRow";
export { Prose, Section } from "./Section";
export { WaveformIcon } from "./WaveformIcon";
