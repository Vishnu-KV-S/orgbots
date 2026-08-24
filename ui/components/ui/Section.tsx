/** A titled block in a side panel. Every panel is a stack of these. */
export function Section({
  title,
  children,
}: {
  title: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

/** A paragraph of free text inside a section — a description, a statement. */
export function Prose({ children }: { children: React.ReactNode }) {
  return <p className="prose">{children}</p>;
}
