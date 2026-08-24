/**
 * The label/value grid that carries most of the inspector.
 *
 * `Field` renders a `<dt>/<dd>` pair, so a panel can build rows in a loop
 * (`model_profiles`, triggers) exactly like it writes them by hand — no
 * fragment gymnastics at the call site.
 */

export function KeyValue({ children }: { children: React.ReactNode }) {
  return <dl className="kv">{children}</dl>;
}

export function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

/** `label` / `value` pairs given as data, for the loop case. */
export function Fields({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return (
    <KeyValue>
      {rows.map(([label, value]) => (
        <Field key={label} label={label}>
          {value}
        </Field>
      ))}
    </KeyValue>
  );
}
